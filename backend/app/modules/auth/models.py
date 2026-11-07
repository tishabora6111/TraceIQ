"""ORM tables owned by the auth module.

All timestamps are timezone-aware and, where the database sets them, use the database clock.
Primary keys are generated in Python (uuid4); there is no database-side UUID default.
Plaintext passwords and refresh tokens are never stored: only the Argon2id password hash and the
SHA-256 hex digest of each refresh token (see ``core/security``).
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

# Upper bound for an address: 64-character local part, "@", 255-character domain.
EMAIL_MAX_LENGTH = 320
# SHA-256 hex digest of the opaque refresh token.
TOKEN_HASH_LENGTH = 64


class RevocationReason(StrEnum):
    ROTATED = "rotated"
    REUSE_DETECTED = "reuse_detected"
    LOGOUT = "logout"
    # Account-wide or security-driven revocation, e.g. revoke all sessions or a password change.
    REVOKED = "revoked"


_REASON_VALUES = ", ".join(f"'{reason.value}'" for reason in RevocationReason)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Stored trimmed and lower-cased by the auth service; the check rejects upper case and any whitespace.
    email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("email"),
        CheckConstraint("email = lower(email) AND email !~ '[[:space:]]'", name="email_normalized"),
        CheckConstraint("strpos(email, '@') > 1", name="email_has_at"),
        CheckConstraint("password_hash <> ''", name="password_hash_not_empty"),
    )


class RefreshToken(Base):
    """One opaque refresh token. Tokens issued from one login share a ``family_id`` (the session id)."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    token_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LENGTH))
    # Rolling expiry of this token; never later than the family's absolute expiry.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Fixed at login and copied to every rotated token in the family.
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[str | None] = mapped_column(String(32))
    # The token issued when this one was rotated. SET NULL so deleting a single token never fails.
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="refresh_tokens")
    replaced_by: Mapped["RefreshToken | None"] = relationship(remote_side=[id])

    __table_args__ = (
        UniqueConstraint("token_hash"),
        # A token is replaced by at most one token, and each token replaces at most one.
        UniqueConstraint("replaced_by_id"),
        CheckConstraint(f"token_hash ~ '^[0-9a-f]{{{TOKEN_HASH_LENGTH}}}$'", name="token_hash_format"),
        CheckConstraint("expires_at <= absolute_expires_at", name="expiry_within_absolute"),
        CheckConstraint(
            f"revocation_reason IS NULL OR revocation_reason IN ({_REASON_VALUES})", name="revocation_reason"
        ),
        CheckConstraint("(revoked_at IS NULL) = (revocation_reason IS NULL)", name="revoked_iff_reason"),
        CheckConstraint(
            "replaced_by_id IS NULL OR revocation_reason = 'rotated'", name="replaced_only_when_rotated"
        ),
        CheckConstraint("replaced_by_id IS NULL OR replaced_by_id <> id", name="not_replaced_by_self"),
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
    )
