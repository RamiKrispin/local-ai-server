"""TestClient-level tests for BearerAuthMiddleware.

All tests are unconditional — no live Ollama needed. The 401-path tests
fire before any backend dispatch; the 200-path test hits /v1/models which
is served from the in-process registry.

Test inventory (13 tests):
  - test_auth_missing_header_returns_401
  - test_auth_malformed_scheme_returns_401
  - test_auth_empty_bearer_returns_401
  - test_auth_no_scheme_returns_401
  - test_auth_unknown_prefix_returns_401
  - test_auth_revoked_key_returns_401
  - test_auth_hash_mismatch_returns_401
  - test_auth_corrupted_hash_returns_401
  - test_auth_valid_key_returns_200
  - test_auth_envelope_shape_exact
  - test_auth_last_used_at_updated_on_success
  - test_auth_public_paths_skip_auth
  - test_auth_lowercase_bearer_scheme_returns_200
"""
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import get_row, revoke
from tests.conftest import _TEST_HASHER


# ---------------------------------------------------------------------------
# 401 paths
# ---------------------------------------------------------------------------


def test_auth_missing_header_returns_401(
    client_with_auth: TestClient,
) -> None:
    """No Authorization header returns 401 with the OpenAI error envelope."""
    response = client_with_auth.get("/v1/models")
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "invalid_api_key"
    assert error["param"] == "Authorization"
    assert "Missing" in error["message"]


def test_auth_malformed_scheme_returns_401(
    client_with_auth: TestClient,
    temp_keys_db: tuple[Path, str, str],
) -> None:
    """Authorization: Token <plaintext> returns 401 (wrong scheme)."""
    _db, plaintext, _prefix = temp_keys_db
    response = client_with_auth.get(
        "/v1/models",
        headers={"Authorization": f"Token {plaintext}"},
    )
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_api_key"
    assert "Malformed" in error["message"]


def test_auth_empty_bearer_returns_401(
    client_with_auth: TestClient,
) -> None:
    """Authorization: Bearer (with no token) returns 401."""
    response = client_with_auth.get(
        "/v1/models",
        headers={"Authorization": "Bearer "},
    )
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_api_key"
    assert "Malformed" in error["message"]


def test_auth_no_scheme_returns_401(
    client_with_auth: TestClient,
    temp_keys_db: tuple[Path, str, str],
) -> None:
    """Authorization: <plaintext> (no scheme) returns 401."""
    _db, plaintext, _prefix = temp_keys_db
    response = client_with_auth.get(
        "/v1/models",
        headers={"Authorization": plaintext},
    )
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_api_key"
    assert "Malformed" in error["message"]


def test_auth_unknown_prefix_returns_401(
    client_with_auth: TestClient,
) -> None:
    """A well-formed Bearer token whose prefix is not in the DB returns 401."""
    response = client_with_auth.get(
        "/v1/models",
        headers={
            "Authorization": (
                "Bearer "
                "sk-local-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            )
        },
    )
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_api_key"
    assert "Invalid" in error["message"]


def test_auth_revoked_key_returns_401(
    client_with_auth: TestClient,
    temp_keys_db: tuple[Path, str, str],
) -> None:
    """A revoked key returns 401 after a prior successful request."""
    db_path, plaintext, prefix = temp_keys_db
    headers = {"Authorization": f"Bearer {plaintext}"}
    # Confirm the key works before revocation.
    resp = client_with_auth.get("/v1/models", headers=headers)
    assert resp.status_code == 200
    # Revoke via the helper and retry.
    revoke(db_path, prefix)
    response = client_with_auth.get("/v1/models", headers=headers)
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_api_key"


def test_auth_hash_mismatch_returns_401(
    client_with_auth: TestClient,
    temp_keys_db: tuple[Path, str, str],
) -> None:
    """A key whose DB hash doesn't match the plaintext returns 401.

    This locks the VerifyMismatchError branch in BearerAuthMiddleware.
    """
    db_path, plaintext, prefix = temp_keys_db
    wrong_hash = _TEST_HASHER.hash("different-secret")
    # Replace the stored hash with one that doesn't match our plaintext.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE api_keys SET hash = ? WHERE prefix = ?",
            (wrong_hash, prefix),
        )
    response = client_with_auth.get(
        "/v1/models",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_api_key"


def test_auth_corrupted_hash_returns_401(
    client_with_auth: TestClient,
    temp_keys_db: tuple[Path, str, str],
) -> None:
    """A row whose hash is not a valid Argon2 string returns 401.

    This locks the InvalidHashError branch in BearerAuthMiddleware.
    """
    db_path, plaintext, prefix = temp_keys_db
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE api_keys SET hash = ? WHERE prefix = ?",
            ("NOT_AN_ARGON2_HASH", prefix),
        )
    response = client_with_auth.get(
        "/v1/models",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_api_key"


# ---------------------------------------------------------------------------
# 200 path
# ---------------------------------------------------------------------------


def test_auth_valid_key_returns_200(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """A valid key returns 200 with a 4-entry model list."""
    response = client_with_auth.get("/v1/models", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 4


# ---------------------------------------------------------------------------
# Envelope shape lock
# ---------------------------------------------------------------------------


def test_auth_envelope_shape_exact(
    client_with_auth: TestClient,
) -> None:
    """The 401 body has exactly one top-level key 'error' with 4 sub-keys."""
    response = client_with_auth.get("/v1/models")
    assert response.status_code == 401
    body = response.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {
        "type",
        "message",
        "param",
        "code",
    }


# ---------------------------------------------------------------------------
# last_used_at regression
# ---------------------------------------------------------------------------


def test_auth_last_used_at_updated_on_success(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
    temp_keys_db: tuple[Path, str, str],
) -> None:
    """last_used_at is None before the request and set after a 200."""
    db_path, _plaintext, prefix = temp_keys_db
    before_row = get_row(db_path, prefix)
    assert before_row is not None
    assert before_row.last_used_at is None

    before_ts = int(time.time())
    response = client_with_auth.get("/v1/models", headers=auth_headers)
    assert response.status_code == 200

    after_row = get_row(db_path, prefix)
    assert after_row is not None
    assert after_row.last_used_at is not None
    assert after_row.last_used_at > 0
    assert after_row.last_used_at >= before_ts


# ---------------------------------------------------------------------------
# Public-path allowlist
# ---------------------------------------------------------------------------


def test_auth_public_paths_skip_auth(
    client_with_auth: TestClient,
) -> None:
    """Public paths return non-401 responses with no Authorization header."""
    for path in ("/healthz", "/readyz", "/openapi.json"):
        response = client_with_auth.get(path)
        assert response.status_code != 401, (
            f"Public path {path} returned 401"
        )
    # /docs and /redoc may redirect (308) to /docs/ or /redoc/; neither is 401.
    for path in ("/docs", "/redoc"):
        response = client_with_auth.get(path, follow_redirects=False)
        assert response.status_code != 401, (
            f"Public path {path} returned 401"
        )


# ---------------------------------------------------------------------------
# Case-insensitive bearer scheme
# ---------------------------------------------------------------------------


def test_auth_lowercase_bearer_scheme_returns_200(
    client_with_auth: TestClient,
    temp_keys_db: tuple[Path, str, str],
) -> None:
    """Authorization: bearer <plaintext> (lowercase) returns 200.

    Locks the re.IGNORECASE behaviour of _BEARER_RE.
    """
    _db, plaintext, _prefix = temp_keys_db
    response = client_with_auth.get(
        "/v1/models",
        headers={"Authorization": f"bearer {plaintext}"},
    )
    assert response.status_code == 200
