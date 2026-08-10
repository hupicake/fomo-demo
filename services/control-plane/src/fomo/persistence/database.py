"""Async SQLAlchemy database lifecycle."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base

P0_REVISION = "0001_p0_baseline"
HEAD_REVISION = "0008_codex_agent_framework"
P0_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "sessions": frozenset({"id", "kind", "expires_at", "created_at"}),
    "projects": frozenset({"id", "owner_session_id", "title", "status", "created_at", "updated_at"}),
    "messages": frozenset({"id", "project_id", "content", "client_message_id", "created_at"}),
    "runs": frozenset({"id", "project_id", "status", "phase", "lease_owner", "lease_expires_at"}),
    "run_events": frozenset({"id", "run_id", "seq", "kind", "payload", "created_at"}),
    "artifacts": frozenset({"id", "run_id", "kind", "schema_version", "content", "created_at"}),
    "spec_items": frozenset({"id", "project_id", "stable_key", "introduced_run_id"}),
    "trace_links": frozenset({"id", "run_id", "source_kind", "target_kind", "metadata"}),
    "verification_evidence": frozenset({"id", "run_id", "acceptance_key", "kind", "status"}),
    "versions": frozenset({"id", "project_id", "number", "commit_sha", "qa_status"}),
    "version_files": frozenset({"id", "version_id", "path", "sha256", "size"}),
}
P1_TABLES = frozenset(
    {
        "goal_graphs",
        "revisions",
        "nodes",
        "checkpoints",
        "checkpoint_files",
        "evidence",
        "usage_entries",
        "run_sandbox_resources",
        "run_input_requests",
    }
)


class MigrationStateError(RuntimeError):
    """The database cannot be safely identified as empty, P0, or versioned."""


class Database:
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: AsyncEngine = create_async_engine(url, future=True, connect_args=connect_args)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", self._enable_sqlite_foreign_keys)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async def create_all(self) -> None:
        """Test/bootstrap escape hatch; production initialization uses migrations."""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def upgrade(self) -> None:
        """Upgrade an empty, explicitly fingerprinted P0, or versioned database.

        An unversioned P0 database is stamped only after every expected table
        and identifying column is present. Partial/unknown schemas fail closed;
        no table is rebuilt and no historical row is rewritten.
        """
        state = await self._migration_state()
        if state == "unknown":
            raise MigrationStateError(
                "unversioned database does not match the P0 fingerprint; refusing migration"
            )
        if state == "p0":
            await asyncio.to_thread(self._alembic_command, "stamp", P0_REVISION)
        await asyncio.to_thread(self._alembic_command, "upgrade", "head")

    async def current_revision(self) -> str | None:
        async with self.engine.connect() as connection:
            return await connection.run_sync(self._current_revision_sync)

    async def _migration_state(self) -> str:
        async with self.engine.connect() as connection:
            return await connection.run_sync(self._migration_state_sync)

    @staticmethod
    def _current_revision_sync(connection) -> str | None:
        inspector = inspect(connection)
        if "alembic_version" not in inspector.get_table_names():
            return None
        return connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one_or_none()

    @staticmethod
    def _migration_state_sync(connection) -> str:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        if "alembic_version" in tables:
            return "versioned"
        if not tables:
            return "empty"
        if P1_TABLES.intersection(tables):
            return "unknown"
        if tables != set(P0_TABLE_COLUMNS):
            return "unknown"
        for table, required in P0_TABLE_COLUMNS.items():
            actual = {column["name"] for column in inspector.get_columns(table)}
            if not required.issubset(actual):
                return "unknown"
        return "p0"

    def _alembic_command(self, action: str, revision: str) -> None:
        root = Path(__file__).resolve().parents[3]
        configuration = Config(str(root / "alembic.ini"))
        # ConfigParser treats percent signs specially; URL-escaped credentials
        # therefore need doubling when passed through Alembic configuration.
        configuration.set_main_option("sqlalchemy.url", self._migration_url().replace("%", "%%"))
        getattr(command, action)(configuration, revision)

    def _migration_url(self) -> str:
        return self.engine.url.render_as_string(hide_password=False)

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session


def run_migrations() -> None:
    """CLI entrypoint: ``fomo-migrate`` upgrades DATABASE_URL to P1 head."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")

    async def _run() -> None:
        database = Database(url)
        try:
            await database.upgrade()
        finally:
            await database.dispose()

    asyncio.run(_run())
