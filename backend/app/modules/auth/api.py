from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.modules.auth import service
from app.modules.auth.cookies import set_session_cookies
from app.modules.auth.dependencies import require_allowed_origin
from app.modules.auth.schemas import LoginRequest, LoginResponse, RegisterRequest, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])

DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


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
