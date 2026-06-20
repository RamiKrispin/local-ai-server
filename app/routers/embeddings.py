from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request, status

from app.adapters.base import BackendAdapter, NotSupportedError
from app.errors import make_error
from app.registry import Capability, Model, Registry
from app.schemas import EmbeddingsRequest

router = APIRouter(tags=["embeddings"])


@router.post(
    "/embeddings",
    summary="OpenAI-compatible embeddings.",
)
async def embeddings(
    body: EmbeddingsRequest,
    request: Request,
) -> dict[str, Any]:
    registry = cast(Registry, request.app.state.registry)
    adapters: dict[str, BackendAdapter] = (
        request.app.state.adapters
    )

    model: Model | None = registry.get(body.model)
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=make_error(
                type_="invalid_request_error",
                message=f"Unknown model '{body.model}'",
                param="model",
                code="model_not_found",
            ),
        )

    if not model.supports(Capability.EMBEDDINGS):
        raise NotSupportedError(
            (
                f"Backend '{model.backend}' does not support "
                "embeddings"
            ),
            backend=model.backend,
            param="model",
            code="backend_capability_missing",
        )

    adapter = adapters[model.backend]
    forwarded = body.model_dump(
        exclude_none=True, by_alias=True
    )
    forwarded["model"] = model.upstream_model
    return await adapter.embeddings(forwarded)
