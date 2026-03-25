"""Tests for security response headers middleware."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.runtime_config import SecurityHeadersRuntimeConfig
from app.middleware.security_headers import SecurityHeadersMiddleware


def _request(path: str = "/health") -> Request:
    """Build a minimal ASGI request for middleware testing."""

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_security_headers_are_attached() -> None:
    """Middleware should attach baseline secure headers to responses."""

    async def run() -> None:
        middleware = SecurityHeadersMiddleware(
            app=FastAPI(),
            config=SecurityHeadersRuntimeConfig(),
        )

        async def ok_call_next(_: Request):
            return JSONResponse({"ok": True})

        response = await middleware.dispatch(_request(), ok_call_next)
        assert response.status_code == 200
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "Content-Security-Policy" in response.headers

    asyncio.run(run())


def test_csp_is_exempted_for_docs_path() -> None:
    """CSP header should be omitted for docs path to allow Swagger assets."""

    async def run() -> None:
        middleware = SecurityHeadersMiddleware(
            app=FastAPI(),
            config=SecurityHeadersRuntimeConfig(csp_exempt_paths=["/docs"]),
        )

        async def ok_call_next(_: Request):
            return JSONResponse({"ok": True})

        response = await middleware.dispatch(_request("/docs"), ok_call_next)
        assert response.status_code == 200
        assert "Content-Security-Policy" not in response.headers
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    asyncio.run(run())
