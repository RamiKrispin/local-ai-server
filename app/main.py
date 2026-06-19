import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters import build_adapters
from app.auth import BearerAuthMiddleware
from app.config import get_settings
from app.errors import install_exception_handlers
from app.registry import load_registry
from app.routers.chat import router as chat_router
from app.routers.embeddings import router as embeddings_router
from app.routers.health import router as health_router
from app.routers.models import router as models_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("app.main")

    registry = load_registry(settings.models_yaml_path)
    app.state.registry = registry
    app.state.settings = settings

    adapters = build_adapters(registry, settings)
    app.state.adapters = adapters
    log.info(
        "registry loaded: %d models (%s); adapters: %s",
        len(registry.models),
        ", ".join(registry.ids()),
        ", ".join(sorted(adapters.keys())),
    )

    try:
        yield
    finally:
        _keys = sorted(app.state.adapters.keys())
        results = await asyncio.gather(
            *(app.state.adapters[k].close() for k in _keys),
            return_exceptions=True,
        )
        for backend, result in zip(_keys, results):
            if isinstance(result, BaseException):
                log.warning(
                    "adapter close failed for %s: %s",
                    backend,
                    result,
                )
        log.info("gateway shutdown complete")


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
