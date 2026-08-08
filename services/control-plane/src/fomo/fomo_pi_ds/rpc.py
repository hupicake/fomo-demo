"""Strict streaming decoder for the root-owned fomo-pi-ds bridge protocol."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .invocation import (
    FOMO_PI_MODEL,
    FOMO_PI_THINKING,
    IDENTIFIER_PATTERN,
    MAX_IDENTIFIER_LENGTH,
    SESSION_ID_PATTERN,
)

SCHEMA_VERSION = 1
MAX_LINE_BYTES = 16 * 1024 * 1024
_ENVELOPE_KEYS = frozenset(
    {"schemaVersion", "requestId", "correlationId", "seq", "type", "payload"}
)
_LIFECYCLE_TYPES = frozenset({"started", "pi.event", "completed", "failed"})
_PUBLIC_PI_KINDS = frozenset(
    {
        "agent_start",
        "agent_end",
        "agent_settled",
        "turn_start",
        "turn_end",
        "message_start",
        "message_delta",
        "message_end",
        "bash_output",
        "tool_start",
        "tool_output",
        "tool_end",
        "queue_update",
        "compaction_start",
        "compaction_end",
        "auto_retry_start",
        "auto_retry_end",
        "summarization_retry_scheduled",
        "summarization_retry_attempt_start",
        "summarization_retry_finished",
        "extension_error",
    }
)
_FORBIDDEN_KEYS = frozenset({"thinking", "reasoning_content"})
_FORBIDDEN_BLOCK_TYPES = frozenset({"thinking", "reasoning"})


class PiBridgeProtocolError(RuntimeError):
    """The bridge stream violated the fail-closed protocol."""


class PiBridgeFailed(RuntimeError):
    """The bridge emitted a valid terminal failure envelope."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        code = payload.get("code", "bridge_failed")
        phase = payload.get("phase", "unknown")
        super().__init__(f"fomo-pi-ds failed ({code}, phase={phase})")


@dataclass(frozen=True, slots=True)
class PiBridgeEnvelope:
    seq: int
    type: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PiBridgeResult:
    started: dict[str, Any]
    events: tuple[PiBridgeEnvelope, ...]
    completed: dict[str, Any]


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_object(line: bytes) -> dict[str, Any]:
    try:
        text = line.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PiBridgeProtocolError("bridge emitted invalid JSONL") from exc
    if not isinstance(value, dict):
        raise PiBridgeProtocolError("bridge JSONL record must be an object")
    return value


