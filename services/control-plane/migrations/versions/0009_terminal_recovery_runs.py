"""Fork terminal failures into immutable recovery runs."""

import sqlalchemy as sa
from alembic import op

revision = "0009_terminal_recovery_runs"
down_revision = "0008_codex_agent_framework"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(
            sa.Column("recovered_from_run_id", sa.String(36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("recovered_from_goal_id", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("recovered_from_checkpoint_id", sa.String(36), nullable=True)
        )
        batch_op.add_column(sa.Column("recovery_mode", sa.String(32), nullable=True))
        batch_op.create_foreign_key(
            "fk_runs_recovered_from_run",
            "runs",
            ["recovered_from_run_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_runs_recovered_from_checkpoint",
            "checkpoints",
            ["recovered_from_checkpoint_id"],
            ["id"],
        )
        batch_op.create_check_constraint(
            "ck_runs_recovery_mode",
            "recovery_mode IS NULL OR recovery_mode IN "
            "('verified_checkpoint', 'verified_version', 'base_restart')",
        )
        batch_op.create_check_constraint(
            "ck_runs_recovery_lineage",
            "(recovery_mode IS NULL AND recovered_from_run_id IS NULL "
            "AND recovered_from_goal_id IS NULL "
            "AND recovered_from_checkpoint_id IS NULL) "
            "OR (recovery_mode = 'verified_checkpoint' "
            "AND recovered_from_run_id IS NOT NULL "
            "AND recovered_from_goal_id IS NOT NULL "
            "AND recovered_from_checkpoint_id IS NOT NULL) "
            "OR (recovery_mode = 'verified_version' "
            "AND recovered_from_run_id IS NOT NULL "
            "AND recovered_from_goal_id IS NULL "
            "AND recovered_from_checkpoint_id IS NULL "
            "AND base_version_id IS NOT NULL) "
            "OR (recovery_mode = 'base_restart' "
            "AND recovered_from_run_id IS NOT NULL "
            "AND recovered_from_goal_id IS NULL "
            "AND recovered_from_checkpoint_id IS NULL "
            "AND base_version_id IS NULL)",
        )
        batch_op.create_index(
            "ix_runs_recovered_from",
            ["recovered_from_run_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    raise RuntimeError("terminal recovery lineage is immutable execution history")
