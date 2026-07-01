"""Tests for GET /v1/models.

All tests use the `client_with_auth` fixture (TestClient with lifespan
running and BearerAuthMiddleware wired to a temp key store).
No live Ollama dependency — the endpoint reads only from the in-process
registry.

v0.2.0 auth migration (Phase 3):
  - All requests now include auth_headers (Authorization: Bearer sk-local-...).
  - The two v0.1.0 "no auth required" tests are rewritten to assert 401,
    reflecting the new auth contract under v0.2.0.
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


def test_get_v1_models_returns_200(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    response = client_with_auth.get("/v1/models", headers=auth_headers)
    assert response.status_code == 200


def test_get_v1_models_response_shape(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    body = client_with_auth.get("/v1/models", headers=auth_headers).json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)


def test_get_v1_models_lists_all_four_registry_entries(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    data = client_with_auth.get(
        "/v1/models", headers=auth_headers
    ).json()["data"]
    assert len(data) == 4
    assert {entry["id"] for entry in data} == _LOCKED_IDS


def test_get_v1_models_each_entry_shape(
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Every entry must have object='model', owned_by='local', and a
    positive integer `created` timestamp."""
    data = client_with_auth.get(
        "/v1/models", headers=auth_headers
    ).json()["data"]
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


def test_get_v1_models_without_auth_returns_401(
    client_with_auth: TestClient,
) -> None:
    """Under v0.2.0, no Authorization header returns 401 with the OpenAI
    error envelope.  Replaces the v0.1.0 'no_auth_required' test."""
    response = client_with_auth.get("/v1/models")
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_api_key"


def test_get_v1_models_with_random_bearer_returns_401(
    client_with_auth: TestClient,
) -> None:
    """Under v0.2.0, an unrecognised Bearer token returns 401.
    Replaces the v0.1.0 'random_authorization_header_still_200' test."""
    response = client_with_auth.get(
        "/v1/models",
        headers={"Authorization": "Bearer whatever"},
    )
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_api_key"
