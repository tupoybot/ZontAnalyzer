"""Persist structured owner-recorded manual interventions."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a1f0c4d920"
down_revision: str | None = "b81c3d4e5f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intervention_experiments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("intervention_id", sa.String(),
                  sa.ForeignKey("interventions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("parameter", sa.Text(), nullable=True),
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("after_json", sa.Text(), nullable=True),
        sa.Column("performed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot_json", sa.Text(), nullable=True),
        sa.Column("snapshot_fingerprint", sa.String(), nullable=True),
        sa.Column("snapshot_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot_source", sa.String(), nullable=True),
        sa.Column("historical_context", sa.String(), nullable=False, server_default="unknown"),
        sa.UniqueConstraint("intervention_id"),
    )
    op.create_index("ix_intervention_experiments_performed_at", "intervention_experiments", ["performed_at"])


def downgrade() -> None:
    op.drop_table("intervention_experiments")
