"""NOAA links on gauges, and the NOAA forecast archive.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("gauge", sa.Column("nws_lid", sa.String(8)))
    op.add_column("gauge", sa.Column("nwm_reach_id", sa.BigInteger()))
    op.add_column(
        "gauge",
        sa.Column("has_rfc_forecast", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("gauge", sa.Column("noaa_linked_at", TS))

    op.create_table(
        "noaa_forecast",
        sa.Column(
            "gauge_id",
            sa.Integer(),
            sa.ForeignKey("gauge.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source", sa.String(16), primary_key=True),
        sa.Column("issued_at", TS, primary_key=True),
        sa.Column("valid_time", TS, primary_key=True),
        sa.Column("flow_cfs", sa.Float()),
        sa.Column("stage_ft", sa.Float()),
    )


def downgrade() -> None:
    op.drop_table("noaa_forecast")
    for col in ("noaa_linked_at", "has_rfc_forecast", "nwm_reach_id", "nws_lid"):
        op.drop_column("gauge", col)
