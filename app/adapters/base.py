from abc import ABC, abstractmethod
from typing import AsyncIterator


class NotSupportedError(Exception):
    """Raised by adapters when an operation isn't supported.

    Translated to HTTP 501 with type='not_supported' by the FastAPI
    exception handler in app/errors.py.
    """

    def __init__(
        self,
        message: str,
        *,
        backend: str | None = None,
        param: str | None = None,
        code: str = "backend_capability_missing",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.backend = backend
        self.param = param
        self.code = code


class BackendAdapter(ABC):
    """Common contract for every backend behind the gateway.

    Concrete subclasses are constructed once during FastAPI lifespan
    startup, share a single httpx.AsyncClient (Phase 2), and are closed
    on lifespan shutdown.
    """

    name: str
    base_url: str

    @abstractmethod
    async def chat_completions(
        self,
        body: dict,
        stream: bool,
    ) -> dict | AsyncIterator[bytes]:
        """Forward a chat completion request to the upstream backend.

        When stream=False, return the parsed JSON response body.
        When stream=True, return an async iterator yielding raw bytes
        (SSE 'data: {json}\\n\\n' chunks plus the terminal
        'data: [DONE]\\n\\n'), to be wrapped in a StreamingResponse
        by the router.
        """
        ...

    @abstractmethod
    async def embeddings(self, body: dict) -> dict:
        """Forward an embeddings request; return parsed JSON response."""
        ...

    @abstractmethod
    async def health(self) -> dict:
        """Return {"status": "ok"|"unreachable", ...} for /readyz (Phase 3)."""
        ...

    async def close(self) -> None:
        """Release any held resources (httpx clients, etc.). Default no-op.

        Concrete adapters that own clients override this. Called from the
        FastAPI lifespan shutdown handler.
        """
        return None
