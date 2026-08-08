"""Bounded, fail-closed parser for Playwright's structured JSON reporter.

Playwright 1.55.1's ``json`` reporter nests tests inside ``suites[].specs[]``:
the unique test title lives on the spec, while ``spec.tests[]`` entries carry
``expectedStatus``, ``status`` (expected/unexpected/flaky/skipped/didNotRun/
interrupted) and ``results[]`` whose per-result ``status`` is passed/failed/
timedOut/skipped/interrupted. The SOP trusts only this projection; oversized,
malformed, or structurally invalid output returns ``None`` and the caller
treats that as an infrastructure failure — never a fabricated assertion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

# Reporter output is bounded well below the command output cap so a truncated
# or hostile stream can never be parsed as a valid report.
_MAX_PLAYWRIGHT_REPORT_BYTES = 2_000_000

PlaywrightTestStatus = Literal["passed", "failed", "did_not_run"]


@dataclass(frozen=True, slots=True)
class PlaywrightReport:
    """Projection of exactly what the SOP is allowed to trust."""

    test_count: int
    title: str | None
    status: PlaywrightTestStatus | None
    top_level_errors: int
    load_errors: int


def _result_status(test: dict[str, Any]) -> tuple[bool, str | None]:
    """Return ``(valid, last_result_status)`` after validating every result.

    ``valid=False`` means the results structure is missing, empty, or
    malformed and the caller must fail closed (``None``). ``valid=True``
    returns the last result's status, which still decides the single-run
    status; a flaky overall status keeps the test unverified at the caller.
    """
    results = test.get("results")
    if not isinstance(results, list) or not results:
        return False, None
    statuses: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            return False, None
        status = result.get("status")
        if not isinstance(status, str):
            return False, None
        statuses.append(status)
    return True, statuses[-1]


def parse_playwright_json(
    stdout: str, *, max_bytes: int = _MAX_PLAYWRIGHT_REPORT_BYTES
) -> PlaywrightReport | None:
    """Return ``None`` (fail closed) unless the report is structurally sound.

    A passed test requires the overall test status ``expected`` and a real
    ``passed`` result; an assertion failure requires overall ``unexpected``
    with a plain ``failed`` result. timedOut/interrupted/skipped results,
    flaky/didNotRun/skipped overall statuses, zero or multiple tests,
    top-level (startup/worker) errors, spec load errors and any structural
    anomaly map to ``did_not_run`` or ``None`` — never to a fabricated pass.
    """
    if len(stdout.encode("utf-8")) > max_bytes:
        return None
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    top_level_errors = payload.get("errors")
    if not isinstance(top_level_errors, list):
        return None

    suites = payload.get("suites")
    if not isinstance(suites, list):
        return None
    specs: list[dict[str, Any]] = []
    load_errors = 0
    stack = list(suites)
    while stack:
        suite = stack.pop()
        if not isinstance(suite, dict):
            return None
        nested = suite.get("suites")
        if nested is not None:
            if not isinstance(nested, list) or not all(
                isinstance(item, dict) for item in nested
            ):
                return None
            stack.extend(nested)
        suite_specs = suite.get("specs")
        if suite_specs is None:
            continue
        if not isinstance(suite_specs, list) or not all(
            isinstance(item, dict) for item in suite_specs
        ):
            return None
        specs.extend(suite_specs)

    tests: list[tuple[str, str, str | None]] = []
    for spec in specs:
        spec_errors = spec.get("errors")
        if spec_errors:
            if not isinstance(spec_errors, list) or not all(
                isinstance(item, dict) for item in spec_errors
            ):
                return None
            load_errors += len(spec_errors)
        title = spec.get("title")
        if not isinstance(title, str):
            return None
        spec_tests = spec.get("tests")
        if spec_tests is None:
            continue
        if not isinstance(spec_tests, list) or not all(
            isinstance(item, dict) for item in spec_tests
        ):
            return None
        for test in spec_tests:
            overall = test.get("status")
            if not isinstance(overall, str):
                return None
            valid_results, result_status = _result_status(test)
            if not valid_results:
                return None
            tests.append((title, overall, result_status))

    if len(tests) == 1:
        title, overall, result_status = tests[0]
        if overall == "expected" and result_status == "passed":
            mapped: PlaywrightTestStatus = "passed"
        elif overall == "unexpected" and result_status == "failed":
            mapped = "failed"
        else:
            mapped = "did_not_run"
    else:
        title = None
        mapped = None
    return PlaywrightReport(
        test_count=len(tests),
        title=title,
        status=mapped,
        top_level_errors=len(top_level_errors),
        load_errors=load_errors,
    )
