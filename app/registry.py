from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from collections.abc import Iterator

import yaml


class Capability(str, Enum):
    CHAT = "chat"
    EMBEDDINGS = "embeddings"
    TOOLS = "tools"


class RegistryError(ValueError):
    """Raised when models.yaml is structurally invalid."""


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    backend: str  # 'ollama' | 'mlx' | 'docker_model_runner'
    upstream_model: str
    capabilities: frozenset[Capability]
    base_url: str | None = None

    def supports(self, cap: Capability) -> bool:
        """Return True iff cap is in this model's capabilities set."""
        return cap in self.capabilities


@dataclass(frozen=True, slots=True)
class Registry:
    models: tuple[Model, ...]
    _by_id: dict[str, Model] = field(
        default_factory=dict, compare=False
    )

    def get(self, model_id: str) -> Model | None:
        """O(1) lookup; returns None if not found."""
        return self._by_id.get(model_id)

    def __iter__(self) -> Iterator["Model"]:
        """Iterate models in source order."""
        return iter(self.models)

    def ids(self) -> list[str]:
        """List model ids in source order."""
        return [m.id for m in self.models]


def load_registry(path: str | Path) -> Registry:
    """Load and validate models.yaml. One-shot; called once at lifespan start.

    Raises:
        FileNotFoundError: path does not exist.
        RegistryError: YAML is malformed, missing required fields, has
            duplicate ids, or declares an unknown capability.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Registry file not found: {path}"
        )

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict) or "models" not in raw:
        raise RegistryError(
            "models.yaml must contain a top-level 'models' key"
        )

    entries = raw["models"]
    if not isinstance(entries, list):
        raise RegistryError("models.yaml 'models' must be a list")

    models: list[Model] = []
    seen_ids: set[str] = set()

    for i, entry in enumerate(entries):
        prefix = f"models[{i}]"

        if not isinstance(entry, dict):
            raise RegistryError(
                f"{prefix}: each entry must be a mapping"
            )

        # Required fields
        for req in ("id", "backend", "upstream_model", "capabilities"):
            if req not in entry or not entry[req]:
                raise RegistryError(
                    f"{prefix}: missing '{req}'"
                )

        model_id: str = entry["id"]
        if not isinstance(model_id, str) or not model_id.strip():
            raise RegistryError(f"{prefix}: 'id' must be a non-empty string")

        if model_id in seen_ids:
            raise RegistryError(
                f"{prefix}: duplicate id '{model_id}'"
            )
        seen_ids.add(model_id)

        backend: str = entry["backend"]
        if not isinstance(backend, str) or not backend.strip():
            raise RegistryError(
                f"{prefix}: 'backend' must be a non-empty string"
            )

        upstream_model: str = entry["upstream_model"]
        if (
            not isinstance(upstream_model, str)
            or not upstream_model.strip()
        ):
            raise RegistryError(
                f"{prefix}: 'upstream_model' must be a non-empty string"
            )

        raw_caps = entry["capabilities"]
        if not isinstance(raw_caps, list):
            raise RegistryError(
                f"{prefix}: 'capabilities' must be a list"
            )

        caps: set[Capability] = set()
        for cap_str in raw_caps:
            try:
                caps.add(Capability(cap_str))
            except ValueError:
                valid = ", ".join(c.value for c in Capability)
                raise RegistryError(
                    f"{prefix}: unknown capability '{cap_str}'"
                    f" (valid: {valid})"
                )

        base_url: str | None = entry.get("base_url") or None

        models.append(
            Model(
                id=model_id,
                backend=backend,
                upstream_model=upstream_model,
                capabilities=frozenset(caps),
                base_url=base_url,
            )
        )

    by_id = {m.id: m for m in models}
    return Registry(models=tuple(models), _by_id=by_id)
