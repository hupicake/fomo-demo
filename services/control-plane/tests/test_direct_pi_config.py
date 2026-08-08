from __future__ import annotations

import pytest

from fomo.config import Settings


def test_litellm_root_and_v1_urls_have_one_canonical_direct_pi_contract() -> None:
    root = Settings(litellm_base_url="http://litellm:4000")
    versioned = Settings(litellm_base_url="http://litellm:4000/v1/")

    assert root.litellm_management_url == "http://litellm:4000"
    assert root.pi_provider_base_url == "http://litellm:4000/v1"
    assert versioned.litellm_management_url == root.litellm_management_url
    assert versioned.pi_provider_base_url == root.pi_provider_base_url


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
    monkeypatch.setenv("FOMO_INFERENCE_TOKEN_TTL", "4800")
    monkeypatch.setenv("RUN_MAX_WALL_SECONDS", "3600")
    monkeypatch.setenv("RUN_MAX_SPEND", "3.5")
    monkeypatch.setenv("RUN_INFERENCE_RPM_LIMIT", "40")
    monkeypatch.setenv("RUN_INFERENCE_TPM_LIMIT", "900000")
    settings = Settings.from_env()

    assert settings.inference_token_ttl_seconds == 4_800
    assert settings.run_max_wall_seconds == 3_600
    assert settings.run_max_spend == 3.5
    assert settings.run_inference_rpm_limit == 40
    assert settings.run_inference_tpm_limit == 900_000


def test_direct_pi_virtual_key_ttl_must_cover_wall_budget_and_expiry_grace() -> None:
    with pytest.raises(ValueError, match=r"run_max_wall_seconds \+ 600"):
        Settings(run_max_wall_seconds=3_600, inference_token_ttl_seconds=4_199)
