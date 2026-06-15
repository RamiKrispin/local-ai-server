from typing import AsyncIterator

from app.adapters.base import BackendAdapter


class OllamaAdapter(BackendAdapter):
    """Adapter for a host-native Ollama process serving the OpenAI-compatible
    surface at {base_url}/v1/...

    Phase 1: skeleton only. All methods raise NotImplementedError.
    Phase 2 wires real httpx calls per spec §5.2 / §5.3.
    """

    name: str = "ollama"

    def __init__(
        self, base_url: str = "http://localhost:11434"
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Phase 2 will create an httpx.AsyncClient here.

    async def chat_completions(
        self, body: dict, stream: bool
    ) -> dict | AsyncIterator[bytes]:
        raise NotImplementedError("filled in Phase 2")

    async def embeddings(self, body: dict) -> dict:
        raise NotImplementedError("filled in Phase 2")

    async def health(self) -> dict:
        raise NotImplementedError("filled in Phase 2")

    async def close(self) -> None:
        return None
