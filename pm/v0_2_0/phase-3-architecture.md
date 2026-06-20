# Phase 3 — Tests + mypy + docs — Architecture Specification

**Project**: local-ai-server
**Version**: v0.2.0
**Phase**: 3 — Tests + mypy + docs (FINAL phase of v0.2.0)
**Branch**: `phase/local-ai-server/3-tests-mypy-docs` (already created off `dev/local-ai-server`)
**Date**: 2026-06-19
**Precondition**: Phase 1 (Auth foundation) merged at `d9fba5c` on 2026-06-19; Phase 2 (Observability) merged at `766cb04` on 2026-06-19. The wired surface (`BearerAuthMiddleware`, `app/auth.py:{insert,get_row,touch,revoke,PREFIX_LEN}`, `scripts/generate_api_key.py`, `scripts/revoke_api_key.py`, `app/logging.py:{configure_structlog,redact_authorization}`, `app/middleware_logging.py:RequestLoggingMiddleware`, `app/registry_watcher.py:start_registry_watcher`, `app/routers/health.py:readyz`, `app/auth.py:PUBLIC_PATHS` containing `/readyz`) is the seam this phase tests and types.

Target output path: `/Users/ramikrispin/Personal/tutorials/local-ai-server/pm/v0_2_0/phase-3-architecture.md`.

---

## 1. Overview & Boundaries

### 1.1 Goal

Phase 3 closes v0.2.0 by adding the live + mocked test suite covering everything Phase 1 and Phase 2 built, fixing every `mypy --strict` finding (the 27 carried forward from v0.1.0 plus any introduced by `app/auth.py`, `app/logging.py`, `app/middleware_logging.py`, `app/registry_watcher.py`, the `app/routers/health.py` extension, `app/main.py` rewrites, and the two scripts), rewriting `README.md` for v0.2.0, and bringing `docker/requirements.txt` to runtime parity. Phase 3 owns no new user-facing surface — no notebook, no new endpoint.

This document is the implementation contract for the Builder Agent and the assertion contract for the Reviewer.

### 1.2 In scope

| Surface | Phase 3 deliverable |
|---|---|
| `tests/conftest.py` | EXTEND with `temp_keys_db`, `client_with_auth`, `auth_headers`, `captured_log`, `tmp_models_yaml` fixtures plus a structlog test-mode setup. |
| `tests/test_keys_db.py` | NEW — direct CRUD tests for `app.auth.{insert, get_row, touch, revoke, _init_db}`. |
| `tests/test_auth.py` | NEW — TestClient-level tests for the five 401 paths + 200 + `last_used_at` regression. |
| `tests/test_readyz.py` | NEW — `/readyz` happy path (live Ollama) + httpx-mocked all-down 503 + public-path no-auth assertion. |
| `tests/test_hot_reload.py` | NEW — mutate a temp `models.yaml`, poll `/v1/models`, assert the new id appears, assert object identity changed. |
| `tests/test_logging.py` | NEW — capture structlog output, assert `event="request"` fields, deliberate-leak redaction grep, `event="registry_loaded"` regression. |
| `tests/test_chat.py`, `test_chat_streaming.py`, `test_embeddings.py`, `test_models_endpoint.py` | MIGRATE — switch from `client` fixture to `client_with_auth` / send the bearer header so tests stay green under the new middleware. |
| `app/**/*.py` | TYPE FIXES ONLY — no behavior changes; address every `mypy --strict` finding per §6. |
| `README.md` | REWRITE for v0.2.0 (Auth section, `/readyz`, hot-reload, SDK snippet with `api_key="sk-local-..."`, version roadmap update). |
| `docker/requirements.txt` | APPEND `argon2-cffi`, `structlog`, `watchfiles`. |
| `pyproject.toml` | AUDIT (already at `0.2.0`; deps already added in Phase 1+2). No version bump. |
| `ruff.toml` | AUDIT — Phase 1+2 left it clean; final check pass. |

### 1.3 Out of scope (explicit)

- **No `app/` behavior changes**. Type annotations only. If a test surfaces a behavior bug, the Builder reports it as a Phase 1 or Phase 2 regression and stops.
- **No `tests/test_registry.py`, `tests/adapters/*` changes** — those are v0.1.0 tests, frozen.
- **No new notebook** (plan §1: "no notebook in Phase 3").
- **No coverage tooling** (pytest-cov, coverage.xml) — `htmlcov/` remains in `.gitignore` as forward-looking insurance.
- **No `app/` source moves or refactors**. `app/auth.py`, `app/logging.py`, `app/middleware_logging.py`, `app/registry_watcher.py` are frozen except for type annotations.
- **No `pytest -n auto` (xdist) wiring** — same reason as v0.1.0.
- **No `.devcontainer/`, `.vscode/`, `docs/`, `assets/`, `posts/`, prior `pm/v0_1_0/**` or `pm/v0_2_0/{development_plan,phase-1-architecture,phase-2-architecture,summary}.md` edits**.
- **No `mypy --strict` invocation by this Architect or the Builder** — that is QA's job (input contract). The Builder applies the fixes from §6; QA runs `mypy --strict` and reports the residual count.
- **Caddy / TLS / Compose / real MLX / real DMR / per-key rate limits** — deferred.

### 1.4 What this phase delivers

- 5 NEW test files; 4 MODIFIED test files; 1 EXTENDED conftest.
- Type-clean `app/` per `mypy --strict` (27 v0.1.0 carryovers + any new in Phase 1/2 source).
- Rewritten `README.md` for v0.2.0.
- 3 lines appended to `docker/requirements.txt`.

---

## 2. Module / File Specifications

Conventions inherited from Phases 1 and 2: PEP 604 unions (`X | None`), 79-char line length, double-quote `quote-style`, `frozen=True, slots=True` on value objects, `pytest-asyncio` `auto` mode, `live` marker for tests that need Ollama, `_OpenAIModel` schema base.

The Phase 3 fixtures keep the existing `client`, `app_factory`, `ollama_alive`, `registry`, and the `live`-marker collection hook UNCHANGED. New fixtures are additive.

### 2.1 `tests/conftest.py` — EXTENDED

**Path**: `tests/conftest.py`
**Status**: EXTEND. The existing module is not refactored; new fixtures + a structlog test-setup hook are appended.
**Purpose**: provide hermetic Auth + structlog test wiring without disturbing the v0.1.0 fixtures.

**New imports** (added at the top of the file after the existing imports):

```python
import io
import os
import shutil
from collections.abc import Callable, Iterable
from typing import Any

import structlog
from argon2 import PasswordHasher

from app.auth import PREFIX_LEN, _init_db, insert
```

**Module-level constants** (added after the existing `OLLAMA_PROBE_*` constants):

```python
# Argon2 parameters tuned for fast unit tests. The library default is
# (time_cost=2, memory_cost=65536 KiB, parallelism=8) which costs ~50 ms
# per verify(). We dial it down to ~1 ms per verify() for the test suite
# only — the parameters are encoded into the hash, so each test fixture
# both hashes and verifies under the same (cheap) settings.
_TEST_HASHER: PasswordHasher = PasswordHasher(
    time_cost=1,
    memory_cost=8,        # 8 KiB
    parallelism=1,
)

_TEST_KEY_PLAINTEXT_BASE: str = (
    "sk-local-TestFixturePlaintextValueDoNotUseInProd0123456789"
)
```

**New fixtures**:

```python
@pytest.fixture
def temp_keys_db(tmp_path: Path) -> tuple[Path, str, str]:
    """Create an isolated SQLite key store with one valid key minted into it.

    Returns:
        (db_path, plaintext_token, prefix) — the caller mounts the gateway
        with `KEYS_DB_PATH=db_path` and authenticates with the plaintext.

    Scope: function. Each test gets a fresh DB file in `tmp_path`. Teardown
    is implicit via tmp_path.
    """
    db_path = tmp_path / "keys.db"
    plaintext = _TEST_KEY_PLAINTEXT_BASE
    prefix = plaintext[:PREFIX_LEN]
    hash_ = _TEST_HASHER.hash(plaintext)
    _init_db(db_path)
    insert(db_path, prefix=prefix, hash_=hash_, name="pytest-fixture")
    return db_path, plaintext, prefix


@pytest.fixture
def client_with_auth(
    temp_keys_db: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Function-scoped TestClient over a fresh app whose BearerAuthMiddleware
    is bound to the temp key store.

    Strategy:
      1. monkeypatch `KEYS_DB_PATH` env var BEFORE create_app() runs so
         get_settings() (lru_cached) picks up the temp DB on first call;
      2. clear `get_settings.cache_clear()` so a fresh Settings is built
         (the cache may have been warmed by an earlier test);
      3. construct the app, enter the TestClient context (lifespan runs);
      4. yield;
      5. on teardown clear the cache again so the next test starts clean.

    The yielded TestClient does NOT preset an Authorization header — tests
    use the `auth_headers` helper (or set headers explicitly) so missing-
    header tests are also covered by the same fixture.
    """
    db_path, _plaintext, _prefix = temp_keys_db
    monkeypatch.setenv("KEYS_DB_PATH", str(db_path))
    from app.config import get_settings  # local to avoid import-time cache
    get_settings.cache_clear()
    from app.main import create_app  # local: settings are read in create_app
    app = create_app()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        get_settings.cache_clear()


@pytest.fixture
def auth_headers(
    temp_keys_db: tuple[Path, str, str],
) -> dict[str, str]:
    """Return a dict ready to be passed as `headers=` to TestClient calls,
    carrying a Bearer Authorization for the key minted in temp_keys_db.

    Usage:
        client_with_auth.get("/v1/models", headers=auth_headers)
    """
    _db_path, plaintext, _prefix = temp_keys_db
    return {"Authorization": f"Bearer {plaintext}"}


@pytest.fixture
def captured_log(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[dict[str, Any]]]:
    """Capture every structlog event emitted during the test as a list of
    dicts (the event_dict before JSON rendering).

    Mechanism: replace structlog's processor chain with one that appends the
    event_dict to a list and short-circuits the renderer (returning the
    dict instead of a JSON string). Restore the previous structlog state on
    teardown.

    Yields:
        A mutable list of event_dicts; tests append nothing and inspect at
        will after the action under test.

    Scope: function. The fixture must run AFTER any auto-use that calls
    `configure_structlog`; the lifespan startup of `client_with_auth`
    re-runs `configure_structlog`, which would clobber our capture chain.
    Therefore: depend on this fixture EXPLICITLY in test signatures and
    call `_install_capture(events)` from a TestClient-level hook OR after
    TestClient enter — see §3.5 for the exact dance.

    Limitation: this captures events emitted from structlog directly. Stdlib
    `logging.getLogger(...)` calls flow through the bridge in
    app/logging.py and are NOT visible to this list — for those, use
    `caplog` (pytest's stdlib log fixture) in addition.
    """
    events: list[dict[str, Any]] = []
    saved_config = structlog.get_config()

    def _capture_processor(
        logger: Any, method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        events.append(dict(event_dict))
        return event_dict

    structlog.configure(
        processors=[_capture_processor],
        wrapper_class=structlog.make_filtering_bound_logger(0),  # NOTSET
        logger_factory=structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    try:
        yield events
    finally:
        structlog.configure(**saved_config)


@pytest.fixture
def tmp_models_yaml(tmp_path: Path) -> Path:
    """Copy the repo's config/models.yaml to a tempdir so tests can mutate
    it without polluting the working tree.

    Returns:
        Path to the temp YAML. The caller monkeypatches MODELS_YAML_PATH to
        this path BEFORE constructing the app so the registry watcher
        watches the temp file.
    """
    src = Path("config/models.yaml")
    dst = tmp_path / "models.yaml"
    shutil.copy(src, dst)
    return dst
```

**Interface contracts**:

| Fixture | Scope | What it yields | Teardown |
|---|---|---|---|
| `temp_keys_db` | function | `(Path, str, str)` | implicit (tmp_path) |
| `client_with_auth` | function | `TestClient` with lifespan running, KEYS_DB_PATH bound to temp DB | TestClient context exit + `get_settings.cache_clear()` |
| `auth_headers` | function | `{"Authorization": "Bearer sk-local-..."}` | none |
| `captured_log` | function | `list[dict[str, Any]]` | restore prior structlog config |
| `tmp_models_yaml` | function | `Path` to a temp copy of `config/models.yaml` | implicit |

