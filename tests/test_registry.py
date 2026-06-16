"""Unit tests for app/registry.py.

All tests are synchronous and use only tmp_path — no live deps, no app
fixtures.  These are pure-function tests of the YAML parser and lookup
layer.

Error-path inventory (per phase-3-architecture.md §7):
  Row 12: malformed YAML → RegistryError (or yaml.YAMLError)
  Row 13: duplicate id → RegistryError mentioning the id
  Row 14: unknown capability → RegistryError mentioning the bad cap
  Row 15: missing required field → RegistryError mentioning the path
"""
from pathlib import Path

import pytest
import yaml

from app.registry import (
    Capability,
    Model,
    Registry,
    RegistryError,
    load_registry,
)

# The four ids locked in config/models.yaml for v0.1.0.
_LOCKED_IDS = frozenset(
    {
        "ollama-llama3",
        "ollama-nomic-embed",
        "mlx-mistral",
        "model-runner-llama32",
    }
)


# ---------------------------------------------------------------------------
# Happy-path / functional tests
# ---------------------------------------------------------------------------


def test_load_registry_from_repo_yaml() -> None:
    """Regression lock: config/models.yaml parses into exactly 4 entries
    whose ids match the locked set defined above."""
    reg = load_registry("config/models.yaml")
    assert isinstance(reg, Registry)
    assert len(reg.models) == 4
    assert {m.id for m in reg.models} == _LOCKED_IDS


def test_registry_lookup_returns_model() -> None:
    """ollama-llama3 must resolve with the expected backend, upstream
    model, and capabilities."""
    reg = load_registry("config/models.yaml")
    model = reg.get("ollama-llama3")
    assert model is not None
    assert isinstance(model, Model)
    assert model.backend == "ollama"
    assert model.upstream_model == "llama3.1:8b"
    assert Capability.CHAT in model.capabilities
    assert Capability.TOOLS in model.capabilities


def test_registry_lookup_missing_returns_none() -> None:
    """get() on an unknown id returns None, does NOT raise."""
    reg = load_registry("config/models.yaml")
    result = reg.get("does-not-exist")
    assert result is None


def test_registry_iter_yields_source_order() -> None:
    """Iterating the registry yields models in YAML declaration order."""
    reg = load_registry("config/models.yaml")
    ids = [m.id for m in reg]
    # The YAML order is locked; update this if models.yaml changes.
    assert ids == [
        "ollama-llama3",
        "ollama-nomic-embed",
        "mlx-mistral",
        "model-runner-llama32",
    ]


def test_registry_ids_method_matches_iteration() -> None:
    """ids() must equal [m.id for m in registry]."""
    reg = load_registry("config/models.yaml")
    assert reg.ids() == [m.id for m in reg]


def test_capability_enum_string_equality() -> None:
    """Locks the str-Enum contract: Capability members compare equal to
    their string values."""
    assert Capability.CHAT == "chat"
    assert Capability.EMBEDDINGS == "embeddings"
    assert Capability.TOOLS == "tools"


