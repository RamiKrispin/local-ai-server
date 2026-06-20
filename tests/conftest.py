"""Pytest configuration: shared fixtures and the live-marker auto-skip hook.

The `live` marker gates tests that require a real Ollama process on
localhost:11434.  When Ollama isn't reachable, every such test is
automatically skipped (not failed), so contributors without a local
Ollama see a clean skip report rather than a wall of failures.

The reachability probe runs ONCE per session (cached in the module-level
`_alive` flag) to avoid N round-trips for N live tests.

Note: we probe only that the Ollama process answers /api/tags, NOT that
the specific models required by the live tests (llama3.1:8b,
nomic-embed-text) are already pulled.  If Ollama is up but the models
aren't pulled, the live tests will run and fail with an informative
httpx/Ollama error.  This is intentional for v0.1.0; the README lists
both `ollama pull` commands as prerequisites.

v0.2.0 additions (Phase 3):
  - `_TEST_HASHER` / `_TEST_KEY_PLAINTEXT_BASE`: fast Argon2 params for
    unit tests (time_cost=1, memory_cost=8 KiB, parallelism=1 ≈ 1 ms).
  - `temp_keys_db`: function-scoped SQLite key store with one pre-minted key.
  - `client_with_auth`: TestClient over a fresh app wired to the temp DB.
  - `auth_headers`: `{"Authorization": "Bearer sk-local-..."}` helper.
  - `captured_log`: captures structlog events during the test.
  - `tmp_models_yaml`: temp copy of config/models.yaml for hot-reload tests.

Fixture ordering rule: `captured_log` MUST appear BEFORE `client_with_auth`
in a test's argument list when the test asserts on lifespan startup events
(structlog is configured by lifespan; `captured_log` must monkeypatch
`configure_structlog` before that runs).
"""
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.auth import PREFIX_LEN, _init_db, insert
from app.config import get_settings  # noqa: F401
from app.main import create_app
from app.registry import Registry

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
OLLAMA_PROBE_URL = "http://localhost:11434/api/tags"
OLLAMA_PROBE_TIMEOUT = 2.0

# Argon2 parameters tuned for fast unit tests. The library default is
# (time_cost=2, memory_cost=65536 KiB, parallelism=8) which costs ~50 ms
# per verify(). We dial it down to ~1 ms per verify() for the test suite
# only — the parameters are encoded into the hash, so each test fixture
# both hashes and verifies under the same (cheap) settings.
_TEST_HASHER: PasswordHasher = PasswordHasher(
    time_cost=1,
    memory_cost=8,  # 8 KiB
    parallelism=1,
)

_TEST_KEY_PLAINTEXT_BASE: str = (
    "sk-local-TestFixturePlaintextValueDoNotUseInProd0123456789"
)

# ---------------------------------------------------------------------------
# Internal probe cache — private to this module
# ---------------------------------------------------------------------------
_alive: bool | None = None


def _probe_ollama() -> bool:
    """Return True iff Ollama answers OLLAMA_PROBE_URL within the timeout."""
    try:
        r = httpx.get(OLLAMA_PROBE_URL, timeout=OLLAMA_PROBE_TIMEOUT)
        r.raise_for_status()
        return True
    except (httpx.RequestError, httpx.HTTPStatusError):
        return False


# ---------------------------------------------------------------------------
# Marker registration
# ---------------------------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    """Register the 'live' marker so --strict-markers doesn't reject it."""
    config.addinivalue_line(
        "markers",
        "live: tests that require a running Ollama process",
    )


# ---------------------------------------------------------------------------
# Collection hook — auto-skip live tests when Ollama is unreachable
# ---------------------------------------------------------------------------
def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Auto-skip every `live`-marked test when Ollama isn't reachable.

    Runs ONE probe per test session (cached in the module-level `_alive`
    flag).  A contributor running `uv run pytest -q` without Ollama gets
    clean skips, not failures.
    """
    global _alive
    if _alive is None:
        _alive = _probe_ollama()
    if _alive:
        return
    skip = pytest.mark.skip(reason="Ollama not reachable on :11434")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def ollama_alive() -> bool:
    """Probe Ollama once per session; return True iff it answers
    /api/tags within OLLAMA_PROBE_TIMEOUT seconds.

    Tests may depend on this fixture directly if they want to read the
    value (e.g., to skip conditionally in unusual circumstances), though
    the normal mechanism is the pytest_collection_modifyitems hook above.
    """
    global _alive
    if _alive is None:
        _alive = _probe_ollama()
    return _alive


@pytest.fixture
def app_factory():
    """Return create_app as a thin re-export.

    Tests that need a fresh FastAPI instance for state isolation can
    construct one via `app_factory()` without importing app.main directly.
    """
    return create_app


@pytest.fixture
def client(app_factory) -> Iterator[TestClient]:
    """Function-scoped TestClient with the lifespan running.

    Constructs a fresh FastAPI app, enters the TestClient context (which
    runs lifespan startup: loads the registry, builds adapters), yields
    the client, then exits the context (lifespan shutdown closes the
    adapters' httpx.AsyncClient).

    CWD must be the repo root when pytest runs so that the lifespan can
    resolve `config/models.yaml` via the MODELS_YAML_PATH default.
    The `pythonpath = ["."]` setting in pyproject.toml ensures this.
    """
    app = app_factory()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def registry(client: TestClient) -> Registry:
    """Return the Registry attached to the running app's state.

    Depends on `client` so the lifespan has already run and
    app.state.registry is populated.
    """
    return client.app.state.registry  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# v0.2.0 fixtures (Phase 3)
# ---------------------------------------------------------------------------


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
    from app.config import get_settings as _get_settings  # avoid cache

    _get_settings.cache_clear()
    from app.main import create_app as _create_app

    app = _create_app()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        _get_settings.cache_clear()


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

    Mechanism: replace structlog's processor chain with one that appends
    the event_dict to a list. Also monkeypatches
    `app.logging.configure_structlog` to a no-op shim so the lifespan
    startup does NOT clobber our capture chain.

    Yields:
        A mutable list of event_dicts; inspect after the action under test.

    Scope: function. MUST appear BEFORE `client_with_auth` in a test's
    argument list when the test asserts on lifespan startup events.
    """
    events: list[dict[str, Any]] = []

    def _capture_processor(
        logger: Any, method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        events.append(dict(event_dict))
        return event_dict

    # Import here so the closure captures the module-level function.
    import app.logging as _logging_mod
    import app.main as _main_mod
    from app.logging import redact_authorization

    def _install_capture() -> None:
        # Include redact_authorization so captured events are post-redaction.
        structlog.configure(
            processors=[redact_authorization, _capture_processor],
            wrapper_class=structlog.make_filtering_bound_logger(0),  # NOTSET
            logger_factory=structlog.ReturnLoggerFactory(),
            cache_logger_on_first_use=False,
        )

    saved_config = structlog.get_config()
    _install_capture()

    # Prevent the lifespan-time configure_structlog() from clobbering us.
    # IMPORTANT: app.main imports configure_structlog via
    # `from app.logging import configure_structlog`, so we must patch
    # the name in BOTH modules — the source (app.logging) and the
    # consumer (app.main) — to intercept the lifespan's call.
    monkeypatch.setattr(
        _logging_mod,
        "configure_structlog",
        lambda level: _install_capture(),
    )
    monkeypatch.setattr(
        _main_mod,
        "configure_structlog",
        lambda level: _install_capture(),
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