**Notes**:

- `_TEST_HASHER` uses fast Argon2 parameters (`time_cost=1, memory_cost=8, parallelism=1`) to keep auth-test wall time manageable. The PasswordHasher embeds parameters in the encoded hash; verification under a different `PasswordHasher()` instance with stronger defaults still succeeds because argon2-cffi reads parameters from the hash string. The Builder verifies this by manually round-tripping `PasswordHasher().verify(_TEST_HASHER.hash(t), t)`.
- The `_TEST_KEY_PLAINTEXT_BASE` constant is deliberately fixed (not random) so the fixture is reproducible. It is NOT exposed via the public `app.auth` API; tests that need a different plaintext can call `_TEST_HASHER.hash(...)` directly.
- The `client_with_auth` fixture intentionally does NOT call `_init_db` itself — `temp_keys_db` did that.
- The `client` (no-auth) and `client_with_auth` fixtures live side-by-side; the v0.1.0 tests in `tests/test_registry.py` and `tests/adapters/*` continue to use `client` only when the test path stays public. v0.1.0 tests that hit `/v1/*` migrate to `client_with_auth` per §5.

### 2.2 `tests/test_keys_db.py` — NEW

**Path**: `tests/test_keys_db.py`
**Status**: NEW.
**Purpose**: lock the SQLite key-store helpers' contract — direct CRUD round-trips with no FastAPI involvement.

**Imports**:

```python
import sqlite3
import time
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from app.auth import (
    KeyRow,
    PREFIX_LEN,
    _init_db,
    get_row,
    insert,
    revoke,
    touch,
)
```

**Test inventory** (all run unconditionally, no live deps):

| Test function | Assertion summary | Fixtures |
|---|---|---|
| `test_keys_db_init_creates_table(tmp_path)` | `_init_db(path)` creates the `api_keys` table on a fresh path; running it twice is a no-op (no `IntegrityError`). | `tmp_path` |
| `test_keys_db_init_creates_parent_dirs(tmp_path)` | `_init_db(tmp_path / "nested" / "deeper" / "keys.db")` succeeds — parent dirs are created. | `tmp_path` |
| `test_keys_db_insert_roundtrips(tmp_path)` | `insert(...)` then `get_row(...)` returns a `KeyRow` with the same `prefix`, `hash`, `name`; `created_at` is `~int(time.time())`; `last_used_at is None`; `revoked_at is None`. | `tmp_path` |
| `test_keys_db_get_row_missing_returns_none(tmp_path)` | `get_row(path, "sk-local-XXX")` on an empty DB returns `None`, does NOT raise. | `tmp_path` |
| `test_keys_db_insert_duplicate_prefix_raises(tmp_path)` | A second `insert` with the same prefix raises `sqlite3.IntegrityError`. | `tmp_path` |
| `test_keys_db_touch_updates_last_used_at(tmp_path)` | After `insert(...)` then `touch(...)`, `get_row(...).last_used_at` is `~int(time.time())` and `> 0`. | `tmp_path` |
| `test_keys_db_touch_missing_prefix_is_silent(tmp_path)` | `touch(path, "nonexistent")` on an empty DB does NOT raise. | `tmp_path` |
| `test_keys_db_revoke_returns_true_on_first_call(tmp_path)` | After `insert(...)`, `revoke(path, prefix)` returns `True`; `get_row(...).revoked_at` is `> 0`. | `tmp_path` |
| `test_keys_db_revoke_returns_false_on_second_call(tmp_path)` | A second `revoke(path, prefix)` returns `False` (idempotent — no row matches `revoked_at IS NULL`). | `tmp_path` |
| `test_keys_db_revoke_returns_false_on_unknown_prefix(tmp_path)` | `revoke(path, "sk-local-XXX")` on an empty DB returns `False`. | `tmp_path` |
| `test_keys_db_keyrow_is_frozen()` | `dataclasses.replace(KeyRow(...), prefix="x")` works (per dataclass contract); direct attribute assignment raises `dataclasses.FrozenInstanceError` (slots=True + frozen=True). | none |
| `test_keys_db_prefix_len_is_12()` | `PREFIX_LEN == 12`. | none |

**Edge cases covered**: missing parent dir, fresh DB, duplicate insert, missing-row touch (silent), idempotent revoke, KeyRow immutability, PREFIX_LEN regression.

**Marks**: none. All tests run without Ollama.

### 2.3 `tests/test_auth.py` — NEW

**Path**: `tests/test_auth.py`
**Status**: NEW.
**Purpose**: lock the five 401 paths plus the 200 path plus the `last_used_at` regression at the TestClient level.

**Imports**:

```python
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
```

**Test inventory** (all run unconditionally — error paths fire before any backend dispatch):

