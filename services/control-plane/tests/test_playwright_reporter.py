"""Bounded fail-closed contract for the Playwright 1.55.1 JSON reporter parser."""

from __future__ import annotations

import json

from fomo.agent_runtime.playwright_reporter import parse_playwright_json


def _report(suites: list[dict], errors: list | None = None, stats: dict | None = None) -> str:
    return json.dumps(
        {"config": {"rootDir": "."}, "suites": suites, "errors": errors or [], "stats": stats or {}}
    )


def _suite(specs: list[dict], nested: list[dict] | None = None) -> dict:
    return {
        "title": "",
        "file": "tests/generated/library.smoke.spec.ts",
        "specs": specs,
        "suites": nested or [],
    }


def _spec(
    title: str,
    overall: str,
    result_status: str | None = "passed",
    *,
    project_name: str = "chromium",
    no_results: bool = False,
) -> dict:
    """One real-shaped spec: title on the spec, tests[] without a title."""
    test_entry = {
        "expectedStatus": "passed",
        "status": overall,
        "projectId": "project-" + project_name,
        "projectName": project_name,
        "results": (
            []
            if no_results or result_status is None
            else [{"workerIndex": 0, "status": result_status, "duration": 10}]
        ),
    }
    return {"title": title, "ok": overall == "expected", "tests": [test_entry]}


def test_exactly_one_passed_test_is_trusted() -> None:
    outcome = parse_playwright_json(
        _report([_suite([_spec("library keeps a searchable catalog", "expected", "passed")])])
    )
    assert outcome is not None
    assert outcome.test_count == 1
    # The unique title comes from spec.title, never from test entries.
    assert outcome.title == "library keeps a searchable catalog"
    assert outcome.status == "passed"
    assert outcome.top_level_errors == 0
    assert outcome.load_errors == 0


def test_overall_expected_requires_a_real_passed_result() -> None:
    # Overall expected but the last result is timedOut: not a pass.
    timed_out = parse_playwright_json(
        _report([_suite([_spec("slow", "expected", "timedOut")])])
    )
    assert timed_out is not None and timed_out.status == "did_not_run"
    interrupted = parse_playwright_json(
        _report([_suite([_spec("halted", "expected", "interrupted")])])
    )
    assert interrupted is not None and interrupted.status == "did_not_run"
    skipped_result = parse_playwright_json(
        _report([_suite([_spec("skipped", "expected", "skipped")])])
    )
    assert skipped_result is not None and skipped_result.status == "did_not_run"
    # No results at all is structurally invalid: fail closed to None.
    no_results = parse_playwright_json(
        _report([_suite([_spec("no results", "expected", no_results=True)])])
    )
    assert no_results is None


def test_unexpected_failure_or_test_timeout_is_a_product_failure() -> None:
    failed = parse_playwright_json(
        _report([_suite([_spec("broke", "unexpected", "failed")])])
    )
    assert failed is not None
    assert failed.status == "failed"
    assert failed.title == "broke"
    # A completed reporter result proves that this individual product test ran
    # and exhausted its own timeout. The caller separately classifies an outer
    # runner/command timeout as infrastructure.
    timed_out = parse_playwright_json(
        _report([_suite([_spec("broke", "unexpected", "timedOut")])])
    )
    assert timed_out is not None and timed_out.status == "failed"


