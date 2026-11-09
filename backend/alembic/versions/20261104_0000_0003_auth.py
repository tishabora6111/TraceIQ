"""Auth: users and refresh tokens.

Revision ID: 0003_auth
Revises: 0002_jobs
Create Date: 2026-11-04 00:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_auth"
down_revision: Union[str, Sequence[str], None] = "0002_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "email = lower(email) AND email !~ '[[:space:]]'", name=op.f("ck_users_email_normalized")
        ),
        sa.CheckConstraint("strpos(email, '@') > 1", name=op.f("ck_users_email_has_at")),
        sa.CheckConstraint("password_hash <> ''", name=op.f("ck_users_password_hash_not_empty")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.String(length=32), nullable=True),
        sa.Column("replaced_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_refresh_tokens_token_hash_format")),
        sa.CheckConstraint("expires_at <= absolute_expires_at", name=op.f("ck_refresh_tokens_expiry_within_absolute")),
        sa.CheckConstraint(
            "revocation_reason IS NULL OR revocation_reason IN ('rotated', 'reuse_detected', 'logout', 'revoked')",
            name=op.f("ck_refresh_tokens_revocation_reason"),
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revocation_reason IS NULL)", name=op.f("ck_refresh_tokens_revoked_iff_reason")
        ),
        sa.CheckConstraint(
            "replaced_by_id IS NULL OR revocation_reason = 'rotated'",
            name=op.f("ck_refresh_tokens_replaced_only_when_rotated"),
        ),
        sa.CheckConstraint(
            "replaced_by_id IS NULL OR replaced_by_id <> id", name=op.f("ck_refresh_tokens_not_replaced_by_self")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_refresh_tokens_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["refresh_tokens.id"],
            name=op.f("fk_refresh_tokens_replaced_by_id_refresh_tokens"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
        sa.UniqueConstraint("replaced_by_id", name=op.f("uq_refresh_tokens_replaced_by_id")),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
