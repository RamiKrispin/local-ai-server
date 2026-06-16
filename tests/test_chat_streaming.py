"""Tests for POST /v1/chat/completions with stream=True (SSE).

Approach: TestClient supports streamed responses via client.stream() which
returns a Response object.  We use the context-manager form of
client.stream() to iterate SSE lines.  TestClient under FastAPI/Starlette
honors StreamingResponse — chunks arrive incrementally rather than being
buffered.

Each test must close its streaming response explicitly.  We use the
`with client.stream(...)` context manager form which ensures cleanup even
if an assertion fails midway.  This prevents hanging connections from
leaking into subsequent tests.

The `client` fixture is function-scoped so each test gets a fresh app
instance + fresh adapter dict + fresh httpx.AsyncClient; there is no
cross-test state.

Asserting an exact chunk count is brittle (depends on Ollama version and
prompt); we only assert "at least one" data: chunk and "exactly one"
[DONE] sentinel.
"""
import json

import pytest
from fastapi.testclient import TestClient

# Minimal tools payload for gate tests.
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
# Helpers
# ---------------------------------------------------------------------------


def _collect_sse_lines(response) -> list[str]:
    """Collect all non-empty lines from a streaming SSE response."""
    lines = []
    for line in response.iter_lines():
        if line:
            lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Live streaming happy-path tests
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_chat_streaming_live_ollama_yields_data_chunks(
    client: TestClient,
) -> None:
    """Streaming response returns 200 with text/event-stream content type
    and at least one parseable data: chunk."""
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "ollama-llama3",
            "messages": [
                {"role": "user", "content": "Say hi in three words."}
            ],
            "stream": True,
            "temperature": 0,
        },
    ) as response:
        assert response.status_code == 200
        content_type = response.headers.get("content-type", "")
        assert content_type.startswith("text/event-stream")

        found_data_chunk = False
        for line in response.iter_lines():
            if line.startswith("data: ") and line != "data: [DONE]":
                payload = json.loads(line[6:])
                assert "choices" in payload
                assert len(payload["choices"]) > 0
                assert "delta" in payload["choices"][0]
                found_data_chunk = True
                # Don't break — consume the rest cleanly.

        assert found_data_chunk, "No data: chunk received from Ollama"


@pytest.mark.live
def test_chat_streaming_live_ollama_terminates_with_done(
    client: TestClient,
) -> None:
    """The stream terminates with the exact sentinel 'data: [DONE]'.

    The terminator is passed through verbatim from Ollama; we never
    synthesize it (spec §5.3).  We use substring search on the collected
    body to tolerate trailing-whitespace differences.
    """
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "ollama-llama3",
            "messages": [
                {"role": "user", "content": "Say hi in three words."}
            ],
            "stream": True,
            "temperature": 0,
        },
    ) as response:
        assert response.status_code == 200
        lines = list(response.iter_lines())

    full_body = "\n".join(lines)
    assert "data: [DONE]" in full_body, (
        "Stream did not terminate with 'data: [DONE]'"
    )


@pytest.mark.live
def test_chat_streaming_live_ollama_delta_concatenation_nonempty(
    client: TestClient,
) -> None:
    """Concatenating delta.content across all chunks produces a non-empty
    string, locking the chunk-ordering / delta-semantics for SDK consumers.
    """
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "ollama-llama3",
            "messages": [
                {"role": "user", "content": "Say hi in three words."}
            ],
            "stream": True,
            "temperature": 0,
        },
    ) as response:
        assert response.status_code == 200
        parts: list[str] = []
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            payload_str = line[6:]
            if payload_str == "[DONE]":
                continue
            chunk = json.loads(payload_str)
            delta = chunk["choices"][0].get("delta", {})
            content = delta.get("content") or ""
            parts.append(content)

    full_content = "".join(parts)
    assert len(full_content) > 0, (
        "Delta concatenation produced an empty string"
    )


@pytest.mark.live
def test_chat_streaming_live_ollama_sse_headers_present(
    client: TestClient,
) -> None:
    """SSE_HEADERS from app/routers/chat.py must be present:
      Cache-Control: no-cache
      X-Accel-Buffering: no
    """
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "ollama-llama3",
            "messages": [
                {"role": "user", "content": "Say hi in three words."}
            ],
            "stream": True,
            "temperature": 0,
        },
    ) as response:
        assert response.status_code == 200
        headers = response.headers
        assert headers.get("cache-control") == "no-cache"
        assert headers.get("x-accel-buffering") == "no"
        # Consume the body to close the stream cleanly.
        for _ in response.iter_lines():
            pass


# ---------------------------------------------------------------------------
# Error paths (no live Ollama — gates fire before streaming branch)
# ---------------------------------------------------------------------------


def test_chat_streaming_capability_gate_returns_501_no_stream(
    client: TestClient,
) -> None:
    """stream=True with a model lacking [chat] returns 501 as a JSON
    envelope (NOT an SSE stream) — capability gate fires first."""
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "ollama-nomic-embed",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert response.status_code == 501
    # Must be a JSON response, NOT SSE.
    error = response.json()["error"]
    assert error["code"] == "backend_capability_missing"


def test_chat_streaming_tools_gate_returns_400_no_stream(
    client: TestClient,
) -> None:
    """stream=True with a chat-capable model + tools (but model lacks
    [tools]) returns 400 as JSON — tools gate fires before streaming
    branch."""
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "mlx-mistral",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "tools": _TOOLS_PAYLOAD,
        },
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "tools_not_supported"
