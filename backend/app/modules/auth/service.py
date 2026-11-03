"""Public interface of the auth module.

Each function manages its own transaction: it commits on success and rolls back on any exception.
Never logs emails, passwords, or tokens; log lines carry only user and session ids.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import database_error_metadata
from app.core.security import (
    PasswordPolicyError,
    create_access_token,
    create_csrf_token,
    generate_opaque_token,
    hash_opaque_token,
    hash_password,
    verify_dummy_password,
    verify_password,
)
from app.modules.auth import repository
from app.modules.auth.errors import (
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidPasswordError,
    RegistrationDisabledError,
)
from app.modules.auth.schemas import UserRead, normalize_email

logger = logging.getLogger(__name__)

_EMAIL_UNIQUE_CONSTRAINT = "uq_users_email"


@dataclass(frozen=True)
class LoginResult:
    """A new session. Token values are excluded from the repr so they cannot leak into logs."""

    user: UserRead
    session_id: uuid.UUID
    access_token: str = field(repr=False)
    access_token_expires_at: datetime
    refresh_token: str = field(repr=False)
    refresh_token_expires_at: datetime
    csrf_token: str = field(repr=False)


def register_user(session: Session, *, email: str, password: str, settings: Settings | None = None) -> UserRead:
    """Create an active user and commit.

    Raises ``RegistrationDisabledError`` when registration is off for this environment,
    ``InvalidPasswordError`` when the password breaks the length policy, and
    ``EmailAlreadyRegisteredError`` when the normalized email is taken (including a concurrent
    registration that wins the race).
    """
    settings = settings or get_settings()
    if not settings.registration_enabled:
        raise RegistrationDisabledError()

    email = normalize_email(email)
    try:
        if repository.get_user_by_email(session, email) is not None:
            raise EmailAlreadyRegisteredError()
        try:
            password_hash = hash_password(password)
        except PasswordPolicyError as exc:
            raise InvalidPasswordError(str(exc)) from None
        user = repository.insert_user(session, email=email, password_hash=password_hash)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == _EMAIL_UNIQUE_CONSTRAINT:
            raise EmailAlreadyRegisteredError() from None
        # Any other violation is a bug. The message embeds the row (email, hash): log metadata only.
        logger.error("User registration failed", extra=database_error_metadata(exc))
        raise
    except Exception:
        session.rollback()
        raise

    logger.info("User registered", extra={"user_id": str(user.id)})
    return UserRead.model_validate(user)


def login(
    session: Session,
    *,
    email: str,
    password: str,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> LoginResult:
    """Verify credentials and start a session (a new refresh-token family); commit.

    Unknown email, wrong password, and inactive account all raise the same
    ``InvalidCredentialsError`` after the same password-hashing work. On success the stored
    hash is upgraded if its parameters are outdated and ``last_login_at`` is set.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)

    # Every exception, including invalid credentials, rolls back: nothing is written unless the
    # whole session (refresh token, last_login_at, any hash upgrade) commits together.
    try:
        user = repository.get_user_by_email(session, normalize_email(email))
        if user is None:
            verify_dummy_password(password)
            logger.info("Login failed", extra={"reason": "unknown_email"})
            raise InvalidCredentialsError()

        valid, new_hash = verify_password(password, user.password_hash)
        if not valid:
            logger.info("Login failed", extra={"reason": "invalid_password", "user_id": str(user.id)})
            raise InvalidCredentialsError()
        if not user.is_active:
            logger.info("Login failed", extra={"reason": "inactive_user", "user_id": str(user.id)})
            raise InvalidCredentialsError()

        session_id = uuid.uuid4()
        refresh_token = generate_opaque_token()
        absolute_expires_at = now + timedelta(seconds=settings.refresh_token_absolute_ttl_seconds)
        refresh_expires_at = min(now + timedelta(seconds=settings.refresh_token_ttl_seconds), absolute_expires_at)
        # Create every token before changing any row.
        access_token, access_expires_at = create_access_token(
            user_id=user.id, session_id=session_id, settings=settings, now=now
        )
        csrf_token = create_csrf_token(session_id, settings=settings)

        if new_hash is not None:
            user.password_hash = new_hash
        user.last_login_at = now
        repository.insert_refresh_token(
            session,
            user_id=user.id,
            family_id=session_id,
            token_hash=hash_opaque_token(refresh_token),
            expires_at=refresh_expires_at,
            absolute_expires_at=absolute_expires_at,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise

    logger.info(
        "Login succeeded",
        extra={"user_id": str(user.id), "session_id": str(session_id), "password_rehashed": new_hash is not None},
    )
    return LoginResult(
        user=UserRead.model_validate(user),
        session_id=session_id,
        access_token=access_token,
        access_token_expires_at=access_expires_at,
        refresh_token=refresh_token,
        refresh_token_expires_at=refresh_expires_at,
        csrf_token=csrf_token,
    )
