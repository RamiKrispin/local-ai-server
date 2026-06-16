"""Tests for POST /v1/embeddings.

Test inventory:
  - test_embeddings_live_ollama_returns_vector: live happy path.
  - test_embeddings_live_ollama_batch_input: live, batch input.
  - test_embeddings_capability_gate_no_embeddings_returns_501: row 4.
  - test_embeddings_unknown_model_returns_404: row 2.
  - test_embeddings_stub_backend_dmr_returns_501: row 9 (seam test).
  - test_embeddings_stub_backend_mlx_returns_501_via_capability_gate: row 10.
"""
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Live happy paths
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_embeddings_live_ollama_returns_vector(client: TestClient) -> None:
    """Single-string input against nomic-embed-text returns a non-empty
    vector with the expected OpenAI embeddings envelope shape.

    Vector dimension is asserted as > 0, not == 768, to tolerate model
    variants across Ollama releases.
    """
    response = client.post(
        "/v1/embeddings",
        json={"model": "ollama-nomic-embed", "input": "hello world"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    data = body["data"]
    assert len(data) == 1
    entry = data[0]
    assert entry["object"] == "embedding"
    assert entry["index"] == 0
    embedding = entry["embedding"]
    assert isinstance(embedding, list)
    assert len(embedding) > 0
    assert all(isinstance(v, float) for v in embedding)
    usage = body["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["total_tokens"] > 0


@pytest.mark.live
def test_embeddings_live_ollama_batch_input(client: TestClient) -> None:
    """Batch input (list of two strings) returns two embedding entries."""
    response = client.post(
        "/v1/embeddings",
        json={
            "model": "ollama-nomic-embed",
            "input": ["hello", "world"],
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) == 2
    assert data[0]["index"] == 0
    assert data[1]["index"] == 1
    assert len(data[0]["embedding"]) > 0
    assert len(data[1]["embedding"]) > 0


# ---------------------------------------------------------------------------
# Error paths (no live Ollama needed)
# ---------------------------------------------------------------------------


def test_embeddings_capability_gate_no_embeddings_returns_501(
    client: TestClient,
) -> None:
    """A model with only [chat, tools] returns 501 for embeddings.

    Gate fires before any adapter call — passes regardless of Ollama status.

    Error-path row 4 (phase-3-architecture.md §7).
    """
    response = client.post(
        "/v1/embeddings",
        json={"model": "ollama-llama3", "input": "hello"},
    )
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["type"] == "not_supported"
    assert error["code"] == "backend_capability_missing"


def test_embeddings_unknown_model_returns_404(client: TestClient) -> None:
    """Unknown model id → 404 with model_not_found code.

    Error-path row 2 (phase-3-architecture.md §7).
    """
    response = client.post(
        "/v1/embeddings",
        json={"model": "does-not-exist", "input": "hello"},
    )
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "model_not_found"


def test_embeddings_stub_backend_dmr_returns_501(
    client: TestClient,
) -> None:
    """model-runner-llama32 declares [embeddings] in the registry, so the
    capability gate passes.  The DMR stub adapter raises
    NotSupportedError(code='not_implemented') → 501.

    This is the architectural seam test distinguishing "capability declared
    but adapter unwired" from "capability missing".  See
    phase-2-architecture.md §2.6 for the defense-in-depth rationale.

    Error-path row 9 (phase-3-architecture.md §7).
    """
    response = client.post(
        "/v1/embeddings",
        json={"model": "model-runner-llama32", "input": "hello"},
    )
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "not_implemented"


def test_embeddings_stub_backend_mlx_returns_501_via_capability_gate(
    client: TestClient,
) -> None:
    """mlx-mistral declares only [chat], not [embeddings].  The capability
    gate fires and returns 501 with code='backend_capability_missing'.

    The MLXAdapter.embeddings method (which would return
    'backend_capability_missing' too, but for a different reason) is never
    reached.  This documents the layered defense.

    Error-path row 10 (phase-3-architecture.md §7).
    """
    response = client.post(
        "/v1/embeddings",
        json={"model": "mlx-mistral", "input": "hello"},
    )
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "backend_capability_missing"
