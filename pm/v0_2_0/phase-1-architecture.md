# Phase 1 — Auth foundation — Architecture Specification

**Project**: local-ai-server
**Version**: v0.2.0
**Phase**: 1 — Auth foundation
**Branch**: `phase/local-ai-server/1-auth` (already created off `dev/local-ai-server`)
**Date**: 2026-06-19
**Precondition**: v0.1.0 is shipped (PR #1 merged) and `dev/local-ai-server` is the current dev branch. The wired surface (Ollama adapter, three `/v1/*` routers, lifespan-built `app.state.adapters`, OpenAI error envelope, `OllamaAdapter.health()`) is the seam this phase extends.

---

## 1. Overview

Phase 1 layers Argon2id-backed bearer-token authentication onto every `/v1/*` route. It introduces a SQLite key store at `KEYS_DB_PATH` (default `./data/keys.db`), an ASGI `BearerAuthMiddleware` that fires after the existing exception handlers and before the routers (per development plan §4.5), two CLI scripts (`scripts/generate_api_key.py`, `scripts/revoke_api_key.py`) for key lifecycle management, and the dependency / config / gitignore plumbing required to support all of the above. `/healthz` (and the future `/readyz`, `/docs`, `/openapi.json`, `/redoc`) remain public. Out of scope for Phase 1: structlog (Phase 2), `/readyz` fan-out (Phase 2), watchfiles (Phase 2), tests (Phase 3), the auth-demo notebook (step 3.7 — separate Example Generation Agent), README updates (Phase 3).

This document is the implementation contract for the Builder Agent and the assertion contract for the Reviewer.

---

## 2. Module Map

| Path | Status | Purpose |
|---|---|---|
| `pyproject.toml` | MODIFIED | Bump `version = "0.2.0"`; add `argon2-cffi>=23.1` to runtime deps. |
| `uv.lock` | REGENERATED | Side-effect of `uv sync` after the dep change. |
| `app/config.py` | MODIFIED | Add the `keys_db_path: Path` field with alias `KEYS_DB_PATH`. |
| `config/.env.example` | MODIFIED | Append `KEYS_DB_PATH=./data/keys.db`. |
| `.gitignore` | MODIFIED | Append `data/`, `data/*.db`, `data/*.db-journal`. |
| `app/auth.py` | NEW | SQLite key-store helpers (`_init_db`, `insert`, `get_row`, `touch`, `revoke`) **and** `BearerAuthMiddleware`. Single module per development plan §5.1 / LOE table. |
| `scripts/generate_api_key.py` | NEW | CLI: mint a key, hash with Argon2id, insert row, print plaintext exactly once. |
| `scripts/revoke_api_key.py` | NEW | CLI: set `revoked_at` for a `--prefix`; non-zero exit on unknown prefix. |
| `app/main.py` | MODIFIED | Mount `BearerAuthMiddleware` between `install_exception_handlers(app)` and `app.include_router(...)` calls in `create_app()`. |
| `data/` | NEW (gitignored) | Created by the Builder via `mkdir -p data` during the test checkpoint; SQLite file lives here at runtime. The directory itself is NOT committed. |

Files explicitly **not** modified in this phase: `app/errors.py` (we reuse `error_response`/`make_error` directly from `app.errors`); `app/registry.py`, `app/schemas.py`, `app/adapters/*`, `app/routers/{health,models,chat,embeddings}.py`, `config/models.yaml`, `tests/**`, `posts/**`, `docker/requirements.txt`, `README.md`, `ruff.toml`.

---

## 3. `app/config.py` Extension

### 3.1 New field

```python
keys_db_path: Path = Field(
    default=Path("./data/keys.db"),
    alias="KEYS_DB_PATH",
)
```

Inserted **after** the existing `ollama_base_url` field, preserving the existing block layout (snake_case attribute names, SHOUT_CASE alias). All other field declarations and `model_config` are unchanged.

### 3.2 Env-loading semantics

- The existing `model_config = SettingsConfigDict(env_file="config/.env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")` already applies; no changes.
- `KEYS_DB_PATH` is loaded from process env first, then from `config/.env` if present, then default.
- The default path `./data/keys.db` is resolved relative to **the process CWD**, matching how `models_yaml_path` already behaves. The Builder must not `Path.resolve()` it during settings construction — leave it relative; resolve at the call site (`app/auth.py:_init_db` will accept the value as-is).

### 3.3 `get_settings()`

Unchanged. Still `@lru_cache(maxsize=1)`, still returns `Settings()`. The new field is picked up automatically via the BaseSettings constructor.

### 3.4 Reviewer assertions

- The field appears exactly once in `Settings`, with the snake_case name `keys_db_path`, type `Path`, alias `KEYS_DB_PATH`, default `Path("./data/keys.db")`.
- `from app.config import get_settings; get_settings().keys_db_path` yields `Path("data/keys.db")` when the env var is unset, regardless of the `./` prefix in the default.

---

## 4. `app/auth.py` — Key-store layer

### 4.1 Schema

The key-store uses **a single SQLite table** matching spec §7.2 and development plan §5.1 verbatim:

```sql
CREATE TABLE IF NOT EXISTS api_keys (
  prefix       TEXT PRIMARY KEY,
  hash         TEXT NOT NULL,
  name         TEXT,
  created_at   INTEGER NOT NULL,
  last_used_at INTEGER,
  revoked_at   INTEGER
);
```

- `prefix` is the **PRIMARY KEY** — no separate index needed; SQLite covers PK lookups with the implicit `sqlite_autoindex_api_keys_1`.
- `created_at`, `last_used_at`, `revoked_at` are stored as Unix epoch seconds (`INTEGER`).
- `hash` stores Argon2id encoded form (the full string returned by `argon2.PasswordHasher().hash(token)`, including parameters and salt).
- No additional indexes for v0.2.0; volume is low (manually-minted keys).

### 4.2 Connection lifecycle (per-call open/close, NOT a pool)

Every key-store helper opens its own `sqlite3.Connection`, performs its work in a context manager (which auto-commits on success and rolls back on exception), and closes the connection. Rationale (development plan §8 step 1.5): keys.db is low-traffic; the auth path goes through Argon2 verify (~50 ms) which is two orders of magnitude slower than the SQLite open-cost (<1 ms on a warm filesystem). A pool would add lifecycle complexity for negligible win. The constraint is documented; if the gateway ever sees >100 RPS sustained on `/v1/*`, the pool decision should be revisited (open question §14, item 3).

The Builder uses the stdlib `sqlite3` module synchronously. Async access (from `BearerAuthMiddleware`) is via `asyncio.to_thread(...)` (development plan §4.5).

### 4.3 Module-level constants

```python
PREFIX_LEN: int = 12  # spec §7.3, plan §5.1
```

`PREFIX_LEN` is exported (no underscore) so the scripts and the middleware all share one source of truth.

### 4.4 Function signatures

```python
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

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


def get_row(db_path: Path, prefix: str) -> KeyRow | None:
    """SELECT * WHERE prefix = ?. Returns KeyRow or None if no match.
    Does NOT raise on missing row; caller distinguishes None from
    KeyRow(revoked_at=...)."""


def touch(db_path: Path, prefix: str) -> None:
    """UPDATE api_keys SET last_used_at = ? WHERE prefix = ?.
    No-op if the prefix doesn't exist (sqlite UPDATE on a missing row
    silently affects zero rows). Called from the auth middleware after a
    successful verify."""


def revoke(db_path: Path, prefix: str) -> bool:
    """UPDATE api_keys SET revoked_at = ? WHERE prefix = ? AND revoked_at
    IS NULL. Returns True iff a row was updated. The script uses the
    return value to set its exit code."""
```

### 4.5 Parameterized-query examples

Every query uses `?` placeholders. No string interpolation, no f-strings into SQL.

```python
# _init_db
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

# insert
conn.execute(
    "INSERT INTO api_keys (prefix, hash, name, created_at) "
    "VALUES (?, ?, ?, ?)",
    (prefix, hash_, name, int(time.time())),
)

# get_row
cur = conn.execute(
    "SELECT prefix, hash, name, created_at, last_used_at, revoked_at "
    "FROM api_keys WHERE prefix = ?",
    (prefix,),
)
row = cur.fetchone()  # returns tuple or None; caller wraps into KeyRow

# touch
conn.execute(
    "UPDATE api_keys SET last_used_at = ? WHERE prefix = ?",
    (int(time.time()), prefix),
)

# revoke
cur = conn.execute(
    "UPDATE api_keys SET revoked_at = ? "
    "WHERE prefix = ? AND revoked_at IS NULL",
    (int(time.time()), prefix),
)
updated = cur.rowcount > 0
```

The Builder must wrap each function body in:
```python
_init_db(db_path)  # idempotent; ensures parent dir + table exist
with sqlite3.connect(db_path) as conn:
    ...  # the conn context manager auto-commits on success
```

`_init_db` itself opens its own connection (`sqlite3.connect(db_path)`); calling it from inside another helper that already holds a connection is fine because each call uses its own short-lived connection.

`db_path.parent.mkdir(parents=True, exist_ok=True)` runs at the top of `_init_db` so a fresh checkout's first `generate_api_key.py` invocation auto-creates `./data/`.

### 4.6 Error semantics

| Function | Error condition | Behavior |
|---|---|---|
| `_init_db` | parent dir not writable | `OSError` propagates |
| `insert` | prefix collision | `sqlite3.IntegrityError` propagates (caller is `generate_api_key.py`, which prints a clear error and exits 1) |
| `get_row` | row missing | returns `None` |
| `get_row` | DB file missing | `_init_db` creates it first; subsequent SELECT returns `None` |
| `touch` | row missing | silent no-op (rowcount = 0); not an error path because the middleware already validated `get_row(...)` returned a row |
| `revoke` | prefix missing **or** already revoked | returns `False`; caller decides exit code |

---

## 5. `app/auth.py` — `BearerAuthMiddleware`

### 5.1 Class signature

```python
import asyncio
import re
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError, VerificationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import PREFIX_LEN, KeyRow, get_row, touch  # same module — illustrative


PUBLIC_PATHS: frozenset[str] = frozenset({
    "/healthz",
    "/docs",
    "/openapi.json",
    "/redoc",
    # NOTE: /readyz is added in Phase 2 — see §11 forward-compatibility note.
})

# Module-level Argon2id verifier — see §5.5 for the rationale.
_HASHER: PasswordHasher = PasswordHasher()

# Bearer header parser — case-insensitive scheme, exactly one whitespace run.
_BEARER_RE: re.Pattern[str] = re.compile(r"^Bearer\s+(\S+)\s*$", re.IGNORECASE)


class BearerAuthMiddleware:
    """ASGI middleware that gates every /v1/* request behind an Argon2id-
    verified bearer token from data/keys.db. lifespan and websocket
    scopes pass through unchanged (see §5.2)."""

    def __init__(self, app: ASGIApp, *, keys_db_path: Path) -> None:
        self.app: ASGIApp = app
        self.keys_db_path: Path = keys_db_path

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        ...  # see §5.2
```

**Attributes**:
- `self.app: ASGIApp` — the wrapped downstream ASGI app.
- `self.keys_db_path: Path` — passed in at mount time from `settings.keys_db_path` so the middleware does not depend on `get_settings()` at request time.

The middleware is implemented as a **plain ASGI class** (not a Starlette `BaseHTTPMiddleware` subclass). Reasons:
1. We need direct access to `scope` to attach `key_prefix` to `scope["state"]` (see §5.4) before the downstream handler builds its `Request` object.
2. `BaseHTTPMiddleware` buffers the response body, which would break SSE streaming on `/v1/chat/completions` (the streaming branch is one of the production-critical paths). A pure ASGI middleware passes `send` through untouched.
3. The plan §5.1 contract explicitly shows `__init__(app, db_path)` / `__call__(scope, receive, send)`.

### 5.2 `__call__` flow (numbered steps)

1. **Non-http scope passthrough**. If `scope["type"] != "http"` (i.e., `"lifespan"` or `"websocket"`), call `await self.app(scope, receive, send)` and return immediately. Rationale:
   - `lifespan` must reach the FastAPI lifespan handler so `app.state.adapters` etc. get built.
   - **Websockets pass through unguarded for v0.2.0** — the gateway exposes no websocket routes (the only `/v1/*` paths are HTTP). Adding websocket auth requires a different flow (subprotocol negotiation or query-string token) that the development plan does not specify. Documented as an explicit non-goal; if a websocket route lands in a future version the middleware must be updated.

2. **Public-path allowlist**. Compute `path = scope["path"]`. If `path in PUBLIC_PATHS`, skip auth and call `await self.app(scope, receive, send)`. Matching is **exact match**, NOT prefix match. Rationale:
   - Prefix match would let an attacker reach `/healthz/anything` or `/openapi.json/../v1/chat/completions` (Starlette normalizes paths but defense-in-depth says don't trust it).
   - All current public routes are exact paths, not subtrees.
   - One subtle case: FastAPI's Swagger UI at `/docs` loads `/openapi.json` separately — both are exact paths and both are in the allowlist; the Swagger static assets are served from CDN, not from the gateway, so no further allowlist entries are needed.

3. **Extract Authorization header**. Walk `scope["headers"]` (a list of `(bytes, bytes)` tuples), find the first whose name (lowercased) equals `b"authorization"`, decode its value as ASCII (per RFC 7230). Missing header → emit 401 (envelope per §6) and return.

4. **Parse bearer token**. Match `_BEARER_RE` against the header value. On no match (e.g., header is `Token sk-local-...`, `Basic ...`, empty after `Bearer `, multiple tokens) → 401. On match, extract the captured group as `token: str`.

5. **Compute prefix**. `prefix = token[:PREFIX_LEN]` where `PREFIX_LEN = 12`. **This is the first 12 characters of the FULL plaintext token** — see §7 for the disambiguation. For a well-formed token `sk-local-AbCdEf...`, the prefix is `sk-local-AbC` (9 chars of the literal `sk-local-` plus 3 characters of the urlsafe random suffix). The middleware does NOT validate that the token starts with `sk-local-`; that is a deliberate choice (any token whose 12-char prefix isn't in the DB returns 401 anyway, and not enforcing the literal lets us rotate the prefix scheme in a future version without touching the middleware).

6. **DB lookup**. `row = await asyncio.to_thread(get_row, self.keys_db_path, prefix)`. If `row is None` → 401. If `row.revoked_at is not None` → 401.

7. **Argon2 verify**. Call `await asyncio.to_thread(_HASHER.verify, row.hash, token)`. The library raises:
   - `VerifyMismatchError` — token does not match hash → 401.
   - `InvalidHashError` — stored hash is malformed (corrupted DB) → 401 with the same envelope; log a warning at module logger level so operators can see the corruption (Phase 2's structlog migration will pick this up).
   - `VerificationError` (base class for the above plus library-internal failures) — 401.
   - On success returns `True` (or returns without raising depending on argon2-cffi version; treat any non-exception result as success).

8. **Touch last_used_at**. `await asyncio.to_thread(touch, self.keys_db_path, prefix)`. This is fire-and-forget from the middleware's perspective: a rare touch failure (disk full) should NOT fail the request, but for v0.2.0 we let the exception propagate (it will hit the catch-all 500 handler). If empirical failure modes show this is too aggressive, add a try/except that logs and continues — flagged as open question §14, item 4.

9. **Attach key_prefix to scope state**. Set `scope["state"]["key_prefix"] = prefix`. **Note**: Starlette/FastAPI initialize `scope["state"]` to `{}` very early (before any middleware runs) only when a route handler accesses `request.state` — but in pure ASGI middleware we cannot rely on that. The Builder must defensively set:
   ```python
   scope.setdefault("state", {})["key_prefix"] = prefix
   ```
   This is the pattern Starlette's own `SessionMiddleware` uses. Phase 2's `RequestLoggingMiddleware` will read `scope["state"]["key_prefix"]` (or equivalently `request.state.key_prefix` from a downstream FastAPI dependency).

10. **Pass through**. `await self.app(scope, receive, send)` and return.

### 5.3 401 emission

Use `JSONResponse` directly inside the middleware. Construction:

```python
from app.errors import make_error  # reuses the existing envelope builder

response = JSONResponse(
    status_code=401,
    content=make_error(
        type_="invalid_request_error",
        message=<one of the four messages below>,
        param="Authorization",
        code="invalid_api_key",
    ),
)
await response(scope, receive, send)
return
```

`make_error()` already returns the dict shape `{"error": {"type", "message", "param", "code"}}` per `app/errors.py`. We reuse it rather than reimplementing the envelope. Helper extraction is therefore unnecessary (development plan §5.1 already names this contract); see §6 for the JSON shape.

**Message text by failure mode** (used to populate `message`):
- Missing header → `"Missing Authorization header"`
- Malformed header (regex no match) → `"Malformed Authorization header"`
- Unknown prefix → `"Invalid API key"`
- Revoked key → `"Invalid API key"` (deliberately identical to unknown — do not leak to a probing client whether a prefix exists; OWASP standard practice)
- Argon2 verify mismatch → `"Invalid API key"`

The exact message strings are not load-bearing for SDK clients (who key off `code`); they exist for human debuggers. The Builder may shorten them to a single `"Invalid API key"` for all five cases without changing the contract; the variant above is recommended for ops debuggability.

### 5.4 `scope["state"]` mutation pattern

FastAPI `Request.state` is a `starlette.datastructures.State` object, which under the hood reads from `scope["state"]` (a plain dict). Setting `scope["state"]["key_prefix"] = prefix` is therefore equivalent to `request.state.key_prefix = prefix` from a downstream dependency, and Phase 2's logging middleware (or any future FastAPI dependency) can read it via `request.state.key_prefix`.

The `scope.setdefault("state", {})` defensive line is required because pure ASGI middleware runs before the FastAPI app installs its own `state`-population step; in production the existing FastAPI stack will re-use the same dict, but the defensive line is the documented pattern and what Starlette tests assert.

### 5.5 Argon2 verifier instantiation: module-level

**Decision**: instantiate `PasswordHasher()` once at module import (`_HASHER = PasswordHasher()`) and reuse the same instance for every request.

**Justification**:
- `PasswordHasher` is **stateless and thread-safe** for `verify()` per `argon2-cffi` docs.
- Per-request instantiation costs ~1 ms on hot path; negligible compared to the ~50 ms `verify()` cost itself, but architecturally noisier.
- Library defaults are kept (`time_cost=2`, `memory_cost=65536`, `parallelism=8` as of argon2-cffi 23.1). The development plan does not specify alternative parameters; OWASP's current Argon2id recommendation is parameters above these defaults, so we are safe and fast. If parameter tuning is requested in a future version, only the module-level `PasswordHasher(...)` constructor changes.

**Alternative considered and rejected**: per-request `PasswordHasher()` instantiation. Adds no isolation benefit (the object is stateless) and obscures intent.

### 5.6 Concurrency / SQLite open-per-request

The middleware does TWO synchronous SQLite operations per authenticated request, each wrapped in `asyncio.to_thread`:
1. `get_row(prefix)` — one SELECT.
2. `touch(prefix)` — one UPDATE.

Plus one Argon2 verify (also `to_thread`-wrapped, ~50 ms).

**Constraint**: each of those `to_thread` calls borrows a thread from `asyncio`'s default thread pool (default size = `min(32, os.cpu_count() + 4)`). On a Mac Studio with 12 cores, that's ~16 threads, which caps theoretical concurrent auth throughput at ~16 simultaneous in-flight verifications. Past that, requests queue inside `to_thread`.

For v0.2.0's LAN-internal use case (single user, occasional clients) this is fine. The threshold at which it would need revisiting is roughly **>10 concurrent in-flight `/v1/*` requests sustained**; document this in the merge note. Mitigations available later: (a) a key-validity LRU cache keyed by `prefix`, with TTL respecting `revoked_at`; (b) async SQLite via `aiosqlite`. Both are out of scope for v0.2.0.

### 5.7 Reviewer assertions

- `BearerAuthMiddleware.__init__` accepts only `app` (positional) and `keys_db_path` (keyword-only).
- `__call__` is `async` and matches the `(scope, receive, send) -> None` ASGI signature.
- Lifespan and websocket scopes are not blocked.
- All five 401 paths emit identical envelope shape (only `message` may differ).
- `scope["state"]["key_prefix"]` is set to exactly `token[:12]` on success.
- `_HASHER` is a module-level `PasswordHasher()` instance.
- All DB and verify calls are wrapped in `asyncio.to_thread`.

---

## 6. OpenAI 401 envelope

Reuses `app.errors.make_error` and `JSONResponse`. No new helper, no edits to `app/errors.py`.

### 6.1 Exact JSON shape

```json
{
  "error": {
    "type": "invalid_request_error",
    "message": "Invalid API key",
    "param": "Authorization",
    "code": "invalid_api_key"
  }
}
```

Notes on serialization:
- `make_error()` constructs the dict with `type`, `message`, `param`, `code` in that order.
- `JSONResponse` uses Starlette's default `json.dumps(..., separators=(",", ":"))` — compact form, no trailing whitespace.
- HTTP status: `401`.
- Content-Type: `application/json` (set by `JSONResponse`).
- The middleware does not set `WWW-Authenticate: Bearer realm="..."` — that is intentional. OpenAI's upstream 401 also omits it; SDK clients key off the JSON body, not the response challenge header.

### 6.2 Reviewer assertion

For all five 401 paths, `response.json()` deep-equals:
```python
{"error": {"type": "invalid_request_error", "message": <str>, "param": "Authorization", "code": "invalid_api_key"}}
```

---

## 7. `scripts/generate_api_key.py`

### 7.1 argparse signature

```bash
python scripts/generate_api_key.py --name <name>
```

```python
parser = argparse.ArgumentParser(
    description="Mint an sk-local- API key, store its Argon2id hash, "
                "and print the plaintext exactly once."
)
parser.add_argument(
    "--name",
    required=True,
    help="Human-readable label for the key (stored in api_keys.name).",
)
parser.add_argument(
    "--db",
    type=Path,
    default=None,
    help="Override KEYS_DB_PATH. Defaults to settings.keys_db_path.",
)
args = parser.parse_args()
db_path = args.db or get_settings().keys_db_path
```

### 7.2 Generation flow

```python
import secrets
from argon2 import PasswordHasher
from app.auth import PREFIX_LEN, _init_db, insert
from app.config import get_settings

token = "sk-local-" + secrets.token_urlsafe(32)  # ~43 url-safe chars
prefix = token[:PREFIX_LEN]                      # first 12 chars total
hash_ = PasswordHasher().hash(token)             # full encoded form
_init_db(db_path)                                # idempotent
insert(db_path, prefix=prefix, hash_=hash_, name=args.name)
print(token)                                     # stdout — see §7.4
sys.exit(0)
```

### 7.3 Prefix semantics — disambiguation (LOAD-BEARING)

The development plan says `prefix=token[:12]` and the spec §7.2 explicitly comments `'sk-local-XXXX' first 12 chars`. There were two readings of "the 12-char slice":

- **Reading A (CHOSEN)**: `prefix = token[:12]` where `token = "sk-local-" + secrets.token_urlsafe(32)`. Since `len("sk-local-") == 9`, the prefix is `"sk-local-"` plus the **first 3** characters of the random suffix. Example: token `sk-local-AbCdEf...` yields prefix `sk-local-AbC`.

- **Reading B (REJECTED)**: prefix is the first 12 characters of the random suffix only (excluding the literal `sk-local-`).

**Reading A wins** for three reasons:
1. Spec §7.3 explicitly shows `PREFIX_LEN = 12` and `prefix = token[:PREFIX_LEN]` — applied to the full plaintext including the literal scheme.
2. Spec §7.2 schema comment `'sk-local-XXXX' first 12 chars` is consistent only with reading A (the "XXXX" is the first 3 random chars; `sk-local-XXX` totals 12 chars).
3. The plan's example string `sk-local-AbCd` (13 chars) is a typo / aspirational rendering — the contract is `token[:12]`, period. The Reviewer must not treat the loose example as a counter-spec.

**Locked**: `PREFIX_LEN = 12` is a `token[:12]` slice on the FULL plaintext token (literal `sk-local-` included). The Builder uses `token[:PREFIX_LEN]` everywhere — middleware, `generate_api_key.py`, `revoke_api_key.py`. The Reviewer asserts `len(prefix) == 12` and `prefix.startswith("sk-local-")` for any token minted with the standard literal.

### 7.4 stdout contract

The script prints **exactly one line** to stdout and nothing else (no banner, no instructions, no trailing whitespace beyond the newline `print` adds):

```
sk-local-AbCdEf...
```

This is what the development plan §8 step 2 grep relies on:
```bash
KEY=$(uv run python scripts/generate_api_key.py --name claude-code | grep -oE 'sk-local-[A-Za-z0-9_-]+')
```

The grep regex requires the token to be on its own line, surrounded only by whitespace / line boundaries. **Anything else (e.g. a "Key minted!" banner) goes to stderr or is omitted.**

For human ergonomics, the Builder MAY emit a short courtesy line to stderr (NOT stdout) such as:
```
[stderr] mint complete; prefix=sk-local-AbC; copy the line above into your client
```
This is optional and does not affect the grep contract.

### 7.5 Exit codes

- `0` — success.
- `1` — `IntegrityError` on insert (prefix collision; vanishingly rare for `secrets.token_urlsafe(32)`). Print error to stderr; do NOT print any token to stdout.
- `2` — argparse usage error (missing `--name`); argparse handles this automatically.

### 7.6 Reviewer assertions

- `secrets.token_urlsafe(32)` is the literal call (NOT `os.urandom`, NOT `random`).
- `PasswordHasher().hash(token)` is invoked with no kwargs.
- `print(token)` is the ONLY thing on stdout.
- The DB row exists with `name == args.name`, `revoked_at IS NULL`, `last_used_at IS NULL`, `created_at` ≈ `int(time.time())`.

---

## 8. `scripts/revoke_api_key.py`

### 8.1 argparse signature

```bash
python scripts/revoke_api_key.py --prefix <prefix>
```

```python
parser = argparse.ArgumentParser(
    description="Revoke an API key by setting revoked_at = now."
)
parser.add_argument(
    "--prefix",
    required=True,
    help="The 12-char prefix (sk-local-AbC) printed at mint time.",
)
parser.add_argument(
    "--db",
    type=Path,
    default=None,
    help="Override KEYS_DB_PATH.",
)
```

### 8.2 Behavior

```python
from app.auth import revoke
from app.config import get_settings

db_path = args.db or get_settings().keys_db_path
ok = revoke(db_path, args.prefix)
if ok:
    print(f"revoked: {args.prefix}")
    sys.exit(0)
print(f"unknown or already-revoked prefix: {args.prefix}", file=sys.stderr)
sys.exit(1)
```

### 8.3 Behavior on unknown prefix

- `revoke()` returns `False`. The script prints `"unknown or already-revoked prefix: <prefix>"` to stderr and exits with code `1`.
- The development plan's test checkpoint step 8 only asserts the second `curl` returns 401 after revocation; it does NOT exercise the unknown-prefix path. The non-zero exit code on unknown prefix is contractual per the plan's "non-zero exit on unknown prefix" requirement.
- The script does NOT print anything to stdout on failure.

### 8.4 Reviewer assertions

- `revoke()` is imported from `app.auth`.
- Exit code is `0` on success and `1` on unknown / already-revoked prefix.
- A successful revocation sets `revoked_at` to a positive integer; the row is otherwise unchanged.
- A second invocation against the same prefix exits `1` (idempotent revocation: only the first call returns True because `revoke()`'s WHERE clause includes `AND revoked_at IS NULL`).

---

## 9. `app/main.py` mount order

### 9.1 Locked sequence inside `create_app()`

Per development plan §4.5, the order is:

1. `install_exception_handlers(app)` — registers `HTTPException`, `NotSupportedError`, `RequestValidationError`, catch-all (already shipped in v0.1.0; unchanged in Phase 1).
2. `app.add_middleware(BearerAuthMiddleware, keys_db_path=settings.keys_db_path)` — **NEW**.
3. `app.include_router(health_router)` — public, no `/v1` prefix (unchanged).
4. `app.include_router(models_router, prefix="/v1")` (unchanged).
5. `app.include_router(chat_router, prefix="/v1")` (unchanged).
6. `app.include_router(embeddings_router, prefix="/v1")` (unchanged).

### 9.2 Why this order

In Starlette, exception handlers wrap **outside** middleware; middleware added later wraps closer to the app. With this ordering:
- A request flows: client → exception-handler-stack → BearerAuthMiddleware → router.
- A 422 from `RequestValidationError` flows: router → middleware (sees the 422 status as it returns) → exception handler → response.
- A 501 from `NotSupportedError` flows the same way.
- The 401 from `BearerAuthMiddleware` itself fires before reaching the router and is emitted directly via `JSONResponse(401, ...)` — it does NOT pass through the exception handler stack (because the middleware is outside the router and inside the handler stack). This is correct: we want a clean 401 without going through the catch-all.

### 9.3 Settings access at mount time

```python
def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="local-ai-server",
        version="0.2.0",        # NEW: version bump in description-friendly slot
        ...
        lifespan=lifespan,
    )
    install_exception_handlers(app)
    app.add_middleware(
        BearerAuthMiddleware,
        keys_db_path=settings.keys_db_path,
    )
    app.include_router(health_router)
    app.include_router(models_router, prefix="/v1")
    app.include_router(chat_router, prefix="/v1")
    app.include_router(embeddings_router, prefix="/v1")
    return app
```

The Builder updates the FastAPI `version="..."` argument from `"0.1.0"` to `"0.2.0"` in this same edit (it's already on the line below the title in the existing `create_app`). The `description` text is NOT updated in Phase 1 (Phase 3 owns the README and metadata copy).

### 9.4 Reviewer assertions

- `from app.auth import BearerAuthMiddleware` is the only new import in `app/main.py`.
- `app.add_middleware(BearerAuthMiddleware, keys_db_path=settings.keys_db_path)` appears between `install_exception_handlers(app)` and the first `app.include_router(...)` call.
- `keys_db_path` is read from `settings`, NOT from a separately-instantiated `Settings()` (single source of truth).
- Lifespan is unchanged. (The auth middleware does not need lifespan-side init; the DB is opened per-call.)
- FastAPI `version` constructor arg is `"0.2.0"`.

---

## 10. Public-path allowlist

### 10.1 Locked Phase 1 set

```python
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/healthz",
    "/docs",
    "/openapi.json",
    "/redoc",
})
```

### 10.2 Per-route justification

| Path | Source | Public? | Notes |
|---|---|---|---|
| `/healthz` | `app/routers/health.py` (v0.1.0) | yes | Liveness probe; spec §4.4 + plan §5.1 mandate public. |
| `/docs` | FastAPI default | yes | Swagger UI, helpful for dev; the gateway is LAN-internal so leaking the schema is acceptable for v0.2.0. v0.3.0 may close it. |
| `/openapi.json` | FastAPI default | yes | Required by `/docs` to render. |
| `/redoc` | FastAPI default | yes | Companion to `/docs`; same justification. |
| `/v1/models` | `app/routers/models.py` | NO (auth required) | Even listing models leaks model names; the user wants this gated per requirements §1. |
| `/v1/chat/completions`, `/v1/embeddings` | routers | NO | Production paths. |

### 10.3 Forward-compatibility note: `/readyz` (Phase 2)

Phase 2 adds `GET /readyz` to `app/routers/health.py` (development plan §5.3). When that lands, **`/readyz` MUST be added to `PUBLIC_PATHS`** (the development plan's user-requirements §1 explicitly says `/healthz` and `/readyz` remain public).

The Phase 2 architect should make a one-line edit to `app/auth.py:PUBLIC_PATHS` to include `"/readyz"`. This is documented here so the Phase 2 architect cannot miss it. It does NOT require a Phase 1 stub line — adding `/readyz` to the allowlist before the route exists is benign but adds a 401-bypass for an unmounted path, which is technically a tiny attack surface (the 404 from FastAPI would then go through the middleware unauthenticated). Cleaner to add it in lockstep with the route in Phase 2.

### 10.4 Matching rule

**Exact string equality** between `scope["path"]` and each entry of `PUBLIC_PATHS`. NOT prefix match. NOT regex. Justification in §5.2 step 2.

---

## 11. Error handling — failure-path inventory

Every failure path observable from a Phase 1 client. Each row also names the entity that produces the response.

| # | Trigger | Status | Response body | Producer |
|---:|---|---|---|---|
| 1 | `/v1/*` request, no `Authorization` header | 401 | OpenAI envelope, `code=invalid_api_key`, `message="Missing Authorization header"` | `BearerAuthMiddleware` |
| 2 | `/v1/*` request, malformed `Authorization` (wrong scheme, empty token, multi-token) | 401 | OpenAI envelope, `code=invalid_api_key`, `message="Malformed Authorization header"` | `BearerAuthMiddleware` |
| 3 | `/v1/*` request, prefix not in DB | 401 | OpenAI envelope, `code=invalid_api_key`, `message="Invalid API key"` | `BearerAuthMiddleware` |
| 4 | `/v1/*` request, prefix in DB but `revoked_at IS NOT NULL` | 401 | identical to #3 (deliberately) | `BearerAuthMiddleware` |
| 5 | `/v1/*` request, prefix in DB but Argon2 verify mismatch | 401 | identical to #3 | `BearerAuthMiddleware` |
| 6 | `/v1/*` request, valid key, downstream router raises (404 / 422 / 501 / 500) | inherited from v0.1.0 | inherited envelope from `app/errors.py` handlers | router → handler |
| 7 | Public path (`/healthz`, `/docs`, etc.), any auth state | 200 (or whatever the route returns) | route's normal response | router |
| 8 | `scripts/generate_api_key.py` without `--name` | exit 2 | argparse stderr message | argparse |
| 9 | `scripts/generate_api_key.py`, prefix collision (PK violation) | exit 1 | stderr message; nothing to stdout | script |
| 10 | `scripts/revoke_api_key.py` with unknown / already-revoked prefix | exit 1 | stderr message | script |
| 11 | `scripts/revoke_api_key.py` without `--prefix` | exit 2 | argparse stderr | argparse |
| 12 | DB file's parent directory not writable | uvicorn boot failure (script) / 500 from middleware (gateway) | OS error trace | OS / `_init_db` |

Failure paths NOT introduced by Phase 1 (inherited from v0.1.0): all 4xx/5xx behavior of the v1 routers is unchanged. The middleware adds the 401 layer in front and is otherwise transparent.

---

## 12. Test surface for Phase 1 (informational — Phase 3 owns the tests)

These are the seams Phase 3's `tests/test_auth.py`, `tests/test_keys_db.py`, and `tests/conftest.py::temp_keys_db` will hit. Listed so the Phase 1 design is testable without further changes.

| Seam | Test surface |
|---|---|
| `app.auth.PREFIX_LEN` | importable as a module constant; assert `== 12`. |
| `app.auth.{insert, get_row, touch, revoke}` | callable with a temp `Path`; round-trip CRUD assertions. |
| `app.auth.BearerAuthMiddleware.__init__(app, keys_db_path=...)` | constructible with a stub ASGI callable. |
| `BearerAuthMiddleware` end-to-end | `TestClient(create_app())` with `KEYS_DB_PATH` env override + a minted key in the temp DB; assert 401 paths and the 200 path. |
| `app/main.py` mount order | regression test that `app.user_middleware` contains exactly one `BearerAuthMiddleware` entry. |
| `scripts/generate_api_key.py` | `subprocess.run(["python", "scripts/generate_api_key.py", "--name", "x"], env={**os.environ, "KEYS_DB_PATH": str(tmp)})`; assert stdout grep matches; assert DB row exists. |
| `scripts/revoke_api_key.py` | similar invocation; assert exit code 0 then 1; assert `revoked_at IS NOT NULL`. |
| Public-path allowlist | `client.get("/healthz")` → 200 with no auth; `client.get("/v1/models")` → 401 with no auth. |

The architect explicitly designs `BearerAuthMiddleware` to take `keys_db_path` as a keyword arg (NOT to read settings at request time) so test fixtures can mount the middleware with an arbitrary temp DB path.

---

## 13. Out of scope (explicit)

- **structlog migration** — Phase 2 (development plan §5.2). Phase 1 keeps stdlib `logging`. The auth middleware uses a module-level `logging.getLogger("app.auth")` if it logs anything at all (likely just an `InvalidHashError` warning).
- **`/readyz` fan-out** — Phase 2 (development plan §5.3). Phase 1 does not touch `app/routers/health.py`.
- **`watchfiles` hot-reload** — Phase 2 (development plan §5.4).
- **Tests** — Phase 3 (development plan §5.6). No `tests/test_auth.py`, no `tests/test_keys_db.py`, no `conftest.py` extension in this phase.
- **Notebook (`posts/v0_2_0_auth_demo.ipynb`)** — generated by a separate Example Generation Agent in step 3.7. Phase 1 produces no notebook artifact.
- **README updates** — Phase 3.
- **`docker/requirements.txt` parity** — Phase 3.
- **mypy `--strict` cleanup** — Phase 3 (the 27 v0.1.0 carryovers + any new from auth code).
- **Container path move (`/var/lib/local-ai-server/keys.db`)** — v0.3.0 with Compose.
- **Caddy / TLS / CORS** — v0.3.0.
- **Per-key rate limits, quotas, metering** — post-v1.

---

## 14. Open questions / clarifications

| # | Item | Plan reference | Architect's interpretation (locked unless overridden) |
|---:|---|---|---|
| 1 | Prefix slice semantics: `token[:12]` of the FULL plaintext (incl. `sk-local-`) vs. of the random suffix only. | Spec §7.2 + §7.3, plan §5.1 | Reading A — `token[:12]` of the full plaintext. Prefix for `sk-local-AbCdEf...` is `sk-local-AbC`. Justified in §7.3. |
| 2 | Library-default Argon2id parameters vs. explicit tuning. | Plan does not specify. | Keep `argon2-cffi` defaults (`time_cost=2`, `memory_cost=65536`, `parallelism=8`). |
| 3 | Per-call SQLite open vs. pool. | Plan §8 step 1.5 says "open per-call". | Locked: per-call. Threshold for revisit: >10 sustained concurrent `/v1/*` requests (§5.6). |
| 4 | `touch()` failure behavior — silent vs. propagate. | Plan does not specify. | v0.2.0: propagate (hits catch-all 500). If empirical issues arise (disk full, transient lock), log-and-continue can be added in v0.3.0 without contract change. |
| 5 | Websocket scope passthrough — guarded vs. unguarded. | Plan does not specify (no websocket routes in v0.2.0). | Unguarded for v0.2.0; documented in §5.2. Adding websocket auth requires a different contract and is deferred. |
| 6 | 401 message text consistency — vary by failure type or use one string. | Plan does not specify. | Use varied messages (§5.3 table) for ops debuggability. Acceptable to collapse to a single `"Invalid API key"` if the Builder finds the variants brittle to test against; SDK clients only key off `code`. |
| 7 | `WWW-Authenticate: Bearer` response header on 401. | Plan does not specify. | Omit. OpenAI's upstream 401 also omits it; SDKs key off the JSON body. |
| 8 | Identical message for unknown-prefix and revoked-key 401s — security vs. ergonomics. | Plan does not specify. | Identical (`"Invalid API key"`). OWASP guidance: do not reveal whether a prefix exists. |
| 9 | The `data/` directory itself — committed empty (with `.gitkeep`) vs. created on demand. | Plan §7 lists `data/` as "NEW (gitignored)". | Created on demand by `_init_db` (and the Phase 1 test checkpoint's `mkdir -p data`). The directory is NOT committed; the `data/` line in `.gitignore` covers a developer who creates it locally. |
| 10 | The `description` field of the FastAPI app — update to mention auth in Phase 1, or wait for Phase 3 README sweep. | Plan does not specify. | Wait for Phase 3 (README owner). Phase 1 only bumps the FastAPI `version="0.2.0"` constructor arg. |

None of these block implementation. All ten resolutions are the Builder's instructions for the ambiguous-by-default cases.

---

## 15. File creation / modification order for the Builder

This sequence minimizes broken intermediate states. Run `uv run ruff check .` after each step.

1. **`pyproject.toml`** — bump version + add `argon2-cffi>=23.1` to `[project].dependencies`. Run `uv sync` to regenerate `uv.lock`. Verify: `uv run python -c "import argon2; print(argon2.__version__)"`.
2. **`config/.env.example`** — append `KEYS_DB_PATH=./data/keys.db`. (Two lines: a blank line then the var, to keep the file readable.)
3. **`.gitignore`** — append `data/`, `data/*.db`, `data/*.db-journal` under a new comment header `# --- local-ai-server v0.2.0 (Phase 1) ---`.
4. **`app/config.py`** — add the `keys_db_path` field. Verify: `uv run python -c "from app.config import get_settings; print(get_settings().keys_db_path)"` prints `data/keys.db`.
5. **`app/auth.py`** — implement in this order: `PREFIX_LEN`, `KeyRow`, `_init_db`, `insert`, `get_row`, `touch`, `revoke`, then `_HASHER`, `_BEARER_RE`, `PUBLIC_PATHS`, then `BearerAuthMiddleware`. Verify imports: `uv run python -c "from app.auth import BearerAuthMiddleware, insert, get_row, touch, revoke, PREFIX_LEN; print('ok')"`.
6. **`scripts/generate_api_key.py`** — depends on `app.auth.{insert, _init_db, PREFIX_LEN}` and `app.config.get_settings`. Verify: `mkdir -p data && uv run python scripts/generate_api_key.py --name test | grep -E '^sk-local-'`.
7. **`scripts/revoke_api_key.py`** — depends on `app.auth.revoke`. Verify: pick the prefix from step 6, run revoke, observe exit 0; rerun and observe exit 1.
8. **`app/main.py`** — add the `app.add_middleware(BearerAuthMiddleware, keys_db_path=settings.keys_db_path)` line in the locked position; bump `version="0.2.0"`. Verify: full Phase 1 test checkpoint (development plan §8 part C steps 1-9).

---

## 16. Done-when checklist

Mirrors the development plan §8 Phase 1 checkpoint. Phase 1 is complete when:

- [ ] `pyproject.toml` declares `version = "0.2.0"` and lists `argon2-cffi>=23.1` in `[project].dependencies`.
- [ ] `uv sync` succeeds and produces an updated `uv.lock`.
- [ ] `app/config.py` exposes `Settings.keys_db_path: Path` with alias `KEYS_DB_PATH` and default `Path("./data/keys.db")`.
- [ ] `config/.env.example` ends with `KEYS_DB_PATH=./data/keys.db`.
- [ ] `.gitignore` contains `data/`, `data/*.db`, `data/*.db-journal`.
- [ ] `app/auth.py` exports `PREFIX_LEN`, `KeyRow`, `_init_db`, `insert`, `get_row`, `touch`, `revoke`, `BearerAuthMiddleware`, and `PUBLIC_PATHS`.
- [ ] `scripts/generate_api_key.py --name X` prints exactly one `sk-local-...` line on stdout, inserts a row, and exits 0.
- [ ] `scripts/revoke_api_key.py --prefix <prefix>` sets `revoked_at` and exits 0; a second invocation exits 1.
- [ ] `app/main.py:create_app()` adds `BearerAuthMiddleware` between `install_exception_handlers(app)` and the first `app.include_router(...)` call, with `keys_db_path` sourced from `get_settings()`.
- [ ] `GET /healthz` returns 200 with no auth.
- [ ] `GET /v1/models` without an Authorization header returns 401 with the OpenAI envelope (`type=invalid_request_error`, `param=Authorization`, `code=invalid_api_key`).
- [ ] `GET /v1/models` with `Authorization: Bearer <minted-key>` returns 200 with the 4 registry models.
- [ ] After `revoke_api_key.py`, the same key returns 401.
- [ ] A malformed header (`Token <key>`) returns 401.
- [ ] After a successful `/v1/*` call, the row's `last_used_at` is non-null and approximately equal to `int(time.time())`.
- [ ] `uv run ruff check .` is clean.
- [ ] No edits to `app/errors.py`, `app/registry.py`, `app/schemas.py`, `app/adapters/*`, `app/routers/*`, `tests/**`, `posts/**`, `docker/requirements.txt`, `README.md`, or `ruff.toml`.

End of Phase 1 architecture specification.
