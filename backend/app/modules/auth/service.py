"""Public interface of the auth module.

Each function that writes manages its own transaction: it commits on success and rolls back on
any exception. Read-only functions (``get_authenticated_user``) never commit or roll back.
Never logs emails, passwords, or tokens; log lines carry only user and session ids.
"""

import hmac
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import database_error_metadata
from app.core.security import (
    InvalidTokenError,
    PasswordHashingBusyError,
    PasswordPolicyError,
    create_access_token,
    create_csrf_token,
    generate_opaque_token,
    hash_opaque_token,
    hash_password,
    verify_csrf_token,
    verify_dummy_password,
    verify_password,
)
from app.modules.auth import repository
from app.modules.auth.errors import (
    AuthenticationBusyError,
    CsrfValidationError,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidPasswordError,
    NotAuthenticatedError,
    RegistrationDisabledError,
)
from app.modules.auth.models import RefreshToken, RevocationReason
from app.modules.auth.schemas import UserRead, normalize_email

logger = logging.getLogger(__name__)

_EMAIL_UNIQUE_CONSTRAINT = "uq_users_email"


@dataclass(frozen=True)
class LoginResult:
    """A new session (login) or a rotated one (refresh). Token values are excluded from the repr so
    they cannot leak into logs."""

    user: UserRead
    session_id: uuid.UUID
    access_token: str = field(repr=False)
    access_token_expires_at: datetime
    refresh_token: str = field(repr=False)
    refresh_token_expires_at: datetime
    csrf_token: str = field(repr=False)


def get_authenticated_user(
    session: Session, *, user_id: uuid.UUID, session_id: uuid.UUID, now: datetime | None = None
) -> UserRead:
    """The user behind a verified access token, if the user and the token's session are still valid.

    Raises ``NotAuthenticatedError`` when the user does not exist or is inactive, or when the
    session (the token's refresh-token family) is missing, belongs to another user, or has been
    revoked or has expired. Read-only: it neither commits nor rolls back, so it is safe inside a
    caller's transaction.
    """
    now = now or datetime.now(UTC)
    user = repository.get_user_by_id(session, user_id)
    if user is None:
        reason = "unknown_user"
    elif not user.is_active:
        reason = "inactive_user"
    elif not repository.has_active_session(session, user_id=user_id, family_id=session_id, now=now):
        reason = "inactive_session"
    else:
        return UserRead.model_validate(user)
    logger.info(
        "Authentication rejected",
        extra={"reason": reason, "user_id": str(user_id), "session_id": str(session_id)},
    )
    raise NotAuthenticatedError()


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
    except PasswordHashingBusyError:
        session.rollback()
        logger.warning("Registration rejected: password hashing capacity exhausted")
        raise AuthenticationBusyError() from None
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
    except PasswordHashingBusyError:
        # Raised before any verification result is known, for unknown and known emails alike.
        session.rollback()
        logger.warning("Login rejected: password hashing capacity exhausted")
        raise AuthenticationBusyError() from None
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


def _find_refresh_token(session: Session, refresh_token: str | None) -> RefreshToken | None:
    """The stored token matching the presented value (by SHA-256 hash), or None. Takes no locks."""
    if not refresh_token:
        return None
    try:
        token_hash = hash_opaque_token(refresh_token)
    except InvalidTokenError:
        return None
    return repository.get_refresh_token_by_hash(session, token_hash)


def _require_csrf(token: RefreshToken, csrf_header: str | None, csrf_cookie: str | None, settings: Settings) -> None:
    """Signed double-submit check, bound to the token's family. Runs before any state change, so a
    request without the CSRF token can never rotate, revoke, or trigger reuse detection."""
    if not verify_csrf_token(
        header_token=csrf_header, cookie_token=csrf_cookie, session_id=token.family_id, settings=settings
    ):
        logger.warning(
            "CSRF validation failed",
            extra={
                "user_id": str(token.user_id),
                "session_id": str(token.family_id),
                **_csrf_failure_details(csrf_header, csrf_cookie),
            },
        )
        raise CsrfValidationError()


def _csrf_failure_details(csrf_header: str | None, csrf_cookie: str | None) -> dict[str, object]:
    """Which part of the double-submit check failed, for diagnosis. Never includes token values:
    only presence, lengths, and whether header and cookie are equal."""
    if not csrf_header:
        reason = "missing_header"
    elif not csrf_cookie:
        reason = "missing_cookie"
    elif not hmac.compare_digest(csrf_header.encode("utf-8"), csrf_cookie.encode("utf-8")):
        reason = "header_cookie_mismatch"
    else:
        reason = "invalid_signature"  # equal values not signed for this session
    return {
        "reason": reason,
        "header_length": len(csrf_header) if csrf_header else 0,
        "cookie_length": len(csrf_cookie) if csrf_cookie else 0,
    }


def _reject_refresh(reason: str, token: RefreshToken | None = None) -> NotAuthenticatedError:
    extra: dict[str, str] = {"reason": reason}
    if token is not None:
        extra.update(user_id=str(token.user_id), session_id=str(token.family_id))
    logger.info("Refresh rejected", extra=extra)
    return NotAuthenticatedError()


