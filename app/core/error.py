"""Centralized API/domain error definitions and handler registration."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.runtime_config import ErrorRuntimeConfig


class DomainValidationError(ValueError):
    """Base class for domain validation errors."""


class ApplicationValidationError(DomainValidationError):
    """Raised when application payload validation fails."""


class JobOpeningValidationError(DomainValidationError):
    """Raised when job-opening payload validation fails."""


class ReferenceValidationError(DomainValidationError):
    """Raised when reference payload validation fails."""


def build_error_payload(
    *,
    code: str,
    message: str,
    details: object | None = None,
) -> dict[str, object]:
    """Build a consistent API error payload."""

    payload: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        payload["details"] = details
    return {"error": payload}


def register_exception_handlers(app: FastAPI, config: ErrorRuntimeConfig) -> None:
    """Register all standardized API exception handlers on the app."""

    @app.exception_handler(RequestValidationError)
    async def on_request_validation_error(
        _: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """Handle FastAPI request/body validation failures."""

        return JSONResponse(
            status_code=422,
            content=build_error_payload(
                code="request_validation_error",
                message=config.request_validation_message,
                details=exc.errors(),
            ),
        )

    @app.exception_handler(ApplicationValidationError)
    async def on_application_validation_error(
        _: Request,
        exc: ApplicationValidationError,
    ) -> JSONResponse:
        """Handle domain validation errors from applicant flows."""

        return JSONResponse(
            status_code=422,
            content=build_error_payload(
                code="application_validation_error",
                message=str(exc),
            ),
        )

    @app.exception_handler(JobOpeningValidationError)
    async def on_job_opening_validation_error(
        _: Request,
        exc: JobOpeningValidationError,
    ) -> JSONResponse:
        """Handle domain validation errors from job-opening flows."""

        return JSONResponse(
            status_code=422,
            content=build_error_payload(
                code="job_opening_validation_error",
                message=str(exc),
            ),
        )

    @app.exception_handler(ReferenceValidationError)
    async def on_reference_validation_error(
        _: Request,
        exc: ReferenceValidationError,
    ) -> JSONResponse:
        """Handle domain validation errors from reference flows."""

        return JSONResponse(
            status_code=422,
            content=build_error_payload(
                code="reference_validation_error",
                message=str(exc),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def on_http_exception(
        _: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        """Handle all HTTP exceptions with standardized format."""

        code_by_status = config.status_code_map
        details = exc.detail if isinstance(exc.detail, (list, dict)) else None
        if isinstance(exc.detail, (list, dict)):
            if exc.status_code == 422:
                message = config.request_validation_message
            else:
                message = config.http_error_message
        else:
            message = str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=build_error_payload(
                code=code_by_status.get(exc.status_code, "http_error"),
                message=message,
                details=details,
            ),
        )

    @app.exception_handler(Exception)
    async def on_unhandled_exception(_: Request, __: Exception) -> JSONResponse:
        """Handle unexpected server errors without leaking internals."""

        return JSONResponse(
            status_code=500,
            content=build_error_payload(
                code="internal_server_error",
                message=config.internal_error_message,
            ),
        )
