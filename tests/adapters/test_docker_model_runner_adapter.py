"""Tests for DockerModelRunnerAdapter — the v0.1.0 stub that returns HTTP 501.

All tests run unconditionally (no live deps, no Ollama required).

Architectural note on the DMR vs. MLX code distinction:
  - DockerModelRunnerAdapter uses code='not_implemented' uniformly for ALL
    methods — including embeddings — because the registry declares
    embeddings capability for model-runner-llama32 (it is expected to
    eventually support it) and the adapter simply isn't wired yet (v0.4.0+).
  - MLXAdapter differentiates: embeddings uses 'backend_capability_missing'
    because MLX permanently cannot produce embedding vectors.
  This distinction is the architectural seam documented in
  phase-1-architecture.md §2.11 and phase-3-architecture.md §2.11.

The `test_dmr_routed_embeddings_returns_501_with_envelope` test is the
seam test that distinguishes "capability declared but adapter unwired" from
"capability missing" — the capability gate passes (model-runner-llama32
declares embeddings) but the adapter raises not_implemented.
See phase-2-architecture.md §2.6 for the defense-in-depth rationale.
"""
import pytest

from app.adapters.base import NotSupportedError
from app.adapters.docker_model_runner import DockerModelRunnerAdapter


# ---------------------------------------------------------------------------
# Direct adapter-level tests
# ---------------------------------------------------------------------------


async def test_dmr_chat_completions_raises_not_supported_with_not_implemented_code() -> None:  # noqa: E501
    """chat_completions raises NotSupportedError(code='not_implemented')."""
    adapter = DockerModelRunnerAdapter()
    with pytest.raises(NotSupportedError) as exc_info:
        await adapter.chat_completions({}, stream=False)
    exc = exc_info.value
    assert exc.code == "not_implemented"
    assert exc.backend == "docker_model_runner"


async def test_dmr_chat_completions_streaming_raises_not_supported() -> None:
    """stream=True also raises NotSupportedError(code='not_implemented')."""
    adapter = DockerModelRunnerAdapter()
    with pytest.raises(NotSupportedError) as exc_info:
        await adapter.chat_completions({}, stream=True)
    assert exc_info.value.code == "not_implemented"


async def test_dmr_embeddings_raises_not_supported_with_not_implemented_code() -> None:  # noqa: E501
    """embeddings raises NotSupportedError(code='not_implemented').

    Contrast with MLXAdapter.embeddings which raises
    code='backend_capability_missing'.  DMR uses 'not_implemented'
    because the capability IS declared in the registry and WILL be wired
    in v0.4.0+; the adapter just isn't done yet.
    """
    adapter = DockerModelRunnerAdapter()
    with pytest.raises(NotSupportedError) as exc_info:
        await adapter.embeddings({})
    assert exc_info.value.code == "not_implemented"


async def test_dmr_health_raises_not_supported_with_not_implemented_code() -> None:  # noqa: E501
    """health raises NotSupportedError(code='not_implemented')."""
    adapter = DockerModelRunnerAdapter()
    with pytest.raises(NotSupportedError) as exc_info:
        await adapter.health()
    assert exc_info.value.code == "not_implemented"


async def test_dmr_close_is_noop() -> None:
    """close() returns None and does not raise (stub holds no resources)."""
    adapter = DockerModelRunnerAdapter()
    result = await adapter.close()
    assert result is None


# ---------------------------------------------------------------------------
# Routed HTTP surface via TestClient
# ---------------------------------------------------------------------------


def test_dmr_routed_chat_returns_501_with_envelope(
    client_with_auth,
    auth_headers,
) -> None:
    """POST /v1/chat/completions with model-runner-llama32 (chat-capable)
    returns 501 with error.code='not_implemented'.

    Error-path row 8 (phase-3-architecture.md §7).
    """
    response = client_with_auth.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "model-runner-llama32",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "not_implemented"


def test_dmr_routed_embeddings_returns_501_with_envelope(
    client_with_auth,
    auth_headers,
) -> None:
    """POST /v1/embeddings with model-runner-llama32 (embeddings-capable
    per registry) returns 501 with error.code='not_implemented'.

    Architectural seam test: the capability gate passes (DMR declares
    embeddings), but the adapter raises not_implemented.  This locks the
    defense-in-depth seam from phase-2-architecture.md §2.6.

    Error-path row 9 (phase-3-architecture.md §7).
    """
    response = client_with_auth.post(
        "/v1/embeddings",
        headers=auth_headers,
        json={
            "model": "model-runner-llama32",
            "input": "hello",
        },
    )
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "not_implemented"
