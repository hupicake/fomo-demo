"""Environment-only configuration.

This module deliberately does not load ``.env`` files. Deployment tooling owns
secret injection; neither the API nor the worker reads or logs local secret files.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from ipaddress import ip_address
from math import isfinite
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from fomo.agent_framework import (
    DEFAULT_AGENT_FRAMEWORK,
    DEFAULT_ENABLED_AGENT_FRAMEWORKS,
    legacy_framework_mode,
    parse_enabled_agent_frameworks,
    public_framework_from_legacy,
    validated_default_agent_framework,
)
from fomo.runtime_contract import (
    DEFAULT_PROFILE_ID,
    parse_enabled_profile_ids,
    runtime_profile,
    validated_default_profile_id,
)

DEFAULT_OPENSANDBOX_IMAGE = "fomo-sandbox-node:2026-08-08"
DEFAULT_OPENSANDBOX_LIFETIME_SECONDS = 21_600
DEFAULT_OPENSANDBOX_READY_TIMEOUT_SECONDS = 120
DEFAULT_VERIFIED_PREVIEW_LIFETIME_SECONDS = 604_800
DEFAULT_DEV_ACCOUNT_EMAIL = "dev@fomo.local"
DEFAULT_DEV_ACCOUNT_PASSWORD = "fomo-dev-password"
DEFAULT_DEV_ACCOUNT_DISPLAY_NAME = "Dev"
MAX_ENGINEER_FILE_CHARACTERS = 24_000
INFERENCE_TOKEN_EXPIRY_GRACE_SECONDS = 600
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PREVIEW_PATH = re.compile(
    r"^/(?:[A-Za-z0-9][A-Za-z0-9._~-]*)(?:/[A-Za-z0-9][A-Za-z0-9._~-]*)*$"
)
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _opensandbox_lifetime_seconds(default: int) -> int:
    value = int(os.getenv("OPENSANDBOX_LIFETIME_SECONDS", str(default)))
    if not 0 < value <= DEFAULT_OPENSANDBOX_LIFETIME_SECONDS:
        raise ValueError("OPENSANDBOX_LIFETIME_SECONDS must be between 1 and 21600 seconds")
    return value


def _verified_preview_lifetime_seconds(default: int) -> int:
    value = int(os.getenv("VERIFIED_PREVIEW_LIFETIME_SECONDS", str(default)))
    if not 0 < value <= DEFAULT_VERIFIED_PREVIEW_LIFETIME_SECONDS:
        raise ValueError("VERIFIED_PREVIEW_LIFETIME_SECONDS must be between 1 and 604800 seconds")
    return value


def _public_preview_base_domain(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    domain = value.strip().lower().rstrip(".")
    labels = domain.split(".")
    try:
        ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("PUBLIC_PREVIEW_BASE_DOMAIN must be a DNS domain without a scheme or port")
    if (
        len(domain) > 253
        or len(labels) < 2
        or any(not _DNS_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError("PUBLIC_PREVIEW_BASE_DOMAIN must be a DNS domain without a scheme or port")
    return domain


def _public_preview_base_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    candidate = value.strip()
    if candidate != value or "\\" in candidate:
        raise ValueError("PUBLIC_PREVIEW_BASE_URL must be an absolute http(s) URL")
    parsed = urlparse(candidate)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("PUBLIC_PREVIEW_BASE_URL must be an absolute http(s) URL") from exc
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
        or (path and not _PREVIEW_PATH.fullmatch(path))
    ):
        raise ValueError("PUBLIC_PREVIEW_BASE_URL must be an absolute http(s) URL")
    if parsed.scheme.lower() != "https" and not _is_loopback_hostname(parsed.hostname):
        raise ValueError("PUBLIC_PREVIEW_BASE_URL must use HTTPS outside loopback")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def _is_loopback_hostname(hostname: str) -> bool:
    candidate = hostname.casefold().rstrip(".")
    if candidate == "localhost" or candidate.endswith(".localhost"):
        return True
    try:
        return ip_address(candidate).is_loopback
    except ValueError:
        return False


def _registrable_site(hostname: str) -> str:
    candidate = hostname.lower().rstrip(".")
    if candidate == "localhost" or candidate.endswith(".localhost"):
        return "localhost"
    try:
        ip_address(candidate)
    except ValueError:
        labels = candidate.split(".")
        return ".".join(labels[-2:]) if len(labels) >= 2 else candidate
    return candidate


def _origin_identity(value: str) -> tuple[str, str, int]:
    parsed = urlparse(value)
    assert parsed.hostname is not None
    scheme = parsed.scheme.lower()
    return (
        scheme,
        parsed.hostname.casefold().rstrip("."),
        parsed.port or (443 if scheme == "https" else 80),
    )


def _web_origin_site(value: str) -> str:
    parsed = urlparse(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            "WEB_ORIGIN must be an absolute http(s) origin when public previews are enabled"
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65_535
    ):
        raise ValueError(
            "WEB_ORIGIN must be an absolute http(s) origin when public previews are enabled"
        )
    return _registrable_site(parsed.hostname)


def _validated_session_cookie_name(value: str) -> str:
    candidate = value.strip()
    if not candidate or not _COOKIE_NAME.fullmatch(candidate) or candidate == "__Host-":
        raise ValueError("SESSION_COOKIE_NAME must be a valid cookie name")
    return candidate


def _positive_int_environment_value(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return value


def _positive_float_environment_value(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return value


def _litellm_endpoints(base_url: str) -> tuple[str, str]:
    """Return the management root and OpenAI-compatible ``/v1`` endpoint."""
    value = base_url.strip().rstrip("/")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("LITELLM_BASE_URL must contain a valid port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65_535
    ):
        raise ValueError("LITELLM_BASE_URL must be an http(s) URL without userinfo")
    path = parsed.path.rstrip("/")
    if path == "/v1":
        path = ""
    if path:
        raise ValueError("LITELLM_BASE_URL may only use the root path or /v1")
    management_url = parsed._replace(path="", params="", query="", fragment="").geturl()
    management_url = management_url.rstrip("/")
    return management_url, f"{management_url}/v1"


def _engineer_file_character_limits(default_target: int, default_hard: int) -> tuple[int, int]:
    target = _positive_int_environment_value("ENGINEER_TARGET_FILE_CHARACTERS", default_target)
    hard = _positive_int_environment_value("ENGINEER_MAX_FILE_CHARACTERS", default_hard)
    if hard > MAX_ENGINEER_FILE_CHARACTERS:
        raise ValueError(
            f"ENGINEER_MAX_FILE_CHARACTERS must be at most {MAX_ENGINEER_FILE_CHARACTERS}"
        )
    if target > hard:
        raise ValueError(
            "ENGINEER_TARGET_FILE_CHARACTERS must be less than or equal to "
            "ENGINEER_MAX_FILE_CHARACTERS"
        )
    return target, hard


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
        or port is not None
        and not 1 <= port <= 65_535
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
    dev_account_email: str | None = None
    dev_account_password: str | None = None
    dev_account_display_name: str = DEFAULT_DEV_ACCOUNT_DISPLAY_NAME
    litellm_base_url: str = "http://localhost:4000/v1"
    # The worker calls LiteLLM's management API from the control-plane host,
    # while Pi calls the OpenAI-compatible endpoint from inside OpenSandbox.
    # Those are different network namespaces in local Docker development.
    sandbox_litellm_base_url: str | None = None
    litellm_api_key: str | None = None
    runtime_enabled_profiles: tuple[str, ...] = (DEFAULT_PROFILE_ID,)
    runtime_default_profile: str = DEFAULT_PROFILE_ID
    # Public, per-run Coding Agent choices. These identifiers are frozen on a
    # run and are independent from the retired process-wide compatibility mode.
    # Codex is supported by the public contract but remains an explicit rollout
    # choice until its transport is configured in the worker.
    agent_enabled_frameworks: tuple[str, ...] = DEFAULT_ENABLED_AGENT_FRAMEWORKS
    agent_default_framework: str = DEFAULT_AGENT_FRAMEWORK
    # Direct Pi receives only a short-lived LiteLLM virtual key. The master key
    # stays in the control plane and provider credentials stay inside LiteLLM.
    inference_token_ttl_seconds: int = 4_200
    inference_management_timeout_seconds: int = 15
    # Legacy/native compatibility knobs. Direct Pi deliberately does not use
    # these as development quotas; its lifetime is the sandbox resource lease.
    run_max_wall_seconds: int = 3_600
    run_max_spend: float = 5.0
    run_inference_rpm_limit: int = 60
    run_inference_tpm_limit: int = 1_250_000
    run_max_tool_calls: int = 300
    pi_max_file_characters: int = 20_000
    pi_max_changed_files: int = 24
    # Explicit logical context window passed to the bridge. The active model
    # supports a larger provider window; FOMO intentionally compacts within
    # this 200K product budget.
    pi_context_window: int = 200_000
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
    engineer_target_file_characters: int = 12_000
    engineer_max_file_characters: int = 20_000
    # Direct Pi is the production path. Native remains only for legacy SOP
    # compatibility while historical runs and focused tests are retired.
    agent_framework: str = "direct_pi"
    # True-default P1 rollout. Setting this false preserves the graph-less P0
    # Direct Pi contract for controlled rollback and historical test fixtures.
    direct_pi_goal_graph_enabled: bool = True
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
    opensandbox_ready_timeout_seconds: int = DEFAULT_OPENSANDBOX_READY_TIMEOUT_SECONDS
    # Verified previews can use either an isolated wildcard host or a path URL.
    # Same-site paths stay opaque; a dedicated cross-site URL permits interactive
    # previews but shares one browser origin. The two modes are mutually exclusive.
    public_preview_base_domain: str | None = None
    public_preview_base_url: str | None = None
    # Successful, fully verified previews outlive their build sandboxes. The
    # OpenSandbox server hard limit is kept in sync with this bounded value.
    verified_preview_lifetime_seconds: int = DEFAULT_VERIFIED_PREVIEW_LIFETIME_SECONDS
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
    # Native SOP compatibility only. Direct Pi repairs until verification
    # passes, the run is cancelled, or a real infrastructure/provider failure
    # prevents further progress.
    max_repair_rounds: int = 3

    def __post_init__(self) -> None:
        enabled_agent_frameworks = parse_enabled_agent_frameworks(
            self.agent_enabled_frameworks
        )
        default_agent_framework = validated_default_agent_framework(
            self.agent_default_framework,
            enabled_agent_frameworks,
        )
        object.__setattr__(self, "agent_enabled_frameworks", enabled_agent_frameworks)
        object.__setattr__(self, "agent_default_framework", default_agent_framework)
        object.__setattr__(self, "agent_framework", legacy_framework_mode(self.agent_framework))
        enabled_profiles = parse_enabled_profile_ids(
            ",".join(self.runtime_enabled_profiles)
        )
        default_profile = validated_default_profile_id(
            self.runtime_default_profile,
            enabled_profiles,
        )
        object.__setattr__(self, "runtime_enabled_profiles", tuple(sorted(enabled_profiles)))
        object.__setattr__(self, "runtime_default_profile", default_profile)
        largest_context_window = max(
            runtime_profile(profile_id).context_window for profile_id in enabled_profiles
        )
        if self.run_inference_tpm_limit <= largest_context_window:
            raise ValueError(
                "run_inference_tpm_limit must leave output headroom for every enabled "
                "runtime profile"
            )
        normalized_preview_domain = _public_preview_base_domain(self.public_preview_base_domain)
        normalized_preview_url = _public_preview_base_url(self.public_preview_base_url)
        if normalized_preview_domain and normalized_preview_url:
            raise ValueError(
                "PUBLIC_PREVIEW_BASE_URL and PUBLIC_PREVIEW_BASE_DOMAIN are mutually exclusive"
            )
        # Production defaults to the existing web origin so the path gateway can
        # be enabled without provisioning DNS or changing the deployed .env.
        if not normalized_preview_domain and not normalized_preview_url and self.app_env == "production":
            _web_origin_site(self.web_origin)
            normalized_preview_url = _public_preview_base_url(
                f"{self.web_origin.strip().rstrip('/')}/preview"
            )
        object.__setattr__(self, "public_preview_base_domain", normalized_preview_domain)
        object.__setattr__(self, "public_preview_base_url", normalized_preview_url)
        object.__setattr__(
            self,
            "session_cookie_name",
            _validated_session_cookie_name(self.session_cookie_name),
        )
        web_site: str | None = None
        if normalized_preview_domain or normalized_preview_url:
            web_site = _web_origin_site(self.web_origin)
            if (
                self.sandbox_provider != "opensandbox"
                or self.agent_framework != "direct_pi"
                or not self.direct_pi_goal_graph_enabled
            ):
                raise ValueError(
                    "PUBLIC_PREVIEW_BASE_URL or PUBLIC_PREVIEW_BASE_DOMAIN requires "
                    "SANDBOX_PROVIDER=opensandbox, "
                    "AGENT_FRAMEWORK=direct_pi, and DIRECT_PI_GOAL_GRAPH_ENABLED=true"
                )
        if normalized_preview_domain:
            preview_site = _registrable_site(normalized_preview_domain)
            assert web_site is not None
            if preview_site == web_site:
                if preview_site == "localhost":
                    raise ValueError(
                        "PUBLIC_PREVIEW_BASE_DOMAIN must not share the localhost site "
                        "with WEB_ORIGIN"
                    )
                raise ValueError(
                    "PUBLIC_PREVIEW_BASE_DOMAIN must use a different registrable site "
                    "from WEB_ORIGIN"
                )
        if normalized_preview_url:
            preview_host = urlparse(normalized_preview_url).hostname
            assert preview_host is not None and web_site is not None
            if (
                _registrable_site(preview_host) == web_site
                and _origin_identity(normalized_preview_url) != _origin_identity(self.web_origin)
            ):
                raise ValueError(
                    "PUBLIC_PREVIEW_BASE_URL must use WEB_ORIGIN itself or a different "
                    "registrable site"
                )
        _litellm_endpoints(self.litellm_base_url)
        if self.sandbox_litellm_base_url:
            _litellm_endpoints(self.sandbox_litellm_base_url)
        positive_values = {
            "inference_token_ttl_seconds": self.inference_token_ttl_seconds,
            "inference_management_timeout_seconds": self.inference_management_timeout_seconds,
            "run_max_wall_seconds": self.run_max_wall_seconds,
            "run_inference_rpm_limit": self.run_inference_rpm_limit,
            "run_inference_tpm_limit": self.run_inference_tpm_limit,
            "run_max_tool_calls": self.run_max_tool_calls,
            "pi_max_file_characters": self.pi_max_file_characters,
            "pi_max_changed_files": self.pi_max_changed_files,
            "pi_context_window": self.pi_context_window,
            "model_request_timeout_seconds": self.model_request_timeout_seconds,
            "opensandbox_ready_timeout_seconds": self.opensandbox_ready_timeout_seconds,
            "verified_preview_lifetime_seconds": self.verified_preview_lifetime_seconds,
        }
        for name, value in positive_values.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than 0")
        if self.pi_context_window > 8_000_000:
            raise ValueError("pi_context_window must not exceed 8000000")
        if self.verified_preview_lifetime_seconds > DEFAULT_VERIFIED_PREVIEW_LIFETIME_SECONDS:
            raise ValueError("verified_preview_lifetime_seconds must not exceed 604800")
        if not isfinite(self.run_max_spend) or self.run_max_spend <= 0:
            raise ValueError("run_max_spend must be greater than 0")

    @property
    def litellm_management_url(self) -> str:
        return _litellm_endpoints(self.litellm_base_url)[0]

    @property
    def pi_provider_base_url(self) -> str:
        return _litellm_endpoints(self.sandbox_litellm_base_url or self.litellm_base_url)[1]

    @property
    def active_run_inference_token_ttl_seconds(self) -> int:
        """Keep a scoped key alive for the sandbox's full resource lifetime.

        The configured TTL remains a deployment minimum. Clamping it to the
        sandbox lifetime plus cleanup grace prevents key expiry from becoming
        a hidden run wall while retaining alias, rate, spend, and revocation
        controls. The LiteLLM master key never enters the sandbox.
        """

        return max(
            self.inference_token_ttl_seconds,
            self.opensandbox_lifetime_seconds + INFERENCE_TOKEN_EXPIRY_GRACE_SECONDS,
        )

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

    @property
    def session_cookie_secure(self) -> bool:
        return self.app_env not in {"development", "test"} or self.session_cookie_name.startswith(
            "__Host-"
        )

    @property
    def session_cookie_key(self) -> str:
        if self.session_cookie_secure and not self.session_cookie_name.startswith("__Host-"):
            return f"__Host-{self.session_cookie_name}"
        return self.session_cookie_name

    def published_preview_url(self, sandbox_id: str) -> str | None:
        """Return the configured public URL only after verification is complete."""

        if not self.public_preview_base_domain and not self.public_preview_base_url:
            return None
        try:
            canonical_id = str(UUID(sandbox_id))
        except ValueError as exc:
            raise ValueError("public preview requires a canonical sandbox UUID") from exc
        if canonical_id != sandbox_id:
            raise ValueError("public preview requires a canonical sandbox UUID")
        if self.public_preview_base_url:
            return f"{self.public_preview_base_url}/{canonical_id}/"
        return f"https://{canonical_id}.{self.public_preview_base_domain}/"

    def published_preview_base_path(self, sandbox_id: str) -> str | None:
        """Return the path-mode basePath baked into a generated Next build."""

        if not self.public_preview_base_url:
            return None
        public_url = self.published_preview_url(sandbox_id)
        assert public_url is not None
        return urlparse(public_url).path.rstrip("/")

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        app_env = os.getenv("APP_ENV", defaults.app_env).strip().lower()
        database_url = os.getenv("DATABASE_URL", defaults.database_url)
        legacy_agent_framework_raw = os.getenv(
            "AGENT_FRAMEWORK", defaults.agent_framework
        ).strip().lower()
        enabled_agent_frameworks_raw = os.getenv("FOMO_AGENT_ENABLED_FRAMEWORKS")
        default_agent_framework_raw = os.getenv("FOMO_AGENT_DEFAULT_FRAMEWORK")
        enabled_agent_frameworks = parse_enabled_agent_frameworks(
            defaults.agent_enabled_frameworks
            if enabled_agent_frameworks_raw is None
            else enabled_agent_frameworks_raw
        )
        default_agent_framework = (
            validated_default_agent_framework(
                default_agent_framework_raw,
                enabled_agent_frameworks,
            )
            if default_agent_framework_raw is not None
            else validated_default_agent_framework(
                public_framework_from_legacy(legacy_agent_framework_raw),
                enabled_agent_frameworks,
            )
        )
        engineer_target_file_characters, engineer_max_file_characters = (
            _engineer_file_character_limits(
                defaults.engineer_target_file_characters,
                defaults.engineer_max_file_characters,
            )
        )
        # SQLAlchemy's PostgreSQL async driver is selected by the supplied URL.
        return cls(
            app_env=app_env,
            database_url=database_url,
            web_origin=os.getenv("WEB_ORIGIN", defaults.web_origin),
            session_cookie_name=os.getenv("SESSION_COOKIE_NAME", defaults.session_cookie_name),
            dev_account_email=(
                os.getenv("DEV_ACCOUNT_EMAIL", "").strip()
                or (DEFAULT_DEV_ACCOUNT_EMAIL if app_env == "development" else None)
            ),
            dev_account_password=(
                os.getenv("DEV_ACCOUNT_PASSWORD", "")
                or (DEFAULT_DEV_ACCOUNT_PASSWORD if app_env == "development" else None)
            ),
            dev_account_display_name=(
                os.getenv("DEV_ACCOUNT_DISPLAY_NAME", "").strip()
                or DEFAULT_DEV_ACCOUNT_DISPLAY_NAME
            ),
            litellm_base_url=os.getenv("LITELLM_BASE_URL", defaults.litellm_base_url).rstrip("/"),
            sandbox_litellm_base_url=(
                os.getenv("SANDBOX_LITELLM_BASE_URL", "").strip().rstrip("/") or None
            ),
            # This is only a LiteLLM gateway credential. Provider keys stay inside LiteLLM.
            litellm_api_key=(
                os.getenv("LITELLM_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or None
            ),
            runtime_enabled_profiles=tuple(
                sorted(
                    parse_enabled_profile_ids(
                        os.getenv("FOMO_RUNTIME_ENABLED_PROFILES")
                    )
                )
            ),
            runtime_default_profile=(
                os.getenv("FOMO_RUNTIME_DEFAULT_PROFILE", defaults.runtime_default_profile).strip()
                or defaults.runtime_default_profile
            ),
            agent_enabled_frameworks=enabled_agent_frameworks,
            agent_default_framework=default_agent_framework,
            inference_token_ttl_seconds=_positive_int_environment_value(
                "FOMO_INFERENCE_TOKEN_TTL", defaults.inference_token_ttl_seconds
            ),
            inference_management_timeout_seconds=_positive_int_environment_value(
                "INFERENCE_MANAGEMENT_TIMEOUT_SECONDS",
                defaults.inference_management_timeout_seconds,
            ),
            run_max_wall_seconds=_positive_int_environment_value(
                "RUN_MAX_WALL_SECONDS", defaults.run_max_wall_seconds
            ),
            run_max_spend=_positive_float_environment_value(
                "RUN_MAX_SPEND", defaults.run_max_spend
            ),
            run_inference_rpm_limit=_positive_int_environment_value(
                "RUN_INFERENCE_RPM_LIMIT", defaults.run_inference_rpm_limit
            ),
            run_inference_tpm_limit=_positive_int_environment_value(
                "RUN_INFERENCE_TPM_LIMIT", defaults.run_inference_tpm_limit
            ),
            run_max_tool_calls=_positive_int_environment_value(
                "RUN_MAX_TOOL_CALLS", defaults.run_max_tool_calls
            ),
            pi_max_file_characters=_positive_int_environment_value(
                "PI_MAX_FILE_CHARACTERS", defaults.pi_max_file_characters
            ),
            pi_max_changed_files=_positive_int_environment_value(
                "PI_MAX_CHANGED_FILES", defaults.pi_max_changed_files
            ),
            pi_context_window=_positive_int_environment_value(
                "PI_CONTEXT_WINDOW", defaults.pi_context_window
            ),
            model_request_timeout_seconds=_positive_int_environment_value(
                "MODEL_REQUEST_TIMEOUT_SECONDS", defaults.model_request_timeout_seconds
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
                os.getenv(
                    "ENGINEER_MAX_FILES_PER_BATCH", str(defaults.engineer_max_files_per_batch)
                )
            ),
            engineer_target_file_characters=engineer_target_file_characters,
            engineer_max_file_characters=engineer_max_file_characters,
            agent_framework=legacy_agent_framework_raw,
            direct_pi_goal_graph_enabled=_bool("DIRECT_PI_GOAL_GRAPH_ENABLED", True),
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
            opensandbox_ready_timeout_seconds=_positive_int_environment_value(
                "OPENSANDBOX_READY_TIMEOUT_SECONDS",
                defaults.opensandbox_ready_timeout_seconds,
            ),
            public_preview_base_domain=_public_preview_base_domain(
                os.getenv("PUBLIC_PREVIEW_BASE_DOMAIN")
            ),
            public_preview_base_url=_public_preview_base_url(
                os.getenv("PUBLIC_PREVIEW_BASE_URL")
            ),
            verified_preview_lifetime_seconds=_verified_preview_lifetime_seconds(
                defaults.verified_preview_lifetime_seconds
            ),
            allow_unsafe_process_sandbox=_bool("ALLOW_UNSAFE_PROCESS_SANDBOX"),
            dev_sandbox_root=Path(
                os.getenv("FOMO_DEV_SANDBOX_ROOT", str(defaults.dev_sandbox_root))
            ),
            worker_poll_interval_seconds=float(
                os.getenv(
                    "WORKER_POLL_INTERVAL_SECONDS", str(defaults.worker_poll_interval_seconds)
                )
            ),
            worker_lease_seconds=int(
                os.getenv("WORKER_LEASE_SECONDS", str(defaults.worker_lease_seconds))
            ),
            command_timeout_seconds=int(
                os.getenv("COMMAND_TIMEOUT_SECONDS", str(defaults.command_timeout_seconds))
            ),
            command_output_limit_bytes=int(
                os.getenv("COMMAND_OUTPUT_LIMIT_BYTES", str(defaults.command_output_limit_bytes))
            ),
            preview_start_timeout_seconds=int(
                os.getenv(
                    "PREVIEW_START_TIMEOUT_SECONDS", str(defaults.preview_start_timeout_seconds)
                )
            ),
            preview_base_port=int(os.getenv("PREVIEW_BASE_PORT", str(defaults.preview_base_port))),
            structured_output_retries=int(
                os.getenv("STRUCTURED_OUTPUT_RETRIES", str(defaults.structured_output_retries))
            ),
            max_repair_rounds=int(os.getenv("MAX_REPAIR_ROUNDS", str(defaults.max_repair_rounds))),
        )
