import time

from fastapi import APIRouter, Request

from app.schemas import ModelEntry, ModelsListResponse

router = APIRouter(tags=["models"])


@router.get(
    "/models",
    response_model=ModelsListResponse,
    summary="List available models (OpenAI-compatible).",
)
async def list_models(request: Request) -> ModelsListResponse:
    """Return the registry as {object: 'list', data: [...]}."""
    registry = request.app.state.registry
    now = int(time.time())
    return ModelsListResponse(
        data=[
            ModelEntry(id=m.id, created=now, owned_by="local")
            for m in registry
        ]
    )
