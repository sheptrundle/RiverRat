"""The Outlook notes reaches forecast from Army Corps release plans.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reach_outlook", sa.Column("release", JSONB))


def downgrade() -> None:
    op.drop_column("reach_outlook", "release")
