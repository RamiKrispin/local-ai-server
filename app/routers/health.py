import asyncio
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.adapters.base import BackendAdapter
from app.errors import make_error

router = APIRouter(tags=["health"])


def _short_error(exc: BaseException) -> str:
    """Render an exception to a single line, no traceback."""
    return f"{type(exc).__name__}: {exc}"


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    """Return {'status': 'ok'} with HTTP 200. No auth, no upstream pings."""
    return {"status": "ok"}


@router.get(
    "/readyz",
    summary="Readiness probe — fans out to every backend's health().",
    response_model=None,
)
async def readyz(request: Request) -> JSONResponse:
    """Public endpoint (no auth). Probes every adapter in
    app.state.adapters concurrently and returns:
      - 200 with {"status": "ok", "backends": {<name>: <payload>}}
        if AT LEAST ONE backend reports status == "ok".
      - 503 with the OpenAI envelope (type=service_unavailable,
        code=no_backends_reachable) and the same backends mapping
        otherwise.
    """
    adapters = cast(
        dict[str, BackendAdapter], request.app.state.adapters
    )
    names = sorted(adapters.keys())  # deterministic order in the response

    results = cast(
        list[dict[str, Any] | BaseException],
        await asyncio.gather(
            *(adapters[name].health() for name in names),
            return_exceptions=True,
        ),
    )

    backends: dict[str, Any] = {}
    any_ok = False
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            # Exception during health() — most commonly a NotSupportedError
            # from the MLX/DMR stub. Map to a uniform error payload.
            backends[name] = {
                "status": "error",
                "error": _short_error(result),
            }
        elif isinstance(result, dict):
            backends[name] = result
            if result.get("status") == "ok":
                any_ok = True
        else:
            # Defensive: an adapter returned something other than a dict.
            backends[name] = {
                "status": "error",
                "error": (
                    f"unexpected health() return type: {type(result).__name__}"
                ),
            }

    if any_ok:
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "backends": backends},
        )

    # All backends down — emit the OpenAI envelope WITH the per-backend
    # detail nested under a non-spec field so ops still sees what failed.
    envelope = make_error(
        type_="service_unavailable",
        message="No backends reachable",
        param=None,
        code="no_backends_reachable",
    )
    envelope["backends"] = backends  # spec-extension; ops ergonomics
    return JSONResponse(status_code=503, content=envelope)
