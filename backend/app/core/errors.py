"""Domain exceptions and their mapping to consistent JSON error responses.

Error body shape::

    {"error": {"code": "not_found", "message": "...", "details": ...}}
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import database_error_metadata, find_database_error

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for domain errors raised by services."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"
    # Extra response headers, e.g. WWW-Authenticate for 401 responses.
    headers: dict[str, str] | None = None

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class UnauthorizedError(AppError):
    """Missing or invalid credentials (401). Messages must not reveal which credential was wrong."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"
    headers = {"WWW-Authenticate": "Bearer"}


class ForbiddenError(AppError):
    """Authenticated (or credential-free) but not allowed (403), e.g. failed CSRF or Origin checks."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"


def _error_response(
    status_code: int, code: str, message: str, details: Any = None, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status_code, content={"error": body}, headers=dict(headers) if headers else None)


async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    logger.info("Request failed", extra={"path": request.url.path, "error_code": exc.code})
    return _error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def _handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return _error_response(exc.status_code, "http_error", str(exc.detail), headers=exc.headers)


# Only these fields of a Pydantic validation error are returned. "input" (the submitted value,
# e.g. a password), "ctx" (which can embed values), and "url" are dropped.
_VALIDATION_ERROR_FIELDS = ("type", "loc", "msg")


def sanitize_validation_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {field: _json_safe(error[field]) for field in _VALIDATION_ERROR_FIELDS if field in error} for error in errors
    ]


def _json_safe(value: Any) -> Any:
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    return value if isinstance(value, str | int | float | bool) or value is None else str(value)


async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_response(
        422,
        "validation_error",
        "Request validation failed.",
        sanitize_validation_errors(exc.errors()),
    )


def _internal_error_response() -> JSONResponse:
    return _error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", "Internal server error.")


async def _handle_database_error(request: Request, exc: DBAPIError) -> JSONResponse:
    """Log only safe metadata: the message can contain row values (e.g. a password hash or email)."""
    logger.error("Unhandled database error", extra={"path": request.url.path, **database_error_metadata(exc)})
    return _internal_error_response()


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    database_error = find_database_error(exc)
    if database_error is not None:
        # A database error wrapped in another exception: no traceback, which would embed its message.
        logger.error(
            "Unhandled error",
            extra={"path": request.url.path, "error_type": type(exc).__name__, **database_error_metadata(database_error)},
        )
    else:
        logger.exception("Unhandled error", extra={"path": request.url.path})
    return _internal_error_response()


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _handle_http_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _handle_validation_error)  # type: ignore[arg-type]
    # Handled here rather than by the catch-all, which Starlette re-raises for the server to log.
    app.add_exception_handler(DBAPIError, _handle_database_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _handle_unexpected_error)
