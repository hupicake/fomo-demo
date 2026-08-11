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
async def settings(tmp_path: Path, monkeypatch) -> Settings:
    async def discovered_model_aliases(_self) -> set[str]:
        return {
            "fomo-pi-deepseek-flash",
            "fomo-pi-gpt-5.6",
            "fomo-pi-kimi-k2.7-code",
        }

    monkeypatch.setattr(
        "fomo.api.app.LiteLLMRunKeyClient.discover_model_aliases",
        discovered_model_aliases,
    )
    return Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'fomo-test.db'}",
        litellm_api_key="sk-test-management",
        worker_poll_interval_seconds=0.01,
    )
