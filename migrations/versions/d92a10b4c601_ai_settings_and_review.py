"""Persist versioned AI settings and lightweight model maintenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d92a10b4c601"
down_revision: str | None = "c3d7a8e9f102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_settings_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("values_json", sa.Text(), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "model_review_state",
        sa.Column("scope", sa.String(), primary_key=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "model_review_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("settings_version", sa.String(), nullable=False),
        sa.Column("settings_json", sa.Text(), nullable=False),
        sa.Column("sources_json", sa.Text(), nullable=False),
        sa.Column("catalog_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_model_review_state_next_due_at", "model_review_state", ["next_due_at"])
    op.create_index("ix_model_review_runs_scope", "model_review_runs", ["scope"])
    op.create_index("ix_model_review_runs_status", "model_review_runs", ["status"])
    op.create_table(
        "model_review_proposals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), sa.ForeignKey("model_review_runs.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("settings_version", sa.String(), nullable=False),
        sa.Column("profile", sa.String(), nullable=False),
        sa.Column("current_model", sa.String(), nullable=False),
        sa.Column("candidate_model", sa.String(), nullable=False),
        sa.Column("recommendation_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_model_review_proposals_run_id", "model_review_proposals", ["run_id"])
    op.create_index("ix_model_review_proposals_status", "model_review_proposals", ["status"])


def downgrade() -> None:
    op.drop_table("model_review_proposals")
    op.drop_table("model_review_runs")
    op.drop_table("model_review_state")
    op.drop_table("ai_settings_revisions")
