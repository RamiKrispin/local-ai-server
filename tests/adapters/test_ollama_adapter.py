"""Direct adapter-level tests for OllamaAdapter.

These tests bypass FastAPI entirely and call OllamaAdapter methods
directly.  They complement the FastAPI-routed tests in tests/test_chat.py,
tests/test_embeddings.py, and tests/test_chat_streaming.py by locking the
adapter's individual wire-level contract.

Live tests (marked @pytest.mark.live) require:
  - ollama serve running on localhost:11434
  - llama3.1:8b pulled (for chat tests)
  - nomic-embed-text pulled (for embeddings test)

Non-live tests:
  - test_ollama_adapter_health_returns_unreachable_when_offline uses
    pytest-httpx's httpx_mock fixture to simulate a ConnectError.
    This locks the contract from phase-2-architecture.md §3 health():
    health() returns {"status":"unreachable",...} and does NOT raise.

  - test_ollama_adapter_close_is_idempotent: regression lock — calling
    aclose() twice on an httpx.AsyncClient should not raise.

Note on httpx_mock API: we use pytest-httpx>=0.30 which supports
  httpx_mock.add_exception(exc, url=..., method="GET")
If a future pytest-httpx version changes the signature, update the
incantation in test_ollama_adapter_health_returns_unreachable_when_offline.
"""
import pytest

from app.adapters.ollama import OllamaAdapter


# ---------------------------------------------------------------------------
# Live adapter tests
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_ollama_adapter_chat_non_stream_returns_dict() -> None:
    """Non-streaming chat returns a dict with choices[0].message.content."""
    adapter = OllamaAdapter(base_url="http://localhost:11434")
    try:
        result = await adapter.chat_completions(
            {
                "model": "llama3.1:8b",
                "messages": [{"role": "user", "content": "hi"}],
            },
            stream=False,
        )
        assert isinstance(result, dict)
        assert "choices" in result
        assert len(result["choices"]) > 0
        message = result["choices"][0].get("message", {})
        assert isinstance(message.get("content"), str)
        assert len(message["content"]) > 0
    finally:
        await adapter.close()


@pytest.mark.live
async def test_ollama_adapter_chat_stream_returns_async_iterator() -> None:
    """stream=True returns an async iterator yielding bytes; the collected
    output contains the b'data: [DONE]' sentinel."""
    adapter = OllamaAdapter(base_url="http://localhost:11434")
    try:
        result = await adapter.chat_completions(
            {
                "model": "llama3.1:8b",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            stream=True,
        )
        # Must be an async iterable (the _stream generator).
        assert hasattr(result, "__aiter__")
        chunks: list[bytes] = []
        async for chunk in result:
            assert isinstance(chunk, bytes)
            chunks.append(chunk)
        assert len(chunks) > 0
        joined = b"".join(chunks)
        assert b"data: [DONE]" in joined, (
            "Stream did not contain 'data: [DONE]'"
        )
    finally:
        await adapter.close()


@pytest.mark.live
async def test_ollama_adapter_embeddings_returns_dict() -> None:
    """embeddings returns a dict with data[0]["embedding"] (list of floats,
    length > 0)."""
    adapter = OllamaAdapter(base_url="http://localhost:11434")
    try:
        result = await adapter.embeddings(
            {"model": "nomic-embed-text", "input": "hello"}
        )
        assert isinstance(result, dict)
        assert "data" in result
        embedding = result["data"][0]["embedding"]
        assert isinstance(embedding, list)
        assert len(embedding) > 0
    finally:
        await adapter.close()


@pytest.mark.live
async def test_ollama_adapter_health_returns_ok_when_alive() -> None:
    """health() returns {"status":"ok","models":[...]} when Ollama is up.

    We do NOT assert specific model names because the set of pulled models
    varies across contributor machines.
    """
    adapter = OllamaAdapter(base_url="http://localhost:11434")
    try:
        result = await adapter.health()
        assert result["status"] == "ok"
        assert isinstance(result["models"], list)
    finally:
        await adapter.close()


# ---------------------------------------------------------------------------
# Non-live / mocked tests
# ---------------------------------------------------------------------------


async def test_ollama_adapter_health_returns_unreachable_when_offline(
    httpx_mock,
) -> None:
    """health() returns {"status":"unreachable","error":...} on ConnectError
    and does NOT raise.

    Error-path row 17 (phase-3-architecture.md §7).
    Locks the non-raising contract from phase-2-architecture.md §3 health().

    Uses pytest-httpx's httpx_mock fixture to intercept the GET to
    /api/tags and raise httpx.ConnectError.
    """
    import httpx

    httpx_mock.add_exception(
        httpx.ConnectError("simulated offline"),
        url="http://localhost:11434/api/tags",
        method="GET",
    )
    adapter = OllamaAdapter(base_url="http://localhost:11434")
    try:
        result = await adapter.health()
        # Must return a dict, NOT raise.
        assert result["status"] == "unreachable"
        assert isinstance(result.get("error"), str)
        assert len(result["error"]) > 0
    finally:
        await adapter.close()


async def test_ollama_adapter_close_is_idempotent() -> None:
    """Calling close() twice must not raise.

    Regression lock: httpx.AsyncClient.aclose() is safe to call multiple
    times in modern httpx; this test ensures we don't break that contract
    in our wrapper.
    """
    adapter = OllamaAdapter(base_url="http://localhost:11434")
    await adapter.close()
    await adapter.close()  # second call must not raise
