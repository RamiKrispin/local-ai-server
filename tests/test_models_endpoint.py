"""Tests for GET /v1/models.

All tests use the `client` fixture (TestClient with lifespan running).
No live Ollama dependency — the endpoint reads only from the in-process
registry.

Note on auth tests: v0.1.0 has no authentication.  The two tests that
probe Authorization header behaviour are forward-looking regression locks
— when v0.2.0 adds auth, these tests will need to be updated to reflect
the new auth contract.
"""
from fastapi.testclient import TestClient

_LOCKED_IDS = frozenset(
    {
        "ollama-llama3",
        "ollama-nomic-embed",
        "mlx-mistral",
        "model-runner-llama32",
    }
)


def test_get_v1_models_returns_200(client: TestClient) -> None:
    response = client.get("/v1/models")
    assert response.status_code == 200


def test_get_v1_models_response_shape(client: TestClient) -> None:
    body = client.get("/v1/models").json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)


def test_get_v1_models_lists_all_four_registry_entries(
    client: TestClient,
) -> None:
    data = client.get("/v1/models").json()["data"]
    assert len(data) == 4
    assert {entry["id"] for entry in data} == _LOCKED_IDS


def test_get_v1_models_each_entry_shape(client: TestClient) -> None:
    """Every entry must have object='model', owned_by='local', and a
    positive integer `created` timestamp."""
    data = client.get("/v1/models").json()["data"]
    for entry in data:
        assert entry["object"] == "model", f"bad object for {entry['id']}"
        assert entry["owned_by"] == "local", (
            f"bad owned_by for {entry['id']}"
        )
        assert isinstance(entry["created"], int), (
            f"created not int for {entry['id']}"
        )
        assert entry["created"] > 0, (
            f"created not positive for {entry['id']}"
        )


def test_get_v1_models_no_auth_required(client: TestClient) -> None:
    """Sending no Authorization header returns 200 (auth is v0.2.0)."""
    response = client.get("/v1/models")
    assert response.status_code == 200


def test_get_v1_models_with_random_authorization_header_still_200(
    client: TestClient,
) -> None:
    """Sending a Bearer token does not change the response in v0.1.0
    (gateway ignores auth until v0.2.0)."""
    response = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer whatever"},
    )
    assert response.status_code == 200
