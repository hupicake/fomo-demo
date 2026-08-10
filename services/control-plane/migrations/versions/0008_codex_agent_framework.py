"""Allow Codex as an immutable per-run Coding Agent framework."""

from alembic import op

revision = "0008_codex_agent_framework"
down_revision = "0007_run_agent_framework"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing Pi/OpenCode values remain untouched. Batch mode keeps the
    # constraint replacement portable across production Postgres and test SQLite.
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_constraint("ck_runs_agent_framework", type_="check")
        batch_op.create_check_constraint(
            "ck_runs_agent_framework",
            "agent_framework IN ('pi', 'opencode', 'codex')",
        )


def downgrade() -> None:
    raise RuntimeError("Codex run frameworks are immutable execution history")
