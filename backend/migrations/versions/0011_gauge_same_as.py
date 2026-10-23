"""Gauges at the same place as a USGS gauge (NWS locations' USGS twins).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gauge",
        sa.Column("same_as_id", sa.Integer(), sa.ForeignKey("gauge.id", ondelete="SET NULL")),
    )


def downgrade() -> None:
    op.drop_column("gauge", "same_as_id")
