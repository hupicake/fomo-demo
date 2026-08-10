"""Freeze the selected Coding Agent framework on every run."""

import sqlalchemy as sa
from alembic import op

revision = "0007_run_agent_framework"
down_revision = "0006_unlimited_run_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Pi is the only framework that could have created historical runs. New
    # runs always write their explicitly resolved framework in the repository.
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "agent_framework",
                sa.String(24),
                nullable=False,
                server_default="pi",
            )
        )
        batch_op.create_check_constraint(
            "ck_runs_agent_framework",
            "agent_framework IN ('pi', 'opencode')",
        )


def downgrade() -> None:
    raise RuntimeError("run agent frameworks are immutable execution history")
