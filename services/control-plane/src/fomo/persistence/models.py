"""Portable SQLAlchemy models.

UUIDv7 values are stored as strings so the exact same schema works with
PostgreSQL/asyncpg and SQLite/aiosqlite in tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fomo.agent_framework import DEFAULT_AGENT_FRAMEWORK
from fomo.ids import utcnow
from fomo.runtime_contract import (
    DEFAULT_PROFILE_ID,
    LEGACY_CONTEXT_WINDOW,
    LEGACY_INFERENCE_TPM_LIMIT,
    LEGACY_MAX_SPEND_MICROS,
    LEGACY_MODEL_REF,
    LEGACY_RUNTIME_POLICY_VERSION,
    LEGACY_THINKING,
)


class Base(DeclarativeBase):
    pass


class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class SessionRecord(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_user_expires", "user_id", "expires_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), default="user")
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProjectRecord(Base):
    __tablename__ = "projects"
    __table_args__ = (Index("ix_projects_owner_updated", "owner_session_id", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)
    head_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    active_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class MessageRecord(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("project_id", "client_message_id", name="uq_messages_project_client_message"),
        Index("ix_messages_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    client_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RunRecord(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_project_created", "project_id", "created_at"),
        Index("ix_runs_claim", "status", "lease_expires_at", "created_at"),
        Index(
            "uq_runs_one_running_writer_per_project",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
        CheckConstraint(
            "agent_framework IN ('pi', 'opencode', 'codex')",
            name="ck_runs_agent_framework",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    base_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    repair_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Immutable public framework selected when the run is queued. Historical
    # rows are Pi because it was the only production Coding Agent at the time.
    agent_framework: Mapped[str] = mapped_column(
        String(24), nullable=False, default=DEFAULT_AGENT_FRAMEWORK
    )
    # Frozen when the run is queued. Provider aliases never enter the public API,
    # and a clarification resume must use this exact runtime tuple.
    runtime_profile_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_PROFILE_ID
    )
    runtime_model_ref: Mapped[str] = mapped_column(
        String(160), nullable=False, default=LEGACY_MODEL_REF
    )
    runtime_thinking: Mapped[str] = mapped_column(
        String(32), nullable=False, default=LEGACY_THINKING
    )
    runtime_context_window: Mapped[int] = mapped_column(
        Integer, nullable=False, default=LEGACY_CONTEXT_WINDOW
    )
    runtime_policy_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default=LEGACY_RUNTIME_POLICY_VERSION
    )
    # NULL is the explicit runtime-v2 unlimited cumulative-token contract;
    # historical runtime-v0/v1 rows retain their frozen integer value.
    runtime_run_max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runtime_inference_tpm_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=LEGACY_INFERENCE_TPM_LIMIT
    )
    runtime_max_spend_micros: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=LEGACY_MAX_SPEND_MICROS
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sandbox_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The exact Pi JSONL session that owns this run. A clarification resume
    # must use this value rather than synthesizing a replacement session.
    pi_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    continuation_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    continuation_key: Mapped[str | None] = mapped_column(String(96), nullable=True)
    continuation_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    continuation_goal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    continuation_context: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    preview_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class RunEventRecord(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"),
        Index("ix_run_events_run_seq", "run_id", "seq"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RunInputRequestRecord(Base):
    __tablename__ = "run_input_requests"
    __table_args__ = (
        Index("ix_run_input_requests_run_created", "run_id", "created_at"),
        Index(
            "uq_run_input_requests_one_pending",
            "run_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        CheckConstraint(
            "status IN ('pending', 'answered', 'cancelled', 'expired')",
            name="ck_run_input_requests_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    choices: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    allow_freeform: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    goal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    answer_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ArtifactRecord(Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_run_kind", "run_id", "kind"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SpecItemRecord(Base):
    __tablename__ = "spec_items"
    __table_args__ = (UniqueConstraint("project_id", "stable_key", name="uq_spec_items_project_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    stable_key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[str] = mapped_column(String(32), nullable=False, default="must")
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    introduced_run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    retired_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)


class TraceLinkRecord(Base):
    __tablename__ = "trace_links"
    __table_args__ = (
        Index("ix_trace_links_source", "run_id", "source_kind", "source_ref"),
        Index("ix_trace_links_target", "run_id", "target_kind", "target_ref"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    relation: Mapped[str] = mapped_column(String(64), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)


class VerificationEvidenceRecord(Base):
    __tablename__ = "verification_evidence"
    __table_args__ = (Index("ix_evidence_run_ac_status", "run_id", "acceptance_key", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    acceptance_key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)


class VersionRecord(Base):
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("project_id", "number", name="uq_versions_project_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    bundle_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    qa_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class VersionFileRecord(Base):
    __tablename__ = "version_files"
    __table_args__ = (UniqueConstraint("version_id", "path", name="uq_version_files_version_path"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("versions.id"), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)


class GoalGraphRecord(Base):
    __tablename__ = "goal_graphs"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_goal_graphs_run"),
        Index("ix_goal_graphs_project_created", "project_id", "created_at"),
        CheckConstraint(
            "status IN ('active', 'verified', 'failed', 'cancelled', 'superseded')",
            name="ck_goal_graphs_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class GoalGraphRevisionRecord(Base):
    __tablename__ = "revisions"
    __table_args__ = (
        UniqueConstraint("graph_id", "revision", name="uq_revisions_graph_revision"),
        UniqueConstraint("graph_id", "id", name="uq_revisions_graph_id_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    graph_id: Mapped[str] = mapped_column(ForeignKey("goal_graphs.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    product_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    quality_bar: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by_run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GoalNodeRecord(Base):
    __tablename__ = "nodes"
    __table_args__ = (
        UniqueConstraint("revision_id", "goal_key", name="uq_nodes_revision_goal_key"),
        UniqueConstraint("revision_id", "position", name="uq_nodes_revision_position"),
        Index("ix_nodes_graph_revision", "graph_id", "revision_id", "position"),
        Index(
            "uq_nodes_one_current_goal_per_project",
            "project_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'claimed')"),
            sqlite_where=text("status IN ('active', 'claimed')"),
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'claimed', 'verified', 'failed', 'superseded')",
            name="ck_nodes_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    graph_id: Mapped[str] = mapped_column(ForeignKey("goal_graphs.id"), nullable=False)
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    goal_key: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    product_outcome: Mapped[str] = mapped_column(String(500), nullable=False)
    user_visible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    depends_on: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    acceptance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CheckpointRecord(Base):
    __tablename__ = "checkpoints"
    __table_args__ = (
        UniqueConstraint("goal_node_id", name="uq_checkpoints_goal_node"),
        UniqueConstraint("graph_id", "ordinal", name="uq_checkpoints_graph_ordinal"),
        Index("ix_checkpoints_run_created", "run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    graph_id: Mapped[str] = mapped_column(ForeignKey("goal_graphs.id"), nullable=False)
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), nullable=False)
    goal_node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(String(128), nullable=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capsule: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CheckpointFileRecord(Base):
    __tablename__ = "checkpoint_files"
    __table_args__ = (
        UniqueConstraint("checkpoint_id", "path", name="uq_checkpoint_files_checkpoint_path"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(ForeignKey("checkpoints.id"), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)


class GoalEvidenceRecord(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint(
            "checkpoint_id", "acceptance_key", "kind", name="uq_evidence_checkpoint_acceptance_kind"
        ),
        Index("ix_evidence_goal_status", "goal_node_id", "status"),
        CheckConstraint("status = 'passed'", name="ck_evidence_passed_only"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(ForeignKey("checkpoints.id"), nullable=False)
    graph_id: Mapped[str] = mapped_column(ForeignKey("goal_graphs.id"), nullable=False)
    goal_node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    acceptance_key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UsageEntryRecord(Base):
    __tablename__ = "usage_entries"
    __table_args__ = (
        UniqueConstraint("run_id", "request_id", name="uq_usage_entries_run_request"),
        Index("ix_usage_entries_project_created", "project_id", "created_at"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND tool_calls >= 0 AND cost_micros >= 0",
            name="ck_usage_entries_nonnegative",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    graph_id: Mapped[str | None] = mapped_column(ForeignKey("goal_graphs.id"), nullable=True)
    goal_node_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    request_id: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RunSandboxResourceRecord(Base):
    __tablename__ = "run_sandbox_resources"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "sandbox_id", "kind", name="uq_run_sandbox_resources_identity"
        ),
        Index("ix_run_sandbox_resources_pending", "run_id", "cleaned_at"),
        CheckConstraint(
            "kind IN ('generation', 'verification')",
            name="ck_run_sandbox_resources_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    sandbox_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
