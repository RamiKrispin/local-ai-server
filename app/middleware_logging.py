import json
import logging
import time
from typing import Any, Awaitable, Callable, cast

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_log = structlog.get_logger("app.middleware_logging")

# Paths whose JSON request body we attempt to parse for `model`.
# Anything else: model = None.
_BODY_INSPECT_PATHS: frozenset[str] = frozenset(
    {
        "/v1/chat/completions",
        "/v1/embeddings",
    }
)

_MAX_CACHED_BODY: int = 1024 * 1024  # 1 MiB body cache cap
_MAX_CAPTURED_RESPONSE: int = 256 * 1024  # 256 KiB response capture cap


class _BodyHolder:
    __slots__ = ("body", "complete")

    def __init__(self) -> None:
        self.body: bytes = b""
        self.complete: bool = False


class _StatusHolder:
    __slots__ = (
        "status",
        "is_streaming",
        "first_body_chunk",
        "last_body_chunk",
    )

    def __init__(self) -> None:
        self.status: int | None = None
        self.is_streaming: bool = False
        self.first_body_chunk: bytes = b""
        self.last_body_chunk: bytes = b""


def _extract_model(path: str, body: bytes) -> str | None:
    if path not in _BODY_INSPECT_PATHS or not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, dict):
            value = parsed.get("model")
            return value if isinstance(value, str) else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return None


def _extract_stream(path: str, body: bytes) -> bool | None:
    if path != "/v1/chat/completions" or not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, dict):
            value = parsed.get("stream", False)
            return bool(value) if isinstance(value, bool) else False
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return False


def _extract_streaming_usage(
    last_chunk: bytes,
) -> dict[str, int | None]:
    out: dict[str, int | None] = {
        "prompt_tokens": None,
        "completion_tokens": None,
    }
    text = last_chunk.decode("utf-8", errors="replace")
    # Find the LAST "data: " line that is not [DONE].
    candidate: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]" or not payload:
            continue
        candidate = payload
    if candidate is None:
        return out
    try:
        parsed = json.loads(candidate)
        usage = parsed.get("usage") if isinstance(parsed, dict) else None
        if isinstance(usage, dict):
            pt = usage.get("prompt_tokens")
            ct = usage.get("completion_tokens")
            if isinstance(pt, int):
                out["prompt_tokens"] = pt
            if isinstance(ct, int):
                out["completion_tokens"] = ct
    except json.JSONDecodeError:
        pass
    return out


def _extract_usage(path: str, holder: _StatusHolder) -> dict[str, int | None]:
    out: dict[str, int | None] = {
        "prompt_tokens": None,
        "completion_tokens": None,
    }
    if not holder.last_body_chunk:
        return out
    if holder.is_streaming:
        return _extract_streaming_usage(holder.last_body_chunk)
    # Non-streaming JSON.
    try:
        parsed = json.loads(holder.last_body_chunk.decode("utf-8"))
        usage = parsed.get("usage") if isinstance(parsed, dict) else None
        if isinstance(usage, dict):
            pt = usage.get("prompt_tokens")
            ct = usage.get("completion_tokens")
            if isinstance(pt, int):
                out["prompt_tokens"] = pt
            if isinstance(ct, int):
                out["completion_tokens"] = ct
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return out


def _resolve_backend(scope: Scope, model: str | None) -> str | None:
    if model is None:
        return None
    # scope["app"] is the FastAPI instance (Starlette sets this on http
    # scopes that flow through a Starlette router). app.state.registry
    # is the current registry reference; reads are atomic.
    from app.registry import Registry

    app = scope.get("app")
    if app is None:
        return None
    registry = cast(Registry | None, getattr(app.state, "registry", None))
    if registry is None:
        return None
    found = registry.get(model)
    return found.backend if found is not None else None


