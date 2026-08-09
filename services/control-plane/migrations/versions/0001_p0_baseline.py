"""P0 baseline schema.

This revision describes the existing durable schema. Existing databases are
fingerprinted and stamped at this revision by ``Database.upgrade``; Alembic
only executes these CREATE statements for an empty database.
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_p0_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("user_id", sa.String(36)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_session_id", sa.String(36), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("head_version_id", sa.String(36)),
        sa.Column("active_run_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_projects_owner_updated", "projects", ["owner_session_id", "updated_at"])
    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("client_message_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "client_message_id", name="uq_messages_project_client_message"),
    )
    op.create_index("ix_messages_project_created", "messages", ["project_id", "created_at"])
    op.create_table(
        "runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("base_version_id", sa.String(36)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("repair_round", sa.Integer(), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(80)),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("sandbox_id", sa.String(128)),
        sa.Column("preview_url", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_runs_project_created", "runs", ["project_id", "created_at"])
    op.create_index("ix_runs_claim", "runs", ["status", "lease_expires_at", "created_at"])
    op.create_index(
        "uq_runs_one_running_writer_per_project",
        "runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
        sqlite_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "run_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("role", sa.String(32)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"),
    )
    op.create_index("ix_run_events_run_seq", "run_events", ["run_id", "seq"])
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("object_key", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_artifacts_run_kind", "artifacts", ["run_id", "kind"])
    op.create_table(
        "spec_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("stable_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("priority", sa.String(32), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("introduced_run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("retired_run_id", sa.String(36), sa.ForeignKey("runs.id")),
        sa.UniqueConstraint("project_id", "stable_key", name="uq_spec_items_project_key"),
    )
    op.create_table(
        "trace_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("source_kind", sa.String(64), nullable=False),
        sa.Column("source_ref", sa.String(256), nullable=False),
        sa.Column("relation", sa.String(64), nullable=False),
        sa.Column("target_kind", sa.String(64), nullable=False),
        sa.Column("target_ref", sa.String(256), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
    )
    op.create_index("ix_trace_links_source", "trace_links", ["run_id", "source_kind", "source_ref"])
    op.create_index("ix_trace_links_target", "trace_links", ["run_id", "target_kind", "target_ref"])
    op.create_table(
        "verification_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("acceptance_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("artifact_id", sa.String(36)),
        sa.Column("object_key", sa.String(512)),
        sa.Column("summary", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_evidence_run_ac_status",
        "verification_evidence",
        ["run_id", "acceptance_key", "status"],
    )
    op.create_table(
        "versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("commit_sha", sa.String(128), nullable=False),
        sa.Column("parent_version_id", sa.String(36)),
        sa.Column("bundle_key", sa.String(512)),
        sa.Column("snapshot_id", sa.String(128)),
        sa.Column("qa_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "number", name="uq_versions_project_number"),
    )
    op.create_table(
        "version_files",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("versions.id"), nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("content_text", sa.Text()),
        sa.Column("object_key", sa.String(512)),
        sa.UniqueConstraint("version_id", "path", name="uq_version_files_version_path"),
    )


def downgrade() -> None:
    raise RuntimeError("P0 baseline downgrade is intentionally unsupported; historical data is append-only")
