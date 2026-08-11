from __future__ import annotations

from pathlib import Path

import pytest

from fomo.config import Settings
from fomo.sandbox import OpenSandboxProvider
from fomo.worker.runner import WorkerRunner


def test_agent_framework_allowlist_defaults_and_environment_selection(monkeypatch) -> None:
    defaults = Settings()
    assert defaults.agent_enabled_frameworks == ("pi", "opencode")
    assert defaults.agent_default_framework == "pi"

    codex_rollout = Settings(
        agent_enabled_frameworks=("pi", "codex"),
        agent_default_framework="codex",
    )
    assert codex_rollout.agent_enabled_frameworks == ("pi", "codex")
    assert codex_rollout.agent_default_framework == "codex"

    monkeypatch.setenv("FOMO_AGENT_ENABLED_FRAMEWORKS", "opencode")
    monkeypatch.setenv("FOMO_AGENT_DEFAULT_FRAMEWORK", "opencode")
    explicit = Settings.from_env()
    assert explicit.agent_enabled_frameworks == ("opencode",)
    assert explicit.agent_default_framework == "opencode"


def test_agent_framework_configuration_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported agent framework"):
        Settings(agent_enabled_frameworks=("pi", "unknown"))
    with pytest.raises(ValueError, match="default agent framework must be present"):
        Settings(
            agent_enabled_frameworks=("pi",),
            agent_default_framework="opencode",
        )


def test_direct_pi_bridge_enables_official_builtin_tools_and_no_business_policy() -> None:
    bridge = (
        Path(__file__).parents[3] / "infra" / "opensandbox" / "fomo-pi-rpc-bridge.mjs"
    ).read_text(encoding="utf-8")

    assert 'const BUILTIN_TOOLS = "read,write,edit,bash,grep,find,ls";' in bridge
    assert 'const DELEGATE_SUBTASKS_TOOL = "delegate_subtasks";' in bridge
    # No business-file write allowlist survives in the bridge: Pi keeps its
    # official builtin tools with full /workspace permission.
    assert "allowedWritePaths" not in bridge
    assert "FOMO_PI_TOOL_POLICY_B64" not in bridge


def test_litellm_root_and_v1_urls_have_one_canonical_direct_pi_contract() -> None:
    root = Settings(litellm_base_url="http://litellm:4000")
    versioned = Settings(litellm_base_url="http://litellm:4000/v1/")

    assert root.litellm_management_url == "http://litellm:4000"
    assert root.pi_provider_base_url == "http://litellm:4000/v1"
    assert versioned.litellm_management_url == root.litellm_management_url
    assert versioned.pi_provider_base_url == root.pi_provider_base_url


def test_sandbox_litellm_url_is_independent_from_management_network() -> None:
    settings = Settings(
        litellm_base_url="http://127.0.0.1:4000",
        sandbox_litellm_base_url="http://host.docker.internal:4000/v1",
    )

    assert settings.litellm_management_url == "http://127.0.0.1:4000"
    assert settings.pi_provider_base_url == "http://host.docker.internal:4000/v1"


@pytest.mark.parametrize(
    ("management_url", "sandbox_url"),
    [
        ("http://localhost:4000", None),
        ("http://litellm.internal:4000", "http://127.0.0.1:4000"),
        ("http://litellm.internal:4000", "http://127.42.0.9:4000/v1"),
        ("http://litellm.internal:4000", "http://[::1]:4000"),
        ("http://litellm.internal:4000", "http://api.localhost:4000"),
    ],
)
def test_opensandbox_worker_rejects_loopback_pi_provider_url(
    management_url: str,
    sandbox_url: str | None,
) -> None:
    settings = Settings(
        litellm_base_url=management_url,
        sandbox_litellm_base_url=sandbox_url,
        opensandbox_api_key="test-opensandbox-key",
        litellm_api_key="test-litellm-key",
    )

    with pytest.raises(ValueError, match="set SANDBOX_LITELLM_BASE_URL"):
        WorkerRunner(object(), settings)  # type: ignore[arg-type]