def test_model_supports_method() -> None:
    """supports() returns True for a declared capability and False for
    an undeclared one."""
    reg = load_registry("config/models.yaml")
    # ollama-llama3 has [chat, tools] — not embeddings.
    model = reg.get("ollama-llama3")
    assert model is not None
    assert model.supports(Capability.CHAT) is True
    assert model.supports(Capability.TOOLS) is True
    assert model.supports(Capability.EMBEDDINGS) is False


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_load_registry_missing_file_raises_filenotfound(
    tmp_path: Path,
) -> None:
    """A path that does not exist raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_registry(tmp_path / "nope.yaml")


def test_load_registry_malformed_yaml_raises(tmp_path: Path) -> None:
    """A YAML lex error raises RegistryError or yaml.YAMLError.

    Error-path row 12 (phase-3-architecture.md §7).
    The current app/registry.py lets yaml.YAMLError propagate; the test
    accepts either to remain tolerant of a future wrapping change.
    """
    bad = tmp_path / "bad.yaml"
    bad.write_text("models: {oops", encoding="utf-8")
    with pytest.raises((RegistryError, yaml.YAMLError)):
        load_registry(bad)


def test_load_registry_missing_top_level_models_raises(
    tmp_path: Path,
) -> None:
    """A YAML file with no 'models:' key raises RegistryError mentioning
    'models'."""
    f = tmp_path / "no_models.yaml"
    f.write_text("something: else\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="models"):
        load_registry(f)


def test_load_registry_models_not_a_list_raises(tmp_path: Path) -> None:
    """models: 'string' (not a list) raises RegistryError."""
    f = tmp_path / "not_list.yaml"
    f.write_text("models: 'string'\n", encoding="utf-8")
    with pytest.raises(RegistryError):
        load_registry(f)


def test_load_registry_entry_missing_required_field_raises(
    tmp_path: Path,
) -> None:
    """An entry without upstream_model raises RegistryError whose message
    contains 'models[0]' and 'upstream_model'.

    Error-path row 15 (phase-3-architecture.md §7).
    """
    f = tmp_path / "missing_field.yaml"
    f.write_text(
        "models:\n"
        "  - id: test-model\n"
        "    backend: ollama\n"
        "    capabilities: [chat]\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError) as exc_info:
        load_registry(f)
    msg = str(exc_info.value)
    assert "models[0]" in msg
    assert "upstream_model" in msg


def test_load_registry_duplicate_id_raises(tmp_path: Path) -> None:
    """Two entries sharing the same id raise RegistryError whose message
    contains the duplicated id and 'duplicate'.

    Error-path row 13 (phase-3-architecture.md §7).
    """
    f = tmp_path / "dup.yaml"
    f.write_text(
        "models:\n"
        "  - id: same-id\n"
        "    backend: ollama\n"
        "    upstream_model: llama3.1:8b\n"
        "    capabilities: [chat]\n"
        "  - id: same-id\n"
        "    backend: ollama\n"
        "    upstream_model: llama3.1:8b\n"
        "    capabilities: [chat]\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError) as exc_info:
        load_registry(f)
    msg = str(exc_info.value)
    assert "same-id" in msg
    assert "duplicate" in msg


def test_load_registry_unknown_capability_raises(tmp_path: Path) -> None:
    """An entry with an unrecognised capability raises RegistryError whose
    message contains 'unknown capability' and the bad value.

    Error-path row 14 (phase-3-architecture.md §7).
    """
    f = tmp_path / "bad_cap.yaml"
    f.write_text(
        "models:\n"
        "  - id: test-model\n"
        "    backend: ollama\n"
        "    upstream_model: llama3.1:8b\n"
        "    capabilities: [chat, vision]\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError) as exc_info:
        load_registry(f)
    msg = str(exc_info.value)
    assert "unknown capability" in msg
    assert "vision" in msg


def test_load_registry_base_url_optional(tmp_path: Path) -> None:
    """base_url is optional: absent → None; present → the given string."""
    f = tmp_path / "base_url.yaml"
    f.write_text(
        "models:\n"
        "  - id: no-url\n"
        "    backend: ollama\n"
        "    upstream_model: llama3.1:8b\n"
        "    capabilities: [chat]\n"
        "  - id: with-url\n"
        "    backend: mlx\n"
        "    upstream_model: some/model\n"
        "    base_url: http://x:1\n"
        "    capabilities: [chat]\n",
        encoding="utf-8",
    )
    reg = load_registry(f)
    no_url = reg.get("no-url")
    with_url = reg.get("with-url")
    assert no_url is not None
    assert with_url is not None
    assert no_url.base_url is None
    assert with_url.base_url == "http://x:1"


def test_registry_is_immutable() -> None:
    """Registry is a frozen dataclass; mutating .models raises an error."""
    reg = load_registry("config/models.yaml")
    import dataclasses

    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        reg.models = ()  # type: ignore[misc]
