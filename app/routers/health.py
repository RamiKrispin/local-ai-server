from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    """Return {'status': 'ok'} with HTTP 200. No auth, no upstream pings."""
    return {"status": "ok"}
