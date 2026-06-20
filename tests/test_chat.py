"""Tests for POST /v1/chat/completions.

Test inventory:
  - test_chat_non_stream_live_ollama: live happy path (non-streaming).
  - test_chat_unknown_model_returns_404: error-path row 1.
  - test_chat_capability_gate_no_chat_returns_501: error-path row 3.
  - test_chat_tools_gate_chat_only_model_returns_400: error-path row 5.
  - test_chat_capability_first_ordering_no_chat_with_tools_returns_501:
      error-path row 6 — the most architecturally important test in this
      file.  Locks the invariant: capability gate fires BEFORE the tools
      gate.  A model with no `chat` capability returns 501 even if the
      request also carries `tools`, not 400.  See
      phase-3-architecture.md §2.5 and §7 row 6.
  - test_chat_stub_backend_mlx_returns_501: error-path row 7a.
  - test_chat_stub_backend_dmr_returns_501: error-path row 8a.
  - test_chat_invalid_body_returns_422: error-path row 11.
"""
import pytest
from fastapi.testclient import TestClient

# Minimal tools payload used in gate tests.
_TOOLS_PAYLOAD = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "parameters": {"type": "object"},
        },
    }
]


# ---------------------------------------------------------------------------
# Live happy path
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_chat_non_stream_live_ollama(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Non-streaming chat against real Ollama.

    Assertions are intentionally loose (no content pinning) because
    LLM output is non-deterministic across Ollama versions.  temperature=0
    reduces variance but does not guarantee exact text.
    """
    response = client_with_auth.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "ollama-llama3",
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with exactly three words.",
                }
            ],
            "stream": False,
            "temperature": 0,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    choices = body["choices"]
    assert len(choices) > 0
    message = choices[0]["message"]
    assert message["role"] == "assistant"
    content = message["content"]
    assert isinstance(content, str)
    assert len(content) > 0
    usage = body["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] > 0


# ---------------------------------------------------------------------------
# Error paths (no live Ollama needed — gates fire before adapter dispatch)
# ---------------------------------------------------------------------------


def test_chat_unknown_model_returns_404(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Unknown model id → 404 with model_not_found code.

    Error-path row 1 (phase-3-architecture.md §7).
    """
    response = client_with_auth.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "does-not-exist",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "model_not_found"
    assert error["param"] == "model"


def test_chat_capability_gate_no_chat_returns_501(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """A model declaring only [embeddings] returns 501 for chat.

    No live Ollama required — the capability gate fires before any
    adapter call.

    Error-path row 3 (phase-3-architecture.md §7).
    """
    response = client_with_auth.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "ollama-nomic-embed",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["type"] == "not_supported"
    assert error["code"] == "backend_capability_missing"


def test_chat_tools_gate_chat_only_model_returns_400(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """A model with [chat] but no [tools] returns 400 when tools is sent.

    The chat capability passes; the tools gate fires.  No live Ollama
    required — the gate fires before adapter dispatch.

    Error-path row 5 (phase-3-architecture.md §7).
    """
    response = client_with_auth.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "mlx-mistral",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": _TOOLS_PAYLOAD,
        },
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "tools_not_supported"
    assert error["param"] == "tools"


def test_chat_capability_first_ordering_no_chat_with_tools_returns_501(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """A model with no [chat] returns 501 even when the request also has
    tools — the capability gate fires BEFORE the tools gate.

    This test locks the gate-ordering invariant derived in
    phase-2-architecture.md (§4.3 decision graph) and called out
    explicitly in phase-3-architecture.md §2.5 and §7 row 6.

    Returning 501 (not 400) is the correct behaviour because the request
    failed on a capability check, not a parameter check.  If this test
    ever returns 400, the gate order has regressed.
    """
    response = client_with_auth.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "ollama-nomic-embed",  # embeddings only, no chat
            "messages": [{"role": "user", "content": "hi"}],
            "tools": _TOOLS_PAYLOAD,
        },
    )
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "backend_capability_missing"


def test_chat_stub_backend_mlx_returns_501(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """mlx-mistral (chat-capable, stub backend) returns 501.

    Capability gate passes; tools gate not triggered (no tools field);
    adapter raises NotSupportedError(code='not_implemented') → 501.

    Error-path row 7a (phase-3-architecture.md §7).
    """
    response = client_with_auth.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "mlx-mistral",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "not_implemented"


def test_chat_stub_backend_dmr_returns_501(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """model-runner-llama32 (chat-capable, stub backend) returns 501.

    Error-path row 8a (phase-3-architecture.md §7).
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


def test_chat_invalid_body_returns_422(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """A request body missing the required `messages` field returns 422
    with type='invalid_request_error'.

    Error-path row 11 (phase-3-architecture.md §7).
    The validation_exception_handler in app/errors.py converts Pydantic's
    RequestValidationError to the OpenAI envelope.
    """
    response = client_with_auth.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "ollama-llama3"},  # no messages field
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
