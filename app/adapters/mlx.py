from typing import Any, AsyncIterator

from app.adapters.base import BackendAdapter, NotSupportedError


class MLXAdapter(BackendAdapter):
    """Stub adapter for mlx_lm.server. Every method raises NotSupportedError
    so v0.1.0 returns HTTP 501 for any MLX-routed request, locking the seam.
    """

    name: str = "mlx"

    def __init__(
        self, base_url: str = "http://localhost:8080"
    ) -> None:
        self.base_url = base_url.rstrip("/")

    async def chat_completions(
        self, body: dict[str, Any], stream: bool
    ) -> dict[str, Any] | AsyncIterator[bytes]:
        raise NotSupportedError(
            "MLX adapter is not implemented in v0.1.0",
            backend="mlx",
            code="not_implemented",
        )

    async def embeddings(self, body: dict[str, Any]) -> dict[str, Any]:
        raise NotSupportedError(
            "MLX backend does not support embeddings",
            backend="mlx",
            param="model",
            code="backend_capability_missing",
        )

    async def health(self) -> dict[str, Any]:
        raise NotSupportedError(
            "MLX adapter is not implemented in v0.1.0",
            backend="mlx",
            code="not_implemented",
        )

    async def close(self) -> None:
        return None
