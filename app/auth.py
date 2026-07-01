import asyncio
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.errors import make_error

_log = logging.getLogger("app.auth")

PREFIX_LEN: int = 12


@dataclass(frozen=True, slots=True)
class KeyRow:
    """One row of the api_keys table; `hash`, `last_used_at`, `revoked_at` are
    nullable per the schema."""

    prefix: str
    hash: str
    name: str | None
    created_at: int
    last_used_at: int | None
    revoked_at: int | None


def _init_db(db_path: Path) -> None:
    """Create the parent directory and `api_keys` table if missing.
    Idempotent. Called by every helper that writes to the DB and by the
    scripts before their first INSERT."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
              prefix       TEXT PRIMARY KEY,
              hash         TEXT NOT NULL,
              name         TEXT,
              created_at   INTEGER NOT NULL,
              last_used_at INTEGER,
              revoked_at   INTEGER
            )
            """
        )


def insert(
    db_path: Path,
    *,
    prefix: str,
    hash_: str,
    name: str | None,
) -> None:
    """Insert one row. created_at = int(time.time()); last_used_at and
    revoked_at default to NULL.

    Raises:
        sqlite3.IntegrityError: prefix already exists (PK violation).
    """
    _init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO api_keys (prefix, hash, name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (prefix, hash_, name, int(time.time())),
        )


def get_row(db_path: Path, prefix: str) -> KeyRow | None:
    """SELECT * WHERE prefix = ?. Returns KeyRow or None if no match.
    Does NOT raise on missing row; caller distinguishes None from
    KeyRow(revoked_at=...)."""
    _init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT prefix, hash, name, created_at, last_used_at, revoked_at "
            "FROM api_keys WHERE prefix = ?",
            (prefix,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return KeyRow(
        prefix=row[0],
        hash=row[1],
        name=row[2],
        created_at=row[3],
        last_used_at=row[4],
        revoked_at=row[5],
    )


def touch(db_path: Path, prefix: str) -> None:
    """UPDATE api_keys SET last_used_at = ? WHERE prefix = ?.
    No-op if the prefix doesn't exist (sqlite UPDATE on a missing row
    silently affects zero rows). Called from the auth middleware after a
    successful verify."""
    _init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE prefix = ?",
            (int(time.time()), prefix),
        )


def revoke(db_path: Path, prefix: str) -> bool:
    """UPDATE api_keys SET revoked_at = ? WHERE prefix = ? AND revoked_at
    IS NULL. Returns True iff a row was updated. The script uses the
    return value to set its exit code."""
    _init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at = ? "
            "WHERE prefix = ? AND revoked_at IS NULL",
            (int(time.time()), prefix),
        )
        return cur.rowcount > 0


# Module-level Argon2id verifier — stateless and thread-safe for verify().
_HASHER: PasswordHasher = PasswordHasher()

# Bearer header parser — case-insensitive scheme, exactly one whitespace run.
_BEARER_RE: re.Pattern[str] = re.compile(r"^Bearer\s+(\S+)\s*$", re.IGNORECASE)

PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/healthz",
        "/readyz",
        "/docs",
        "/openapi.json",
        "/redoc",
    }
)


class BearerAuthMiddleware:
    """ASGI middleware that gates every /v1/* request behind an Argon2id-
    verified bearer token from data/keys.db. lifespan and websocket
    scopes pass through unchanged."""

    def __init__(self, app: ASGIApp, *, keys_db_path: Path) -> None:
        self.app: ASGIApp = app
        self.keys_db_path: Path = keys_db_path

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        # 1. Non-http scopes (lifespan, websocket) pass through unguarded.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 2. Public-path allowlist — exact match only.
        path: str = cast(str, scope["path"])
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        # 3. Extract Authorization header.
        auth_value: str | None = None
        headers: list[tuple[bytes, bytes]] = cast(
            list[tuple[bytes, bytes]], scope.get("headers", [])
        )
        for name_bytes, value_bytes in headers:
            if name_bytes.lower() == b"authorization":
                auth_value = value_bytes.decode("ascii", errors="replace")
                break

        if auth_value is None:
            await self._reject(
                scope, receive, send, "Missing Authorization header"
            )
            return

        # 4. Parse bearer token.
        match = _BEARER_RE.match(auth_value)
        if match is None:
            await self._reject(
                scope, receive, send, "Malformed Authorization header"
            )
            return
        token: str = match.group(1)

        # 5. Compute prefix — first PREFIX_LEN chars of the full plaintext.
        prefix: str = token[:PREFIX_LEN]

        # 6. DB lookup.
        row: KeyRow | None = await asyncio.to_thread(
            get_row, self.keys_db_path, prefix
        )
        if row is None or row.revoked_at is not None:
            await self._reject(scope, receive, send, "Invalid API key")
            return

        # 7. Argon2 verify.
        try:
            await asyncio.to_thread(_HASHER.verify, row.hash, token)
        except VerifyMismatchError:
            await self._reject(scope, receive, send, "Invalid API key")
            return
        except InvalidHashError:
            _log.warning(
                "Corrupted Argon2 hash for prefix %s; rejecting request.",
                prefix,
            )
            await self._reject(scope, receive, send, "Invalid API key")
            return
        except VerificationError:
            await self._reject(scope, receive, send, "Invalid API key")
            return

        # 8. Touch last_used_at.
        await asyncio.to_thread(touch, self.keys_db_path, prefix)

        # 9. Attach key_prefix to scope state.
        state = cast(dict[str, Any], scope.setdefault("state", {}))
        state["key_prefix"] = prefix

        # 10. Pass through to the downstream app.
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        message: str,
    ) -> None:
        """Emit a 401 JSON response with the OpenAI error envelope."""
        content: dict[str, Any] = make_error(
            type_="invalid_request_error",
            message=message,
            param="Authorization",
            code="invalid_api_key",
        )
        response = JSONResponse(status_code=401, content=content)
        await response(scope, receive, send)
