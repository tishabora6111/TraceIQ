"""ORM tables owned by the projects module.

All timestamps are timezone-aware and, where the database sets them, use the database clock.
Primary keys are generated in Python (uuid4); there is no database-side UUID default.
Users are referenced by foreign key only (``users.id``); this module never imports auth models.
API keys are stored only as the SHA-256 hex digest of the key (see ``core/security``).
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

PROJECT_NAME_MAX_LENGTH = 100
PROJECT_DESCRIPTION_MAX_LENGTH = 1000
API_KEY_NAME_MAX_LENGTH = 100
API_KEY_PREFIX_MAX_LENGTH = 16
# SHA-256 hex digest of the API key.
API_KEY_HASH_LENGTH = 64

# A name is non-empty and carries no leading or trailing whitespace of any kind.
_TRIMMED_NAME = "name <> '' AND name !~ '^[[:space:]]' AND name !~ '[[:space:]]$'"


class ProjectRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


_ROLE_VALUES = ", ".join(f"'{role.value}'" for role in ProjectRole)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(PROJECT_NAME_MAX_LENGTH))
    description: Mapped[str | None] = mapped_column(String(PROJECT_DESCRIPTION_MAX_LENGTH))
    # Per-project overrides of the configured retention defaults; null means "use the defaults".
    # The service validates the keys and bounds; the database only guarantees an object.
    # none_as_null: Python None is stored as SQL NULL, not as a JSON null (which is not an object).
    retention_overrides: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    api_keys: Mapped[list["ApiKey"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint(_TRIMMED_NAME, name="name_trimmed"),
        CheckConstraint(
            "retention_overrides IS NULL OR jsonb_typeof(retention_overrides) = 'object'",
            name="retention_overrides_object",
        ),
    )


class ProjectMember(Base):
    """A user's role in a project. Exactly one member per project is the Owner.

    "At most one" is enforced here by a partial unique index. "At least one" cannot be a constraint
    (the index cannot be deferred); the service enforces it under a project-row lock. An ownership
    transfer demotes the current Owner before promoting the new one, in one transaction.
    """

    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    # RESTRICT: a user who belongs to a project cannot be deleted out from under it (ARCHITECTURE §10.1).
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="members")

    __table_args__ = (
        CheckConstraint(f"role IN ({_ROLE_VALUES})", name="role"),
        # "My projects": memberships by user (project_id is covered by the primary key).
        Index("ix_project_members_user_id", "user_id"),
        Index(
            "uq_project_members_owner",
            "project_id",
            unique=True,
            postgresql_where=text(f"role = '{ProjectRole.OWNER.value}'"),
        ),
    )


class ApiKey(Base):
    """A project API key for ingestion. Revoked keys are kept (for audit) and never resolve."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(API_KEY_NAME_MAX_LENGTH))
    # The key's first characters, shown in listings so users can tell keys apart.
    prefix: Mapped[str] = mapped_column(String(API_KEY_PREFIX_MAX_LENGTH))
    key_hash: Mapped[str] = mapped_column(String(API_KEY_HASH_LENGTH))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Set by ingestion (M2).
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))

    project: Mapped[Project] = relationship(back_populates="api_keys")

    __table_args__ = (
        # Resolving a presented key looks it up by hash.
        UniqueConstraint("key_hash"),
        CheckConstraint(f"key_hash ~ '^[0-9a-f]{{{API_KEY_HASH_LENGTH}}}$'", name="key_hash_format"),
        CheckConstraint("revoked_by IS NULL OR revoked_at IS NOT NULL", name="revoked_by_requires_revoked_at"),
        CheckConstraint(_TRIMMED_NAME, name="name_trimmed"),
        CheckConstraint("prefix <> ''", name="prefix_not_empty"),
        # Listing a project's keys.
        Index("ix_api_keys_project_id", "project_id"),
        # Foreign keys are indexed (CLAUDE.md); also keeps ON DELETE SET NULL from scanning the table.
        Index("ix_api_keys_created_by", "created_by"),
        Index("ix_api_keys_revoked_by", "revoked_by"),
    )
