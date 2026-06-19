import asyncio
from typing import TYPE_CHECKING

import structlog
from watchfiles import awatch

from app.registry import RegistryError, load_registry

if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.config import Settings

_log = structlog.get_logger("app.registry_watcher")


def start_registry_watcher(
    app: "FastAPI",
    settings: "Settings",
) -> "asyncio.Task[None]":
    """Start a background asyncio task that watches the models YAML
    file and atomically swaps app.state.registry on every successful
    reload.

    Returns:
        The task handle. The lifespan caller is responsible for
        cancelling it on shutdown (cancel() + await; CancelledError
        is swallowed).
    """
    return asyncio.create_task(
        _watch_loop(app, settings),
        name="registry-watcher",
    )


async def _watch_loop(app: "FastAPI", settings: "Settings") -> None:
    """The actual watch loop. Cancellation is propagated naturally
    from awatch's async-iterator protocol — when the surrounding task
    is cancelled, the awatch generator's __aexit__ fires and the
    underlying watcher thread is stopped.
    """
    path = str(settings.models_yaml_path)
    _log.info("registry_watcher_started", path=path)

    try:
        async for _changes in awatch(
            path,
            # No stop_event needed — task cancellation handles shutdown.
            # awatch debounces by default (50ms step); no custom debouncing.
            recursive=False,
        ):
            # Any change triggers a reload attempt; change details unused.
            await _try_reload(app, path)
    except asyncio.CancelledError:
        _log.info("registry_watcher_cancelled")
        raise  # propagate cancellation to the lifespan caller.
    except Exception as exc:
        # Unexpected error from watchfiles (filesystem permission, etc.).
        # Log and exit the loop; the gateway keeps serving with the
        # registry it has.
        _log.error(
            "registry_watcher_crashed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise


async def _try_reload(app: "FastAPI", path: str) -> None:
    """Attempt to load the registry from disk. On success: atomic
    swap app.state.registry. On failure: log and keep the previous
    registry.
    """
    try:
        # load_registry is synchronous but cheap (small YAML, no
        # network). Called directly without to_thread because the YAML
        # files are small and reload happens at most once per ~50ms.
        new_registry = load_registry(path)
    except FileNotFoundError as exc:
        _log.warning(
            "registry_reload_failed",
            reason="file_not_found",
            path=path,
            error=str(exc),
        )
        return
    except RegistryError as exc:
        _log.warning(
            "registry_reload_failed",
            reason="parse_error",
            path=path,
            error=str(exc),
        )
        return
    except Exception as exc:
        # yaml.YAMLError, OSError, etc.
        _log.warning(
            "registry_reload_failed",
            reason="unexpected_error",
            path=path,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return

    # Atomic ref swap. Python attribute assignment on an object is
    # one bytecode op (STORE_ATTR); concurrent reads always see either
    # the old or the new reference, never a torn one.
    old_registry = app.state.registry
    app.state.registry = new_registry

    if new_registry.ids() == old_registry.ids():
        # No model-id change — common case for editor formatting changes.
        # Still log so ops can see the watcher is alive.
        _log.info(
            "registry_reloaded_unchanged",
            path=path,
            n_models=len(new_registry.models),
        )
    else:
        _log.info(
            "registry_reloaded",
            path=path,
            n_models=len(new_registry.models),
            old_ids=old_registry.ids(),
            new_ids=new_registry.ids(),
        )