def test_opensandbox_worker_accepts_sandbox_reachable_pi_provider_url() -> None:
    settings = Settings(
        litellm_base_url="http://127.0.0.1:4000",
        sandbox_litellm_base_url="http://host.docker.internal:4000",
        opensandbox_api_key="test-opensandbox-key",
        litellm_api_key="test-litellm-key",
    )

    worker = WorkerRunner(
        object(),  # type: ignore[arg-type]
        settings,
        sandbox=OpenSandboxProvider("http://sandbox.test"),
    )

    assert worker.direct_orchestrator is not None


@pytest.mark.parametrize(
    "url",
    [
        "http://user:password@litellm:4000",
        "http://litellm:4000/proxy",
        "http://litellm:4000?token=secret",
        "ftp://litellm:4000",
    ],
)
def test_litellm_management_url_rejects_ambiguous_or_credentialed_values(url: str) -> None:
    with pytest.raises(ValueError, match="LITELLM_BASE_URL"):
        Settings(litellm_base_url=url)


def test_direct_pi_budget_environment_is_loaded_as_one_validated_set(monkeypatch) -> None:
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm:4000")
    monkeypatch.setenv("SANDBOX_LITELLM_BASE_URL", "http://host.docker.internal:4000/v1")
    monkeypatch.setenv("FOMO_INFERENCE_TOKEN_TTL", "4800")
    monkeypatch.setenv("RUN_MAX_SPEND", "3.5")
    monkeypatch.setenv("RUN_INFERENCE_RPM_LIMIT", "40")
    monkeypatch.setenv("RUN_INFERENCE_TPM_LIMIT", "900000")
    monkeypatch.setenv("FOMO_RUNTIME_ENABLED_PROFILES", "gpt-5.6")
    monkeypatch.setenv("FOMO_RUNTIME_DEFAULT_PROFILE", "gpt-5.6")
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "420")
    settings = Settings.from_env()

    assert settings.inference_token_ttl_seconds == 4_800
    assert settings.run_max_spend == 3.5
    assert settings.run_inference_rpm_limit == 40
    assert settings.run_inference_tpm_limit == 900_000
    assert settings.model_request_timeout_seconds == 420
    assert settings.pi_provider_base_url == "http://host.docker.internal:4000/v1"


def test_public_preview_domain_is_optional_normalized_and_validated(monkeypatch) -> None:
    assert Settings().public_preview_base_domain is None
    configured = Settings(public_preview_base_domain="Preview.Example.Test.")
    assert configured.public_preview_base_domain == "preview.example.test"
    sandbox_id = "11111111-1111-4111-8111-111111111111"
    assert configured.published_preview_url(sandbox_id) == (
        f"https://{sandbox_id}.preview.example.test/"
    )
    with pytest.raises(ValueError, match="canonical sandbox UUID"):
        configured.published_preview_url("sandbox-id")

    monkeypatch.setenv("PUBLIC_PREVIEW_BASE_DOMAIN", "previews.example.com")
    assert Settings.from_env().public_preview_base_domain == "previews.example.com"

    for invalid in ("https://preview.example.test", "preview.example.test:443", "localhost"):
        with pytest.raises(ValueError, match="PUBLIC_PREVIEW_BASE_DOMAIN"):
            Settings(public_preview_base_domain=invalid)


