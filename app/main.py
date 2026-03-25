"""FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.error import register_exception_handlers
from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.infra.database import init_db_schema
from app.middleware import RateLimitMiddleware, RequestTimeoutMiddleware, SecurityHeadersMiddleware


def create_app() -> FastAPI:
    """Create and configure the FastAPI app instance."""

    runtime_config = get_runtime_config()
    settings = get_settings()

    if runtime_config.security.enabled and not settings.admin_jwt_secret:
        raise RuntimeError("ADMIN_JWT_SECRET is required when security.enabled=true")

    @asynccontextmanager
    async def app_lifespan(_: FastAPI):
        """Initialize runtime resources and schema on startup."""

        if (
            runtime_config.storage.backend == "postgres"
            and runtime_config.storage.auto_create_tables
        ):
            await init_db_schema(runtime_config.postgres)
        yield

    app = FastAPI(
        title=runtime_config.api.title,
        version=runtime_config.api.version,
        description=runtime_config.api.description,
        lifespan=app_lifespan,
    )

    if runtime_config.cors.enabled:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=runtime_config.cors.allow_origins,
            allow_methods=runtime_config.cors.allow_methods,
            allow_headers=runtime_config.cors.allow_headers,
            allow_credentials=runtime_config.cors.allow_credentials,
            expose_headers=runtime_config.cors.expose_headers,
            max_age=runtime_config.cors.max_age_seconds,
        )

    if runtime_config.security_headers.enabled:
        app.add_middleware(
            SecurityHeadersMiddleware,
            config=runtime_config.security_headers,
        )

    if runtime_config.timeout.enabled:
        app.add_middleware(
            RequestTimeoutMiddleware,
            timeout_seconds=runtime_config.timeout.seconds,
            message=runtime_config.timeout.message,
            exempt_paths=runtime_config.timeout.exempt_paths,
        )

    if runtime_config.rate_limit.enabled:
        app.add_middleware(
            RateLimitMiddleware,
            window_seconds=runtime_config.rate_limit.window_seconds,
            max_requests=runtime_config.rate_limit.max_requests,
            exempt_paths=runtime_config.rate_limit.exempt_paths,
            message=runtime_config.rate_limit.message,
            key_by_path=runtime_config.rate_limit.key_by_path,
            trust_x_forwarded_for=runtime_config.rate_limit.trust_x_forwarded_for,
            max_tracked_clients=runtime_config.rate_limit.max_tracked_clients,
            cleanup_interval_seconds=runtime_config.rate_limit.cleanup_interval_seconds,
        )

    app.include_router(api_router, prefix="/api/v1")
    register_exception_handlers(app, runtime_config.error)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        """Health probe endpoint."""

        return {"status": "ok"}

    return app


app = create_app()
