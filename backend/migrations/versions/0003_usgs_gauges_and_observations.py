"""USGS site metadata on gauges, and recent observations.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("gauge", sa.Column("geom", Geometry("POINT", srid=4326, spatial_index=False)))
    op.add_column("gauge", sa.Column("agency_name", sa.String()))
    op.add_column("gauge", sa.Column("drainage_area_sqmi", sa.Float()))
    op.add_column("gauge", sa.Column("state_code", sa.String(2)))
    op.add_column("gauge", sa.Column("huc", sa.String(16)))
    for col in ("cfs_begin", "cfs_end", "ft_begin", "ft_end", "metadata_synced_at"):
        op.add_column("gauge", sa.Column(col, TS))
    op.create_index("ix_gauge_geom", "gauge", ["geom"], postgresql_using="gist")

    op.create_table(
        "observation",
        sa.Column(
            "gauge_id",
            sa.Integer(),
            sa.ForeignKey("gauge.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("param", sa.String(8), primary_key=True),
        sa.Column("ts", TS, primary_key=True),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("qualifier", sa.String(32)),
    )


def downgrade() -> None:
    op.drop_table("observation")
    op.drop_index("ix_gauge_geom", table_name="gauge")
    for col in (
        "metadata_synced_at",
        "ft_end",
        "ft_begin",
        "cfs_end",
        "cfs_begin",
        "huc",
        "state_code",
        "drainage_area_sqmi",
        "agency_name",
        "geom",
    ):
        op.drop_column("gauge", col)
