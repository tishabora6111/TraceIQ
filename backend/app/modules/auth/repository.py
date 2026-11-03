"""SQL for the auth module's tables. Callers own the transaction: nothing here commits."""

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.modules.auth.models import RefreshToken, RevocationReason, User


def get_user_by_id(session: Session, user_id: uuid.UUID) -> User | None:
    return session.get(User, user_id)


def has_active_session(session: Session, *, user_id: uuid.UUID, family_id: uuid.UUID, now: datetime) -> bool:
    """Whether the family belongs to the user and still has a token that is unrevoked and unexpired.

    Rotation keeps a family active through its newest token; logout and reuse detection revoke
    every token in the family, which ends the session.
    """
    active = (
        select(RefreshToken.id)
        .where(
            RefreshToken.family_id == family_id,
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
            RefreshToken.absolute_expires_at > now,
        )
        .exists()
    )
    return bool(session.scalar(select(active)))


def get_user_by_email(session: Session, email: str) -> User | None:
    """Exact match on the stored (already normalized) email."""
    return session.scalars(select(User).where(User.email == email)).one_or_none()


def insert_user(session: Session, *, email: str, password_hash: str) -> User:
    user = User(id=uuid.uuid4(), email=email, password_hash=password_hash)
    session.add(user)
    session.flush()
    return user


def insert_refresh_token(
    session: Session,
    *,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
    token_hash: str,
    expires_at: datetime,
    absolute_expires_at: datetime,
) -> RefreshToken:
    token = RefreshToken(
        id=uuid.uuid4(),
        user_id=user_id,
        family_id=family_id,
        token_hash=token_hash,
        expires_at=expires_at,
        absolute_expires_at=absolute_expires_at,
    )
    session.add(token)
    session.flush()
    return token


def get_refresh_token_by_hash(session: Session, token_hash: str) -> RefreshToken | None:
    """Unlocked lookup, used to find the token's user and family before taking locks."""
    return session.scalars(select(RefreshToken).where(RefreshToken.token_hash == token_hash)).one_or_none()


def lock_user(session: Session, user_id: uuid.UUID) -> User | None:
    """``SELECT ... FOR UPDATE`` on the user row, reloading it from the database.

    Every change to a user's refresh tokens (rotation, logout, family and account revocation)
    holds this lock, so those changes are serialized per user and each sees the others' results.
    """
    statement = select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    return session.scalars(statement).one_or_none()


def get_refresh_token_for_update(session: Session, token_hash: str) -> RefreshToken | None:
    """Locked lookup that reloads the row; call it while holding the user lock."""
    statement = (
        select(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.scalars(statement).one_or_none()


def revoke_family(session: Session, *, family_id: uuid.UUID, reason: RevocationReason, now: datetime) -> int:
    """Revoke every unrevoked token in the family. Returns how many were revoked."""
    statement = (
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now, revocation_reason=reason.value)
        .execution_options(synchronize_session=False)
    )
    return session.execute(statement).rowcount


def revoke_user_tokens(session: Session, *, user_id: uuid.UUID, reason: RevocationReason, now: datetime) -> int:
    """Revoke every unrevoked token of every family the user has. Returns how many were revoked."""
    statement = (
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now, revocation_reason=reason.value)
        .execution_options(synchronize_session=False)
    )
    return session.execute(statement).rowcount
