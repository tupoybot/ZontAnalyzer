"""Persist per-series semantic confidence and origin.

Revision ID: 7c8e9f1a2b3c
Revises: 5a9ce2bd8b34
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c8e9f1a2b3c"
down_revision: str | None = "5a9ce2bd8b34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("telemetry_series") as batch_op:
        batch_op.add_column(sa.Column("confidence", sa.Float(), nullable=False, server_default="0.3"))
        batch_op.add_column(sa.Column("provenance", sa.String(), nullable=False, server_default="unknown"))
        batch_op.add_column(sa.Column("origin", sa.String(), nullable=False, server_default="unknown"))


def downgrade() -> None:
    with op.batch_alter_table("telemetry_series") as batch_op:
        batch_op.drop_column("origin")
        batch_op.drop_column("provenance")
        batch_op.drop_column("confidence")
