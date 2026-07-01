"""Tests for hot-reload of config/models.yaml via watchfiles.

All tests are unconditional — no live Ollama needed. The watcher loads
YAML synchronously without hitting Ollama backends.

Test inventory (4 tests):
  - test_hot_reload_added_model_appears_in_v1_models
  - test_hot_reload_changes_app_state_registry_object_identity
  - test_hot_reload_malformed_yaml_keeps_previous_registry
  - test_hot_reload_removed_model_disappears_from_v1_models

Pattern: uses the module-local `client_for_hot_reload` fixture (Pattern B
from the architecture spec) to set both KEYS_DB_PATH and MODELS_YAML_PATH
before app construction, avoiding fixture-ordering subtleties.

Polling discipline: _poll_until checks a predicate every 100 ms for up
to 3 s. This absorbs watchfiles' ~50 ms debounce step plus filesystem
scheduling jitter on macOS (kqueue) and Linux (inotify).
"""
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Module-local fixture: app with both env vars overridden
# ---------------------------------------------------------------------------


@pytest.fixture
def client_for_hot_reload(
    temp_keys_db: tuple[Path, str, str],
    tmp_models_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """TestClient with KEYS_DB_PATH and MODELS_YAML_PATH both overridden.

    Sets both env vars BEFORE constructing the app so the lifespan
    watcher watches the temp YAML file, not the real one.
    """
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


# ---------------------------------------------------------------------------
# Polling helper
# ---------------------------------------------------------------------------


def _poll_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 3.0,
    interval_s: float = 0.1,
) -> bool:
    """Poll predicate every interval_s for up to timeout_s.

    Returns True if predicate() returned True before the deadline,
    False otherwise. Always tries once more after the deadline.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hot_reload_added_model_appears_in_v1_models(
    client_for_hot_reload: TestClient,
    temp_keys_db: tuple[Path, str, str],
    tmp_models_yaml: Path,
) -> None:
    """Appending a model to the YAML causes it to appear in /v1/models."""
    _db, plaintext, _prefix = temp_keys_db
    headers = {"Authorization": f"Bearer {plaintext}"}

    initial = client_for_hot_reload.get("/v1/models", headers=headers)
    assert initial.status_code == 200
    initial_ids = {m["id"] for m in initial.json()["data"]}
    assert "hot-reload-canary" not in initial_ids

    # Append a new model to the temp YAML.
    raw = yaml.safe_load(tmp_models_yaml.read_text())
    raw["models"].append(
        {
            "id": "hot-reload-canary",
            "backend": "ollama",
            "upstream_model": "llama3.1:8b",
            "capabilities": ["chat"],
        }
    )
    tmp_models_yaml.write_text(yaml.safe_dump(raw))

    # Poll until the new id appears.
    def _canary_visible() -> bool:
        r = client_for_hot_reload.get("/v1/models", headers=headers)
        return any(
            m["id"] == "hot-reload-canary" for m in r.json().get("data", [])
        )

    assert _poll_until(_canary_visible), (
        "hot-reload-canary did not appear in /v1/models within 3 s"
    )


def test_hot_reload_changes_app_state_registry_object_identity(
    client_for_hot_reload: TestClient,
    temp_keys_db: tuple[Path, str, str],
    tmp_models_yaml: Path,
) -> None:
    """After a YAML mutation, app.state.registry is a different object.

    Locks the atomic-swap discipline: the whole Registry instance is
    replaced, not mutated in-place.
    """
    _db, plaintext, _prefix = temp_keys_db
    headers = {"Authorization": f"Bearer {plaintext}"}

    # Confirm initial state.
    resp = client_for_hot_reload.get("/v1/models", headers=headers)
    assert resp.status_code == 200

    old_registry = client_for_hot_reload.app.state.registry  # type: ignore[attr-defined]

    # Mutate the YAML.
    raw = yaml.safe_load(tmp_models_yaml.read_text())
    raw["models"].append(
        {
            "id": "identity-canary",
            "backend": "ollama",
            "upstream_model": "llama3.1:8b",
            "capabilities": ["chat"],
        }
    )
    tmp_models_yaml.write_text(yaml.safe_dump(raw))

    # Poll until the registry object changes.
    def _identity_changed() -> bool:
        current = client_for_hot_reload.app.state.registry  # type: ignore[attr-defined]
        return current is not old_registry

    assert _poll_until(_identity_changed), (
        "app.state.registry object identity did not change within 3 s"
    )


def test_hot_reload_malformed_yaml_keeps_previous_registry(
    client_for_hot_reload: TestClient,
    temp_keys_db: tuple[Path, str, str],
    tmp_models_yaml: Path,
) -> None:
    """Malformed YAML leaves the previous registry intact.

    Locks the 'watcher errors never bring down the app' contract.
    """
    _db, plaintext, _prefix = temp_keys_db
    headers = {"Authorization": f"Bearer {plaintext}"}

    initial = client_for_hot_reload.get("/v1/models", headers=headers)
    assert initial.status_code == 200
    initial_ids = {m["id"] for m in initial.json()["data"]}

    # Write malformed YAML.
    tmp_models_yaml.write_text("models: { not valid")

    # Wait for the watcher to process the event.
    time.sleep(1.0)

    # Registry must still serve the original models.
    resp = client_for_hot_reload.get("/v1/models", headers=headers)
    assert resp.status_code == 200
    current_ids = {m["id"] for m in resp.json()["data"]}
    assert current_ids == initial_ids, (
        f"Registry changed after malformed YAML write: {current_ids}"
    )


def test_hot_reload_removed_model_disappears_from_v1_models(
    client_for_hot_reload: TestClient,
    temp_keys_db: tuple[Path, str, str],
    tmp_models_yaml: Path,
) -> None:
    """Rewriting the YAML with fewer models causes /v1/models to shrink."""
    _db, plaintext, _prefix = temp_keys_db
    headers = {"Authorization": f"Bearer {plaintext}"}

    initial = client_for_hot_reload.get("/v1/models", headers=headers)
    assert initial.status_code == 200
    assert len(initial.json()["data"]) == 4

    # Rewrite YAML keeping only the first model.
    raw = yaml.safe_load(tmp_models_yaml.read_text())
    raw["models"] = raw["models"][:1]
    tmp_models_yaml.write_text(yaml.safe_dump(raw))

    def _one_model() -> bool:
        r = client_for_hot_reload.get("/v1/models", headers=headers)
        return len(r.json().get("data", [])) == 1

    assert _poll_until(_one_model), (
        "/v1/models did not shrink to 1 entry within 3 s"
    )
