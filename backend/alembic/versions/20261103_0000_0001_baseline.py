"""Baseline: empty schema.

Marks the starting point of the migration history. Creates no tables; module
tables are added by later revisions.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-03 00:00:00

"""
from typing import Sequence, Union

revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
