import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.adapters import build_adapters
from app.auth import BearerAuthMiddleware
from app.config import get_settings
from app.errors import install_exception_handlers
from app.logging import configure_structlog
from app.middleware_logging import RequestLoggingMiddleware
from app.registry import load_registry
from app.registry_watcher import start_registry_watcher
from app.routers.chat import router as chat_router
from app.routers.embeddings import router as embeddings_router
from app.routers.health import router as health_router
from app.routers.models import router as models_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_structlog(settings.log_level)  # replaces basicConfig
    log = structlog.get_logger("app.main")  # replaces logging.getLogger

    registry = load_registry(settings.models_yaml_path)
    app.state.registry = registry
    app.state.settings = settings

    adapters = build_adapters(registry, settings)
    app.state.adapters = adapters
    log.info(
        "registry_loaded",
        n_models=len(registry.models),
        ids=registry.ids(),
        adapters=sorted(adapters.keys()),
    )

    # Start the registry watcher.
    watcher_task: asyncio.Task[None] = start_registry_watcher(app, settings)

    try:
        yield
    finally:
        # Cancel + await the watcher first; it must NOT block adapter close.
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass

        # Adapter close — unchanged from Phase 1.
        _keys = sorted(app.state.adapters.keys())
        results = await asyncio.gather(
            *(app.state.adapters[k].close() for k in _keys),
            return_exceptions=True,
        )
        for backend, result in zip(_keys, results):
            if isinstance(result, BaseException):
                log.warning(
                    "adapter_close_failed",
                    backend=backend,
                    error=str(result),
                )
        log.info("gateway_shutdown_complete")


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()
    app = FastAPI(
        title="local-ai-server",
        version="0.2.0",
        description=(
            "OpenAI-compatible local AI gateway. v0.1.0: Ollama "
            "wired; MLX and Docker Model Runner stubbed at 501."
        ),
        lifespan=lifespan,
    )
    install_exception_handlers(app)
    # NOTE: add_middleware order is REVERSED relative to execution order
    # (Starlette wraps in LIFO). RequestLoggingMiddleware is added FIRST
    # so it ends up OUTERMOST and times the full request including auth.
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(
        BearerAuthMiddleware,
        keys_db_path=settings.keys_db_path,
    )
    app.include_router(health_router)
    app.include_router(models_router, prefix="/v1")
    app.include_router(chat_router, prefix="/v1")
    app.include_router(embeddings_router, prefix="/v1")
    return app


app = create_app()
