"""SQL for the auth module's tables. Callers own the transaction: nothing here commits."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.auth.models import RefreshToken, User


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
