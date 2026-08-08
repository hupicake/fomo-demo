"""Immutable fomo-pi-ds invocation: request validation and bridge command/env construction.

The control plane never launches Pi directly. It runs the root-owned bridge
``/opt/fomo/bin/fomo-pi-rpc-bridge.mjs`` inside the generation sandbox G with a
strict environment contract:

- the prompt enters only as base64 in ``FOMO_PI_PROMPT_B64``,
- the opaque run-scoped virtual key enters only in ``FOMO_PI_VIRTUAL_KEY``,
- the command line contains neither the prompt nor any key.

``PiRequest`` is the immutable, validated input; ``PiInvocation`` derives the
bridge command line and environment from it. ``repr`` never includes the
prompt or the virtual key.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Environment contract shared with infra/opensandbox/fomo-pi-rpc-bridge.mjs.
FOMO_PI_PROMPT_B64 = "FOMO_PI_PROMPT_B64"
FOMO_PI_SESSION_ID = "FOMO_PI_SESSION_ID"
FOMO_PI_REQUEST_ID = "FOMO_PI_REQUEST_ID"
FOMO_PI_CORRELATION_ID = "FOMO_PI_CORRELATION_ID"
FOMO_PI_PROVIDER_BASE_URL = "FOMO_PI_PROVIDER_BASE_URL"
FOMO_PI_VIRTUAL_KEY = "FOMO_PI_VIRTUAL_KEY"
FOMO_PI_WORKSPACE = "FOMO_PI_WORKSPACE"
FOMO_PI_STATE_DIR = "FOMO_PI_STATE_DIR"
FOMO_PI_BIN = "FOMO_PI_BIN"
FOMO_PI_TIMEOUT_SECONDS = "FOMO_PI_TIMEOUT_SECONDS"
FOMO_PI_GRACE_SECONDS = "FOMO_PI_GRACE_SECONDS"

DEFAULT_BRIDGE_BIN = "/opt/fomo/bin/fomo-pi-rpc-bridge.mjs"
DEFAULT_PI_BIN = "/opt/fomo/pi/bin/pi"
DEFAULT_STATE_DIR = "/var/lib/fomo-pi"
DEFAULT_WORKSPACE = "/workspace"
FOMO_PI_MODEL = "fomo-litellm/fomo-pi-flash"
FOMO_PI_THINKING = "max"

# Mirrors the bridge's own limits so validation fails before any sandbox call.
MAX_PROMPT_CHARACTERS = 100_000
MAX_IDENTIFIER_LENGTH = 128
MAX_VIRTUAL_KEY_LENGTH = 4096
MAX_GRACE_SECONDS = 60

# Pi validates session ids with this exact pattern (core/session-manager.ts).
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

def _require_identifier(value: str, name: str) -> None:
    if not value or len(value) > MAX_IDENTIFIER_LENGTH or not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(
            f"{name} must be a non-empty identifier of at most {MAX_IDENTIFIER_LENGTH} characters"
        )


def _require_absolute_path(value: str, name: str) -> None:
    if not value or not value.startswith("/"):
        raise ValueError(f"{name} must be an absolute path")


def _require_http_url(value: str, name: str) -> None:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} must contain a valid port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.rstrip("/").endswith("/v1")
        or port is not None
        and not 1 <= port <= 65_535
    ):
        raise ValueError(
            f"{name} must be an http(s) URL ending in /v1 without userinfo, query, or fragment"
        )


@dataclass(frozen=True, slots=True)
class PiRequest:
    """Immutable, validated fomo-pi-ds foreground invocation request."""

    request_id: str
    correlation_id: str
    session_id: str
    provider_base_url: str
    # Secrets: excluded from repr and never written to logs by this package.
    prompt: str = field(repr=False)
    virtual_key: str = field(repr=False)
    workspace: str = DEFAULT_WORKSPACE
    state_dir: str = DEFAULT_STATE_DIR
    bridge_bin: str = DEFAULT_BRIDGE_BIN
    pi_bin: str = DEFAULT_PI_BIN
    thinking: str = FOMO_PI_THINKING
    model: str = FOMO_PI_MODEL
    timeout_seconds: int | None = None
    grace_seconds: int = 10

    def __post_init__(self) -> None:
        _require_identifier(self.request_id, "request_id")
        _require_identifier(self.correlation_id, "correlation_id")
        if not SESSION_ID_PATTERN.fullmatch(self.session_id):
            raise ValueError("session_id is not a valid Pi session id")
        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")
        if len(self.prompt) > MAX_PROMPT_CHARACTERS:
            raise ValueError(f"prompt exceeds {MAX_PROMPT_CHARACTERS} characters")
        if not self.virtual_key or len(self.virtual_key) > MAX_VIRTUAL_KEY_LENGTH:
            raise ValueError("virtual_key must be non-empty and bounded")
        if any(ord(character) < 32 or ord(character) == 127 for character in self.virtual_key):
            raise ValueError("virtual_key cannot contain control characters")
        if self.thinking != FOMO_PI_THINKING:
            raise ValueError(f"thinking must be fixed to {FOMO_PI_THINKING}")
        if self.model != FOMO_PI_MODEL:
            raise ValueError(f"model must be fixed to {FOMO_PI_MODEL}")
        _require_http_url(self.provider_base_url, "provider_base_url")
        _require_absolute_path(self.workspace, "workspace")
        _require_absolute_path(self.state_dir, "state_dir")
        _require_absolute_path(self.bridge_bin, "bridge_bin")
        _require_absolute_path(self.pi_bin, "pi_bin")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive or None")
        if not 1 <= self.grace_seconds <= MAX_GRACE_SECONDS:
            raise ValueError(f"grace_seconds must be between 1 and {MAX_GRACE_SECONDS}")


@dataclass(frozen=True, slots=True)
class PiInvocation:
    """Bridge command line and environment derived from an immutable request."""

    request: PiRequest

    def command_line(self) -> tuple[str, ...]:
        """Executable argv for the bridge. Never contains prompt or key material."""
        return (self.request.bridge_bin,)

    def fomo_environment(self) -> dict[str, str]:
        """The FOMO_PI_* contract only; caller merges with its own base env."""
        request = self.request
        prompt_b64 = base64.b64encode(request.prompt.encode("utf-8")).decode("ascii")
        environment = {
            FOMO_PI_PROMPT_B64: prompt_b64,
            FOMO_PI_SESSION_ID: request.session_id,
            FOMO_PI_REQUEST_ID: request.request_id,
            FOMO_PI_CORRELATION_ID: request.correlation_id,
            FOMO_PI_PROVIDER_BASE_URL: request.provider_base_url,
            FOMO_PI_VIRTUAL_KEY: request.virtual_key,
            FOMO_PI_WORKSPACE: request.workspace,
            FOMO_PI_STATE_DIR: request.state_dir,
            FOMO_PI_BIN: request.pi_bin,
            FOMO_PI_GRACE_SECONDS: str(request.grace_seconds),
        }
        if request.timeout_seconds is not None:
            environment[FOMO_PI_TIMEOUT_SECONDS] = str(request.timeout_seconds)
        return environment

    def redact(self, text: str) -> str:
        """Replace the virtual key and the prompt (raw and base64) with a marker."""
        request = self.request
        prompt_b64 = base64.b64encode(request.prompt.encode("utf-8")).decode("ascii")
        value = text
        for secret in (request.virtual_key, request.prompt, prompt_b64):
            if secret and secret in value:
                value = value.replace(secret, "[redacted]")
        return value

    def __repr__(self) -> str:
        request = self.request
        return (
            f"PiInvocation(request_id={request.request_id!r}, "
            f"session_id={request.session_id!r}, workspace={request.workspace!r}, "
            f"state_dir={request.state_dir!r}, model={request.model!r}, "
            f"thinking={request.thinking!r}, timeout_seconds={request.timeout_seconds!r})"
        )
