"""Add the idempotent, privacy-minimized ZONT source event log.

Revision ID: 5a9ce2bd8b34
Revises: dd4272b6d030
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5a9ce2bd8b34"
down_revision: str | None = "dd4272b6d030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("timestamp_utc", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("important", sa.Boolean(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_source_events_device_id"), "source_events", ["device_id"], unique=False)
    op.create_index(op.f("ix_source_events_event_type"), "source_events", ["event_type"], unique=False)
    op.create_index(op.f("ix_source_events_timestamp_utc"), "source_events", ["timestamp_utc"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_source_events_timestamp_utc"), table_name="source_events")
    op.drop_index(op.f("ix_source_events_event_type"), table_name="source_events")
    op.drop_index(op.f("ix_source_events_device_id"), table_name="source_events")
    op.drop_table("source_events")