def _assert_public(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        block_type = value.get("type")
        if isinstance(block_type, str):
            normalized = block_type.lower()
            if normalized in _FORBIDDEN_BLOCK_TYPES or normalized.startswith(
                ("thinking_", "reasoning_")
            ):
                raise PiBridgeProtocolError(f"hidden reasoning block escaped at {path}")
        for key, item in value.items():
            if key.lower() in _FORBIDDEN_KEYS:
                raise PiBridgeProtocolError(f"hidden reasoning field escaped at {path}.{key}")
            _assert_public(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_public(item, f"{path}[{index}]")


def _require_identifier(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_IDENTIFIER_LENGTH
        or not IDENTIFIER_PATTERN.fullmatch(value)
    ):
        raise PiBridgeProtocolError(f"{name} is not a valid identifier")
    return value


class PiBridgeStreamReducer:
    """Decode byte chunks and reduce one bridge invocation to a terminal result.

    The stream is valid only after :meth:`finish` observes one completed
    envelope and a clean byte boundary. A terminal failed envelope raises
    :class:`PiBridgeFailed`; an EOF without either terminal is a protocol error.
    """

    def __init__(self, *, request_id: str, correlation_id: str, session_id: str) -> None:
        self.request_id = _require_identifier(request_id, "request_id")
        self.correlation_id = _require_identifier(correlation_id, "correlation_id")
        if len(session_id) > MAX_IDENTIFIER_LENGTH or not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("session_id is not valid")
        self.session_id = session_id
        self._buffer = bytearray()
        self._next_seq = 1
        self._state = "waiting"
        self._started: dict[str, Any] | None = None
        self._completed: dict[str, Any] | None = None
        self._failed: dict[str, Any] | None = None
        self._events: list[PiBridgeEnvelope] = []
        self._saw_settled = False
        self._finished = False

    def feed(self, chunk: bytes) -> tuple[PiBridgeEnvelope, ...]:
        if self._finished:
            raise PiBridgeProtocolError("cannot feed a finished bridge stream")
        if not isinstance(chunk, bytes):
            raise TypeError("bridge stream chunks must be bytes")
        if not chunk:
            return ()
        self._buffer.extend(chunk)
        produced: list[PiBridgeEnvelope] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > MAX_LINE_BYTES:
                    raise PiBridgeProtocolError("bridge JSONL record exceeds its byte limit")
                break
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line or len(line) > MAX_LINE_BYTES:
                raise PiBridgeProtocolError("bridge emitted an empty or oversized JSONL record")
            envelope = self._reduce(_strict_json_object(line))
            produced.append(envelope)
        return tuple(produced)

    def finish(self) -> PiBridgeResult:
        if self._finished:
            raise PiBridgeProtocolError("bridge stream was already finished")
        self._finished = True
        if self._buffer:
            raise PiBridgeProtocolError("bridge stream ended with an unterminated JSONL record")
        if self._state == "failed" and self._failed is not None:
            raise PiBridgeFailed(self._failed)
        if self._state != "completed" or self._started is None or self._completed is None:
            raise PiBridgeProtocolError("bridge stream ended before completed")
        return PiBridgeResult(
            started=self._started,
            events=tuple(self._events),
            completed=self._completed,
        )

    def _reduce(self, record: dict[str, Any]) -> PiBridgeEnvelope:
        if set(record) != _ENVELOPE_KEYS:
            raise PiBridgeProtocolError("bridge envelope fields do not match schema v1")
        if record["schemaVersion"] != SCHEMA_VERSION:
            raise PiBridgeProtocolError("bridge envelope schemaVersion is unsupported")
        if record["requestId"] != self.request_id:
            raise PiBridgeProtocolError("bridge requestId does not match invocation")
        if record["correlationId"] != self.correlation_id:
            raise PiBridgeProtocolError("bridge correlationId does not match invocation")
        seq = record["seq"]
        if type(seq) is not int or seq != self._next_seq:
            raise PiBridgeProtocolError("bridge sequence must start at 1 and remain contiguous")
        self._next_seq += 1
        event_type = record["type"]
        if event_type not in _LIFECYCLE_TYPES:
            raise PiBridgeProtocolError("bridge envelope type is unknown")
        payload = record["payload"]
        if not isinstance(payload, dict):
            raise PiBridgeProtocolError("bridge envelope payload must be an object")
        _assert_public(payload)
        envelope = PiBridgeEnvelope(seq=seq, type=event_type, payload=payload)

        if self._state in {"completed", "failed"}:
            raise PiBridgeProtocolError("bridge emitted data after a terminal envelope")
        if event_type == "started":
            self._on_started(payload)
        elif event_type == "pi.event":
            self._on_pi_event(payload)
        elif event_type == "completed":
            self._on_completed(payload)
        else:
            self._on_failed(payload)
        self._events.append(envelope)
        return envelope

    def _on_started(self, payload: dict[str, Any]) -> None:
        if self._state != "waiting":
            raise PiBridgeProtocolError("started must be the first lifecycle envelope")
        if (
            payload.get("sessionId") != self.session_id
            or payload.get("model") != FOMO_PI_MODEL
            or payload.get("thinkingLevel") != FOMO_PI_THINKING
        ):
            raise PiBridgeProtocolError("started payload does not match the invocation contract")
        if not isinstance(payload.get("initialStats"), dict):
            raise PiBridgeProtocolError("started payload is missing initialStats")
        self._started = payload
        self._state = "running"

    def _on_pi_event(self, payload: dict[str, Any]) -> None:
        if self._state != "running":
            raise PiBridgeProtocolError("pi.event is only valid after started")
        kind = payload.get("kind")
        if kind not in _PUBLIC_PI_KINDS:
            raise PiBridgeProtocolError("pi.event kind is unknown")
        if kind == "agent_settled":
            if self._saw_settled:
                raise PiBridgeProtocolError("agent_settled was emitted more than once")
            self._saw_settled = True

    def _on_completed(self, payload: dict[str, Any]) -> None:
        if self._state != "running" or not self._saw_settled:
            raise PiBridgeProtocolError("completed requires a prior agent_settled event")
        if payload.get("sessionId") != self.session_id:
            raise PiBridgeProtocolError("completed sessionId does not match invocation")
        state = payload.get("state")
        stats = payload.get("stats")
        if not isinstance(state, dict) or not isinstance(stats, dict):
            raise PiBridgeProtocolError("completed payload requires state and stats objects")
        if state.get("sessionId") != self.session_id or stats.get("sessionId") != self.session_id:
            raise PiBridgeProtocolError("completed state or stats belongs to another session")
        self._completed = payload
        self._state = "completed"

    def _on_failed(self, payload: dict[str, Any]) -> None:
        if self._state not in {"waiting", "running"}:
            raise PiBridgeProtocolError("failed envelope is out of lifecycle order")
        if not all(isinstance(payload.get(key), str) and payload[key] for key in ("code", "message", "phase")):
            raise PiBridgeProtocolError("failed payload is incomplete")
        self._failed = payload
        self._state = "failed"
