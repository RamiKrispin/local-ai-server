import logging
from typing import AsyncIterator

import httpx

from app.adapters.base import BackendAdapter

_log = logging.getLogger("app.adapters.ollama")


class OllamaAdapter(BackendAdapter):
    """Adapter for a host-native Ollama process serving the OpenAI-
    compatible surface at {base_url}/v1/... and the native API at
    {base_url}/api/...

    A single httpx.AsyncClient is constructed in __init__ and reused for
    every request; close() releases it during lifespan shutdown.
    """

    name: str = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=5.0,
                read=None,
                write=10.0,
                pool=5.0,
            ),
        )

    async def chat_completions(
        self,
        body: dict,
        stream: bool,
    ) -> dict | AsyncIterator[bytes]:
        if not stream:
            r = await self._client.post(
                "/v1/chat/completions", json=body
            )
            r.raise_for_status()
            return r.json()
        return self._stream(body)

    async def _stream(
        self, body: dict
    ) -> AsyncIterator[bytes]:
        try:
            async with self._client.stream(
                "POST", "/v1/chat/completions", json=body,
            ) as r:
                r.raise_for_status()
                async for chunk in r.aiter_raw():
                    yield chunk
        finally:
            # `async with` already closes on normal exit; the
            # try/finally is the cancel-safety seam (spec §5.3):
            # if the consumer (StreamingResponse) is cancelled
            # the GeneratorExit propagates here, the async-with
            # exit handler runs, and the upstream Response is
            # closed. No additional aclose() needed.
            pass

    async def embeddings(self, body: dict) -> dict:
        r = await self._client.post(
            "/v1/embeddings", json=body
        )
        r.raise_for_status()
        return r.json()

    async def health(self) -> dict:
        try:
            r = await self._client.get("/api/tags")
            r.raise_for_status()
            payload = r.json() or {}
            names = [
                m.get("name")
                for m in payload.get("models", [])
                if isinstance(m, dict)
            ]
            return {"status": "ok", "models": names}
        except httpx.RequestError as exc:
            return {"status": "unreachable", "error": str(exc)}
        except httpx.HTTPStatusError as exc:
            return {
                "status": "unreachable",
                "error": f"HTTP {exc.response.status_code}",
            }

    async def close(self) -> None:
        await self._client.aclose()
