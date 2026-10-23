"""How often each reach has been in range, and a flag for ranges that look wrong.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reach_outlook", sa.Column("in_range_share", sa.Float()))
    op.add_column("reach_outlook", sa.Column("range_note", sa.String(16)))


def downgrade() -> None:
    op.drop_column("reach_outlook", "range_note")
    op.drop_column("reach_outlook", "in_range_share")
