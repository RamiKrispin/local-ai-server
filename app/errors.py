import logging
import traceback
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.adapters.base import NotSupportedError

_log = logging.getLogger("app.errors")


class ErrorBody(BaseModel):
    type: str
    message: str
    param: str | None = None
    code: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


def make_error(
    *,
    type_: str,
    message: str,
    param: str | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    """Return a dict matching the OpenAI error envelope shape."""
    body: dict[str, Any] = {
        "type": type_,
        "message": message,
        "param": param,
        "code": code,
    }
    return {"error": body}


def error_response(
    *,
    status_code: int,
    type_: str,
    message: str,
    param: str | None = None,
    code: str | None = None,
) -> JSONResponse:
    """Return a JSONResponse with the OpenAI error envelope."""
    return JSONResponse(
        status_code=status_code,
        content=make_error(
            type_=type_,
            message=message,
            param=param,
            code=code,
        ),
    )


async def http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    """Translate HTTPException -> JSON response.

    When detail is a dict (e.g. from make_error()), emit it directly as
    the response body so the OpenAI envelope shape is preserved.
    FastAPI's built-in handler would nest it under {"detail": ...}.
    """
    if isinstance(exc.detail, dict):
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


async def not_supported_handler(
    request: Request, exc: NotSupportedError
) -> JSONResponse:
    """Translate NotSupportedError -> HTTP 501 with the envelope."""
    return error_response(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        type_="not_supported",
        message=exc.message,
        param=exc.param,
        code=exc.code,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Translate pydantic ValidationError -> HTTP 422 with the envelope.
    The param field carries the dotted location of the first invalid field."""
    errors = exc.errors()
    param: str | None = None
    if errors:
        loc = errors[0].get("loc", ())
        if loc:
            param = ".".join(str(p) for p in loc)
    message = errors[0].get("msg", "Invalid request") if errors else (
        "Invalid request"
    )
    return error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        type_="invalid_request_error",
        message=message,
        param=param,
        code=None,
    )


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Catch-all -> HTTP 500 with type='internal_error', code='internal_error'.
    Logs traceback via stdlib logging; never leaks internals into message."""
    _log.error(
        "Unhandled exception for %s %s\n%s",
        request.method,
        request.url,
        traceback.format_exc(),
    )
    return error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        type_="internal_error",
        message="Internal server error",
        code="internal_error",
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Register all handlers on the FastAPI app."""
    app.add_exception_handler(
        HTTPException, http_exception_handler  # type: ignore[arg-type]
    )
    app.add_exception_handler(
        NotSupportedError, not_supported_handler  # type: ignore[arg-type]
    )
    app.add_exception_handler(
        RequestValidationError,
        validation_exception_handler,  # type: ignore[arg-type]
    )
    # Catch-all must be last — order matters in Starlette.
    app.add_exception_handler(
        Exception, unhandled_exception_handler  # type: ignore[arg-type]
    )
