"""Dam operators' notices (TVA's lake messages).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dam_notice",
        sa.Column(
            "gauge_id",
            sa.Integer(),
            sa.ForeignKey("gauge.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("dam_notice")
