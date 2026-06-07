from typing import AsyncIterator

from app.adapters.base import BackendAdapter, NotSupportedError


class DockerModelRunnerAdapter(BackendAdapter):
    """Stub adapter for Docker Desktop Model Runner. Every method raises
    NotSupportedError. Real implementation lands in a later version.
    """

    name: str = "docker_model_runner"

    def __init__(
        self,
        base_url: str = (
            "http://model-runner.docker.internal/engines/v1"
        ),
    ) -> None:
        self.base_url = base_url.rstrip("/")

    async def chat_completions(
        self, body: dict, stream: bool
    ) -> dict | AsyncIterator[bytes]:
        raise NotSupportedError(
            "Docker Model Runner adapter is not implemented in v0.1.0",
            backend="docker_model_runner",
            code="not_implemented",
        )

    async def embeddings(self, body: dict) -> dict:
        raise NotSupportedError(
            "Docker Model Runner adapter is not implemented in v0.1.0",
            backend="docker_model_runner",
            code="not_implemented",
        )

    async def health(self) -> dict:
        raise NotSupportedError(
            "Docker Model Runner adapter is not implemented in v0.1.0",
            backend="docker_model_runner",
            code="not_implemented",
        )

    async def close(self) -> None:
        return None
