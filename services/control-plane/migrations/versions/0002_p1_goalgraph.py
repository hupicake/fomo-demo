"""Add P1 GoalGraph, verified checkpoint, evidence, and usage ledger tables."""

import sqlalchemy as sa
from alembic import op

revision = "0002_p1_goalgraph"
down_revision = "0001_p0_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "goal_graphs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'verified', 'failed', 'cancelled', 'superseded')",
            name="ck_goal_graphs_status",
        ),
        sa.UniqueConstraint("run_id", name="uq_goal_graphs_run"),
    )
    op.create_index("ix_goal_graphs_project_created", "goal_graphs", ["project_id", "created_at"])
    op.create_table(
        "revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("graph_id", sa.String(36), sa.ForeignKey("goal_graphs.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("product_outcome", sa.Text(), nullable=False),
        sa.Column("quality_bar", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("created_by_run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("graph_id", "revision", name="uq_revisions_graph_revision"),
        sa.UniqueConstraint("graph_id", "id", name="uq_revisions_graph_id_id"),
    )
    op.create_table(
        "nodes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("graph_id", sa.String(36), sa.ForeignKey("goal_graphs.id"), nullable=False),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("revisions.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("goal_key", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("product_outcome", sa.String(500), nullable=False),
        sa.Column("user_visible", sa.Boolean(), nullable=False),
        sa.Column("depends_on", sa.JSON(), nullable=False),
        sa.Column("acceptance", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'claimed', 'verified', 'failed', 'superseded')",
            name="ck_nodes_status",
        ),
        sa.UniqueConstraint("revision_id", "goal_key", name="uq_nodes_revision_goal_key"),
        sa.UniqueConstraint("revision_id", "position", name="uq_nodes_revision_position"),
    )
    op.create_index("ix_nodes_graph_revision", "nodes", ["graph_id", "revision_id", "position"])
    op.create_index(
        "uq_nodes_one_current_goal_per_project",
        "nodes",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'claimed')"),
        sqlite_where=sa.text("status IN ('active', 'claimed')"),
    )
    op.create_table(
        "checkpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("graph_id", sa.String(36), sa.ForeignKey("goal_graphs.id"), nullable=False),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("revisions.id"), nullable=False),
        sa.Column("goal_node_id", sa.String(36), sa.ForeignKey("nodes.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("commit_sha", sa.String(128)),
        sa.Column("snapshot_id", sa.String(128)),
        sa.Column("capsule", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("goal_node_id", name="uq_checkpoints_goal_node"),
        sa.UniqueConstraint("graph_id", "ordinal", name="uq_checkpoints_graph_ordinal"),
    )
    op.create_index("ix_checkpoints_run_created", "checkpoints", ["run_id", "created_at"])
    op.create_table(
        "checkpoint_files",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("checkpoint_id", sa.String(36), sa.ForeignKey("checkpoints.id"), nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.UniqueConstraint("checkpoint_id", "path", name="uq_checkpoint_files_checkpoint_path"),
    )
    op.create_table(
        "evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("checkpoint_id", sa.String(36), sa.ForeignKey("checkpoints.id"), nullable=False),
        sa.Column("graph_id", sa.String(36), sa.ForeignKey("goal_graphs.id"), nullable=False),
        sa.Column("goal_node_id", sa.String(36), sa.ForeignKey("nodes.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("acceptance_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id")),
        sa.Column("reference", sa.String(1024)),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status = 'passed'", name="ck_evidence_passed_only"),
        sa.UniqueConstraint(
            "checkpoint_id", "acceptance_key", "kind", name="uq_evidence_checkpoint_acceptance_kind"
        ),
    )
    op.create_index("ix_evidence_goal_status", "evidence", ["goal_node_id", "status"])
    op.create_table(
        "usage_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("graph_id", sa.String(36), sa.ForeignKey("goal_graphs.id")),
        sa.Column("goal_node_id", sa.String(36), sa.ForeignKey("nodes.id")),
        sa.Column("request_id", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND tool_calls >= 0 AND cost_micros >= 0",
            name="ck_usage_entries_nonnegative",
        ),
        sa.UniqueConstraint("run_id", "request_id", name="uq_usage_entries_run_request"),
    )
    op.create_index("ix_usage_entries_project_created", "usage_entries", ["project_id", "created_at"])
    op.create_table(
        "run_sandbox_resources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("sandbox_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleaned_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('generation', 'verification')",
            name="ck_run_sandbox_resources_kind",
        ),
        sa.UniqueConstraint(
            "run_id", "sandbox_id", "kind", name="uq_run_sandbox_resources_identity"
        ),
    )
    op.create_index(
        "ix_run_sandbox_resources_pending",
        "run_sandbox_resources",
        ["run_id", "cleaned_at"],
    )


def downgrade() -> None:
    raise RuntimeError("P1 downgrade is intentionally unsupported; checkpoints are recovery truth")
