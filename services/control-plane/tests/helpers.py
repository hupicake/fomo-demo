from __future__ import annotations

from uuid import uuid4

from fomo.persistence import Repository
from fomo.persistence.models import SessionRecord


async def create_user_session(repository: Repository) -> SessionRecord:
    """Create an isolated authenticated owner for repository-level tests."""
    _, session = await repository.register_user(
        f"test-{uuid4().hex}@example.test",
        "test-only password",
    )
    return session
