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
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from fomo.runtime_contract import (
    MAX_CONTEXT_WINDOW as RUNTIME_MAX_CONTEXT_WINDOW,
)
from fomo.runtime_contract import (
    allowed_model_refs,
    context_limit_for_model_ref,
    thinking_levels_for_model_ref,
)

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
FOMO_PI_THINKING_LEVEL = "FOMO_PI_THINKING_LEVEL"
FOMO_PI_MODEL_REF = "FOMO_PI_MODEL_REF"
FOMO_PI_CONTEXT_WINDOW = "FOMO_PI_CONTEXT_WINDOW"
FOMO_PI_ACTIVITY_SILENCE_SECONDS = "FOMO_PI_ACTIVITY_SILENCE_SECONDS"
FOMO_PI_TIMEOUT_SECONDS = "FOMO_PI_TIMEOUT_SECONDS"
FOMO_PI_GRACE_SECONDS = "FOMO_PI_GRACE_SECONDS"
FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64 = "FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64"
FOMO_PI_USER_INPUT_ENABLED = "FOMO_PI_USER_INPUT_ENABLED"
FOMO_PI_REQUIRE_RESUME = "FOMO_PI_REQUIRE_RESUME"

DEFAULT_BRIDGE_BIN = "/opt/fomo/bin/fomo-pi-rpc-bridge.mjs"
DEFAULT_PI_BIN = "/opt/fomo/pi/bin/pi"
DEFAULT_STATE_DIR = "/var/lib/fomo-pi"
DEFAULT_WORKSPACE = "/workspace"
FOMO_PI_PLANNING_MODEL = "fomo-litellm/fomo-pi-flash"
FOMO_PI_BUILD_MODEL = "fomo-litellm/fomo-pi-build"
FOMO_PI_MODEL = FOMO_PI_BUILD_MODEL
FOMO_PI_MODELS = allowed_model_refs()
# Default thinking must be legal for the default model (fomo-pi-build):
# "high" is the shared level between the flash and build aliases.
FOMO_PI_THINKING = "high"
FOMO_PI_THINKING_LEVELS = frozenset(
    level
    for model_ref in FOMO_PI_MODELS
    for level in thinking_levels_for_model_ref(model_ref)
)
# Mirror the bridge's model-specific thinking maps so invalid combinations
# fail in Python instead of waiting for the bridge. (The bridge accepts "off"
# for both aliases because neither map declares it, so it is included here.)
_MODEL_THINKING_LEVELS = {
    model_ref: thinking_levels_for_model_ref(model_ref)
    for model_ref in FOMO_PI_MODELS
}

# Mirrors the bridge's own limits so validation fails before any sandbox call.
MAX_PROMPT_CHARACTERS = 100_000
MAX_IDENTIFIER_LENGTH = 128
MAX_VIRTUAL_KEY_LENGTH = 4096
MAX_GRACE_SECONDS = 60
MAX_STRUCTURED_OUTPUT_SCHEMA_BYTES = 64 * 1024

# FOMO declares a 200K logical context budget even when the upstream model
# supports more. The bridge couples this window to explicit compaction settings
# so output headroom is reserved before the product budget is exhausted.
DEFAULT_CONTEXT_WINDOW = 200_000
MAX_CONTEXT_WINDOW = RUNTIME_MAX_CONTEXT_WINDOW
MAX_ACTIVITY_SILENCE_SECONDS = 3_600

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


