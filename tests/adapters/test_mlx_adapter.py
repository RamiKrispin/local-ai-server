"""Tests for MLXAdapter — the v0.1.0 stub that returns HTTP 501.

All tests run unconditionally (no live deps, no Ollama required) because
the stub raises NotSupportedError immediately without making any HTTP call.

Architectural note on the code split between not_implemented and
backend_capability_missing:
  - chat_completions and health use code='not_implemented' because MLX
    chat/health is a deferred implementation, not a permanent capability
    gap.  The adapter will eventually be wired in v0.4.0+.
  - embeddings uses code='backend_capability_missing' because MLX (as a
    chat/language model backend) permanently cannot produce OpenAI-style
    embedding vectors.  This is the deliberate distinction per
    phase-3-architecture.md §2.10 and phase-1-architecture.md §2.10.
"""
import pytest

from app.adapters.base import NotSupportedError
from app.adapters.mlx import MLXAdapter


# ---------------------------------------------------------------------------
# Direct adapter-level tests (no FastAPI, no TestClient)
# ---------------------------------------------------------------------------


async def test_mlx_chat_completions_raises_not_supported_with_not_implemented_code() -> None:  # noqa: E501
    """chat_completions raises NotSupportedError(code='not_implemented')."""
    adapter = MLXAdapter()
    with pytest.raises(NotSupportedError) as exc_info:
        await adapter.chat_completions({}, stream=False)
    exc = exc_info.value
    assert exc.code == "not_implemented"
    assert exc.backend == "mlx"


async def test_mlx_chat_completions_streaming_raises_not_supported() -> None:
    """stream=True also raises NotSupportedError(code='not_implemented')
    before any streaming branch is entered."""
    adapter = MLXAdapter()
    with pytest.raises(NotSupportedError) as exc_info:
        await adapter.chat_completions({}, stream=True)
    assert exc_info.value.code == "not_implemented"


async def test_mlx_embeddings_raises_not_supported_with_capability_missing_code() -> None:  # noqa: E501
    """embeddings raises NotSupportedError(code='backend_capability_missing').

    This is the permanent capability gap: MLX does not produce embeddings.
    See phase-3-architecture.md §2.10 and phase-1-architecture.md §5.2
    for the rationale behind the code distinction vs. chat/health.
    """
    adapter = MLXAdapter()
    with pytest.raises(NotSupportedError) as exc_info:
        await adapter.embeddings({})
    exc = exc_info.value
    assert exc.code == "backend_capability_missing"
    assert exc.param == "model"
    assert exc.backend == "mlx"


async def test_mlx_health_raises_not_supported_with_not_implemented_code() -> None:  # noqa: E501
    """health raises NotSupportedError(code='not_implemented')."""
    adapter = MLXAdapter()
    with pytest.raises(NotSupportedError) as exc_info:
        await adapter.health()
    assert exc_info.value.code == "not_implemented"


async def test_mlx_close_is_noop() -> None:
    """close() returns None and does not raise (stub holds no resources)."""
    adapter = MLXAdapter()
    result = await adapter.close()
    assert result is None


# ---------------------------------------------------------------------------
# Routed HTTP surface via TestClient
# (overlaps test_chat.py::test_chat_stub_backend_mlx_returns_501 by design —
# this copy anchors the envelope contract at the adapter-test layer for
# traceability.  See phase-3-architecture.md §2.10.)
# ---------------------------------------------------------------------------


def test_mlx_routed_chat_returns_501_with_envelope(client) -> None:
    """POST /v1/chat/completions with mlx-mistral (chat-capable, no tools)
    returns 501 with the OpenAI error envelope.

    The capability gate passes; the adapter raises NotSupportedError which
    the not_supported_handler translates to 501.
    """
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "mlx-mistral",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 501
    body = response.json()
    error = body["error"]
    assert error["type"] == "not_supported"
    assert error["code"] == "not_implemented"