| Test function | Assertion summary | Fixtures |
|---|---|---|
| `test_auth_missing_header_returns_401(client_with_auth)` | `client_with_auth.get("/v1/models")` (no header) → 401; envelope: `{type=invalid_request_error, code=invalid_api_key, param=Authorization, message="Missing Authorization header"}`. | `client_with_auth` |
| `test_auth_malformed_scheme_returns_401(client_with_auth, temp_keys_db)` | `Authorization: Token <plaintext>` → 401; same envelope; `message="Malformed Authorization header"`. | `client_with_auth`, `temp_keys_db` |
| `test_auth_empty_bearer_returns_401(client_with_auth)` | `Authorization: Bearer ` (empty token) → 401; `message="Malformed Authorization header"` (regex no-match). | `client_with_auth` |
| `test_auth_no_scheme_returns_401(client_with_auth, temp_keys_db)` | `Authorization: <plaintext>` (raw token, no scheme) → 401; `message="Malformed Authorization header"`. | `client_with_auth`, `temp_keys_db` |
| `test_auth_unknown_prefix_returns_401(client_with_auth)` | `Authorization: Bearer sk-local-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` (well-formed scheme, prefix not in DB) → 401; `message="Invalid API key"`. | `client_with_auth` |
| `test_auth_revoked_key_returns_401(client_with_auth, temp_keys_db)` | Mint key, succeed once (200), revoke via `revoke(...)`, retry → 401 with the same `Invalid API key` envelope as unknown-prefix (OWASP: do not distinguish). | `client_with_auth`, `temp_keys_db` |
| `test_auth_hash_mismatch_returns_401(client_with_auth, temp_keys_db)` | Insert a hash that DOES NOT match the plaintext token (replace the row's hash with `_TEST_HASHER.hash("different")`); request with the original plaintext → 401 with `Invalid API key`. Locks the `VerifyMismatchError` branch. | `client_with_auth`, `temp_keys_db` |
| `test_auth_corrupted_hash_returns_401(client_with_auth, temp_keys_db)` | Replace the row's hash with the literal string `"NOT_AN_ARGON2_HASH"`; request → 401. Locks the `InvalidHashError` branch (logged at warning level). | `client_with_auth`, `temp_keys_db` |
| `test_auth_valid_key_returns_200(client_with_auth, auth_headers)` | `client_with_auth.get("/v1/models", headers=auth_headers)` → 200; body has `data` with 4 entries. | `client_with_auth`, `auth_headers` |
| `test_auth_envelope_shape_exact(client_with_auth)` | The 401 body is **exactly** `{"error": {"type": "invalid_request_error", "message": <str>, "param": "Authorization", "code": "invalid_api_key"}}` with NO extra top-level keys. Locks the SDK contract. | `client_with_auth` |
| `test_auth_last_used_at_updated_on_success(client_with_auth, auth_headers, temp_keys_db)` | Before request: `get_row(...).last_used_at is None`. After 200: `get_row(...).last_used_at` is `> 0` and within 5s of now. | `client_with_auth`, `auth_headers`, `temp_keys_db` |
| `test_auth_public_paths_skip_auth(client_with_auth)` | `client_with_auth.get("/healthz")`, `.get("/readyz")`, `.get("/openapi.json")`, `.get("/docs")`, `.get("/redoc")` all return non-401 responses with no Authorization header (200 for the first three, 200 or 308 for `/docs`/`/redoc` depending on FastAPI's redirect behavior). | `client_with_auth` |
| `test_auth_lowercase_bearer_scheme_returns_200(client_with_auth, temp_keys_db)` | `Authorization: bearer <plaintext>` (lowercase) → 200, because `_BEARER_RE` is `re.IGNORECASE`. Regression lock on case-insensitivity. | `client_with_auth`, `temp_keys_db` |

**Marks**: none. None of these need Ollama (the `client_with_auth` lifespan still loads the registry and builds adapters, but `/v1/models` is served from the in-process registry without hitting Ollama).

**Notes**:
- `test_auth_revoked_key_returns_401` writes directly to the DB via `app.auth.revoke` rather than spawning the script (faster, hermetic; `tests/adapters/*` and `tests/test_keys_db.py` already cover the script paths if needed via subprocess).
- `test_auth_hash_mismatch_returns_401` mutates `temp_keys_db` directly via `sqlite3.connect(db_path)` — `_init_db` was already called by the fixture.
- `test_auth_envelope_shape_exact` asserts `set(body["error"].keys()) == {"type", "message", "param", "code"}` and `set(body.keys()) == {"error"}`.
- The `last_used_at` test reads the temp DB AFTER the request returns; `BearerAuthMiddleware.__call__` calls `await asyncio.to_thread(touch, ...)` synchronously on the response path, so by the time TestClient returns, the DB row is guaranteed updated.

### 2.4 `tests/test_readyz.py` — NEW

**Path**: `tests/test_readyz.py`
**Status**: NEW.
**Purpose**: lock `/readyz`'s 200 / 503 / public-no-auth contract.

**Imports**:

```python
import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock
```

**Test inventory**:

| Test function | Marker | Assertion summary | Fixtures |
|---|---|---|---|
| `test_readyz_no_auth_required(client_with_auth)` | none | `client_with_auth.get("/readyz")` (no header) returns 200 OR 503 (not 401). The presence of the Authorization header does not change behavior. Locks `PUBLIC_PATHS` containing `/readyz`. | `client_with_auth` |
| `test_readyz_live_ollama_returns_200(client_with_auth)` | `live` | With Ollama up: 200; body has `status == "ok"` and `backends.ollama.status == "ok"` (and a `models` list); `backends.mlx.status == "error"`; `backends.docker_model_runner.status == "error"`. | `client_with_auth` |
| `test_readyz_all_unreachable_returns_503(client_with_auth, httpx_mock)` | none | Mock Ollama's `GET /api/tags` to raise `httpx.ConnectError("simulated")`. Result: 503; body's `error` is `{"type": "service_unavailable", "message": "No backends reachable", "param": null, "code": "no_backends_reachable"}`; body has a `backends` key with `ollama.status == "unreachable"`, `mlx.status == "error"`, `docker_model_runner.status == "error"`. | `client_with_auth`, `httpx_mock` |
| `test_readyz_envelope_extends_openai_shape(client_with_auth, httpx_mock)` | none | On the 503 path, `body.error` matches the OpenAI envelope shape exactly (4 keys: `type`, `message`, `param`, `code`); `body.backends` is present at the top level alongside `error`. | `client_with_auth`, `httpx_mock` |
| `test_readyz_per_backend_payload_shape(client_with_auth)` | `live` | For each entry in `body.backends`, the payload is a dict with at least a `status` key. For MLX/DMR (which raise `NotSupportedError`), the payload is `{"status": "error", "error": "NotSupportedError: ..."}`. | `client_with_auth` |
| `test_readyz_status_field_is_binary(client_with_auth, httpx_mock)` | none | When all backends are unreachable, `body` has NO top-level `status` field (it's a 503 envelope, not a `{"status": "degraded"}` payload). When at least one backend is up (live test), the body's top-level `status` is exactly `"ok"`. The 200 path NEVER returns `"degraded"`. | `client_with_auth`, `httpx_mock` |

**Marks**: `live` for the two tests that need a real Ollama; the `httpx_mock`-based tests run unconditionally.

**Notes**:
- `httpx_mock.add_exception(httpx.ConnectError("simulated"), url="http://localhost:11434/api/tags", method="GET")` is the canonical pytest-httpx incantation for `OllamaAdapter.health()`'s only HTTP call. Same pattern as `tests/adapters/test_ollama_adapter.py::test_ollama_adapter_health_returns_unreachable_when_offline`.
- The `client_with_auth` fixture is used (NOT plain `client`) for two reasons: (a) auth middleware is mounted and we want to validate it correctly skips `/readyz`; (b) the lifespan still constructs adapters from the same registry, so the test surface is realistic.
- `test_readyz_status_field_is_binary` is the architectural lock for the "binary 200/503, no `degraded`" decision documented in Phase 2 architecture §7.5.

### 2.5 `tests/test_hot_reload.py` — NEW

**Path**: `tests/test_hot_reload.py`
**Status**: NEW.
**Purpose**: assert that mutating `models.yaml` is reflected in `/v1/models` without restart, and that `app.state.registry` object identity changes across the swap.

**Imports**:

```python
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
```

**Test inventory** (all run unconditionally, no live deps; the watcher loads YAML synchronously without hitting Ollama):

| Test function | Assertion summary | Fixtures |
|---|---|---|
| `test_hot_reload_added_model_appears_in_v1_models(client_with_auth, auth_headers, tmp_models_yaml, monkeypatch)` | (1) Set `MODELS_YAML_PATH=str(tmp_models_yaml)` BEFORE the client lifespan starts; (2) hit `/v1/models` and capture the initial 4 ids; (3) read `tmp_models_yaml`, append a new model entry `{id: "hot-reload-canary", backend: "ollama", upstream_model: "llama3.1:8b", capabilities: [chat]}`, write back; (4) poll `/v1/models` (with auth headers) every 100 ms for up to 3 s; (5) assert the new id appears in the response. | `client_with_auth`, `auth_headers`, `tmp_models_yaml`, `monkeypatch` |
| `test_hot_reload_changes_app_state_registry_object_identity(client_with_auth, tmp_models_yaml, monkeypatch)` | Same setup; capture `app.state.registry` reference before mutation (`old_registry = client_with_auth.app.state.registry`); mutate yaml; poll `client_with_auth.app.state.registry is old_registry` for up to 3 s; assert it eventually becomes `False`. Locks the atomic-swap discipline. | `client_with_auth`, `tmp_models_yaml`, `monkeypatch` |
| `test_hot_reload_malformed_yaml_keeps_previous_registry(client_with_auth, auth_headers, tmp_models_yaml, monkeypatch)` | Set up; capture initial models. Write malformed yaml (`tmp_models_yaml.write_text("models: { not valid")`). Wait 1 s. `/v1/models` still returns the original 4 ids (NOT 401, NOT 503, NOT 500). Locks the "watcher errors never bring down the app" contract. | `client_with_auth`, `auth_headers`, `tmp_models_yaml`, `monkeypatch` |
| `test_hot_reload_removed_model_disappears_from_v1_models(client_with_auth, auth_headers, tmp_models_yaml, monkeypatch)` | Setup; rewrite the YAML with only 1 of the 4 entries; poll `/v1/models` for up to 3 s until it shows 1 entry. Assert. | `client_with_auth`, `auth_headers`, `tmp_models_yaml`, `monkeypatch` |

**Marks**: none.

**Critical fixture wiring (load-bearing)**:

```python
# Inside each test, BEFORE constructing the client:
monkeypatch.setenv("MODELS_YAML_PATH", str(tmp_models_yaml))
# Force re-read of settings on next get_settings() call.
from app.config import get_settings
get_settings.cache_clear()
```

This MUST happen before `client_with_auth` triggers `create_app()` — but `client_with_auth` already runs `create_app()` itself. The Builder uses one of two patterns:

**Pattern A (preferred)**: convert `tmp_models_yaml` and the env-set into an autouse fixture that runs BEFORE `client_with_auth`. Implement via fixture ordering — pytest evaluates fixtures left-to-right in a function signature, so put `tmp_models_yaml` BEFORE `client_with_auth` AND make `client_with_auth` indirectly depend on a fixture that consumes `MODELS_YAML_PATH` from the env.

**Pattern B (fallback)**: bypass `client_with_auth` and write a local `client_with_auth_and_yaml` fixture in `tests/test_hot_reload.py` that takes `temp_keys_db`, `tmp_models_yaml`, and `monkeypatch`, sets both env vars, and constructs the app. This is simpler and avoids fixture-ordering subtleties.

**Locked recommendation: Pattern B**. The Builder writes a module-local fixture in `tests/test_hot_reload.py`:

```python
@pytest.fixture
def client_for_hot_reload(
    temp_keys_db: tuple[Path, str, str],
    tmp_models_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    db_path, _plaintext, _prefix = temp_keys_db
    monkeypatch.setenv("KEYS_DB_PATH", str(db_path))
    monkeypatch.setenv("MODELS_YAML_PATH", str(tmp_models_yaml))
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        get_settings.cache_clear()
```

The 4 hot-reload tests use `client_for_hot_reload` instead of `client_with_auth`.

**Polling discipline**:

```python
def _poll_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 3.0,
    interval_s: float = 0.1,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()
```

The 3 s ceiling absorbs `watchfiles`' default 50 ms debounce step plus filesystem scheduling jitter.

**Notes**:
- These tests are NOT flaky on macOS dev hardware (kqueue is fast). On a Linux CI runner with inotify, the same 3 s ceiling holds.
- `test_hot_reload_malformed_yaml_keeps_previous_registry` depends on the `_try_reload`'s exception handling (Phase 2 architecture §8.3) being correct. If a future regression makes the watcher crash on malformed YAML, this test fails.
- The atomic-identity test (`test_hot_reload_changes_app_state_registry_object_identity`) is the one that the development plan §11 risks #1 mitigation calls out explicitly.

### 2.6 `tests/test_logging.py` — NEW

**Path**: `tests/test_logging.py`
**Status**: NEW.
**Purpose**: lock the structured-log contract — `event="request"` with all required fields, the redaction grep, the `event="registry_loaded"` regression.

**Imports**:

```python
import io
import json
import logging as stdlib_logging
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient

from app.logging import _scrub, configure_structlog, redact_authorization
```

**Test inventory** (all run unconditionally — none of these need Ollama; the request-logging middleware emits the `request` event on every HTTP request):

| Test function | Marker | Assertion summary | Fixtures |
|---|---|---|---|
| `test_logging_redact_authorization_key_match()` | none | `redact_authorization(None, "info", {"authorization": "Bearer sk-local-AbCdEf123456"})` returns event_dict whose `authorization` value equals `"<redacted: sk-local-AbC>"`. The literal substring `"sk-local-AbCdEf"` is NOT in the rendered repr. | none |
| `test_logging_redact_authorization_value_match()` | none | `redact_authorization(None, "info", {"some_field": "Bearer sk-local-AbCdEf123456"})` returns event_dict whose `some_field` value equals `"<redacted: sk-local-AbC>"`. | none |
| `test_logging_redact_authorization_case_insensitive_key()` | none | Keys `Authorization`, `AUTHORIZATION`, `authorization`, `aUtHoRiZaTiOn` all trigger the key-match redaction. | none |
| `test_logging_redact_authorization_case_insensitive_scheme()` | none | Values starting with `Bearer `, `bearer `, `BEARER `, `BeArEr ` all trigger the value-match redaction. | none |
| `test_logging_redact_authorization_short_token_falls_back_to_redacted()` | none | `_scrub("Bearer x")` returns `"<redacted>"` (no prefix to preserve when token < `PREFIX_LEN`). | none |
| `test_logging_redact_authorization_non_string_value_under_authorization_key()` | none | `redact_authorization(None, "info", {"authorization": None})` produces `authorization == "<redacted>"`. | none |
| `test_logging_redact_authorization_does_not_walk_nested()` | none | `redact_authorization(None, "info", {"headers": {"Authorization": "Bearer sk-local-..."}})` does NOT recurse — the nested `Authorization` is left untouched. Locks the v0.2.0 top-level-only behavior (Phase 2 architecture §4.4). Documented as a known limitation in the README. | none |
| `test_logging_request_event_emits_required_fields(client_with_auth, auth_headers, captured_log)` | none | After `client_with_auth.get("/v1/models", headers=auth_headers)`, at least one captured event has `event == "request"`; that event has `path`, `method`, `status`, `latency_ms`, `key_prefix`, `model`, `backend`, `stream`, `prompt_tokens`, `completion_tokens` keys (model/backend/stream/usage may be `None` for `/v1/models`). | `client_with_auth`, `auth_headers`, `captured_log` |
| `test_logging_request_event_carries_key_prefix_12_chars(client_with_auth, auth_headers, captured_log, temp_keys_db)` | none | The `request` event's `key_prefix` field equals exactly `temp_keys_db[2]` (the 12-char prefix of the minted token). | as above |
| `test_logging_request_event_for_chat_completions_carries_model_backend(client_with_auth, auth_headers, captured_log, httpx_mock)` | none | Mock Ollama's `POST /v1/chat/completions` to return a fake JSON body. POST `/v1/chat/completions` with `model="ollama-llama3"`. The request event has `model="ollama-llama3"`, `backend="ollama"`, `stream=False`, `status=200`, `latency_ms` ≥ 0. | `client_with_auth`, `auth_headers`, `captured_log`, `httpx_mock` |
| `test_logging_streaming_request_event_emits_null_usage(client_with_auth, auth_headers, captured_log, httpx_mock)` | none | Mock Ollama's streaming endpoint to emit chunks WITHOUT a `usage` block. The captured `request` event has `prompt_tokens` is `None` and `completion_tokens` is `None` (locks the Phase 2 architecture §5.5 punt). | `client_with_auth`, `auth_headers`, `captured_log`, `httpx_mock` |
| `test_logging_no_bearer_substring_in_captured_events(client_with_auth, auth_headers, captured_log)` | none | After issuing requests, iterate every captured event_dict and assert that no string value in any field contains `"Bearer "` followed by anything other than the `<redacted: ...>` form. The grep is performed on `json.dumps(events, default=str)`. | `client_with_auth`, `auth_headers`, `captured_log` |
| `test_logging_deliberate_leak_attempt_redacts(captured_log)` | none | `structlog.get_logger("test").info("leaktest", authorization="Bearer sk-local-LeakAttempt9876", header="Bearer sk-local-Other1234567")` — capture; assert the captured event has both `authorization` and `header` redacted to `<redacted: sk-local-...>`. Locks the redaction defense at the processor layer. | `captured_log` |
| `test_logging_registry_loaded_event_emitted_on_startup(client_with_auth, captured_log)` | none | The list of captured events (after `client_with_auth` lifespan startup) contains an `event == "registry_loaded"` entry with `n_models == 4`, `ids` is a list of 4 strings, `adapters` is a list of 3 strings (sorted). Locks the v0.1.0-startup-line regression per Phase 2 architecture §6.3. | `client_with_auth`, `captured_log` |
| `test_logging_invalid_request_validation_carries_key_prefix(client_with_auth, auth_headers, captured_log)` | none | POST `/v1/chat/completions` with a body MISSING `messages` (triggers 422 from RequestValidationError). The captured `request` event has `status == 422` AND `key_prefix == temp_keys_db[2]` — auth ran first, so `key_prefix` is set even on a validation-rejection path. Locks the install-order invariant from Phase 2 architecture §6. | `client_with_auth`, `auth_headers`, `captured_log` |
| `test_logging_configure_structlog_sets_stdout_handler()` | none | After `configure_structlog("INFO")`, `logging.getLogger().handlers[0].stream is sys.stdout` (Phase 2 architecture §4.7 lock). | none |
| `test_logging_configure_structlog_unknown_level_falls_back_to_info()` | none | `configure_structlog("NOT_A_LEVEL")` does not raise; `logging.getLogger().level == logging.INFO`. | none |
| `test_logging_stdlib_bridge_redacts(caplog)` | none | After `configure_structlog("DEBUG")`, calling `stdlib_logging.getLogger("test").info("x", extra={"authorization": "Bearer sk-local-AAAAAAAAAAAAAAAAAAAA"})` flows through `foreign_pre_chain` and the resulting record's rendered output contains `<redacted:` and NOT `sk-local-AAAA`. Implementation note: this test is the trickiest because `caplog` doesn't pass through structlog's bridge by default. The Builder asserts on the JSON-rendered output captured via a `StringIO` `StreamHandler` attached after `configure_structlog`. | none |

**Marks**: none. None require Ollama. The two `httpx_mock`-using tests are mocked.

**`captured_log` fixture caveats** (Phase 2 architecture §13 / "structlog ReturnLoggerFactory ergonomics" risk):

The fixture replaces structlog's processor chain with a single capture processor. There are TWO consequences the Builder must handle:

1. **Lifespan-startup events are captured ONLY if the fixture runs BEFORE `client_with_auth` enters its TestClient context**. Pytest fixture ordering: `captured_log` and `client_with_auth` are both function-scoped; pytest evaluates them in argument order. The Builder must put `captured_log` **before** `client_with_auth` in the test signature when the test asserts on startup events. Document this in the test file's module docstring.

2. **`configure_structlog` runs in the lifespan AFTER our capture is installed** — and `configure_structlog` calls `structlog.configure(...)` which REPLACES the processor chain. Therefore `captured_log` would be obliterated by the lifespan startup, defeating its purpose.

**Solution (LOCKED)**: the `captured_log` fixture must monkeypatch `app.logging.configure_structlog` to be a no-op (or to install our capture chain) for the duration of the fixture. The fixture body becomes:

```python
@pytest.fixture
def captured_log(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []

    def _capture_processor(
        logger: Any, method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        events.append(dict(event_dict))
        return event_dict

    def _install_capture() -> None:
        structlog.configure(
            processors=[_capture_processor],
            wrapper_class=structlog.make_filtering_bound_logger(0),
            logger_factory=structlog.ReturnLoggerFactory(),
            cache_logger_on_first_use=False,
        )

    saved_config = structlog.get_config()
    _install_capture()

    # Prevent the lifespan-time configure_structlog() from clobbering us.
    import app.logging as _logging_mod
    monkeypatch.setattr(_logging_mod, "configure_structlog", lambda level: _install_capture())

    try:
        yield events
    finally:
        structlog.configure(**saved_config)
```

The Builder verifies this dance works by asserting `event="registry_loaded"` is captured (the `test_logging_registry_loaded_event_emitted_on_startup` test). If the dance is incorrect, that test fails.

### 2.7 `tests/test_chat.py`, `test_chat_streaming.py`, `test_embeddings.py`, `test_models_endpoint.py` — MIGRATIONS

See §5 for the per-file diff. Summary: replace the `client` fixture with `client_with_auth` and add `headers=auth_headers` (or equivalent) to every request.

---

## 3. Test Fixture Design (consolidated)

| Fixture | Defined in | Scope | Yields | Teardown |
|---|---|---|---|---|
| `ollama_alive` | `tests/conftest.py` (existing) | session | `bool` | none |
| `app_factory` | `tests/conftest.py` (existing) | function | `create_app` callable | none |
| `client` | `tests/conftest.py` (existing) | function | `TestClient` (no auth) | TestClient context exit |
| `registry` | `tests/conftest.py` (existing) | function | `Registry` | implicit |
| **`temp_keys_db`** | `tests/conftest.py` (NEW) | function | `(Path, str, str)` | implicit (tmp_path) |
| **`client_with_auth`** | `tests/conftest.py` (NEW) | function | `TestClient` + auth wired to temp DB | TestClient exit + `get_settings.cache_clear()` |
| **`auth_headers`** | `tests/conftest.py` (NEW) | function | `dict[str, str]` | none |
| **`captured_log`** | `tests/conftest.py` (NEW) | function | `list[dict[str, Any]]` (mutated by structlog processor) | restore prior `structlog` config |
| **`tmp_models_yaml`** | `tests/conftest.py` (NEW) | function | `Path` to a temp copy of `config/models.yaml` | implicit |
| `client_for_hot_reload` | `tests/test_hot_reload.py` (NEW, module-local) | function | `TestClient` with both `KEYS_DB_PATH` and `MODELS_YAML_PATH` overridden | TestClient exit + cache clear |
| `httpx_mock` | from `pytest-httpx` (existing) | function | `HTTPXMock` | implicit |
| `caplog` | from pytest (built-in) | function | `LogCaptureFixture` | implicit |
| `tmp_path`, `monkeypatch` | from pytest (built-in) | function | as documented | implicit |

**Fixture ordering rules** (pytest evaluates in left-to-right argument order):

- `temp_keys_db` MUST be evaluated before `client_with_auth` (the latter depends on it).
- `tmp_models_yaml` MUST be evaluated before `client_for_hot_reload`.
- `captured_log` MUST be evaluated before `client_with_auth` when the test asserts on lifespan startup events.

**Argon2 fast-params performance budget**:

- `_TEST_HASHER.hash(plaintext)` ≈ 1 ms (vs ~50 ms for default params).
- `_HASHER.verify(...)` in `app.auth` (uses default-param `PasswordHasher()`) reads parameters from the encoded hash string and runs at the cost specified IN the hash. Therefore each `client_with_auth.get(...)` request through the auth middleware costs ~1 ms verify, not ~50 ms. The full auth-test suite (~14 tests, ~3 requests each) costs <100 ms of crypto, well within budget.

**Hash-mismatch gotcha**: `_TEST_HASHER` and the runtime `_HASHER` are different `PasswordHasher()` instances with different parameters. Argon2id stores parameters in the hash string; `verify()` uses the parameters from the hash, not from the verifier instance. Therefore tests work — the verify cost matches the hash cost. The Builder writes the hash via `_TEST_HASHER` and verifies via the runtime `_HASHER`; both succeed.

---

## 4. Test Case Enumeration (consolidated)

Total NEW test functions: **62** across 5 new files.

| File | Test count | Live? | Mocked? |
|---|---:|---|---|
| `tests/test_keys_db.py` | 12 | 0 | 0 (tmp_path only) |
| `tests/test_auth.py` | 13 | 0 | 0 |
| `tests/test_readyz.py` | 6 | 2 | 2 (httpx_mock) |
| `tests/test_hot_reload.py` | 4 | 0 | 0 |
| `tests/test_logging.py` | 17 | 0 | 2 (httpx_mock) |
| **subtotal NEW** | **52** | **2** | **4** |

Plus migrated v0.1.0 tests (auth-header threaded through; same count, same assertions).

| Migrated file | Tests touched | Behavior change |
|---|---:|---|
| `tests/test_chat.py` | 8 (all) | header added; one `no_auth_required` removed (irrelevant under v0.2.0) |
| `tests/test_chat_streaming.py` | 6 (all) | header added |
| `tests/test_embeddings.py` | 6 (all) | header added |
| `tests/test_models_endpoint.py` | 6 (4 kept, 2 rewritten) | the two "no auth required" tests are converted into negative-auth assertions OR deleted |

`tests/test_registry.py` (16 tests) and `tests/adapters/*` (19 tests) are UNCHANGED.

**Grand total tests after Phase 3**: ~93 tests (~50 existing + ~43 net new across Phase 3 additions and migrations). Live tests: ~13 (existing 11 + 2 new in test_readyz.py).

---

## 5. Existing Test Migrations

### 5.1 `tests/test_chat.py`

**Replace fixture**: `client` → `client_with_auth`. **Add header param**: every `client.post(...)` becomes `client_with_auth.post(..., headers=auth_headers)`. **Delete tests**: none. **No assertion changes** (HTTP status codes, envelope shapes, error codes are all unchanged).

Per-test diff:

| Test | Change |
|---|---|
| `test_chat_non_stream_live_ollama` | Signature: `(client_with_auth: TestClient, auth_headers: dict[str, str])`; `client.post(...)` → `client_with_auth.post("/v1/chat/completions", json=..., headers=auth_headers)`. |
| `test_chat_unknown_model_returns_404` | Same. |
| `test_chat_capability_gate_no_chat_returns_501` | Same. |
| `test_chat_tools_gate_chat_only_model_returns_400` | Same. |
| `test_chat_capability_first_ordering_no_chat_with_tools_returns_501` | Same. |
| `test_chat_stub_backend_mlx_returns_501` | Same. |
| `test_chat_stub_backend_dmr_returns_501` | Same. |
| `test_chat_invalid_body_returns_422` | Same — auth runs first, then validation; the 422 envelope is unchanged. |

### 5.2 `tests/test_chat_streaming.py`

Same migration. Each `client.stream("POST", ...)` and `client.post(...)` adds `headers=auth_headers`. The existing `_TOOLS_PAYLOAD` and `_collect_sse_lines` helpers are untouched.

### 5.3 `tests/test_embeddings.py`

Same migration; 6 tests, each gets `client_with_auth` + `auth_headers`.

### 5.4 `tests/test_models_endpoint.py`

This file has TWO tests that were forward-looking auth regression locks:

- `test_get_v1_models_no_auth_required` — under v0.2.0 this MUST become 401, not 200.
- `test_get_v1_models_with_random_authorization_header_still_200` — under v0.2.0 a random Bearer token returns 401.

**Locked rewrite plan**:

| Old test | New test | Notes |
|---|---|---|
| `test_get_v1_models_returns_200` | `test_get_v1_models_returns_200(client_with_auth, auth_headers)` | Add header. |
| `test_get_v1_models_response_shape` | same migration | |
| `test_get_v1_models_lists_all_four_registry_entries` | same migration | |
| `test_get_v1_models_each_entry_shape` | same migration | |
| `test_get_v1_models_no_auth_required` | **DELETE** and replace with `test_get_v1_models_without_auth_returns_401(client_with_auth)` | Asserts 401 + envelope on a header-less call. Belongs more naturally in `tests/test_auth.py` — the Builder may move it there instead, but a duplicate in this file is fine. |
| `test_get_v1_models_with_random_authorization_header_still_200` | **DELETE** and replace with `test_get_v1_models_with_random_bearer_returns_401(client_with_auth)` | Assert 401. |

The `_LOCKED_IDS` constant and the four happy-path tests are unchanged in behavior. The two new auth-regression tests duplicate coverage from `tests/test_auth.py` but anchor the lock at the `models_endpoint` site.

---

## 6. mypy `--strict` Cleanup Plan

### 6.1 Source of the carryover count

The 27 findings come from v0.1.0 Phase 3, where the architect explicitly deferred mypy cleanup to v0.2.0 Phase 3 (v0.1.0 phase-3-architecture.md §11 row 2). The list itself was never enumerated by file:line in v0.1.0 docs — only the count (27) was recorded. The Phase 2 +1 finding referenced in `pm/project-state.md:46` is also unspecified.

**This Architect therefore enumerates the LIKELY findings by inspecting the post-Phase-2 source tree** (read-only, no `mypy` invocation). The enumeration below is a best-effort inventory; the QA pass will produce the authoritative list. The Builder fixes the items below; if QA's strict run surfaces additional findings, they are mechanical applications of the same patterns.

### 6.2 Strict-mode rules in effect

`mypy --strict` enables: `--warn-unused-configs`, `--disallow-untyped-defs`, `--disallow-untyped-calls`, `--disallow-untyped-decorators`, `--check-untyped-defs`, `--no-implicit-optional`, `--warn-redundant-casts`, `--warn-unused-ignores`, `--warn-return-any`, `--no-implicit-reexport`, `--strict-equality`. The relevant ones for this codebase:

- `disallow-untyped-defs` — every function/method must be fully annotated.
- `warn-return-any` — returning `Any` from a function annotated to return a non-`Any` type fires.
- `disallow-untyped-decorators` — `@app.get(...)` etc. fires if the decorator is `Any`-typed (FastAPI's decorators are typed; this should be quiet, but `@asynccontextmanager` from `contextlib` IS typed in stdlib, so OK).
- `no-implicit-reexport` — submodule imports without `from x import y as y` may need `__all__`.

### 6.3 Inventory of likely findings (by file)

#### `app/main.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 22 | `async def lifespan(app: FastAPI):` — return annotation missing on an async generator function | `disallow-untyped-defs` | `async def lifespan(app: FastAPI) -> AsyncIterator[None]:` (import `AsyncIterator` from `collections.abc`). |
| 22 | `@asynccontextmanager` over an unannotated function may cascade; once the inner function is annotated, the decorator wrap typing should flow. | `disallow-untyped-decorators` (resolved by fix above) | n/a |
| 33-38 | `log.info("registry_loaded", n_models=..., ids=registry.ids(), adapters=sorted(adapters.keys()))` — `structlog`'s `BindableLogger.info` is overload-typed; should be clean. | n/a | n/a |
| 41 | `watcher_task: asyncio.Task[None] = start_registry_watcher(app, settings)` — annotation present; should be clean. | n/a | n/a |

#### `app/auth.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 174 | `headers: list[tuple[bytes, bytes]] = scope.get("headers", [])` — `scope` is a `Scope` (TypedDict-like Mapping); `scope.get(...)` returns `Any`. Assigning to `list[tuple[bytes, bytes]]` triggers no error directly, but the iteration may return Any. | `warn-return-any` (latent) | Use `cast(list[tuple[bytes, bytes]], scope.get("headers", []))` from `typing.cast`. Alternatively, `Iterable[tuple[bytes, bytes]]` — but `cast` is the cleaner local fix. |
| 227 | `scope.setdefault("state", {})["key_prefix"] = prefix` — `scope.setdefault` on a TypedDict-like `Scope` returns `Any` and accepts `Any`. | `warn-return-any` if assigned, `disallow-untyped-calls` if Scope is annotated. | The `Scope` type from starlette is `MutableMapping[str, Any]`-ish; this works at runtime. To satisfy strict, wrap: `state = cast(dict[str, Any], scope.setdefault("state", {})); state["key_prefix"] = prefix`. |
| 162 | `scope["type"] != "http"` — `Scope` value access is `Any`; the comparison is fine but mypy may flag the `!=` as `[comparison-overlap]` under `strict-equality` (rare). | `strict-equality` | Likely clean; if not, `cast(str, scope["type"]) != "http"`. |
| 240 | `content: dict[str, Any] = make_error(...)` — annotation present; should be clean. | n/a | n/a |

#### `app/middleware_logging.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 73-74 | `def _extract_streaming_usage(last_chunk: bytes) -> dict[str, int | None]:` — annotated; clean. | n/a | n/a |
| 169 | `path: str = scope["path"]` — `scope["path"]` is `Any` (TypedDict-Like Scope returns Any for indexing). | `warn-return-any` | `path = cast(str, scope["path"])` OR `path: str = str(scope["path"])`. |
| 170 | `method: str = scope.get("method", "?")` — `scope.get(...)` returns `Any`. | `warn-return-any` | `method = cast(str, scope.get("method", "?"))`. |
| 138 | `app = scope.get("app")` — returns `Any`; flows into `getattr(app.state, "registry", None)` → no annotation, mypy may infer `Any`. | `warn-return-any` | Annotate the helper: `def _resolve_backend(scope: Scope, model: str | None) -> str | None:` already done. The body: `app = cast("FastAPI | None", scope.get("app"))` if `from typing import TYPE_CHECKING` + `from fastapi import FastAPI`. To avoid runtime import overhead inside the hot path, use a string forward reference and a TYPE_CHECKING guard at the top. |
| 141 | `registry = getattr(app.state, "registry", None)` — `getattr` with a default returns `Any`. | `warn-return-any` | `registry = cast("Registry | None", getattr(app.state, "registry", None))`. |
| 144 | `found = registry.get(model)` — Any chain. | resolved by fix above | n/a |
| 145 | `return found.backend if found is not None else None` — once `found` is `Model | None`, this is clean. | resolved | n/a |
| 178-247 | Nested `async def replay_receive`, `async def abandon_receive` — annotations: each must declare `-> Message`. The Builder confirms each helper has explicit return annotations. | `disallow-untyped-defs` | All inner async helpers → `async def replay_receive() -> Message:` (already correct). |
| 252-275 | `async def capturing_send(message: Message) -> None:` — annotated; clean. | n/a | n/a |
| 281 | `key_prefix = scope.get("state", {}).get("key_prefix")` — chained `Any`. | `warn-return-any` | `state_dict = cast(dict[str, Any], scope.get("state") or {}); key_prefix = state_dict.get("key_prefix")`. The result type is then `Any | None`; annotate `key_prefix: str | None = cast(str | None, state_dict.get("key_prefix"))`. |
| 222 | `wrapped_receive = abandon_receive` — local rebinding of `Callable[[], Awaitable[Message]]` between two branches. mypy may flag this as "redefining variable with another type" in some versions; safe fix: type the variable at the top: `wrapped_receive: Callable[[], Awaitable[Message]] = receive`. | `assignment` | as noted; add `from typing import Awaitable, Callable` and the explicit annotation. |

#### `app/logging.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 22-29 | `_scrub` returns `str` — annotated; clean. | n/a | n/a |
| 32-75 | `redact_authorization` — `MutableMapping[str, Any]` annotated. structlog's `Processor` type accepts a `(WrappedLogger, str, EventDict) -> EventDict` callable. `EventDict` IS `MutableMapping[str, Any]`. Clean. | n/a | n/a |
| 90-92 | `level_int: int = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)` — `getLevelNamesMapping` is typed `Mapping[str, int]`; `.get(...)` returns `int | None`. The default `logging.INFO` (int) makes the return type `int` after the `.get` overload — should be clean. | n/a | n/a |
| 102-110 | `structlog.configure(processors=[...], ...)` — third-party signatures; `structlog`'s type stubs as of 24.4 should be clean. If mypy complains about `processors=[...]` being inferred as `list[object]` due to mixed Processor types, annotate `shared_processors: list[Processor]` (already done). | n/a | n/a |
| 113-119 | `formatter = structlog.stdlib.ProcessorFormatter(...)` — should be clean. | n/a | n/a |
| 121 | `handler = logging.StreamHandler(sys.stdout)` — `StreamHandler` is generic-typed in stdlib stubs. May need `logging.StreamHandler[TextIO]` on strict. | possibly `type-arg` | `handler: logging.StreamHandler[Any] = logging.StreamHandler(sys.stdout)` OR leave it; `--strict` only requires generic params on USER-DEFINED generics by default. Likely clean. |

#### `app/registry_watcher.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 17-33 | `def start_registry_watcher(app, settings) -> "asyncio.Task[None]":` — annotations are quoted (forward refs). Clean. | n/a | n/a |
| 36-66 | `async def _watch_loop(app, settings) -> None:` — clean. | n/a | n/a |
| 110 | `app.state.registry = new_registry` — `app.state` is `starlette.datastructures.State`, indexing/attribute access is `Any`. | `warn-return-any` (latent in `app.state.registry`) | `cast` the read site: `old_registry = cast(Registry, app.state.registry)`. Add `from app.registry import Registry` (NOT under TYPE_CHECKING since we use it in a runtime cast — actually `cast` doesn't evaluate at runtime, so `TYPE_CHECKING` import is fine). |
| 112 | `if new_registry.ids() == old_registry.ids():` — once cast, this is clean. | resolved | n/a |

#### `app/registry.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 42 | `def __iter__(self):` — missing return annotation. | `disallow-untyped-defs` | `def __iter__(self) -> Iterator[Model]:` (import `Iterator` from `collections.abc`). |
| 65-66 | `with path.open("r", encoding="utf-8") as fh: raw = yaml.safe_load(fh)` — `yaml.safe_load` returns `Any` (PyYAML's stub). Assigning `raw = ...` — clean (`Any` is acceptable here; subsequent isinstance narrowing handles it). | n/a | n/a |
| 68-145 | Subsequent narrowing via `isinstance(...)` checks — the parser already does explicit type checks; mypy narrows correctly. Clean. | n/a | n/a |
| 130-134 | `raise RegistryError(...)` inside the except block lacks `from exc` — not a strict finding, just a code-style nit; leave alone. | n/a | n/a |

#### `app/errors.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 138-153 | `install_exception_handlers` has FOUR `# type: ignore[arg-type]` lines (existing from v0.1.0 Phase 1). Under `--warn-unused-ignores` strict mode, these are now **load-bearing** — Starlette's `add_exception_handler(exc_type, handler)` signature changed in 0.50+ to accept `Callable[[Request, Exception], JSONResponse]` more loosely. | possibly `unused-ignore` if Starlette pinned `<1.0` resolved to 0.52.x with the typed signature | The Builder runs `mypy --strict app/errors.py` and:<br/>(a) if the four `# type: ignore[arg-type]` lines are now flagged as unused → REMOVE them; <br/>(b) if they remain necessary → leave as-is, the `[arg-type]` code is specific enough that `--warn-unused-ignores` accepts it.<br/>The development plan §8 step 3.8 explicitly permits these `# type: ignore` lines to stay if Starlette's signatures still don't admit the union. The Builder reports the outcome in the merge note. |
| 24-32 | `make_error` — annotated; clean. | n/a | n/a |
| 96-116 | `validation_exception_handler` — `errors = exc.errors()` returns `list[dict[str, Any]]`. Subsequent `errors[0].get("loc", ())` returns `Any`. | `warn-return-any` (latent) | `loc = cast(tuple[Any, ...], errors[0].get("loc", ()))`. The `.join(str(p) for p in loc)` is then clean. |

#### `app/config.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 1-44 | `BaseSettings` subclass with `Field(...)` — pydantic-settings is fully typed. Clean. | n/a | n/a |
| 41 | `@lru_cache(maxsize=1)` over `def get_settings() -> Settings:` — `functools.lru_cache` is generic-typed. Clean. | n/a | n/a |

#### `app/adapters/base.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 41-43 | `body: dict, stream: bool` and `-> dict | AsyncIterator[bytes]` — `dict` without type parameters! | `type-arg` | Replace every `body: dict` with `body: dict[str, Any]` and every `-> dict` with `-> dict[str, Any]`. Same for `health() -> dict` and `embeddings(self, body: dict) -> dict`. Add `from typing import Any` to the imports. **5 occurrences in this file.** |
| 1-25 | `class NotSupportedError(Exception)` — `__init__` annotated; clean. | n/a | n/a |
| 27-70 | `class BackendAdapter(ABC)` — methods annotated except for the parametrize-less `dict`. | resolved by parametrizing | n/a |

#### `app/adapters/ollama.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 39, 41, 43, 51, 69, 76 | `body: dict` and `-> dict` un-parametrized — same pattern, **6 occurrences.** | `type-arg` | Same fix: `body: dict[str, Any]` and `-> dict[str, Any]`. |
| 47 | `return r.json()` — `httpx.Response.json()` returns `Any`. The function is annotated `-> dict[str, Any]` (after fix above), so this triggers `warn-return-any`. | `warn-return-any` | `return cast(dict[str, Any], r.json())`. |
| 80-86 | `payload = r.json() or {}` — same Any flow. | `warn-return-any` | `payload = cast(dict[str, Any], r.json() or {})`. |
| 81-85 | `names = [m.get("name") for m in payload.get("models", []) if isinstance(m, dict)]` — type is `list[Any]`. Function returns `dict[str, Any]`, so this is fine. | n/a | n/a |
| 50-67 | `async def _stream(self, body: dict) -> AsyncGenerator[bytes, None]:` — `body: dict` without params; same fix. | `type-arg` | parametrize. |

#### `app/adapters/mlx.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 18-40 | Six `dict` un-parametrized. | `type-arg` | parametrize. |

#### `app/adapters/docker_model_runner.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 21-43 | Six `dict` un-parametrized. | `type-arg` | parametrize. |

#### `app/adapters/__init__.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 1-58 | Already typed; `__all__` includes the public surface. | n/a | n/a |
| 33-37 | `if m.backend not in _ADAPTER_CLASSES: raise ValueError(...)` — `m.backend` is `str`; comparison is clean. | n/a | n/a |

#### `app/routers/health.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 12-14 | `def _short_error(exc: BaseException) -> str:` — annotated; clean. | n/a | n/a |
| 18-20 | `async def healthz() -> dict[str, str]:` — annotated; clean. | n/a | n/a |
| 23-83 | `async def readyz(request: Request) -> JSONResponse:` — annotated. The `adapters` and `results` locals are typed via inference. `result = await asyncio.gather(...)` returns `tuple[Any, ...]` because `return_exceptions=True` removes type info. | `warn-return-any` (probably not — `gather` overloads return `list[T | BaseException]`) | Annotate: `results: list[dict[str, Any] \| BaseException] = await asyncio.gather(...)` — but `asyncio.gather` with heterogeneous awaitables typically returns `list[Any]` to mypy. Cast: `results = cast(list[dict[str, Any] \| BaseException], await asyncio.gather(...))`. |
| 37-43 | `adapters = request.app.state.adapters` — `Any` chain. | `warn-return-any` | `adapters = cast(dict[str, BackendAdapter], request.app.state.adapters)` (import `BackendAdapter`). |

#### `app/routers/models.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 17 | `registry = request.app.state.registry` — `Any` chain. | `warn-return-any` | `registry = cast(Registry, request.app.state.registry)`. |

#### `app/routers/chat.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 9-13 | `SSE_HEADERS: dict[str, str]` — annotated; clean. | n/a | n/a |
| 25-89 | `async def chat_completions(body: ChatCompletionRequest, request: Request) -> dict | StreamingResponse:` — `dict` un-parametrized. | `type-arg` | `dict[str, Any] \| StreamingResponse`. |
| 29 | `registry = request.app.state.registry` — `Any`. | `warn-return-any` | `registry = cast(Registry, request.app.state.registry)`. |
| 30-32 | `adapters: dict[str, BackendAdapter] = (request.app.state.adapters)` — already annotated; mypy may still flag the cast-from-Any — but since the variable annotation forces narrowing, this is usually accepted. | n/a | n/a |
| 72-74 | `forwarded = body.model_dump(exclude_none=True, by_alias=True)` — `BaseModel.model_dump()` returns `dict[str, Any]`. Clean. | n/a | n/a |
| 79-81 | `result = await adapter.chat_completions(forwarded, stream=body.stream)` — return type `dict[str, Any] \| AsyncIterator[bytes]`; downstream `if body.stream: return StreamingResponse(result, ...)` and `return result` — the `else` branch returns a `dict[str, Any] \| AsyncIterator[bytes]` while annotated to return `dict[str, Any] \| StreamingResponse`. Type narrowing not automatic. | `return-value` | After parametrization, narrow with an isinstance or split:<br/>`if body.stream: return StreamingResponse(result, ...)` — but mypy will complain `StreamingResponse(result, ...)` requires `result` to be an iterator; cast: `return StreamingResponse(cast(AsyncIterator[bytes], result), ...)`. The else-return: `return cast(dict[str, Any], result)`. |

#### `app/routers/embeddings.py`

| Line | Likely finding | Rule | Proposed fix |
|---|---|---|---|
| 15-18 | `async def embeddings(body: EmbeddingsRequest, request: Request) -> dict:` — `dict` un-parametrized. | `type-arg` | `-> dict[str, Any]`. |
| 19-21 | Same `app.state` Any chain as in chat.py. | resolved by cast | as above. |
| 47-52 | `forwarded = body.model_dump(...)`; `forwarded["model"] = model.upstream_model`; `return await adapter.embeddings(forwarded)` — clean once return type is parametrized; `r.json()` Any flows from the adapter. | `warn-return-any` | The adapter method already returns `dict[str, Any]` after the adapter fix. Clean. |

#### `app/schemas.py`

Already fully typed (Pydantic v2 + Literal + `_OpenAIModel`). Clean.

### 6.4 Inventory summary

| File | Likely findings |
|---|---:|
| `app/main.py` | 1 (lifespan return type) |
| `app/auth.py` | 2 (scope cast + setdefault cast) |
| `app/middleware_logging.py` | ~5 (scope casts, wrapped_receive annotation) |
| `app/logging.py` | 0-1 (StreamHandler generic, may already be clean) |
| `app/registry_watcher.py` | 1 (registry cast) |
| `app/registry.py` | 1 (`__iter__` return type) |
| `app/errors.py` | 0-4 (the four `type: ignore` lines may need removal under `--warn-unused-ignores`; `errors()` cast) |
| `app/adapters/base.py` | 5 (`dict` parametrization) |
| `app/adapters/ollama.py` | 8 (`dict` parametrization × 6, `r.json()` cast × 2) |
| `app/adapters/mlx.py` | 6 (`dict`) |
| `app/adapters/docker_model_runner.py` | 6 (`dict`) |
| `app/adapters/__init__.py` | 0 |
| `app/routers/health.py` | 2 (asyncio.gather cast, app.state cast) |
| `app/routers/models.py` | 1 (registry cast) |
| `app/routers/chat.py` | 3 (dict param, registry cast, return-narrowing cast) |
| `app/routers/embeddings.py` | 2 (dict param, registry cast) |
| `app/config.py` | 0 |
| `app/schemas.py` | 0 |
| **Total likely** | **~43 (the v0.1.0 baseline of 27 + Phase 2's +1 + the new auth/logging/middleware/watcher findings)** |

The "27 carryover" figure was for v0.1.0 source only. Phases 1+2 added new code in `app/auth.py`, `app/logging.py`, `app/middleware_logging.py`, `app/registry_watcher.py`, and `app/main.py` (lifespan rewrite). The likely-new findings concentrate in `app/middleware_logging.py` (Scope/state Any chains, ~5) and `app/auth.py` (~2). The Phase 1+2 architects already wrote most of those modules with strict-mode discipline (PEP 604 unions, parametrized generics), so the net new finding count is small (~7-10), keeping the total close to the documented 27+1.

### 6.5 Locked fix patterns

| Pattern | Tool | Example |
|---|---|---|
| `dict` → `dict[str, Any]` | mechanical | `body: dict` → `body: dict[str, Any]`. |
| `r.json()` returns `Any` → `dict[str, Any]` | `cast` | `return cast(dict[str, Any], r.json())`. |
| `scope[X]` returns `Any` → narrow type | `cast` | `path = cast(str, scope["path"])`. |
| `scope.get(X, default)` returns `Any` | `cast` | `method = cast(str, scope.get("method", "?"))`. |
| `app.state.X` returns `Any` | `cast` with TYPE_CHECKING import | `cast("Registry", app.state.registry)`. |
| `__iter__` no return type | annotate | `def __iter__(self) -> Iterator[Model]:`. |
| `async def lifespan(app):` no return type | annotate | `async def lifespan(app: FastAPI) -> AsyncIterator[None]:`. |
| `errors()` returns `list[dict[str, Any]]` but `.get("loc", ())` returns `Any` | `cast` | `loc = cast(tuple[Any, ...], errors[0].get("loc", ()))`. |
| Variable rebound between branches with different inferred types | explicit annotation at first bind | `wrapped_receive: Callable[[], Awaitable[Message]] = receive`. |
| `list[T]` for asyncio.gather with `return_exceptions=True` | `cast` | `cast(list[dict[str, Any] \| BaseException], await asyncio.gather(...))`. |

**Locked rule (development plan §8 step 3.8)**: NO `# type: ignore` additions for any of the above. Real fixes only. The four pre-existing `# type: ignore[arg-type]` lines in `app/errors.py:install_exception_handlers` MAY stay if Starlette's signature still mandates them; the Builder verifies by deleting one, running QA mypy, and observing the result.

### 6.6 Follow-up risk

After parametrizing every `dict` in the adapters and routers, **chained type checking** may surface follow-on findings. Example: once `OllamaAdapter.chat_completions` returns `dict[str, Any] | AsyncIterator[bytes]`, the chat router's narrowing logic needs an explicit cast in each branch (§ chat.py findings above). The Builder applies fixes file-by-file; QA's strict run produces the final clean signal.

---

## 7. README v0.2.0 Rewrite Outline

**Path**: `README.md`
**Status**: REWRITE.
**Strategy**: section-by-section preservation/replacement. Preserve the WIP banner, the architecture diagram embed, the CC-BY-NC-SA-4.0 license footer, and the existing reference-docs links verbatim.

### 7.1 Section diff vs current README

| Section | Action | Notes |
|---|---|---|
| WIP banner | PRESERVE verbatim | line 3 of current README |
| One-line description | UPDATE | "v0.1.0 was the first vertical slice (Ollama wired). v0.2.0 adds API-key authentication, structured JSON logging, `/readyz` per-backend health, and hot-reload of `models.yaml`." |
| `## What's in v0.1.0` | RENAME to `## What's in v0.2.0` | Replace bullets with the v0.2.0 set (see §7.2). |
| `## What's deferred` | UPDATE | Move "API-key auth", "structlog", "watchfiles hot-reload" out (now in v0.2.0); keep Caddy/TLS/Compose (v0.3.0), real MLX/DMR (v0.4.0+). |
| `## Architecture` | PRESERVE diagram embed; UPDATE prose | "v0.2.0 adds an ASGI auth middleware, structlog JSON logging with Authorization redaction, a watchfiles-driven registry hot-reloader, and a `/readyz` endpoint that fans out over `app.state.adapters[*].health()`." |
| `## Prerequisites` | PRESERVE | unchanged from v0.1.0 |
| `## Quick start` | UPDATE | Add a `## Mint a key` step. See §7.3. |
| `## Auth: minting and using API keys` | NEW | Full new section. See §7.4. |
| `## Using the OpenAI Python SDK` | UPDATE | Replace `api_key="not-used-yet"` with `api_key="sk-local-..."` and a comment about exporting it. |
| `## Configuration: config/models.yaml` | PRESERVE + ADD note | Same yaml example; add a one-paragraph note about hot-reload. |
| `## /readyz` | NEW | Sample 200 + 503 payloads; the `code=no_backends_reachable` envelope. |
| `## Hot-reload of models.yaml` | NEW | Atomic-swap behavior; the v0.2.0 limitation (adding a NEW backend requires restart). |
| `## Tests` | PRESERVE + ADD note | Add: "tests for auth use a temp SQLite key store; live tests still need `ollama serve`." |
| `## Demo notebook` | UPDATE | Add the two new notebooks: `posts/v0_2_0_auth_demo.ipynb` (Phase 1) and `posts/v0_2_0_readyz_hotreload_demo.ipynb` (Phase 2). |
| `## Version roadmap` | UPDATE | Bump v0.2.0 entry from "deferred" to "delivered". See §7.5. |
| `## Documentation` | PRESERVE | unchanged. |
| `## Reference docs` | PRESERVE | unchanged. |
| `## License` | PRESERVE verbatim | unchanged. |

### 7.2 New `## What's in v0.2.0` content

```markdown
## What's in v0.2.0

- **API-key authentication** — every `/v1/*` route now requires `Authorization: Bearer sk-local-...`. `/healthz`, `/readyz`, `/docs`, `/openapi.json`, `/redoc` remain public.
- **Argon2id key store** — keys are stored in SQLite at `KEYS_DB_PATH` (default `./data/keys.db`); plaintexts are never persisted.
- **Mint and revoke CLIs** — `python scripts/generate_api_key.py --name <name>` prints the key once; `python scripts/revoke_api_key.py --prefix <prefix>` revokes it.
- **Structured JSON logging** — every gateway log line on stdout is parseable JSON with `ts`, `level`, `event`, and per-request fields (`key_prefix`, `model`, `backend`, `stream`, `status`, `latency_ms`, `prompt_tokens`, `completion_tokens` when available).
- **`Authorization` redaction** — global redaction processor scrubs any value starting with `Bearer ` to `<redacted: sk-local-XXX>`; full tokens never appear in logs.
- **`/readyz` per-backend health** — public endpoint that fans out over every backend's `health()`; 200 if any backend reachable, 503 with the OpenAI envelope only if every backend is down.
- **Hot-reload of `config/models.yaml`** — `watchfiles` watches the file; on save, the registry is atomically swapped without restarting the gateway.
- Everything from v0.1.0 (Ollama wired, MLX + DMR stubbed at 501, capability/tools gating) is unchanged on the wire.
```

### 7.3 New Quick start step (after `uv sync`)

```bash
# Mint your first API key (the plaintext is printed once — save it).
mkdir -p data
KEY=$(uv run python scripts/generate_api_key.py --name claude-code | grep -oE 'sk-local-[A-Za-z0-9_-]+')
echo "Save this key: $KEY"

# Start the gateway.
uv run uvicorn app.main:app --port 8000
```

### 7.4 New `## Auth: minting and using API keys` section

Sub-headings:

- **The key format** — `sk-local-` + 32 URL-safe random bytes; 12-char prefix is the SQL primary key.
- **Minting** — `python scripts/generate_api_key.py --name <human-readable-label>`; prints the plaintext exactly once to stdout; the script also stores the Argon2id hash and the prefix.
- **Using the key** — `Authorization: Bearer sk-local-...` on every `/v1/*` request; the `openai` SDK accepts it as `api_key="sk-local-..."`.
- **The 401 envelope** — exact JSON shape:
  ```json
  {"error": {"type": "invalid_request_error", "message": "Invalid API key", "param": "Authorization", "code": "invalid_api_key"}}
  ```
- **Revocation** — `python scripts/revoke_api_key.py --prefix <prefix>` (the 12-char prefix, NOT the full token).
- **Where keys are stored** — `KEYS_DB_PATH` (default `./data/keys.db`); gitignored; SQLite, no separate service.
- **Last-used tracking** — every successful request updates `last_used_at` in the row.
- **What's NOT done** — per-key rate limits, quotas, expiry; deferred to post-v1.

### 7.5 Updated Version roadmap

```markdown
## Version roadmap

| Version | Status | Scope |
|---|---|---|
| v0.1.0 | shipped (2026-06-18) | Ollama wired; MLX + DMR stubbed at 501; no auth; host process. |
| **v0.2.0** | **shipped (2026-06-19)** | **API-key auth (Argon2id + SQLite); structlog JSON logging; watchfiles hot-reload; `/readyz`.** |
| v0.3.0 | planned | Caddy + TLS + LAN binding; `compose.yaml`; `Dockerfile.gateway`. |
| v0.4.0+ | planned | Real MLX adapter; real Docker Model Runner adapter; `Makefile` for host-backend lifecycle. |
```

### 7.6 New `## /readyz` section

Sub-content:

- **What it does** — fans out over every adapter's `health()` concurrently.
- **200 response (any backend up)** — embed a sample payload (Ollama up, MLX/DMR error).
- **503 response (all backends down)** — embed the OpenAI envelope + `backends` map.
- **Public, no auth required** — same allowlist as `/healthz`.

### 7.7 New `## Hot-reload of models.yaml` section

Sub-content:

- **What it does** — `watchfiles` watches `MODELS_YAML_PATH`; on every save, the registry is reloaded atomically.
- **Atomic-swap discipline** — `app.state.registry = new_registry` is one Python operation; in-flight requests complete against the registry snapshot they entered with.
- **What works** — adding/editing/removing a model that targets an existing backend (`ollama`).
- **Locked v0.2.0 limitation** — adding a model with a NEW backend type (e.g., the first-ever `mlx` entry when no MLX models existed at startup) requires a process restart, because adapters are constructed at lifespan startup and not rebuilt on hot-reload. Documented as a known limitation.
- **Failure handling** — if the new YAML is malformed, the watcher logs `event="registry_reload_failed"` and keeps the previous registry live.

---

## 8. `docker/requirements.txt` Extension

**Path**: `docker/requirements.txt`
**Status**: APPEND.
**Strategy**: append three lines at the end of the existing v0.1.0 block. Preserve every existing line.

**Exact lines to append** (versions match `pyproject.toml` minimums):

```
argon2-cffi>=23.1
structlog>=24.4
watchfiles>=0.24
```

**Resulting tail** (illustrative — Builder appends only the three lines):

```
# --- local-ai-server v0.1.0 runtime + dev deps (Phase 3) ---
fastapi>=0.115
uvicorn[standard]>=0.32
httpx>=0.27
pydantic>=2.9
pydantic-settings>=2.5
pyyaml>=6.0
pytest>=8
pytest-asyncio>=0.24
pytest-httpx>=0.30
ruff>=0.6
mypy>=1.11
argon2-cffi>=23.1
structlog>=24.4
watchfiles>=0.24
```

The Builder MAY add a comment header like `# --- local-ai-server v0.2.0 (Phase 3) ---` above the three new lines for traceability — optional.

**Verification**: `grep -E '^(argon2-cffi|structlog|watchfiles)' docker/requirements.txt` returns 3 lines.

**Notes**:
- Pre-existing dev-container deps (pandas, plotly, jupyter, etc.) are UNCHANGED. Pre-existing v0.1.0 lines are UNCHANGED.
- Do NOT touch `Dockerfile_Base`, `Dockerfile_Dev`, `build_*.sh`, `install_*.sh`, `.p10k.zsh`. Out of scope.

---

## 9. `pyproject.toml` Audit

**Path**: `pyproject.toml`
**Status**: AUDIT ONLY (no change).

**Confirmed state** (read at architecture time):

```toml
[project]
name = "local-ai-server"
version = "0.2.0"  # ← already correct from Phase 1 step 1.1
...
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "httpx>=0.27",
    "pydantic>=2.9",
    "pydantic-settings>=2.5",
    "starlette<1.0",
    "pyyaml>=6.0",
    "argon2-cffi>=23.1",   # ← Phase 1
    "structlog>=24.4",     # ← Phase 2
    "watchfiles>=0.24",    # ← Phase 2
]
```

**Phase 3 verification** (Builder runs):

```bash
grep '^version' pyproject.toml
# expected: version = "0.2.0"

grep -E '^\s*"(argon2-cffi|structlog|watchfiles)' pyproject.toml
# expected: 3 lines (each with the >= version pin)
```

**No version bump in this phase**. The version is `0.2.0` already, set in Phase 1 step 1.1.

The `description` field is `"OpenAI-compatible local AI gateway (v0.1.0: Ollama only)."` — out of date. The Builder MAY update it to `"OpenAI-compatible local AI gateway with bearer auth, structured logging, and hot-reload (v0.2.0)."`. Optional cosmetic change; not a Phase 3 hard requirement.

The `[tool.pytest.ini_options]` block already exists; do not modify. The `[dependency-groups].dev` already lists `pytest`, `pytest-asyncio`, `pytest-httpx`, `ruff`, `mypy`. No additions needed.

---

## 10. `ruff check .` Audit

**Status**: AUDIT ONLY.

**Expected**: Phase 1 and Phase 2 both ran `uv run ruff check .` as part of their builder verification (step 1.X and 2.X). The tree is clean entering Phase 3.

**Phase 3 final check pass** (Builder runs):

```bash
uv run ruff check .
# expected: All checks passed!
```

**No new lint config changes**. `ruff.toml` has the v0.1.0 `quote-style = "double"` fix in place (Phase 3 of v0.1.0 was the typo fix). The `exclude`, `line-length`, `indent-width`, and `[lint] extend-select = ["E501"]` settings are unchanged.

If the final check pass surfaces ANY new violation in `app/` or `tests/` files modified during Phase 3, the Builder fixes it inline (a long line wrap, an unused import). The Builder does NOT modify pre-existing v0.1.0 or Phase 1/2 source.

If a finding surfaces in `tests/` (e.g., E501 on a long parametrize id or test line), the Builder may add a `[lint.per-file-ignores]` carve-out:

```toml
[lint.per-file-ignores]
"tests/**/*.py" = ["E501"]
```

This was permitted by v0.1.0 phase-3-architecture.md §2.14 as escape-hatch insurance. **Strong recommendation**: do NOT add the carve-out unless ruff actually flags a test line that cannot be naturally wrapped.

---

## 11. Build Sequence — Per-Plan-Step Mapping

This table maps every dev-plan step (3.1–3.12) to specific Builder actions, in order.

| Step | Plan task | Builder actions | Files touched |
|---|---|---|---|
| 3.1 | Extend `tests/conftest.py` | Add `_TEST_HASHER`, `_TEST_KEY_PLAINTEXT_BASE` constants; add `temp_keys_db`, `client_with_auth`, `auth_headers`, `captured_log`, `tmp_models_yaml` fixtures per §2.1. Verify imports: `uv run python -c "from tests.conftest import temp_keys_db, client_with_auth; print('ok')"` (note: tests/__init__.py exists). | `tests/conftest.py` |
| 3.2 | Implement `tests/test_keys_db.py` | Write all 12 tests per §2.2. Verify: `uv run pytest tests/test_keys_db.py -q` is GREEN. | `tests/test_keys_db.py` (NEW) |
| 3.3 | Implement `tests/test_auth.py` | Write all 13 tests per §2.3. Verify: `uv run pytest tests/test_auth.py -q` is GREEN. | `tests/test_auth.py` (NEW) |
| 3.4 | Implement `tests/test_readyz.py` | Write all 6 tests per §2.4. Verify (no Ollama): `uv run pytest tests/test_readyz.py -q -m "not live"` is GREEN; with Ollama: all 6 GREEN. | `tests/test_readyz.py` (NEW) |
| 3.5 | Implement `tests/test_hot_reload.py` | Write the local `client_for_hot_reload` fixture + all 4 tests per §2.5. Verify: `uv run pytest tests/test_hot_reload.py -q` is GREEN. | `tests/test_hot_reload.py` (NEW) |
| 3.6 | Implement `tests/test_logging.py` | Write all 17 tests per §2.6. Confirm the `captured_log` fixture monkeypatches `configure_structlog` correctly (verified by `test_logging_registry_loaded_event_emitted_on_startup`). Verify: `uv run pytest tests/test_logging.py -q` is GREEN. | `tests/test_logging.py` (NEW) |
| 3.7 | Migrate existing tests to bearer header | Apply the per-file diff in §5 to `tests/test_chat.py`, `tests/test_chat_streaming.py`, `tests/test_embeddings.py`, `tests/test_models_endpoint.py`. Replace `client` → `client_with_auth`, add `headers=auth_headers`. Rewrite the two no-auth tests in `tests/test_models_endpoint.py` per §5.4. Verify: `uv run pytest -q` is GREEN (live tests skip cleanly when Ollama is down). | 4 test files MODIFIED |
| 3.8 | mypy `--strict` cleanup | Apply each fix from §6.3 file-by-file, in this order: `app/adapters/base.py` → `app/adapters/ollama.py` → `app/adapters/mlx.py` → `app/adapters/docker_model_runner.py` → `app/registry.py` → `app/main.py` → `app/auth.py` → `app/middleware_logging.py` → `app/registry_watcher.py` → `app/routers/health.py` → `app/routers/models.py` → `app/routers/chat.py` → `app/routers/embeddings.py` → `app/errors.py` → `app/logging.py` → `app/config.py`. After each file, the Builder may run `uv run pytest -q` to confirm no test broke. The Builder does NOT run `uv run mypy app/ --strict` themselves (QA's job per the input contract); the Builder applies the fixes from §6.3 and trusts that QA's strict pass will be clean. | up to 16 `app/` files |
| 3.9 | Rewrite `README.md` | Apply the section-by-section plan in §7. Preserve WIP banner, architecture diagram embed, and license footer verbatim. Add Auth, /readyz, hot-reload sections. Update Version roadmap. Update SDK snippet. Verify: `grep -E '(generate_api_key|/readyz|hot-reload|sk-local-)' README.md \| head` matches expected lines. | `README.md` |
| 3.10 | Extend `docker/requirements.txt` | Append the 3 lines per §8. Verify: `grep -E '^(argon2-cffi\|structlog\|watchfiles)' docker/requirements.txt` returns 3 lines. | `docker/requirements.txt` |
| 3.11 | Final `ruff check .` pass | Run `uv run ruff check .`; fix any violation introduced by 3.1–3.10. Should be clean. | repo-wide if needed |
| 3.12 | Final regression: re-run Phase 1 and Phase 2 manual checkpoints | Execute the checkpoint scripts from `pm/v0_2_0/development_plan.md` §8 Phase 1 and Phase 2 in a fresh shell; confirm all green. This is operational verification, not a code change. | n/a |

**Order constraint**: 3.1 (conftest) MUST come before 3.2–3.7 (the new and migrated test files all depend on the new fixtures). 3.8 (mypy cleanup) is independent of the test work and can interleave with it; the Builder may do 3.8 before, after, or alongside 3.2–3.7. 3.9–3.11 are post-test-and-mypy; they can interleave with each other.

The development plan §8 Phase 3 test checkpoint runs at the end of step 3.12 and asserts:

```bash
uv run pytest -q                                # all green
uv run pytest -q tests/test_auth.py tests/test_keys_db.py tests/test_readyz.py tests/test_hot_reload.py tests/test_logging.py
uv run mypy app/ --strict                       # zero errors
uv run ruff check .                             # All checks passed!
grep -E '^(argon2-cffi|structlog|watchfiles)' docker/requirements.txt   # 3 lines
grep -E '(generate_api_key|/readyz|hot-reload|sk-local-)' README.md | head
grep '^version' pyproject.toml                  # version = "0.2.0"
```

---

## 12. Risks & Open Questions

| # | Item | Severity | Mitigation / open question |
|---:|---|---|---|
| 1 | `captured_log` fixture incorrectly clobbered by `configure_structlog` during lifespan startup | High (test breaks) | The fixture monkeypatches `app.logging.configure_structlog` to a no-op shim that re-installs the capture chain. Locked in §2.6. The `test_logging_registry_loaded_event_emitted_on_startup` test is the canary; if it ever flakes, the dance is broken. |
| 2 | Argon2 default-params verify (~50 ms) blowing up the auth test suite | Med (slow tests) | `_TEST_HASHER` uses `time_cost=1, memory_cost=8, parallelism=1` (~1 ms per verify). Each Argon2 hash embeds its parameters; the runtime `_HASHER` reads them from the hash and verifies at the cheap cost. Locked in §2.1. |
| 3 | `lru_cache` on `get_settings` makes env-var monkeypatching brittle (cached `Settings` survives across tests) | Med | Both `client_with_auth` and `client_for_hot_reload` call `get_settings.cache_clear()` BEFORE and AFTER constructing the app. Documented in §2.1 and §2.5. |
| 4 | Hot-reload polling timing (3 s timeout) flakes on slow filesystems / CI | Low-Med | `_poll_until` polls every 100 ms for 3 s — far above the `watchfiles` 50 ms debounce + filesystem latency on macOS/Linux. If CI surfaces flakes, bump the timeout to 5 s. |
| 5 | mypy carryover findings list (27 in v0.1.0 + 1 in Phase 2) was never enumerated by file:line | Med (architect uncertainty) | This document enumerates likely findings via static inspection (§6.3). The actual count after the fixes land may differ; QA's strict run produces the authoritative list. The Builder applies the listed fixes; if QA reports residual findings, they are mechanical applications of the same patterns (mostly `dict` parametrization + `cast` for Any chains). |
| 6 | `app/errors.py` four `# type: ignore[arg-type]` lines may flip to "unused" under `--warn-unused-ignores` after Starlette pin updates | Low | The Builder removes one and observes QA's response; if mypy complains, restore. Documented in §6.3. The plan §8 step 3.8 explicitly permits these ignores to remain. |
| 7 | `tests/test_models_endpoint.py` had two forward-looking tests asserting "no auth required" — they MUST become inverted under v0.2.0 | Low (mechanical) | Locked rewrite plan in §5.4: delete and replace with negative-auth assertions. |
| 8 | The development plan §5.2 says streaming `usage` fields should be "omitted" when absent; Phase 2 architecture §5.5 / §15 OQ #11 LOCKED `null` instead | Low (already-resolved deviation) | Phase 3 tests assert `null` (Phase 2 architecture §15 OQ #11). The README documents the field shape. If the orchestrator overrides to "omit", `tests/test_logging.py::test_logging_streaming_request_event_emits_null_usage` flips its assertion in one line. **Open question for orchestrator**: confirm `null` vs omit; default per Phase 2 architecture is `null`. |
| 9 | The `client_with_auth` fixture re-imports `app.main` and `app.config` lazily inside the fixture body to avoid module-level cache contamination | Low (idiom unfamiliarity) | Documented inline; the Builder copies the pattern verbatim. |
| 10 | `httpx_mock` from `pytest-httpx>=0.30` API stability for `add_exception(...)` | Low | Already used in `tests/adapters/test_ollama_adapter.py::test_ollama_adapter_health_returns_unreachable_when_offline` — same pattern, same call site. |
| 11 | `MODELS_YAML_PATH` default in `app/config.py` is `Path("config/models.yaml")` — relative; tests that override it must do so via env var, NOT by mutating the Path post-construction | Low (already understood) | The `client_for_hot_reload` fixture uses `monkeypatch.setenv` + `cache_clear()`; locked in §2.5. |
| 12 | Migration of v0.1.0 tests adds `auth_headers` to ~26 test functions; risk of mechanical-error typos | Low | The Builder applies the same diff to each: `(client: TestClient)` → `(client_with_auth: TestClient, auth_headers: dict[str, str])`, `client.<method>(...)` → `client_with_auth.<method>(..., headers=auth_headers)`. Verification: `uv run pytest -q` after each file is migrated. |
| 13 | The `tests/test_hot_reload.py::test_hot_reload_malformed_yaml_keeps_previous_registry` test verifies "watcher errors never bring down the app" — depends on the watcher's exception handling being correct (Phase 2 architecture §8.3). If a future regression breaks it, this test is the canary. | Low (canary design) | Documented; no mitigation needed. |
| 14 | `event="registry_loaded"` regression test (`test_logging_registry_loaded_event_emitted_on_startup`) depends on Phase 2's rename from the v0.1.0 `"registry loaded: %d models ..."` stdlib format. If a future contributor reverts to stdlib formatting, this test fires. | Low (regression lock) | Documented; intended behavior. |
| 15 | The v0.1.0 `test_get_v1_models_no_auth_required` test rewrite REMOVES a fixture-only test from `tests/test_models_endpoint.py` and the equivalent assertion moves to `tests/test_auth.py`. The Builder must ensure both files compile cleanly after the rewrite. | Low | Documented in §5.4. |

**Open question for orchestrator** (only one): risk #8 — confirm the `null`-vs-omit decision for streaming usage fields. Default: `null` (matches Phase 2 architecture §15 OQ #11). If the orchestrator wants strict plan-§5.2 adherence ("omit"), update `app/middleware_logging.py:_extract_usage` to drop keys with `None` values before passing to `_log.info(...)`, and flip the corresponding test assertion.

---

## 13. Done-when Checklist

Mirrors development plan §8 Phase 3 test checkpoint. Phase 3 is complete when:

- [ ] `tests/conftest.py` exports `temp_keys_db`, `client_with_auth`, `auth_headers`, `captured_log`, `tmp_models_yaml` fixtures.
- [ ] `tests/test_keys_db.py` exists with 12 passing tests.
- [ ] `tests/test_auth.py` exists with 13 passing tests covering missing/malformed/empty/no-scheme/unknown/revoked/hash-mismatch/corrupted-hash 401 paths, the 200 path, the envelope-shape lock, the `last_used_at` regression, the public-allowlist lock, and the lowercase-bearer regression.
- [ ] `tests/test_readyz.py` exists with 6 tests; live tests skip cleanly without Ollama; the httpx-mocked 503 path passes unconditionally.
- [ ] `tests/test_hot_reload.py` exists with 4 tests (added/removed/identity/malformed); all pass within ~3 s each.
- [ ] `tests/test_logging.py` exists with 17 tests; the `captured_log` fixture correctly captures lifespan startup events; the deliberate-leak test confirms redaction; the streaming-null-usage test confirms the Phase 2 punt.
- [ ] `tests/test_chat.py`, `tests/test_chat_streaming.py`, `tests/test_embeddings.py`, `tests/test_models_endpoint.py` all migrated to `client_with_auth` + `auth_headers`. The two `tests/test_models_endpoint.py` no-auth tests are rewritten to assert 401.
- [ ] `uv run pytest -q` is fully GREEN against a live Ollama. Approximately 93 tests; ~13 marked `live`.
- [ ] `uv run pytest -q` against a host without Ollama auto-skips the 13 `live` tests; the rest pass; exit 0.
- [ ] `uv run mypy app/ --strict` reports **zero** errors.
- [ ] `uv run ruff check .` outputs "All checks passed!".
- [ ] `docker/requirements.txt` contains `argon2-cffi>=23.1`, `structlog>=24.4`, `watchfiles>=0.24`.
- [ ] `pyproject.toml` `[project].version == "0.2.0"`; `[project].dependencies` lists `argon2-cffi`, `structlog`, `watchfiles`.
- [ ] `README.md` describes the auth flow (mint, revoke, header format, 401 envelope, KEYS_DB_PATH), the `/readyz` payload (200 + 503 samples), the hot-reload behavior (atomic-swap discipline + adapter limitation), and the v0.2.0 SDK snippet with `api_key="sk-local-..."`. Preserves the WIP banner, architecture diagram embed, and license footer verbatim.
- [ ] No edits to `app/main.py`, `app/auth.py`, `app/logging.py`, `app/middleware_logging.py`, `app/registry_watcher.py`, `app/routers/*.py`, `app/config.py`, `app/registry.py`, `app/schemas.py`, `app/errors.py`, `app/adapters/*.py` BEYOND the type-annotation fixes enumerated in §6.
- [ ] No edits to `pm/v0_2_0/{development_plan,phase-1-architecture,phase-2-architecture,summary}.md`, `pm/v0_1_0/**`, `posts/**`, `assets/**`, `docs/**`, `.devcontainer/**`, `.vscode/**`, `Dockerfile_*`, `build_*.sh`, `install_*.sh`, `.p10k.zsh`, `ruff.toml`, `.gitignore`, `config/.env.example`, `scripts/*.py`.
- [ ] The architectural invariants from Phase 1 + Phase 2 are now locked by tests:
    - 401 envelope shape (test_auth_envelope_shape_exact)
    - 5-way 401 path coverage (test_auth_*)
    - Lowercase-bearer case-insensitivity (test_auth_lowercase_bearer_scheme_returns_200)
    - `last_used_at` update on success (test_auth_last_used_at_updated_on_success)
    - `/readyz` 200/503 binary aggregation (test_readyz_status_field_is_binary)
    - Atomic-swap object identity (test_hot_reload_changes_app_state_registry_object_identity)
    - Malformed-yaml resilience (test_hot_reload_malformed_yaml_keeps_previous_registry)
    - `Authorization` redaction at top-level event_dict (test_logging_redact_authorization_*)
    - `event="request"` carries all 10 spec §8 fields (test_logging_request_event_emits_required_fields)
    - 12-char `key_prefix` regression (test_logging_request_event_carries_key_prefix_12_chars)
    - Bearer plaintext never appears in any captured event (test_logging_no_bearer_substring_in_captured_events)
    - `event="registry_loaded"` startup-line regression (test_logging_registry_loaded_event_emitted_on_startup)
    - `/v1/*` validation runs after auth so `key_prefix` is set even on 422 (test_logging_invalid_request_validation_carries_key_prefix)

End of Phase 3 architecture specification.

---

# Final Confirmation Summary

**Path where the architecture should be saved**: `/Users/ramikrispin/Personal/tutorials/local-ai-server/pm/v0_2_0/phase-3-architecture.md` (the Architect's tool set in this session does not include Write; the orchestrator persists this content to the path above from this assistant message).

**Files to create / modify in Phase 3** (Builder scope):
- 5 NEW test files: `tests/test_keys_db.py`, `tests/test_auth.py`, `tests/test_readyz.py`, `tests/test_hot_reload.py`, `tests/test_logging.py`
- 1 EXTENDED: `tests/conftest.py` (5 new fixtures + constants)
- 4 MIGRATED: `tests/test_chat.py`, `tests/test_chat_streaming.py`, `tests/test_embeddings.py`, `tests/test_models_endpoint.py`
- Up to 16 `app/` files (type annotations only, ~43 likely findings)
- 1 REWRITE: `README.md`
- 1 APPEND: `docker/requirements.txt` (3 lines)
- AUDIT (no change expected): `pyproject.toml`, `ruff.toml`

**Total: ~13 file edits + ~16 type-only patches.**

**Test count**: 52 NEW tests across 5 files + ~26 migrated tests across 4 files = ~78 net new/touched tests; total suite ≈93 tests after Phase 3 (~13 `live`-gated, ~80 unconditional).

**mypy finding count**: ~43 likely (27 v0.1.0 carryover + 1 Phase 2 +new auth/logging/middleware/watcher findings); inventory enumerated in §6.3 by file. All fixes are real (`dict` parametrization + `cast` for `Any` chains + return-type annotations); no new `# type: ignore` lines.

**Open question for orchestrator (only one)**: confirm Phase 2 architecture §15 OQ #11 — emit `null` (locked here) vs. omit absent `prompt_tokens`/`completion_tokens` keys per plan §5.2 literal reading. Default: `null` (stable schema). Override surface: `app/middleware_logging.py:_extract_usage` + the corresponding test assertion in `tests/test_logging.py`.

**Risks flagged**: (a) `captured_log` clobbering by lifespan-time `configure_structlog` (mitigation: monkeypatch `app.logging.configure_structlog` to a no-op shim, verified by the registry_loaded canary test); (b) Argon2 default-params verify cost (~50 ms × N tests) — mitigated by `_TEST_HASHER` with cheap params (~1 ms verify) and the parameter-in-hash property of Argon2id; (c) 27-finding list never enumerated by file:line in v0.1.0 docs — this document provides a best-effort inventory in §6.3 by static inspection; QA's strict run produces the authoritative count.
