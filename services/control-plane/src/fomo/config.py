"""Environment-only configuration.

This module deliberately does not load ``.env`` files. Deployment tooling owns
secret injection; neither the API nor the worker reads or logs local secret files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_OPENSANDBOX_IMAGE = "fomo-sandbox-node:2026-08-08"
DEFAULT_OPENSANDBOX_LIFETIME_SECONDS = 21_600


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _opensandbox_lifetime_seconds(default: int) -> int:
    value = int(os.getenv("OPENSANDBOX_LIFETIME_SECONDS", str(default)))
    if not 0 < value <= DEFAULT_OPENSANDBOX_LIFETIME_SECONDS:
        raise ValueError("OPENSANDBOX_LIFETIME_SECONDS must be between 1 and 21600 seconds")
    return value


def _sandbox_proxy_url(name: str) -> str | None:
    """Read one explicit sandbox-only proxy URL without inheriting host proxy vars."""
    value = os.getenv(name, "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} must contain a valid proxy port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65_535
    ):
        raise ValueError(f"{name} must be an http(s) proxy URL without userinfo")
    return value


def _sandbox_no_proxy() -> str | None:
    value = os.getenv("SANDBOX_NO_PROXY", "").strip()
    if not value:
        return None
    if any(character in value for character in "\r\n\x00"):
        raise ValueError("SANDBOX_NO_PROXY cannot contain control characters")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./fomo.db"
    web_origin: str = "http://localhost:3000"
    session_cookie_name: str = "fomo_session"
    litellm_base_url: str = "http://localhost:4000/v1"
    litellm_api_key: str | None = None
    # Model calls are intentionally decoupled from sandbox command timeouts.
    # Smaller Engineer batches should complete well within this bound.
    model_request_timeout_seconds: int = 300
    # Independent transport budget for LiteLLM gateway transient failures; it
    # must not consume the SOP's schema/structured-output retry budget.
    model_network_retries: int = 5
    model_network_retry_base_delay_seconds: float = 1.0
    model_network_retry_max_delay_seconds: float = 16.0
    model_retry_after_max_seconds: float = 60.0
    model_pm: str = "pm"
    model_architect: str = "architect"
    model_engineer: str = "engineer"
    model_reviewer: str = "reviewer"
    # The Engineer emits a compact plan, then complete files in bounded batches
    # so a full project is never one unbounded model response.
    engineer_max_batches: int = 24
    engineer_max_files_per_batch: int = 1
    engineer_max_file_characters: int = 12_000
    # MetaGPT is the production coordination layer. `native` is intentionally
    # reserved for explicit test and diagnostic runs.
    agent_framework: str = "metagpt"
    sandbox_provider: str = "opensandbox"
    opensandbox_base_url: str = "http://localhost:8080"
    opensandbox_api_key: str | None = None
    # Sandboxes never inherit the control-plane proxy environment. These are
    # the only three explicit egress proxy variables that can cross into a
    # generated-code container.
    sandbox_http_proxy: str | None = None
    sandbox_https_proxy: str | None = None
    sandbox_no_proxy: str | None = None
    # This curated image is built by local infrastructure with Node, pnpm and
    # Git. Deployments may override it through OPENSANDBOX_IMAGE.
    opensandbox_image: str = DEFAULT_OPENSANDBOX_IMAGE
    opensandbox_lifetime_seconds: int = DEFAULT_OPENSANDBOX_LIFETIME_SECONDS
    allow_unsafe_process_sandbox: bool = False
    dev_sandbox_root: Path = Path("/tmp/fomo-dev-sandboxes")
    worker_poll_interval_seconds: float = 0.5
    worker_lease_seconds: int = 120
    command_timeout_seconds: int = 300
    command_output_limit_bytes: int = 64 * 1024
    preview_start_timeout_seconds: int = 25
    # 44772 is OpenSandbox execd; generated apps only use the fixed 8080 app port.
    preview_base_port: int = 8080
    structured_output_retries: int = 1
    max_repair_rounds: int = 3

    @property
    def sandbox_proxy_environment(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("HTTP_PROXY", self.sandbox_http_proxy),
                ("HTTPS_PROXY", self.sandbox_https_proxy),
                ("NO_PROXY", self.sandbox_no_proxy),
            )
            if value
        }

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        database_url = os.getenv("DATABASE_URL", defaults.database_url)
        # SQLAlchemy's PostgreSQL async driver is selected by the supplied URL.
        return cls(
            app_env=os.getenv("APP_ENV", defaults.app_env),
            database_url=database_url,
            web_origin=os.getenv("WEB_ORIGIN", defaults.web_origin),
            session_cookie_name=os.getenv("SESSION_COOKIE_NAME", defaults.session_cookie_name),
            litellm_base_url=os.getenv("LITELLM_BASE_URL", defaults.litellm_base_url).rstrip("/"),
            # This is only a LiteLLM gateway credential. Provider keys stay inside LiteLLM.
            litellm_api_key=(
                os.getenv("LITELLM_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or None
            ),
            model_request_timeout_seconds=int(
                os.getenv("MODEL_REQUEST_TIMEOUT_SECONDS", str(defaults.model_request_timeout_seconds))
            ),
            model_network_retries=int(
                os.getenv("MODEL_NETWORK_RETRIES", str(defaults.model_network_retries))
            ),
            model_network_retry_base_delay_seconds=float(
                os.getenv(
                    "MODEL_NETWORK_RETRY_BASE_DELAY_SECONDS",
                    str(defaults.model_network_retry_base_delay_seconds),
                )
            ),
            model_network_retry_max_delay_seconds=float(
                os.getenv(
                    "MODEL_NETWORK_RETRY_MAX_DELAY_SECONDS",
                    str(defaults.model_network_retry_max_delay_seconds),
                )
            ),
            model_retry_after_max_seconds=float(
                os.getenv(
                    "MODEL_RETRY_AFTER_MAX_SECONDS",
                    str(defaults.model_retry_after_max_seconds),
                )
            ),
            model_pm=os.getenv("MODEL_PM", defaults.model_pm),
            model_architect=os.getenv("MODEL_ARCHITECT", defaults.model_architect),
            model_engineer=os.getenv("MODEL_ENGINEER", defaults.model_engineer),
            model_reviewer=os.getenv("MODEL_REVIEWER", defaults.model_reviewer),
            engineer_max_batches=int(
                os.getenv("ENGINEER_MAX_BATCHES", str(defaults.engineer_max_batches))
            ),
            engineer_max_files_per_batch=int(
                os.getenv("ENGINEER_MAX_FILES_PER_BATCH", str(defaults.engineer_max_files_per_batch))
            ),
            engineer_max_file_characters=int(
                os.getenv("ENGINEER_MAX_FILE_CHARACTERS", str(defaults.engineer_max_file_characters))
            ),
            agent_framework=os.getenv("AGENT_FRAMEWORK", defaults.agent_framework).strip().lower(),
            sandbox_provider=os.getenv("SANDBOX_PROVIDER", defaults.sandbox_provider).lower(),
            opensandbox_base_url=os.getenv("OPENSANDBOX_BASE_URL", defaults.opensandbox_base_url),
            opensandbox_api_key=os.getenv("OPENSANDBOX_API_KEY") or None,
            sandbox_http_proxy=_sandbox_proxy_url("SANDBOX_HTTP_PROXY"),
            sandbox_https_proxy=_sandbox_proxy_url("SANDBOX_HTTPS_PROXY"),
            sandbox_no_proxy=_sandbox_no_proxy(),
            opensandbox_image=(
                os.getenv("OPENSANDBOX_IMAGE", defaults.opensandbox_image).strip()
                or defaults.opensandbox_image
            ),
            opensandbox_lifetime_seconds=_opensandbox_lifetime_seconds(
                defaults.opensandbox_lifetime_seconds
            ),
            allow_unsafe_process_sandbox=_bool("ALLOW_UNSAFE_PROCESS_SANDBOX"),
            dev_sandbox_root=Path(os.getenv("FOMO_DEV_SANDBOX_ROOT", str(defaults.dev_sandbox_root))),
            worker_poll_interval_seconds=float(
                os.getenv("WORKER_POLL_INTERVAL_SECONDS", str(defaults.worker_poll_interval_seconds))
            ),
            worker_lease_seconds=int(os.getenv("WORKER_LEASE_SECONDS", str(defaults.worker_lease_seconds))),
            command_timeout_seconds=int(
                os.getenv("COMMAND_TIMEOUT_SECONDS", str(defaults.command_timeout_seconds))
            ),
            command_output_limit_bytes=int(
                os.getenv("COMMAND_OUTPUT_LIMIT_BYTES", str(defaults.command_output_limit_bytes))
            ),
            preview_start_timeout_seconds=int(
                os.getenv("PREVIEW_START_TIMEOUT_SECONDS", str(defaults.preview_start_timeout_seconds))
            ),
            preview_base_port=int(os.getenv("PREVIEW_BASE_PORT", str(defaults.preview_base_port))),
            structured_output_retries=int(
                os.getenv("STRUCTURED_OUTPUT_RETRIES", str(defaults.structured_output_retries))
            ),
            max_repair_rounds=int(os.getenv("MAX_REPAIR_ROUNDS", str(defaults.max_repair_rounds))),
        )
