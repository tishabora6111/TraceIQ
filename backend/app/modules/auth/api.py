from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, Response, status
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.modules.auth import service
from app.modules.auth.cookies import (
    CSRF_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    clear_session_cookies,
    set_session_cookies,
)
from app.modules.auth.dependencies import CurrentUser, require_allowed_origin
from app.modules.auth.schemas import LoginRequest, LoginResponse, RegisterRequest, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])

DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
# Cookie-authenticated endpoints read the refresh token only from its HttpOnly cookie, and the
# CSRF token from both its cookie and the X-CSRF-Token header (signed double-submit).
# The cookies are hidden from the OpenAPI schema: browsers send them automatically and never let
# page scripts (including Swagger UI) set a Cookie header, so input fields for them would do nothing.
RefreshCookie = Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME, include_in_schema=False)]
CsrfCookie = Annotated[str | None, Cookie(alias=CSRF_COOKIE_NAME, include_in_schema=False)]
CsrfHeader = Annotated[
    str | None,
    Header(
        alias="X-CSRF-Token",
        description=(
            "The exact `csrf_token` value from the latest login or refresh response (also the value of the"
            " `traceiq_csrf` cookie). It must match the cookie. In Swagger UI, paste the whole value without"
            " quotes; it changes on every refresh."
        ),
    ),
]


def _session_response(response: Response, result: service.LoginResult, settings: Settings) -> LoginResponse:
    set_session_cookies(
        response,
        refresh_token=result.refresh_token,
        csrf_token=result.csrf_token,
        expires_at=result.refresh_token_expires_at,
        settings=settings,
    )
    response.headers["Cache-Control"] = "no-store"
    return LoginResponse(
        access_token=result.access_token,
        expires_in=settings.access_token_ttl_seconds,
        csrf_token=result.csrf_token,
        user=result.user,
    )


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserRead,
    dependencies=[Depends(require_allowed_origin)],
)
def register(body: RegisterRequest, session: DbSession, settings: AppSettings) -> UserRead:
    """Create an account. Does not log in; call /login next."""
    return service.register_user(
        session, email=body.email, password=body.password.get_secret_value(), settings=settings
    )


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(require_allowed_origin)])
def login(body: LoginRequest, response: Response, session: DbSession, settings: AppSettings) -> LoginResponse:
    """Start a session: access token in the body, refresh and CSRF tokens as cookies."""
    result = service.login(session, email=body.email, password=body.password.get_secret_value(), settings=settings)
    return _session_response(response, result, settings)


@router.post("/refresh", response_model=LoginResponse, dependencies=[Depends(require_allowed_origin)])
def refresh(
    response: Response,
    session: DbSession,
    settings: AppSettings,
    refresh_token: RefreshCookie = None,
    csrf_cookie: CsrfCookie = None,
    csrf_header: CsrfHeader = None,
) -> LoginResponse:
    """Rotate the refresh-token cookie and return a new access token and CSRF token.

    Requires an allowed Origin and the CSRF token in both cookie and X-CSRF-Token header.
    Reusing an already rotated refresh token revokes the whole session.
    """
    result = service.refresh_session(
        session,
        refresh_token=refresh_token,
        csrf_header=csrf_header,
        csrf_cookie=csrf_cookie,
        settings=settings,
    )
    return _session_response(response, result, settings)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_allowed_origin)])
def logout(
    response: Response,
    session: DbSession,
    settings: AppSettings,
    refresh_token: RefreshCookie = None,
    csrf_cookie: CsrfCookie = None,
    csrf_header: CsrfHeader = None,
) -> None:
    """End the current session and clear its cookies. Safe to repeat.

    Requires an allowed Origin; when a known session is presented, also the CSRF token.
    """
    service.logout(
        session,
        refresh_token=refresh_token,
        csrf_header=csrf_header,
        csrf_cookie=csrf_cookie,
        settings=settings,
    )
    clear_session_cookies(response, settings=settings)
    response.headers["Cache-Control"] = "no-store"


@router.get("/me", response_model=UserRead)
def me(user: CurrentUser, response: Response) -> UserRead:
    """The authenticated user. Requires a valid bearer access token whose session is still active."""
    response.headers["Cache-Control"] = "no-store"
    return user
