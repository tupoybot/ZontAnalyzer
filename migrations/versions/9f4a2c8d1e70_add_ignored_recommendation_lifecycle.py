"""Checkpoint ignored recommendation lifecycle maintenance.

The existing text status column already supports ignored. This revision has no
DDL: the migration guard creates and verifies an online backup before the first
startup updates accumulated recommendations.
"""
from collections.abc import Sequence

revision: str = "9f4a2c8d1e70"
down_revision: str | None = "7c8e9f1a2b3c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
