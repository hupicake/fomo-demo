"""Sandbox providers."""

from fomo.config import Settings

from .base import (
    Command,
    ExecResult,
    FileChange,
    PreviewRef,
    RetainedPreviewSandboxProvider,
    SandboxCapabilities,
    SandboxProvider,
    SandboxRef,
    SnapshotRef,
)
from .fake import FakeSandboxProvider
from .opensandbox import OpenSandboxProvider


def create_opensandbox_provider(settings: Settings) -> OpenSandboxProvider:
    """Build the sole production sandbox provider without a host-process fallback."""

    return OpenSandboxProvider(
        base_url=settings.opensandbox_base_url,
        api_key=settings.opensandbox_api_key,
        image=settings.opensandbox_image,
        lifetime_seconds=settings.opensandbox_lifetime_seconds,
        ready_timeout_seconds=settings.opensandbox_ready_timeout_seconds,
        proxy_environment=settings.sandbox_proxy_environment,
    )


__all__ = [
    "Command",
    "ExecResult",
    "FakeSandboxProvider",
    "FileChange",
    "OpenSandboxProvider",
    "PreviewRef",
    "RetainedPreviewSandboxProvider",
    "SandboxCapabilities",
    "SandboxProvider",
    "SandboxRef",
    "SnapshotRef",
    "create_opensandbox_provider",
]