def test_failed_assertion_projects_only_bounded_sanitized_details() -> None:
    secret = "diagnostic-secret-value"
    base64_body = "A" * 4_000
    oversized_tail = "TAIL-MUST-NOT-SURVIVE-" + "z" * 20_000
    message = "\n".join(
        (
            "Error: \x1b[2mexpect(locator).toBeVisible()\x1b[22m failed",
            "Locator: getByText('张三', { exact: true }).first()",
            "Expected: visible",
            "Received: <element(s) not found>",
            f"PASSWORD={secret}",
            f"attachment: data:image/png;base64,{base64_body}",
            "Call log:",
            oversized_tail,
        )
    )
    report = _report(
        [
            _suite(
                [
                    {
                        "title": "报名成功后展示报名人",
                        "tests": [
                            {
                                "status": "unexpected",
                                "results": [
                                    {
                                        "status": "failed",
                                        "error": {
                                            "message": message,
                                            "stack": f"TRACE-MUST-NOT-LEAK {secret}",
                                            "location": {
                                                "file": "/workspace/tests/private.spec.ts",
                                                "line": 16,
                                                "column": 94,
                                            },
                                            "snippet": base64_body,
                                        },
                                        "attachments": [
                                            {
                                                "name": "trace",
                                                "body": base64_body,
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            )
        ]
    )

    outcome = parse_playwright_json(report)

    assert outcome is not None and outcome.status == "failed"
    assert outcome.assertion is not None
    assert outcome.assertion.test_name == "报名成功后展示报名人"
    assert outcome.assertion.locator == "getByText('张三', { exact: true }).first()"
    assert outcome.assertion.line == 16
    assert "Expected: visible" in outcome.assertion.message
    assert "[REDACTED]" in outcome.assertion.message
    assert len(outcome.assertion.message) <= 1_200
    serialized = repr(outcome)
    for forbidden in (
        "\x1b",
        secret,
        "TRACE-MUST-NOT-LEAK",
        base64_body,
        oversized_tail,
        "/workspace/tests/private.spec.ts",
    ):
        assert forbidden not in serialized


def test_oversized_assertion_message_is_truncated_without_unbounded_body() -> None:
    message = "Error: assertion failed\nLocator: getByText('候补位置 #1')\n" + (
        "safe-detail " * 50_000
    )
    payload = json.loads(
        _report([_suite([_spec("候补位置保留", "unexpected", "failed")])])
    )
    result = payload["suites"][0]["specs"][0]["tests"][0]["results"][0]
    result["error"] = {"message": message, "location": {"line": 21}}

    outcome = parse_playwright_json(json.dumps(payload))

    assert outcome is not None and outcome.assertion is not None
    assert outcome.assertion.locator == "getByText('候补位置 #1')"
    assert outcome.assertion.line == 21
    assert len(outcome.assertion.message) <= 1_200
    assert "…" in outcome.assertion.message


def test_select_option_failure_keeps_action_and_target_without_call_log_body() -> None:
    payload = json.loads(
        _report([_suite([_spec("选择票种", "unexpected", "failed")])])
    )
    result = payload["suites"][0]["specs"][0]["tests"][0]["results"][0]
    result["error"] = {
        "message": "\n".join(
            (
                "Error: locator.selectOption: Element is not a <select> element",
                "Call log:",
                "  - waiting for getByLabel('票种')",
                "  - locator resolved to <button>内部大段 DOM 不应进入诊断</button>",
            )
        ),
        "location": {"line": 8},
    }

    outcome = parse_playwright_json(json.dumps(payload, ensure_ascii=False))

    assert outcome is not None and outcome.assertion is not None
    assert "locator.selectOption" in outcome.assertion.message
    assert outcome.assertion.locator == "getByLabel('票种')"
    assert "内部大段 DOM" not in repr(outcome.assertion)


def test_flaky_skipped_and_interrupted_overall_never_become_assertions() -> None:
    for overall in ("flaky", "skipped", "didNotRun", "interrupted"):
        outcome = parse_playwright_json(
            _report([_suite([_spec("odd", overall, "passed")])])
        )
        assert outcome is not None
        assert outcome.status == "did_not_run"


def test_multiple_specs_and_projects_count_every_test() -> None:
    report = _report(
        [
            _suite(
                [
                    _spec("first", "expected", "passed", project_name="chromium"),
                    _spec("first", "expected", "passed", project_name="firefox"),
                ]
            ),
            _suite([_spec("second", "unexpected", "failed", project_name="chromium")]),
        ]
    )
    outcome = parse_playwright_json(report)
    assert outcome is not None
    assert outcome.test_count == 3
    # More than one test can never prove a single planned title.
    assert outcome.title is None
    assert outcome.status is None


def test_zero_tests_fails_closed() -> None:
    outcome = parse_playwright_json(_report([_suite([])]))
    assert outcome is not None
    assert outcome.test_count == 0
    assert outcome.status is None


def test_nested_describe_suites_are_walked_deterministically() -> None:
    outcome = parse_playwright_json(
        _report([_suite([], nested=[_suite([_spec("nested test", "expected", "passed")])])])
    )
    assert outcome is not None
    assert outcome.test_count == 1
    assert outcome.title == "nested test"


def test_top_level_and_load_errors_are_infrastructure_signal() -> None:
    with_error = parse_playwright_json(
        _report([_suite([_spec("x", "expected", "passed")])], errors=[{"message": "boom"}])
    )
    assert with_error is not None and with_error.top_level_errors == 1
    with_load_error = parse_playwright_json(
        _report(
            [
                _suite(
                    [
                        {
                            "title": "bad spec",
                            "ok": False,
                            "errors": [{"message": "load failed"}],
                            "tests": [],
                        }
                    ]
                )
            ]
        )
    )
    assert with_load_error is not None and with_load_error.load_errors == 1


def test_structural_validation_covers_every_result_entry() -> None:
    # A malformed middle result must fail closed even when the last result
    # would otherwise read as passed.
    report = _report(
        [
            _suite(
                [
                    {
                        "title": "x",
                        "ok": True,
                        "tests": [
                            {
                                "expectedStatus": "passed",
                                "status": "expected",
                                "projectName": "chromium",
                                "results": [
                                    {"workerIndex": 0, "status": "passed"},
                                    {"workerIndex": 0, "status": 42},
                                ],
                            }
                        ],
                    }
                ]
            )
        ]
    )
    assert parse_playwright_json(report) is None

    # A test without a results key at all is also structurally invalid.
    missing = _report(
        [
            _suite(
                [
                    {
                        "title": "missing",
                        "ok": True,
                        "tests": [
                            {
                                "expectedStatus": "passed",
                                "status": "expected",
                                "projectName": "chromium",
                            }
                        ],
                    }
                ]
            )
        ]
    )
    assert parse_playwright_json(missing) is None

    # Every result entry must be validated; the last result still decides the
    # single-run status.
    retried = parse_playwright_json(
        _report(
            [
                _suite(
                    [
                        {
                            "title": "retried",
                            "ok": True,
                            "tests": [
                                {
                                    "expectedStatus": "passed",
                                    "status": "expected",
                                    "projectName": "chromium",
                                    "results": [
                                        {"workerIndex": 0, "status": "failed"},
                                        {"workerIndex": 0, "status": "passed"},
                                    ],
                                }
                            ],
                        }
                    ]
                )
            ]
        )
    )
    assert retried is not None
    assert retried.status == "passed"


def test_malformed_oversized_or_non_object_output_fails_closed() -> None:
    assert parse_playwright_json("") is None
    assert parse_playwright_json("not json") is None
    assert parse_playwright_json('{"stats": 1}') is None
    assert parse_playwright_json("[1, 2]") is None
    assert parse_playwright_json(
        _report([_suite([_spec("x", "expected", "passed")])]), max_bytes=10
    ) is None
    assert parse_playwright_json('{"suites": [{"specs": "bad"}], "errors": []}') is None
    assert (
        parse_playwright_json(
            '{"suites": [{"specs": [{"title": "x", "tests": [{"status": 1}]}]}], "errors": []}'
        )
        is None
    )
    assert (
        parse_playwright_json('{"suites": [{"suites": [1], "specs": []}], "errors": []}') is None
    )
