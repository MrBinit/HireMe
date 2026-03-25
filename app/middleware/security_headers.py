"""Middleware for baseline secure HTTP response headers."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.runtime_config import SecurityHeadersRuntimeConfig


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach configurable security headers on every HTTP response."""

    def __init__(self, app, *, config: SecurityHeadersRuntimeConfig):
        """Initialize with header values from runtime config."""

        super().__init__(app)
        self._config = config
        self._csp_exempt_paths = set(config.csp_exempt_paths)

    async def dispatch(self, request, call_next) -> Response:
        """Set security headers after downstream handlers return."""

        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", self._config.x_content_type_options)
        response.headers.setdefault("X-Frame-Options", self._config.x_frame_options)
        response.headers.setdefault("Referrer-Policy", self._config.referrer_policy)
        if request.url.path not in self._csp_exempt_paths:
            response.headers.setdefault(
                "Content-Security-Policy", self._config.content_security_policy
            )

        if self._config.include_hsts:
            hsts = f"max-age={self._config.hsts_max_age_seconds}"
            if self._config.hsts_include_subdomains:
                hsts += "; includeSubDomains"
            if self._config.hsts_preload:
                hsts += "; preload"
            response.headers.setdefault("Strict-Transport-Security", hsts)

        return response
