"""Tests for GET /readyz.

Test inventory (6 tests):
  - test_readyz_no_auth_required (unconditional)
  - test_readyz_live_ollama_returns_200 (live)
  - test_readyz_all_unreachable_returns_503 (httpx_mock)
  - test_readyz_envelope_extends_openai_shape (httpx_mock)
  - test_readyz_per_backend_payload_shape (live)
  - test_readyz_status_field_is_binary (httpx_mock)

The `client_with_auth` fixture is used (not plain `client`) to exercise
the real middleware stack and confirm that /readyz bypasses auth.
"""
import httpx
import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock


def _mock_ollama_unreachable(httpx_mock: HTTPXMock) -> None:
    """Helper: configure httpx_mock to raise ConnectError on /api/tags."""
    httpx_mock.add_exception(
        httpx.ConnectError("simulated"),
        url="http://localhost:11434/api/tags",
        method="GET",
    )


# ---------------------------------------------------------------------------
# Public-path (no auth) assertion
# ---------------------------------------------------------------------------


def test_readyz_no_auth_required(client_with_auth: TestClient) -> None:
    """GET /readyz without an Authorization header returns 200 or 503, not 401.

    Locks the PUBLIC_PATHS invariant: /readyz must bypass auth.
    """
    response = client_with_auth.get("/readyz")
    assert response.status_code in (200, 503), (
        f"Expected 200 or 503 from public /readyz, got {response.status_code}"
    )


# ---------------------------------------------------------------------------
# Live paths (require Ollama)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_readyz_live_ollama_returns_200(client_with_auth: TestClient) -> None:
    """With Ollama up: 200 with status='ok' and backends map."""
    response = client_with_auth.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "backends" in body
    assert body["backends"]["ollama"]["status"] == "ok"
    assert "models" in body["backends"]["ollama"]
    # Stub adapters return error, not unreachable.
    assert body["backends"]["mlx"]["status"] == "error"
    assert body["backends"]["docker_model_runner"]["status"] == "error"


@pytest.mark.live
def test_readyz_per_backend_payload_shape(
    client_with_auth: TestClient,
) -> None:
    """Every backend entry has at least a 'status' key."""
    response = client_with_auth.get("/readyz")
    body = response.json()
    backends = body.get("backends", {})
    for name, payload in backends.items():
        assert isinstance(payload, dict), f"backends[{name}] is not a dict"
        assert "status" in payload, f"backends[{name}] missing 'status'"


# ---------------------------------------------------------------------------
# Mocked paths (httpx_mock intercepts the Ollama health probe)
# ---------------------------------------------------------------------------


def test_readyz_all_unreachable_returns_503(
    client_with_auth: TestClient,
    httpx_mock: HTTPXMock,
) -> None:
    """When Ollama's /api/tags raises ConnectError: 503 with the OpenAI
    error envelope and a backends map."""
    _mock_ollama_unreachable(httpx_mock)
    response = client_with_auth.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    error = body.get("error", {})
    assert error.get("type") == "service_unavailable"
    assert error.get("code") == "no_backends_reachable"
    assert error.get("param") is None
    assert "backends" in body
    assert body["backends"]["ollama"]["status"] == "unreachable"
    assert body["backends"]["mlx"]["status"] == "error"
    assert body["backends"]["docker_model_runner"]["status"] == "error"


def test_readyz_envelope_extends_openai_shape(
    client_with_auth: TestClient,
    httpx_mock: HTTPXMock,
) -> None:
    """On 503, body.error matches the 4-key OpenAI envelope shape exactly."""
    _mock_ollama_unreachable(httpx_mock)
    response = client_with_auth.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    # 'error' and 'backends' are the two top-level keys.
    assert "error" in body
    assert "backends" in body
    error = body["error"]
    assert set(error.keys()) == {"type", "message", "param", "code"}


def test_readyz_status_field_is_binary(
    client_with_auth: TestClient,
    httpx_mock: HTTPXMock,
) -> None:
    """503 path has NO top-level 'status' field (OpenAI envelope, not a
    status payload). Locks the binary 200/503, no 'degraded' decision."""
    _mock_ollama_unreachable(httpx_mock)
    response = client_with_auth.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    # The 503 body must NOT carry a top-level 'status' key.
    assert "status" not in body
