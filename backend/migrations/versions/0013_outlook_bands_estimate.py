"""The Outlook keeps the range it judged each reach by, and how estimates were made.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reach_outlook", sa.Column("range_min", sa.Float()))
    op.add_column("reach_outlook", sa.Column("range_max", sa.Float()))
    op.add_column("reach_outlook", sa.Column("estimate", JSONB))


def downgrade() -> None:
    op.drop_column("reach_outlook", "estimate")
    op.drop_column("reach_outlook", "range_max")
    op.drop_column("reach_outlook", "range_min")
