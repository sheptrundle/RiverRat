"""Watersheds above the take-outs of reaches without a gauge.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reach_basin",
        sa.Column(
            "reach_id",
            sa.Integer(),
            sa.ForeignKey("reach.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("comid", sa.BigInteger(), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.Geometry("MULTIPOLYGON", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("area_sqmi", sa.Float(), nullable=False),
        sa.Column("putin_area_sqmi", sa.Float()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("reach_basin")
