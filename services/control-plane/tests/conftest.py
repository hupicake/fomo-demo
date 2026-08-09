from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from fomo.config import Settings
from fomo.persistence import Database, Repository


@pytest_asyncio.fixture
async def repository(tmp_path: Path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'fomo-test.db'}")
    repository = Repository(database)
    await repository.initialize()
    yield repository
    await database.dispose()


@pytest_asyncio.fixture
async def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'fomo-test.db'}",
        # Native is a deliberately explicit test-mode diagnostic path. Runtime
        # defaults remain Direct Pi.
        agent_framework="native",
        sandbox_provider="fake",
        allow_unsafe_process_sandbox=False,
        dev_sandbox_root=tmp_path / "sandboxes",
        structured_output_retries=0,
        worker_poll_interval_seconds=0.01,
    )
