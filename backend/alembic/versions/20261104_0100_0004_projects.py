"""Projects: projects, memberships and API keys.

Revision ID: 0004_projects
Revises: 0003_auth
Create Date: 2026-11-04 01:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_projects"
down_revision: Union[str, Sequence[str], None] = "0003_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TRIMMED_NAME = "name <> '' AND name !~ '^[[:space:]]' AND name !~ '[[:space:]]$'"


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("retention_overrides", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(_TRIMMED_NAME, name=op.f("ck_projects_name_trimmed")),
        sa.CheckConstraint(
            "retention_overrides IS NULL OR jsonb_typeof(retention_overrides) = 'object'",
            name=op.f("ck_projects_retention_overrides_object"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
    )

    op.create_table(
        "project_members",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name=op.f("ck_project_members_role")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_project_members_project_id_projects"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_project_members_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("project_id", "user_id", name=op.f("pk_project_members")),
    )
    op.create_index("ix_project_members_user_id", "project_members", ["user_id"])
    # At most one Owner per project. Not deferrable: a transfer demotes the old Owner first.
    op.create_index(
        "uq_project_members_owner",
        "project_members",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner'"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_api_keys_key_hash_format")),
        sa.CheckConstraint(
            "revoked_by IS NULL OR revoked_at IS NOT NULL", name=op.f("ck_api_keys_revoked_by_requires_revoked_at")
        ),
        sa.CheckConstraint(_TRIMMED_NAME, name=op.f("ck_api_keys_name_trimmed")),
        sa.CheckConstraint("prefix <> ''", name=op.f("ck_api_keys_prefix_not_empty")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_api_keys_project_id_projects"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_api_keys_created_by_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by"], ["users.id"], name=op.f("fk_api_keys_revoked_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")),
    )
    op.create_index("ix_api_keys_project_id", "api_keys", ["project_id"])
    op.create_index("ix_api_keys_created_by", "api_keys", ["created_by"])
    op.create_index("ix_api_keys_revoked_by", "api_keys", ["revoked_by"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_revoked_by", table_name="api_keys")
    op.drop_index("ix_api_keys_created_by", table_name="api_keys")
    op.drop_index("ix_api_keys_project_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("uq_project_members_owner", table_name="project_members")
    op.drop_index("ix_project_members_user_id", table_name="project_members")
    op.drop_table("project_members")
    op.drop_table("projects")
