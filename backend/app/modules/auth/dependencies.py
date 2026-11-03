"""FastAPI dependencies provided by the auth module."""

import logging
from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.core.security import is_origin_allowed, request_origin
from app.modules.auth.errors import OriginNotAllowedError

logger = logging.getLogger(__name__)

# Rejected origins are attacker-controlled; cap what reaches the logs.
MAX_LOGGED_ORIGIN_LENGTH = 255


def require_allowed_origin(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """Reject the request unless its Origin (or, if absent, Referer) is in TRACEIQ_CORS_ORIGINS.

    A request with neither header is rejected: browsers always send Origin on these requests.
    """
    origin = request_origin(request.headers.get("origin"), request.headers.get("referer"))
    if not is_origin_allowed(origin, settings.cors_origins):
        logged_origin = origin[:MAX_LOGGED_ORIGIN_LENGTH] if origin else None
        logger.warning("Request origin rejected", extra={"path": request.url.path, "origin": logged_origin})
        raise OriginNotAllowedError()