def refresh_session(
    session: Session,
    *,
    refresh_token: str | None,
    csrf_header: str | None,
    csrf_cookie: str | None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> LoginResult:
    """Rotate a refresh token: revoke it (``rotated``) and issue its replacement in the same family,
    with a new access token and CSRF token; commit.

    Order of checks: a token must be presented and found (401); the CSRF token must be valid for
    its family (403, nothing changes); then, holding the user's row lock, the token is re-read.
    A revoked token means reuse: every token in the family is revoked (``reuse_detected``) and
    committed, then 401. An expired token or an inactive user gives 401 without changes.

    Concurrency: rotation and revocation hold the user lock, so of two concurrent refreshes with
    the same token exactly one rotates; the other then sees a revoked token and, under the strict
    reuse policy, revokes the family (including the winner's replacement).
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    try:
        found = _find_refresh_token(session, refresh_token)
        if found is None:
            raise _reject_refresh("missing_refresh_token" if not refresh_token else "unknown_refresh_token")
        _require_csrf(found, csrf_header, csrf_cookie, settings)

        user = repository.lock_user(session, found.user_id)
        token = repository.get_refresh_token_for_update(session, found.token_hash)
        if user is None or token is None:
            raise _reject_refresh("unknown_refresh_token", found)

        if token.revoked_at is not None:
            revoked = repository.revoke_family(
                session, family_id=token.family_id, reason=RevocationReason.REUSE_DETECTED, now=now
            )
            session.commit()
            logger.warning(
                "Refresh token reuse detected; session revoked",
                extra={
                    "user_id": str(token.user_id),
                    "session_id": str(token.family_id),
                    "previous_reason": token.revocation_reason,
                    "tokens_revoked": revoked,
                },
            )
            raise NotAuthenticatedError()
        if token.expires_at <= now or token.absolute_expires_at <= now:
            raise _reject_refresh("expired_refresh_token", token)
        if not user.is_active:
            raise _reject_refresh("inactive_user", token)

        new_refresh_token = generate_opaque_token()
        refresh_expires_at = min(now + timedelta(seconds=settings.refresh_token_ttl_seconds), token.absolute_expires_at)
        # Create every token before changing any row.
        access_token, access_expires_at = create_access_token(
            user_id=user.id, session_id=token.family_id, settings=settings, now=now
        )
        csrf_token = create_csrf_token(token.family_id, settings=settings)

        replacement = repository.insert_refresh_token(
            session,
            user_id=user.id,
            family_id=token.family_id,
            token_hash=hash_opaque_token(new_refresh_token),
            expires_at=refresh_expires_at,
            absolute_expires_at=token.absolute_expires_at,
        )
        token.revoked_at = now
        token.revocation_reason = RevocationReason.ROTATED.value
        token.replaced_by_id = replacement.id
        session.commit()
    except Exception:
        session.rollback()
        raise

    logger.info("Session refreshed", extra={"user_id": str(user.id), "session_id": str(token.family_id)})
    return LoginResult(
        user=UserRead.model_validate(user),
        session_id=token.family_id,
        access_token=access_token,
        access_token_expires_at=access_expires_at,
        refresh_token=new_refresh_token,
        refresh_token_expires_at=refresh_expires_at,
        csrf_token=csrf_token,
    )


def logout(
    session: Session,
    *,
    refresh_token: str | None,
    csrf_header: str | None,
    csrf_cookie: str | None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> None:
    """End the session of the presented refresh token: revoke its whole family (``logout``); commit.

    Idempotent: with no token, an unknown token, or an already revoked or expired family, nothing
    changes and no error is raised. A known token requires a valid CSRF token for its family
    (``CsrfValidationError`` otherwise, and nothing changes). Access tokens of the session stop
    working immediately, because the current-user check requires an active family.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    try:
        found = _find_refresh_token(session, refresh_token)
        if found is None:
            session.rollback()
            logger.info("Logout without a known session", extra={"token_presented": bool(refresh_token)})
            return
        _require_csrf(found, csrf_header, csrf_cookie, settings)
        repository.lock_user(session, found.user_id)
        revoked = repository.revoke_family(session, family_id=found.family_id, reason=RevocationReason.LOGOUT, now=now)
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info(
        "Logged out",
        extra={"user_id": str(found.user_id), "session_id": str(found.family_id), "tokens_revoked": revoked},
    )


def revoke_all_sessions(
    session: Session,
    *,
    user_id: uuid.UUID,
    reason: RevocationReason = RevocationReason.REVOKED,
    now: datetime | None = None,
) -> int:
    """Revoke every session the user has (account-wide revocation, e.g. after a password change or a
    compromised account); commit. Returns the number of tokens revoked. Access tokens of those
    sessions stop working immediately.
    """
    now = now or datetime.now(UTC)
    try:
        repository.lock_user(session, user_id)
        revoked = repository.revoke_user_tokens(session, user_id=user_id, reason=reason, now=now)
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info(
        "All sessions revoked", extra={"user_id": str(user_id), "reason": reason.value, "tokens_revoked": revoked}
    )
    return revoked
