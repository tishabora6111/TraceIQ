"""Session cookies set by the auth API. Flags come from settings and are never weakened here.

* Refresh cookie: HttpOnly, restricted to the auth endpoints, so JavaScript never reads it and
  other API paths never receive it.
* CSRF cookie: readable by JavaScript (it sends the value back in ``X-CSRF-Token``), site-wide.
"""

from datetime import UTC, datetime

from fastapi import Response

from app.core.config import Settings

REFRESH_COOKIE_NAME = "traceiq_refresh"
CSRF_COOKIE_NAME = "traceiq_csrf"
# The auth router's mount point (API prefix + "/auth"); checked by the tests.
REFRESH_COOKIE_PATH = "/api/v1/auth"
CSRF_COOKIE_PATH = "/"


def set_session_cookies(
    response: Response,
    *,
    refresh_token: str,
    csrf_token: str,
    expires_at: datetime,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    """Set both cookies to expire with the refresh token."""
    max_age = max(0, int((expires_at - (now or datetime.now(UTC))).total_seconds()))
    common = {
        "max_age": max_age,
        "expires": expires_at,
        "domain": settings.auth_cookie_domain,
        "secure": settings.auth_cookie_secure,
        "samesite": settings.auth_cookie_samesite,
    }
    response.set_cookie(REFRESH_COOKIE_NAME, refresh_token, path=REFRESH_COOKIE_PATH, httponly=True, **common)
    response.set_cookie(CSRF_COOKIE_NAME, csrf_token, path=CSRF_COOKIE_PATH, httponly=False, **common)
