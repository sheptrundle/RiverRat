"""The latest Outlook per reach.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reach_outlook",
        sa.Column(
            "reach_id",
            sa.Integer(),
            sa.ForeignKey("reach.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "gauge_id", sa.Integer(), sa.ForeignKey("gauge.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("primary", sa.Boolean(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rain_issued_at", sa.DateTime(timezone=True)),
        sa.Column("metric", sa.String(8), nullable=False),
        sa.Column("now_value", sa.Float(), nullable=False),
        sa.Column("now_band", sa.String(16), nullable=False),
        sa.Column("extrapolated", sa.Boolean(), nullable=False),
        sa.Column("days", JSONB(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("reach_outlook")
