"""Tests for request-timeout and rate-limit middleware."""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_timeout import RequestTimeoutMiddleware


def _request(path: str = "/api/v1/applications") -> Request:
    """Build a minimal ASGI request for middleware testing."""

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_rate_limit_blocks_after_threshold() -> None:
    """Middleware should reject requests after reaching max_requests."""

    async def run() -> None:
        middleware = RateLimitMiddleware(
            app=FastAPI(),
            window_seconds=60,
            max_requests=2,
            exempt_paths=[],
        )

        allowed_1, _ = await middleware._is_allowed("127.0.0.1")
        allowed_2, _ = await middleware._is_allowed("127.0.0.1")
        allowed_3, retry_after = await middleware._is_allowed("127.0.0.1")

        assert allowed_1 is True
        assert allowed_2 is True
        assert allowed_3 is False
        assert retry_after >= 1

    asyncio.run(run())


def test_rate_limit_response_is_standardized() -> None:
    """429 payload should follow the standardized error structure."""

    async def run() -> None:
        middleware = RateLimitMiddleware(
            app=FastAPI(),
            window_seconds=60,
            max_requests=1,
            exempt_paths=[],
        )

        async def ok_call_next(_: Request):
            return JSONResponse({"ok": True})

        first = await middleware.dispatch(_request(), ok_call_next)
        second = await middleware.dispatch(_request(), ok_call_next)

        assert first.status_code == 200
        assert second.status_code == 429
        payload = json.loads(second.body.decode("utf-8"))
        assert payload["error"]["code"] == "rate_limited"
        assert payload["error"]["message"] == "rate limit exceeded"

    asyncio.run(run())


def test_request_timeout_returns_504() -> None:
    """Timeout middleware should return HTTP 504 on long handlers."""

    async def run() -> None:
        middleware = RequestTimeoutMiddleware(
            app=FastAPI(),
            timeout_seconds=0.01,
            message="request timed out",
            exempt_paths=[],
        )

        async def slow_call_next(_: Request):
            await asyncio.sleep(0.05)
            return JSONResponse({"ok": True})

        response = await middleware.dispatch(_request(), slow_call_next)
        assert response.status_code == 504
        payload = json.loads(response.body.decode("utf-8"))
        assert payload["error"]["code"] == "request_timeout"
        assert payload["error"]["message"] == "request timed out"

    asyncio.run(run())
