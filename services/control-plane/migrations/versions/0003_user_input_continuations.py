"""Add durable Direct Pi clarification requests and continuation identity."""

import sqlalchemy as sa
from alembic import op

revision = "0003_user_input_continuations"
down_revision = "0002_p1_goalgraph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("pi_session_id", sa.String(128), nullable=True))
    op.add_column(
        "runs", sa.Column("continuation_request_id", sa.String(36), nullable=True)
    )
    op.add_column("runs", sa.Column("continuation_key", sa.String(96), nullable=True))
    op.add_column("runs", sa.Column("continuation_stage", sa.String(32), nullable=True))
    op.add_column("runs", sa.Column("continuation_goal_id", sa.String(64), nullable=True))
    op.add_column("runs", sa.Column("continuation_context", sa.JSON(), nullable=True))
    op.create_table(
        "run_input_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("choices", sa.JSON(), nullable=False),
        sa.Column("allow_freeform", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("goal_id", sa.String(64), nullable=True),
        sa.Column(
            "answer_message_id",
            sa.String(36),
            sa.ForeignKey("messages.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'answered', 'cancelled', 'expired')",
            name="ck_run_input_requests_status",
        ),
    )
    op.create_index(
        "ix_run_input_requests_run_created",
        "run_input_requests",
        ["run_id", "created_at"],
    )
    op.create_index(
        "uq_run_input_requests_one_pending",
        "run_input_requests",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    raise RuntimeError("clarification continuations are durable run history")
