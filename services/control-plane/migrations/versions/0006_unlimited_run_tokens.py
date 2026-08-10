"""Represent unlimited cumulative run tokens explicitly."""

import sqlalchemy as sa
from alembic import op

revision = "0006_unlimited_run_tokens"
down_revision = "0005_run_runtime_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing integers are immutable execution history. New runtime-v2 rows
    # freeze NULL, which is the explicit unlimited cumulative-token contract.
    with op.batch_alter_table("runs") as batch_op:
        batch_op.alter_column(
            "runtime_run_max_tokens",
            existing_type=sa.Integer(),
            nullable=True,
            server_default=None,
        )


def downgrade() -> None:
    raise RuntimeError("unlimited runtime contracts are immutable execution history")
