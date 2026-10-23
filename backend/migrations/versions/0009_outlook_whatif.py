"""The Outlook page's region-wide what-if, per reach.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reach_outlook", sa.Column("whatif", JSONB()))


def downgrade() -> None:
    op.drop_column("reach_outlook", "whatif")
