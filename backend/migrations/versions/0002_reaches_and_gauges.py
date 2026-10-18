"""Reach catalog: reaches, gauges, and their links with AW flow ranges.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reach",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("aw_id", sa.Integer(), nullable=True, unique=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("river", sa.String(), nullable=False),
        sa.Column("section", sa.String(), nullable=False),
        sa.Column("altname", sa.String(), nullable=False),
        sa.Column("difficulty_raw", sa.String(32), nullable=True),
        sa.Column("class_low", sa.String(8), nullable=True),
        sa.Column("class_high", sa.String(8), nullable=True),
        sa.Column("class_standout", sa.String(8), nullable=True),
        sa.Column("states", postgresql.ARRAY(sa.String(16)), nullable=False),
        sa.Column("length_mi", sa.Float(), nullable=True),
        sa.Column("gradient_fpm", sa.Float(), nullable=True),
        sa.Column("geom", Geometry(srid=4326, spatial_index=False), nullable=True),
        sa.Column("next_release", sa.Date(), nullable=True),
        sa.Column("aw_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("listed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_reach_geom", "reach", ["geom"], postgresql_using="gist")

    op.create_table(
        "gauge",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.UniqueConstraint("source", "source_id"),
    )

    op.create_table(
        "reach_gauge",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reach_id", sa.Integer(), sa.ForeignKey("reach.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("gauge_id", sa.Integer(), sa.ForeignKey("gauge.id"), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("metric", sa.String(16), nullable=True),
        sa.Column("range_min", sa.Float(), nullable=True),
        sa.Column("range_max", sa.Float(), nullable=True),
        sa.Column("aw_status", sa.String(32), nullable=True),
        sa.UniqueConstraint("reach_id", "gauge_id"),
    )


def downgrade() -> None:
    op.drop_table("reach_gauge")
    op.drop_table("gauge")
    op.drop_index("ix_reach_geom", table_name="reach")
    op.drop_table("reach")