def test_public_preview_path_url_is_normalized_exclusive_and_defaults_in_production() -> None:
    sandbox_id = "11111111-1111-4111-8111-111111111111"
    configured = Settings(
        web_origin="https://app.example.test",
        public_preview_base_url="https://APP.Example.Test/preview/",
    )
    assert configured.public_preview_base_url == "https://app.example.test/preview"
    assert configured.published_preview_url(sandbox_id) == (
        f"https://app.example.test/preview/{sandbox_id}/"
    )
    assert configured.published_preview_base_path(sandbox_id) == (
        f"/preview/{sandbox_id}"
    )
    assert Settings(public_preview_base_domain="preview.example.net").published_preview_base_path(
        sandbox_id
    ) is None
    assert Settings().public_preview_base_url is None
    assert Settings(
        app_env="production",
        web_origin="https://app.example.test",
    ).public_preview_base_url == "https://app.example.test/preview"

    with pytest.raises(ValueError, match="mutually exclusive"):
        Settings(
            public_preview_base_url="https://app.example.test/preview",
            public_preview_base_domain="preview.example.net",
        )
    for invalid in (
        "//app.example.test/preview",
        "http://preview.example.net/preview",
        "https://user:secret@app.example.test/preview",
        "https://app.example.test/preview?token=secret",
        "https://app.example.test/../preview",
    ):
        with pytest.raises(ValueError, match="PUBLIC_PREVIEW_BASE_URL"):
            Settings(public_preview_base_url=invalid)

    assert Settings(
        web_origin="http://localhost:3000",
        public_preview_base_url="http://localhost:3000/preview",
    ).public_preview_base_url == "http://localhost:3000/preview"


def test_public_preview_path_rejects_same_site_different_origin() -> None:
    with pytest.raises(ValueError, match="WEB_ORIGIN itself or a different registrable site"):
        Settings(
            web_origin="https://app.example.test",
            public_preview_base_url="https://preview.example.test/preview",
        )

    assert Settings(
        web_origin="https://app.example.test",
        public_preview_base_url="https://preview.example.net/preview",
    ).public_preview_base_url == "https://preview.example.net/preview"


@pytest.mark.parametrize(
    ("web_origin", "preview_domain", "message"),
    [
        (
            "https://app.example.com",
            "preview.example.com",
            "different registrable site",
        ),
        (
            "http://localhost:3000",
            "preview.localhost",
            "must not share the localhost site",
        ),
        (
            "http://studio.localhost:3000",
            "preview.localhost",
            "must not share the localhost site",
        ),
        (
            "https://app.example.co.uk",
            "preview.other.co.uk",
            "different registrable site",
        ),
    ],
)
def test_public_preview_requires_a_conservatively_isolated_site(
    web_origin: str,
    preview_domain: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(
            web_origin=web_origin,
            public_preview_base_domain=preview_domain,
        )

    isolated = Settings(
        web_origin="https://app.example.com",
        public_preview_base_domain="preview.example.net",
    )
    assert isolated.public_preview_base_domain == "preview.example.net"


@pytest.mark.parametrize(
    "preview_setting",
    [
        {"public_preview_base_domain": "preview.example.net"},
        {"public_preview_base_url": "https://preview.example.net/preview"},
    ],
)
def test_public_preview_rejects_invalid_web_origin_without_echoing_it(
    preview_setting: dict[str, str],
) -> None:
    sensitive_origin = "https://user:private-token@app.example.com"
    with pytest.raises(ValueError, match="WEB_ORIGIN") as captured:
        Settings(
            web_origin=sensitive_origin,
            **preview_setting,
        )

    assert sensitive_origin not in str(captured.value)
    assert "private-token" not in str(captured.value)


def test_session_cookie_key_is_host_prefixed_only_for_secure_runtime() -> None:
    development = Settings(app_env="development", session_cookie_name="fomo_session")
    production = Settings(app_env="production", session_cookie_name="fomo_session")
    explicit_host = Settings(app_env="development", session_cookie_name="__Host-session")

    assert development.session_cookie_key == "fomo_session"
    assert development.session_cookie_secure is False
    assert production.session_cookie_key == "__Host-fomo_session"
    assert production.session_cookie_secure is True
    assert explicit_host.session_cookie_key == "__Host-session"
    assert explicit_host.session_cookie_secure is True

    with pytest.raises(ValueError, match="SESSION_COOKIE_NAME"):
        Settings(session_cookie_name="invalid cookie\r\n")


def test_direct_pi_virtual_key_ttl_covers_active_sandbox_and_cleanup_grace() -> None:
    settings = Settings(inference_token_ttl_seconds=300)

    assert settings.active_run_inference_token_ttl_seconds == 22_200
