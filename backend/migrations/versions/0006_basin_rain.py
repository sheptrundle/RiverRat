"""Hourly basin rain from the live product, and which hours have been processed.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "basin_rain",
        sa.Column(
            "gauge_id",
            sa.Integer(),
            sa.ForeignKey("gauge.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source", sa.String(16), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("mm", sa.Float()),
    )
    op.create_table(
        "rain_hour",
        sa.Column("source", sa.String(16), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("file_modified", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("rain_hour")
    op.drop_table("basin_rain")
