"""Shared redaction and bounded terminal-text helpers."""

from __future__ import annotations

import re

_CREDENTIAL_NAME = (
    r"(?:[A-Za-z_][A-Za-z0-9_-]*[_-])?"
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|"
    r"secret|cookie|private[_-]?key|master[_-]?key)"
)
_QUOTED_CREDENTIAL = re.compile(
    rf"(?i)(?P<prefix>[\"']?{_CREDENTIAL_NAME}[\"']?\s*[=:]\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_UNQUOTED_CREDENTIAL = re.compile(
    rf"(?i)(?P<prefix>[\"']?{_CREDENTIAL_NAME}[\"']?\s*[=:]\s*)"
    r"(?P<value>[^\s,;}\]]+)"
)
_ANSI_ESCAPE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])"
)
_DATA_URI_BASE64 = re.compile(
    r"(?i)data:[^\s,]{1,128};base64,[A-Za-z0-9+/_=-]{32,}"
)
_LONG_BASE64 = re.compile(
    r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{128,}={0,2}(?![A-Za-z0-9+/_=-])"
)


def redact(value: str) -> str:
    """Redact common credential forms before text crosses a trust boundary."""

    value = re.sub(
        r"(?im)(\b(?:cookie|set-cookie)\s*:\s*)[^\r\n]+",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"(?i)(\bbearer\s+)[^\s,;]+", r"\1[REDACTED]", value)
    value = _QUOTED_CREDENTIAL.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}"
            f"[REDACTED]{match.group('quote')}"
        ),
        value,
    )
    value = _UNQUOTED_CREDENTIAL.sub(r"\g<prefix>[REDACTED]", value)
    return re.sub(r"\bsk-[A-Za-z0-9_\-]{8,}\b", "[REDACTED]", value)


def strip_terminal_controls(value: str) -> str:
    """Remove ANSI/OSC sequences and unsafe C0 controls from terminal text."""

    without_ansi = _ANSI_ESCAPE.sub("", value)
    return without_ansi.translate(
        {character: None for character in range(32) if character not in {9, 10, 13}}
    )


def bounded_diagnostic_text(value: str, *, limit: int) -> str:
    """Return a redacted, terminal-safe value with bounded input and output.

    Only a small prefix is inspected so an arbitrary reporter body cannot make
    diagnostic construction itself unbounded. Long base64-like payloads and
    data URIs are replaced rather than forwarded to artifacts or prompts.
    """

    if limit < 1:
        raise ValueError("limit must be positive")
    sample = value[: max(limit * 8, limit)]
    cleaned = strip_terminal_controls(sample)
    cleaned = redact(cleaned)
    cleaned = _DATA_URI_BASE64.sub("[REDACTED_BASE64]", cleaned)
    cleaned = _LONG_BASE64.sub("[REDACTED_BASE64]", cleaned)
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 1].rstrip()}…"
