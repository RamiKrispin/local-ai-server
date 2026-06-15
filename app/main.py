import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.errors import install_exception_handlers
from app.registry import load_registry
from app.routers.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the registry once at startup. Phase 2 will also build the
    adapter dict here and close httpx clients on shutdown."""
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("app.main")

    registry = load_registry(settings.models_yaml_path)
    app.state.registry = registry
    app.state.settings = settings
    # Phase 2: app.state.adapters = {"ollama": OllamaAdapter(...), ...}
    log.info(
        "registry loaded: %d models (%s)",
        len(registry.models),
        ", ".join(registry.ids()),
    )

    try:
        yield
    finally:
        # Phase 2: await asyncio.gather(
        #     *(a.close() for a in app.state.adapters.values())
        # )
        log.info("gateway shutdown complete")


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="local-ai-server",
        version="0.1.0",
        description=(
            "OpenAI-compatible local AI gateway. v0.1.0 ships the "
            "skeleton + healthz; Phase 2 wires Ollama."
        ),
        lifespan=lifespan,
    )
    install_exception_handlers(app)
    app.include_router(health_router)
    return app


app = create_app()
