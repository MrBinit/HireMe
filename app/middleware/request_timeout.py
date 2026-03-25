"""Middleware that enforces a global request timeout."""

from __future__ import annotations

import asyncio

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.error import build_error_payload


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    """Abort requests that exceed the configured duration."""

    def __init__(
        self,
        app,
        *,
        timeout_seconds: float,
        message: str = "request timed out",
        exempt_paths: list[str] | None = None,
    ):
        """Initialize middleware with timeout threshold in seconds."""

        super().__init__(app)
        self._timeout_seconds = timeout_seconds
        self._message = message
        self._exempt_paths = set(exempt_paths or [])

    async def dispatch(self, request: Request, call_next) -> Response:
        """Process request and return 504 if processing times out."""

        if request.url.path in self._exempt_paths:
            return await call_next(request)

        try:
            return await asyncio.wait_for(
                call_next(request),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                content=build_error_payload(
                    code="request_timeout",
                    message=self._message,
                ),
            )
