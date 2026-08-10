"""OpenSandbox transport adapter for the root-owned Codex CLI bridge."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from fomo.sandbox.base import SandboxRef

from .invocation import PiInvocation
from .rpc import PiBridgeFailed, PiBridgeProtocolError
from .transport import (
    OpenSandboxPiTransport,
    PiDiagnosticSink,
    PiEventSink,
    PiTransportCancelled,
    PiTransportError,
    PiTransportResult,
)

CODEX_BRIDGE_BIN = "/opt/fomo/bin/fomo-codex-rpc-bridge.mjs"
CODEX_BIN = "/opt/fomo/pi/bin/codex"
CODEX_STATE_DIR = "/var/lib/fomo-codex"
CODEX_MODEL_REFS = frozenset(
    {
        "fomo-litellm/fomo-pi-gpt-5.5",
        "fomo-litellm/fomo-pi-gpt-5.6",
    }
)
CODEX_THINKING_LEVELS = frozenset({"low", "medium", "high", "xhigh"})


class OpenSandboxCodexTransport(OpenSandboxPiTransport):
    """Run one Codex CLI turn without falling back to Pi or OpenCode."""

    async def run(
        self,
        ref: SandboxRef,
        invocation: PiInvocation,
        *,
        on_event: PiEventSink | None = None,
        on_diagnostic: PiDiagnosticSink | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> PiTransportResult:
        request = invocation.request
        if request.model not in CODEX_MODEL_REFS:
            raise PiBridgeFailed(
                {
                    "code": "codex_profile_unsupported",
                    "message": "Codex requires a GPT runtime profile.",
                    "phase": "dispatch",
                }
            )
        if request.thinking not in CODEX_THINKING_LEVELS:
            raise PiBridgeFailed(
                {
                    "code": "codex_thinking_unsupported",
                    "message": "Codex does not support the selected thinking level.",
                    "phase": "dispatch",
                }
            )
        codex_request = replace(
            request,
            bridge_bin=CODEX_BRIDGE_BIN,
            pi_bin=CODEX_BIN,
            state_dir=CODEX_STATE_DIR,
            # Codex exec/resume has no FOMO request_user_input virtual tool.
            user_input_enabled=False,
        )
        try:
            return await super().run(
                ref,
                PiInvocation(codex_request),
                on_event=on_event,
                on_diagnostic=on_diagnostic,
                cancel_event=cancel_event,
            )
        except (PiBridgeFailed, PiTransportCancelled):
            raise
        except (PiBridgeProtocolError, PiTransportError):
            raise PiBridgeFailed(
                {
                    "code": "codex_runtime_failed",
                    "message": "Codex runtime could not complete the request.",
                    "phase": "transport",
                }
            ) from None