def _canonical_structured_output_schema(
    value: dict[str, object] | None,
) -> tuple[dict[str, object] | None, bytes | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict) or value.get("type") != "object":
        raise ValueError("structured_output_schema must be a JSON object schema")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        copied = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("structured_output_schema must contain valid JSON") from exc
    if len(encoded) > MAX_STRUCTURED_OUTPUT_SCHEMA_BYTES:
        raise ValueError(
            f"structured_output_schema exceeds {MAX_STRUCTURED_OUTPUT_SCHEMA_BYTES} bytes"
        )
    return copied, encoded


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
    context_window: int = DEFAULT_CONTEXT_WINDOW
    # Bridge-level Pi protocol inactivity budget. Hidden reasoning content is
    # never published, but its validated stream events still prove the model is
    # alive and reset this watchdog.
    activity_silence_seconds: int | None = None
    timeout_seconds: int | None = None
    grace_seconds: int = 10
    # Planning-only JSON Schema for the trusted terminating virtual tool.
    # It is copied during validation so later caller mutations cannot alter
    # the invocation contract.
    structured_output_schema: dict[str, object] | None = field(
        default=None,
        repr=False,
    )
    user_input_enabled: bool = False
    require_resume: bool = False

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
        if self.thinking not in FOMO_PI_THINKING_LEVELS:
            allowed = ", ".join(sorted(FOMO_PI_THINKING_LEVELS))
            raise ValueError(f"thinking must be one of: {allowed}")
        if self.model not in FOMO_PI_MODELS:
            allowed = ", ".join(sorted(FOMO_PI_MODELS))
            raise ValueError(f"model must be one of: {allowed}")
        if self.thinking not in _MODEL_THINKING_LEVELS[self.model]:
            allowed = ", ".join(sorted(_MODEL_THINKING_LEVELS[self.model]))
            raise ValueError(
                f"thinking level {self.thinking!r} is not supported by {self.model}; "
                f"allowed: {allowed}"
            )
        if not 1 <= self.context_window <= MAX_CONTEXT_WINDOW:
            raise ValueError(f"context_window must be between 1 and {MAX_CONTEXT_WINDOW}")
        model_context_limit = context_limit_for_model_ref(self.model)
        if self.context_window > model_context_limit:
            raise ValueError(
                f"context_window exceeds the {model_context_limit} limit for {self.model}"
            )
        if self.activity_silence_seconds is not None and not (
            1 <= self.activity_silence_seconds <= MAX_ACTIVITY_SILENCE_SECONDS
        ):
            raise ValueError(
                "activity_silence_seconds must be between 1 and "
                f"{MAX_ACTIVITY_SILENCE_SECONDS}"
            )
        _require_http_url(self.provider_base_url, "provider_base_url")
        _require_absolute_path(self.workspace, "workspace")
        _require_absolute_path(self.state_dir, "state_dir")
        _require_absolute_path(self.bridge_bin, "bridge_bin")
        _require_absolute_path(self.pi_bin, "pi_bin")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive or None")
        if not 1 <= self.grace_seconds <= MAX_GRACE_SECONDS:
            raise ValueError(f"grace_seconds must be between 1 and {MAX_GRACE_SECONDS}")
        schema, _encoded = _canonical_structured_output_schema(self.structured_output_schema)
        object.__setattr__(self, "structured_output_schema", schema)


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
            FOMO_PI_THINKING_LEVEL: request.thinking,
            FOMO_PI_MODEL_REF: request.model,
            FOMO_PI_CONTEXT_WINDOW: str(request.context_window),
            FOMO_PI_GRACE_SECONDS: str(request.grace_seconds),
        }
        if request.activity_silence_seconds is not None:
            environment[FOMO_PI_ACTIVITY_SILENCE_SECONDS] = str(
                request.activity_silence_seconds
            )
        if request.timeout_seconds is not None:
            environment[FOMO_PI_TIMEOUT_SECONDS] = str(request.timeout_seconds)
        _schema, structured_schema = _canonical_structured_output_schema(
            request.structured_output_schema
        )
        if structured_schema is not None:
            environment[FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64] = base64.b64encode(
                structured_schema
            ).decode("ascii")
        if request.user_input_enabled:
            environment[FOMO_PI_USER_INPUT_ENABLED] = "1"
        if request.require_resume:
            environment[FOMO_PI_REQUIRE_RESUME] = "1"
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
            f"thinking={request.thinking!r}, "
            f"structured_output={request.structured_output_schema is not None!r}, "
            f"user_input={request.user_input_enabled!r}, "
            f"require_resume={request.require_resume!r}, "
            f"timeout_seconds={request.timeout_seconds!r})"
        )
