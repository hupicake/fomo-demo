"""Sandbox providers."""

from fomo.config import Settings

from .base import (
    Command,
    ExecResult,
    FileChange,
    PreviewRef,
    SandboxCapabilities,
    SandboxProvider,
    SandboxRef,
    SnapshotRef,
)
from .fake import FakeSandboxProvider
from .opensandbox import OpenSandboxProvider
from .process import ProcessSandboxProvider


def create_sandbox_provider(settings: Settings) -> SandboxProvider:
    if settings.sandbox_provider == "process":
        return ProcessSandboxProvider(
            settings.dev_sandbox_root,
            enabled=settings.allow_unsafe_process_sandbox,
            default_timeout_seconds=settings.command_timeout_seconds,
        )
    if settings.sandbox_provider == "opensandbox":
        # The boundary remains visible; it must not degrade to unsafe execution.
        return OpenSandboxProvider(
            base_url=settings.opensandbox_base_url,
            api_key=settings.opensandbox_api_key,
            image=settings.opensandbox_image,
            lifetime_seconds=settings.opensandbox_lifetime_seconds,
            proxy_environment=settings.sandbox_proxy_environment,
        )
    if settings.sandbox_provider == "fake":
        return FakeSandboxProvider()
    raise ValueError(f"unknown SANDBOX_PROVIDER: {settings.sandbox_provider}")


__all__ = [
    "Command",
    "ExecResult",
    "FakeSandboxProvider",
    "FileChange",
    "OpenSandboxProvider",
    "PreviewRef",
    "ProcessSandboxProvider",
    "SandboxCapabilities",
    "SandboxProvider",
    "SandboxRef",
    "SnapshotRef",
    "create_sandbox_provider",
]
