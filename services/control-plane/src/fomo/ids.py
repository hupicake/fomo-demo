"""UUIDv7-compatible identifiers without a runtime dependency."""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime
from uuid import UUID


def uuid7() -> str:
    """Return a RFC 9562 UUIDv7 value (millisecond-sortable)."""
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    value = timestamp_ms << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return str(UUID(int=value))


def utcnow() -> datetime:
    return datetime.now(UTC)
