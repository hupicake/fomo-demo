from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from fomo.persistence import Database, MigrationStateError, Repository
from fomo.persistence.database import HEAD_REVISION, P0_REVISION


@pytest.mark.asyncio
async def test_fresh_database_upgrades_to_head(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    try:
        await database.upgrade()
        await database.upgrade()
        assert await database.current_revision() == HEAD_REVISION
    finally:
        await database.dispose()
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {
        "goal_graphs",
        "revisions",
        "nodes",
        "checkpoints",
        "checkpoint_files",
        "evidence",
        "usage_entries",
        "run_sandbox_resources",
        "run_input_requests",
        "users",
    } <= tables
    with sqlite3.connect(path) as connection:
        session_columns = {row[1] for row in connection.execute("PRAGMA table_info('sessions')")}
        assert "revoked_at" in session_columns


@pytest.mark.asyncio
async def test_unversioned_p0_database_is_fingerprinted_stamped_and_preserved(
    tmp_path: Path,
) -> None:
    path = tmp_path / "p0.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    try:
        await asyncio.to_thread(database._alembic_command, "upgrade", P0_REVISION)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO sessions (id, kind, user_id, expires_at, created_at) "
                "VALUES ('legacy', 'guest', NULL, '2099-01-01', '2026-01-01')"
            )
            connection.execute("DROP TABLE alembic_version")
            connection.commit()
        await database.upgrade()
        assert await database.current_revision() == HEAD_REVISION
        assert (await Repository(database).get_session("legacy")).kind == "guest"
    finally:
        await database.dispose()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT kind FROM sessions WHERE id = 'legacy'").fetchone() == (
            "guest",
        )


@pytest.mark.asyncio
async def test_unknown_unversioned_schema_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "unknown.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE mystery (id INTEGER PRIMARY KEY)")
    database = Database(f"sqlite+aiosqlite:///{path}")
    try:
        with pytest.raises(MigrationStateError):
            await database.upgrade()
    finally:
        await database.dispose()
