"""Archived basin rain forecasts (NBM percentiles) and which runs are stored.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERCENTILES = ("p05", "p10", "p25", "p50", "p75", "p90", "p95")


def upgrade() -> None:
    op.create_table(
        "rain_forecast",
        sa.Column(
            "gauge_id",
            sa.Integer(),
            sa.ForeignKey("gauge.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source", sa.String(16), primary_key=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("valid_end", sa.DateTime(timezone=True), primary_key=True),
        *(sa.Column(name, sa.Float()) for name in PERCENTILES),
    )
    op.create_table(
        "rain_forecast_run",
        sa.Column("source", sa.String(16), primary_key=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("rain_forecast_run")
    op.drop_table("rain_forecast")
