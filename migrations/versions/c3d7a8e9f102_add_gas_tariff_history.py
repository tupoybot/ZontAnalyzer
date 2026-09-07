"""Store monthly gas tariff history and correction audit."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d7a8e9f102"
down_revision: str | None = "e5a1f0c4d920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gas_tariffs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("effective_month", sa.String(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope", "effective_month"),
    )
    op.create_index("ix_gas_tariffs_scope", "gas_tariffs", ["scope"])
    op.create_index("ix_gas_tariffs_effective_from", "gas_tariffs", ["effective_from"])
    op.create_table(
        "gas_tariff_audit",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "tariff_id",
            sa.String(),
            sa.ForeignKey("gas_tariffs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("after_json", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gas_tariff_audit_tariff_id", "gas_tariff_audit", ["tariff_id"])


def downgrade() -> None:
    op.drop_table("gas_tariff_audit")
    op.drop_table("gas_tariffs")
