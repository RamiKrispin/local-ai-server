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
"""
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings  # noqa: F401
from app.main import create_app
from app.registry import Registry

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
OLLAMA_PROBE_URL = "http://localhost:11434/api/tags"
OLLAMA_PROBE_TIMEOUT = 2.0

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
