"""Each morning's forecasts, kept and later scored against what happened.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forecast_archive",
        sa.Column(
            "reach_id",
            sa.Integer(),
            sa.ForeignKey("reach.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("lead_days", sa.Integer(), nullable=False),
        sa.Column(
            "gauge_id", sa.Integer(), sa.ForeignKey("gauge.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("metric", sa.String(8), nullable=False),
        sa.Column("range_min", sa.Float()),
        sa.Column("range_max", sa.Float()),
        sa.Column("p_runnable", sa.Float(), nullable=False),
        sa.Column("persistence", sa.Boolean(), nullable=False),
        sa.Column("replay", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("observed", sa.Boolean()),
        sa.Column("scored_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_forecast_archive_day", "forecast_archive", ["day"])


def downgrade() -> None:
    op.drop_table("forecast_archive")
