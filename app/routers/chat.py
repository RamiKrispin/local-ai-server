from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.adapters.base import BackendAdapter, NotSupportedError
from app.errors import make_error
from app.registry import Capability, Model
from app.schemas import ChatCompletionRequest

SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

router = APIRouter(tags=["chat"])


@router.post(
    "/chat/completions",
    response_model=None,
    summary=(
        "OpenAI-compatible chat completions (stream + non-stream)."
    ),
)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
) -> dict | StreamingResponse:
    registry = request.app.state.registry
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

    if not model.supports(Capability.CHAT):
        raise NotSupportedError(
            (
                f"Backend '{model.backend}' / model '{model.id}' "
                f"does not support chat"
            ),
            backend=model.backend,
            param="model",
            code="backend_capability_missing",
        )

    if body.tools and not model.supports(Capability.TOOLS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=make_error(
                type_="invalid_request_error",
                message=(
                    f"Model '{model.id}' does not support "
                    "tool calls"
                ),
                param="tools",
                code="tools_not_supported",
            ),
        )

    adapter = adapters[model.backend]
    forwarded = body.model_dump(
        exclude_none=True, by_alias=True
    )
    # Replace the registry-facing model id with the upstream
    # one so Ollama receives 'llama3.1:8b' not 'ollama-llama3'.
    forwarded["model"] = model.upstream_model

    result = await adapter.chat_completions(
        forwarded, stream=body.stream
    )

    if body.stream:
        return StreamingResponse(
            result,
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
    return result
