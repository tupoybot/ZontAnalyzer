"""Store versioned owner context and auditable daily gas readings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b81c3d4e5f60"
down_revision: str | None = "9f4a2c8d1e70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "owner_profile_revisions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("provenance", sa.String(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_reset", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_owner_profile_revisions_device_id", "owner_profile_revisions", ["device_id"])
    op.create_index("ix_owner_profile_revisions_field", "owner_profile_revisions", ["field"])
    op.create_index("ix_owner_profile_revisions_effective_from", "owner_profile_revisions", ["effective_from"])
    op.create_table(
        "gas_readings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("report_id", sa.String(), nullable=False),
        sa.Column("reading_day", sa.String(), nullable=False),
        sa.Column("meter_segment", sa.String(), nullable=False, server_default="default"),
        sa.Column("value_m3", sa.String(), nullable=False),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("device_id", "reading_day"),
    )
    op.create_index("ix_gas_readings_device_id", "gas_readings", ["device_id"])
    op.create_index("ix_gas_readings_report_id", "gas_readings", ["report_id"])
    op.create_table(
        "gas_meter_boundaries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("report_id", sa.String(), nullable=False),
        sa.Column("boundary_day", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("device_id", "boundary_day"),
    )
    op.create_index("ix_gas_meter_boundaries_device_id", "gas_meter_boundaries", ["device_id"])
    op.create_index("ix_gas_meter_boundaries_boundary_day", "gas_meter_boundaries", ["boundary_day"])
    op.create_table(
        "gas_reading_audit",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("reading_id", sa.String(), nullable=True),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("report_id", sa.String(), nullable=False),
        sa.Column("reading_day", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("after_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gas_reading_audit_reading_id", "gas_reading_audit", ["reading_id"])
    op.create_index("ix_gas_reading_audit_device_id", "gas_reading_audit", ["device_id"])
    op.create_index("ix_gas_reading_audit_reading_day", "gas_reading_audit", ["reading_day"])


def downgrade() -> None:
    op.drop_table("gas_reading_audit")
    op.drop_table("gas_meter_boundaries")
    op.drop_table("gas_readings")
    op.drop_table("owner_profile_revisions")
