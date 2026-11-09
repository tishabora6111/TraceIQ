"""FastAPI application factory."""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.core.config import AuthConfigurationError, Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.modules.auth.api import router as auth_router
from app.modules.health.api import router as health_router
from app.modules.projects.api import router as projects_router

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API. Raises ``AuthConfigurationError`` if the JWT or CSRF secret is missing or weak,
    so the server never starts serving requests without valid signing secrets."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    settings.validate_auth_secrets()
    # Also enforced when settings are loaded; repeated for Settings objects built without validation.
    if settings.env == "production" and not settings.auth_cookie_secure:
        raise AuthConfigurationError("TRACEIQ_AUTH_COOKIE_SECURE must be true in production")
    logger = logging.getLogger(__name__)
    if not settings.cors_origins:
        logger.warning(
            "TRACEIQ_CORS_ORIGINS is empty: no browser origin is allowed, so login and registration"
            " requests will be rejected with 403"
        )

    app = FastAPI(title=settings.app_name, version=__version__)
    app.dependency_overrides[get_settings] = lambda: settings

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_error_handlers(app)

    # Operational endpoints live at the root; domain routers are mounted under API_PREFIX.
    app.include_router(health_router)
    app.include_router(auth_router, prefix=API_PREFIX)
    app.include_router(projects_router, prefix=API_PREFIX)

    logger.info("Application created", extra={"environment": settings.env})
    return app


app = create_app()
