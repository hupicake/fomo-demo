from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from fomo.persistence import Database, MigrationStateError
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
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info('runs')")}
        assert {
            "runtime_profile_id",
            "runtime_model_ref",
            "runtime_thinking",
            "runtime_context_window",
            "runtime_policy_version",
            "runtime_run_max_tokens",
            "runtime_inference_tpm_limit",
            "runtime_max_spend_micros",
            "agent_framework",
        } <= run_columns
        token_budget_column = next(
            row
            for row in connection.execute("PRAGMA table_info('runs')")
            if row[1] == "runtime_run_max_tokens"
        )
        assert token_budget_column[3] == 0  # nullable: NULL means unlimited
        assert token_budget_column[4] is None


@pytest.mark.asyncio
async def test_pre_model_selection_run_upgrades_with_legacy_execution_contract(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-run.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    try:
        await asyncio.to_thread(database._alembic_command, "upgrade", "0004_accounts")
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO sessions (id, kind, user_id, expires_at, created_at) "
                "VALUES ('session-1', 'guest', NULL, '2099-01-01', '2026-01-01')"
            )
            connection.execute(
                "INSERT INTO projects "
                "(id, owner_session_id, title, status, created_at, updated_at) "
                "VALUES ('project-1', 'session-1', 'Legacy', 'queued', "
                "'2026-01-01', '2026-01-01')"
            )
            connection.execute(
                "INSERT INTO runs "
                "(id, project_id, status, phase, repair_round, created_at, updated_at) "
                "VALUES ('run-1', 'project-1', 'queued', 'queued', 0, "
                "'2026-01-01', '2026-01-01')"
            )
            connection.commit()
        await database.upgrade()
    finally:
        await database.dispose()

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT runtime_profile_id, runtime_model_ref, runtime_thinking, "
            "runtime_context_window, runtime_policy_version, runtime_run_max_tokens, "
            "runtime_inference_tpm_limit, runtime_max_spend_micros, agent_framework "
            "FROM runs WHERE id = 'run-1'"
        ).fetchone()
    assert row == (
        "deepseek-flash",
        "fomo-litellm/fomo-pi-flash",
        "high",
        200_000,
        "direct-pi-legacy-v0",
        400_000,
        1_000_000,
        2_000_000,
        "pi",
    )


@pytest.mark.asyncio
async def test_codex_framework_constraint_upgrade_preserves_old_runs_and_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "codex-framework.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    try:
        await asyncio.to_thread(
            database._alembic_command,
            "upgrade",
            "0007_run_agent_framework",
        )
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO sessions (id, kind, user_id, expires_at, created_at) "
                "VALUES ('session-1', 'guest', NULL, '2099-01-01', '2026-01-01')"
            )
            connection.execute(
                "INSERT INTO projects "
                "(id, owner_session_id, title, status, created_at, updated_at) "
                "VALUES ('project-1', 'session-1', 'Existing', 'queued', "
                "'2026-01-01', '2026-01-01')"
            )
            connection.execute(
                "INSERT INTO runs "
                "(id, project_id, status, phase, repair_round, agent_framework, "
                "created_at, updated_at) VALUES "
                "('run-pi', 'project-1', 'queued', 'queued', 0, 'pi', "
                "'2026-01-01', '2026-01-01')"
            )
            connection.commit()

        await database.upgrade()
        assert await database.current_revision() == HEAD_REVISION
    finally:
        await database.dispose()

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT agent_framework FROM runs WHERE id = 'run-pi'"
        ).fetchone() == ("pi",)
        connection.execute(
            "INSERT INTO runs "
            "(id, project_id, status, phase, repair_round, agent_framework, "
            "created_at, updated_at) VALUES "
            "('run-codex', 'project-1', 'queued', 'queued', 0, 'codex', "
            "'2026-01-02', '2026-01-02')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO runs "
                "(id, project_id, status, phase, repair_round, agent_framework, "
                "created_at, updated_at) VALUES "
                "('run-unknown', 'project-1', 'queued', 'queued', 0, 'unknown', "
                "'2026-01-03', '2026-01-03')"
            )


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
