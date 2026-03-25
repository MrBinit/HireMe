"""In-memory per-client rate limit middleware."""

from __future__ import annotations

import asyncio
import time
from collections import deque

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.error import build_error_payload


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Limit client request rate inside a rolling time window."""

    def __init__(
        self,
        app,
        *,
        window_seconds: int,
        max_requests: int,
        exempt_paths: list[str] | None = None,
        message: str = "rate limit exceeded",
        key_by_path: bool = True,
        trust_x_forwarded_for: bool = False,
        max_tracked_clients: int = 100_000,
        cleanup_interval_seconds: int = 30,
    ):
        """Initialize middleware with rate-limit settings."""

        super().__init__(app)
        self._window_seconds = window_seconds
        self._max_requests = max_requests
        self._exempt_paths = set(exempt_paths or [])
        self._message = message
        self._key_by_path = key_by_path
        self._trust_x_forwarded_for = trust_x_forwarded_for
        self._max_tracked_clients = max_tracked_clients
        self._cleanup_interval_seconds = cleanup_interval_seconds
        self._hits: dict[str, deque[float]] = {}
        self._last_seen: dict[str, float] = {}
        self._last_cleanup = time.monotonic()
        self._lock = asyncio.Lock()

    async def dispatch(self, request: Request, call_next) -> Response:
        """Apply rate limiting and continue request when allowed."""

        if request.url.path in self._exempt_paths:
            return await call_next(request)

        key = self._resolve_rate_key(request)
        allowed, retry_after = await self._is_allowed(key)
        if not allowed:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content=build_error_payload(
                    code="rate_limited",
                    message=self._message,
                ),
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)

    async def _is_allowed(self, key: str) -> tuple[bool, int]:
        """Return whether request is allowed and retry-after seconds."""

        now = time.monotonic()
        oldest_allowed = now - self._window_seconds

        async with self._lock:
            self._cleanup_if_due(now=now, oldest_allowed=oldest_allowed)

            queue = self._hits.get(key)
            if queue is None:
                if len(self._hits) >= self._max_tracked_clients:
                    return False, self._window_seconds
                queue = deque()
                self._hits[key] = queue

            while queue and queue[0] <= oldest_allowed:
                queue.popleft()

            if len(queue) >= self._max_requests:
                retry_after = max(1, int(self._window_seconds - (now - queue[0])))
                return False, retry_after

            queue.append(now)
            self._last_seen[key] = now
            return True, 0

    def _cleanup_if_due(self, *, now: float, oldest_allowed: float) -> None:
        """Trim stale buckets and bound in-memory key growth."""

        if now - self._last_cleanup < self._cleanup_interval_seconds:
            return
        self._last_cleanup = now

        stale_keys = []
        for key, queue in self._hits.items():
            while queue and queue[0] <= oldest_allowed:
                queue.popleft()
            if not queue:
                stale_keys.append(key)

        for key in stale_keys:
            self._hits.pop(key, None)
            self._last_seen.pop(key, None)

        if len(self._hits) <= self._max_tracked_clients:
            return

        overflow = len(self._hits) - self._max_tracked_clients
        oldest_keys = sorted(self._last_seen.items(), key=lambda item: item[1])[:overflow]
        for key, _ in oldest_keys:
            self._hits.pop(key, None)
            self._last_seen.pop(key, None)

    def _resolve_rate_key(self, request: Request) -> str:
        """Return a stable client key, optionally partitioned by route path."""

        client_key = self._resolve_client_key(request)
        if not self._key_by_path:
            return client_key
        return f"{client_key}:{request.url.path}"

    def _resolve_client_key(self, request: Request) -> str:
        """Return client identifier for rate-limit tracking."""

        if self._trust_x_forwarded_for:
            forwarded_for = request.headers.get("X-Forwarded-For", "")
            if forwarded_for:
                return forwarded_for.split(",")[0].strip()

        if request.client and request.client.host:
            return request.client.host
        return "unknown"
