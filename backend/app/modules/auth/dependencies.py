"""FastAPI dependencies provided by the auth module."""

import logging
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.security import (
    AccessTokenClaims,
    InvalidTokenError,
    decode_access_token,
    is_origin_allowed,
    request_origin,
)
from app.modules.auth import service
from app.modules.auth.errors import NotAuthenticatedError, OriginNotAllowedError
from app.modules.auth.schemas import UserRead

logger = logging.getLogger(__name__)

_BEARER_SCHEME = "bearer"


def get_bearer_token(request: Request) -> str:
    """The token from ``Authorization: Bearer <token>``. The scheme is case-insensitive (RFC 7235).

    Only the Authorization header is accepted: never cookies or query parameters.
    """
    header = request.headers.get("authorization")
    if header is None:
        raise NotAuthenticatedError()
    scheme, _, token = header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != _BEARER_SCHEME or not token or any(char.isspace() for char in token):
        logger.info("Authentication rejected", extra={"reason": "malformed_authorization_header"})
        raise NotAuthenticatedError()
    return token


def get_access_token_claims(
    token: Annotated[str, Depends(get_bearer_token)], settings: Annotated[Settings, Depends(get_settings)]
) -> AccessTokenClaims:
    """Verified claims: signature (HS256 only), issuer, audience, expiry, not-before and type."""
    try:
        return decode_access_token(token, settings=settings)
    except InvalidTokenError:
        logger.info("Authentication rejected", extra={"reason": "invalid_token"})
        raise NotAuthenticatedError() from None


def get_current_user(
    claims: Annotated[AccessTokenClaims, Depends(get_access_token_claims)],
    session: Annotated[Session, Depends(get_db)],
) -> UserRead:
    """The authenticated, active user whose session is still valid; otherwise 401."""
    return service.get_authenticated_user(session, user_id=claims.user_id, session_id=claims.session_id)


CurrentUser = Annotated[UserRead, Depends(get_current_user)]

# Rejected origins are attacker-controlled; cap what reaches the logs.
MAX_LOGGED_ORIGIN_LENGTH = 255


# Methods that must not change state (RFC 9110). Every other method counts as a write.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def require_allowed_origin(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """Reject the request unless its Origin (or, if absent, Referer) is in TRACEIQ_CORS_ORIGINS.

    A request with neither header is rejected: browsers always send Origin on these requests.
    """
    _check_origin(request, settings)


def require_allowed_origin_for_writes(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> None:
    """``require_allowed_origin`` for every state-changing request (any method except GET, HEAD,
    OPTIONS and TRACE); safe methods pass unchecked.

    Intended as a router-level dependency for JWT-authenticated routers, so that every write route,
    including ones added later, gets Origin validation (ARCHITECTURE §7.2)::

        router = APIRouter(dependencies=[Depends(require_allowed_origin_for_writes)])

    It is an addition to authentication, never a replacement: routes still declare ``CurrentUser``.
    Not for API-key ingestion routers, which serve non-browser clients.
    """
    if request.method.upper() not in SAFE_METHODS:
        _check_origin(request, settings)


def _check_origin(request: Request, settings: Settings) -> None:
    origin = request_origin(request.headers.get("origin"), request.headers.get("referer"))
    if not is_origin_allowed(origin, settings.cors_origins):
        logged_origin = origin[:MAX_LOGGED_ORIGIN_LENGTH] if origin else None
        logger.warning("Request origin rejected", extra={"path": request.url.path, "origin": logged_origin})
        raise OriginNotAllowedError()
