from typing import Any

from app.adapters.base import BackendAdapter, NotSupportedError
from app.adapters.ollama import OllamaAdapter
from app.adapters.mlx import MLXAdapter
from app.adapters.docker_model_runner import DockerModelRunnerAdapter
from app.config import Settings
from app.registry import Registry

# The value type uses Callable[..., BackendAdapter] semantics; the concrete
# constructors accept an optional base_url positional argument.  Typing as
# dict[str, type[Any]] lets mypy accept both cls() and cls(url) call forms
# without requiring a Protocol — the ABC's abstract contract is checked at
# the concrete-class level.
_ADAPTER_CLASSES: dict[str, type[Any]] = {
    "ollama": OllamaAdapter,
    "mlx": MLXAdapter,
    "docker_model_runner": DockerModelRunnerAdapter,
}


def build_adapters(
    registry: Registry,
    settings: Settings,
) -> dict[str, BackendAdapter]:
    """Construct one adapter per distinct backend referenced by the
    registry, honoring per-model base_url overrides where present.

    Returns a dict whose keys are backend strings ('ollama', 'mlx',
    'docker_model_runner') and whose values are the singleton adapters
    used by the routers via app.state.adapters[model.backend].
    """
    # Per-backend base_url resolution rules (in priority order):
    #   1. First registry model.base_url that is non-None for that backend.
    #   2. settings.ollama_base_url if backend == 'ollama'.
    #   3. The adapter class's hard-coded default (set in its __init__
    #      signature: 'http://localhost:11434' / ':8080' / DMR default).
    base_urls: dict[str, str | None] = {}
    for m in registry:
        if m.backend not in _ADAPTER_CLASSES:
            raise ValueError(
                f"registry: unknown backend '{m.backend}' for "
                f"model '{m.id}'"
            )
        if m.base_url and m.backend not in base_urls:
            base_urls[m.backend] = m.base_url

    adapters: dict[str, BackendAdapter] = {}
    for backend in {m.backend for m in registry}:
        cls = _ADAPTER_CLASSES[backend]
        if backend == "ollama" and backend not in base_urls:
            base_urls[backend] = settings.ollama_base_url
        url = base_urls.get(backend)
        adapters[backend] = cls(url) if url is not None else cls()
    return adapters


__all__ = [
    "BackendAdapter",
    "NotSupportedError",
    "OllamaAdapter",
    "MLXAdapter",
    "DockerModelRunnerAdapter",
    "build_adapters",
]