class RequestLoggingMiddleware:
    """ASGI middleware that emits one structlog `request` event per HTTP
    request. Mounted BEFORE BearerAuthMiddleware (added first in
    create_app so it ends up outermost) so timing includes auth time.
    Reads `key_prefix` from scope state in its FINALLY block, after auth
    has populated it.

    See architecture §6 for mount-order rationale.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app: ASGIApp = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_time = time.perf_counter()
        path: str = cast(str, scope["path"])
        method: str = cast(str, scope.get("method", "?"))

        body_holder = _BodyHolder()
        status_holder = _StatusHolder()

        # Body cache — only for POST requests to inspectable paths to
        # avoid reading/replaying bodies for health probes etc.
        if path in _BODY_INSPECT_PATHS and method == "POST":
            body_chunks: list[bytes] = []
            total_size = 0
            abandoned = False
            abandoned_message: Message | None = None  # non-http.request msg

            while True:
                message = await receive()
                if message["type"] != "http.request":
                    # Unexpected message (e.g. http.disconnect).
                    # Abandon the cache; stash the message so we can
                    # replay it to the downstream app.
                    abandoned = True
                    abandoned_message = message
                    break
                chunk = message.get("body", b"")
                total_size += len(chunk)
                if total_size > _MAX_CACHED_BODY:
                    # DOS guard: abandon cache.  Any bytes already read
                    # must still be replayed so downstream is not starved.
                    body_chunks.append(chunk)
                    abandoned = True
                    break
                body_chunks.append(chunk)
                if not message.get("more_body", False):
                    break

            if abandoned:
                # Build a replay shim that re-emits every consumed byte
                # before delegating to the upstream receive.  This ensures
                # the downstream router never sees a truncated body.
                replay_list: list[Message] = []
                if body_chunks:
                    replay_list.append(
                        {
                            "type": "http.request",
                            "body": b"".join(body_chunks),
                            # We don't know whether upstream had more; set
                            # more_body=True so downstream keeps reading.
                            "more_body": True,
                        }
                    )
                if abandoned_message is not None:
                    replay_list.append(abandoned_message)
                replay_iter = iter(replay_list)

                async def abandon_receive() -> Message:
                    try:
                        return next(replay_iter)
                    except StopIteration:
                        return await receive()

                wrapped_receive: Callable[[], Awaitable[Message]] = (
                    abandon_receive
                )
            else:
                full_body = b"".join(body_chunks)
                body_holder.body = full_body

                replayed = False

                async def replay_receive() -> Message:
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {
                            "type": "http.request",
                            "body": full_body,
                            "more_body": False,
                        }
                    # Subsequent calls delegate to the original receive.
                    return await receive()

                wrapped_receive = replay_receive
        else:
            wrapped_receive = receive

        async def capturing_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder.status = message.get("status", 500)
                for name, value in message.get("headers", []):
                    if name.lower() == b"content-type":
                        if b"text/event-stream" in value.lower():
                            status_holder.is_streaming = True
                        break
            elif message["type"] == "http.response.body":
                chunk: bytes = message.get("body", b"")
                if not status_holder.is_streaming:
                    if (
                        len(status_holder.last_body_chunk) + len(chunk)
                        <= _MAX_CAPTURED_RESPONSE
                    ):
                        status_holder.last_body_chunk += chunk
                    else:
                        # Abandon response capture.
                        status_holder.last_body_chunk = b""
                else:
                    # Streaming: retain only the most recent non-empty chunk.
                    if chunk.strip():
                        status_holder.last_body_chunk = chunk
            await send(message)

        try:
            await self.app(scope, wrapped_receive, capturing_send)
        finally:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            state_dict = cast(
                dict[str, Any], scope.get("state") or {}
            )
            key_prefix: str | None = cast(
                str | None, state_dict.get("key_prefix")
            )
            model = _extract_model(path, body_holder.body)
            backend = _resolve_backend(scope, model)
            stream_flag = _extract_stream(path, body_holder.body)
            usage = _extract_usage(path, status_holder)

            try:
                _log.info(
                    "request",
                    path=path,
                    method=method,
                    status=status_holder.status,
                    latency_ms=latency_ms,
                    key_prefix=key_prefix,
                    model=model,
                    backend=backend,
                    stream=stream_flag,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                )
            except Exception as exc:  # noqa: BLE001 — defense in depth
                try:
                    # Last-ditch: bypass structlog and use the stdlib root
                    # logger directly. This also flows through the bridge,
                    # so it's still JSON, but it eliminates dependency on
                    # any custom processor that may have raised.
                    logging.getLogger("app.middleware_logging").error(
                        "request_log_failed",
                        extra={"err": str(exc)},
                    )
                except Exception:
                    pass  # truly nothing we can do; never raise upward
