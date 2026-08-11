"""Shared fake sandbox helpers for Direct Pi tests (not collected by pytest).

``GitAwareSandbox`` extends the repository's in-memory provider with
deterministic answers for FOMO's fixed Git commands so publish-time SHA
validation and clean-HEAD binding checks behave like a real workspace.
"""

from __future__ import annotations

import json
import re

from fomo.persistence import Repository
from fomo.persistence.models import RunRecord
from fomo.sandbox.base import ExecResult
from fomo.sandbox.fake import FakeSandboxProvider

STARTER_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
CHECKPOINT_SHA = "c" * 40


async def persisted_sandbox_id(repository: Repository, run_id: str) -> str | None:
    """Read the internal durable sandbox reference without widening RunResponse."""
    async with repository.database.session_factory() as session:
        record = await session.get(RunRecord, run_id)
        assert record is not None
        return record.sandbox_id


class GitAwareSandbox(FakeSandboxProvider):
    """FakeSandboxProvider that answers FOMO's fixed Git command patterns.

    `git rev-parse HEAD` yields a deterministic 40-hex SHA selected by the
    commit message marker; the clean-HEAD binding probe, `git init`, and
    `git tag` are accepted. Everything else falls through to the configured
    results or the default success.
    """

    async def exec(self, ref, command, sink):
        text = command.command
        result = None
        if text.startswith("test ") and '"$(git rev-parse HEAD)"' in text:
            result = ExecResult(0, "", "")
        elif "git rev-parse HEAD" in text:
            if "checkpoint candidate" in text:
                sha = CHECKPOINT_SHA
            elif "feat(agent): run" in text:
                sha = CANDIDATE_SHA
            else:
                sha = STARTER_SHA
            result = ExecResult(0, f"{sha}\n", "")
        elif text.startswith("git tag "):
            result = ExecResult(0, "", "")
        elif text.startswith("git init"):
            result = ExecResult(0, "", "")
        elif (
            "tests/fomo-acceptance/navigation-v" in text
            and text not in self.command_results
        ):
            path_match = re.search(
                r"(tests/fomo-acceptance/navigation-v\d+/[^\s]+\.smoke\.spec\.ts)",
                text,
            )
            if path_match is not None:
                source = self._sandbox(ref).files.get(path_match.group(1), b"").decode()
                title_match = re.search(r"test\((\"(?:[^\"\\]|\\.)*\")", source)
                if title_match is not None:
                    title = json.loads(title_match.group(1))
                    result = ExecResult(
                        0,
                        json.dumps(
                            {
                                "errors": [],
                                "suites": [
                                    {
                                        "specs": [
                                            {
                                                "title": title,
                                                "errors": [],
                                                "tests": [
                                                    {
                                                        "status": "expected",
                                                        "results": [{"status": "passed"}],
                                                    }
                                                ],
                                            }
                                        ]
                                    }
                                ],
                            }
                        ),
                        "",
                    )
        if result is None:
            return await super().exec(ref, command, sink)
        self._sandbox(ref).commands.append(text)
        if result.stdout:
            await sink("stdout", result.stdout)
        if result.stderr:
            await sink("stderr", result.stderr)
        return result
