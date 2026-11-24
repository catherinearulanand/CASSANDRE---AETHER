"""Error types and the single API error envelope {code,message,field_errors,correlation_id,retryable}."""
from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("aether")


class ApiError(Exception):
    status_code = 400
    code = "BAD_REQUEST"
    retryable = False

    def __init__(self, message: str, *, field_errors: dict[str, str] | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.field_errors = field_errors or {}
        if code:
            self.code = code


class ValidationFailed(ApiError):
    status_code, code = 422, "VALIDATION_FAILED"


class Forbidden(ApiError):
    status_code, code = 403, "FORBIDDEN"


class Unauthenticated(ApiError):
    status_code, code = 401, "UNAUTHENTICATED"


class NotFound(ApiError):
    """Also used for resources in another tenant: never reveal existence."""
    status_code, code = 404, "NOT_FOUND"


class Conflict(ApiError):
    status_code, code = 409, "CONFLICT"


class Blocked(ApiError):
    """Material action blocked by a gate (unknown rule, stale data, missing evidence...)."""
    status_code, code = 409, "BLOCKED"


class TooManyRequests(ApiError):
    status_code, code, retryable = 429, "RATE_LIMITED", True


class Unavailable(ApiError):
    status_code, code, retryable = 503, "UNAVAILABLE", True


class NotImplementedCapability(ApiError):
    status_code, code = 501, "NOT_IMPLEMENTED"


def correlation_id_of(request: Request) -> str:
    return getattr(request.state, "correlation_id", None) or str(uuid.uuid4())


def envelope(request: Request, status: int, code: str, message: str, field_errors=None, retryable=False):
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "field_errors": field_errors or {},
            "correlation_id": correlation_id_of(request),
            "retryable": retryable,
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(request: Request, exc: ApiError):
        return envelope(request, exc.status_code, exc.code, exc.message, exc.field_errors, exc.retryable)

    @app.exception_handler(RequestValidationError)
    async def _val(request: Request, exc: RequestValidationError):
        fe = {".".join(str(p) for p in e["loc"] if p != "body"): e["msg"] for e in exc.errors()}
        return envelope(request, 422, "VALIDATION_FAILED", "Request validation failed", fe)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return envelope(request, exc.status_code, f"HTTP_{exc.status_code}", str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # No stack trace to the client; full detail to the server log with the correlation id.
        log.exception("unhandled error cid=%s", correlation_id_of(request))
        return envelope(request, 500, "INTERNAL_ERROR", "Unexpected server error", retryable=False)
