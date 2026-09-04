from __future__ import annotations

import json
from pathlib import Path

from tools import cognitive_v2_acceptance as acceptance


def test_fixed_clock_advances_without_wall_time() -> None:
    clock = acceptance.FixedClock()

    assert clock.iso() == "2026-09-01T08:30:00.000000Z"
    clock.advance(hours=24, minutes=5)

    assert clock.iso() == "2026-09-02T08:35:00.000000Z"


def test_acceptance_runner_passes_required_and_boundary_scenarios(
    tmp_path: Path,
) -> None:
    report = acceptance.run_acceptance(report_dir=tmp_path, profile="ci")

    assert report["summary"] == {
        "status": "PASS",
        "scenario_count": 14,
        "passed": 14,
        "failed": 0,
    }
    assert {item["name"] for item in report["scenarios"]} == {
        "happy_path_resolved",
        "same_hypothesis_relapse",
        "ordinary_error_reschedule",
        "control_interruptions",
        "outbox_failure_isolation",
        "restart_and_rebuild",
        "lease_takeover_fencing",
        "cognitive_off_on_equivalence",
        "same_second_out_of_order_facts",
        "unknown_version_set_fail_closed",
        "legacy_database_copy_migration",
        "retention_question_family_rotation",
        "retention_early_hint_expired_window",
        "all_cognitive_features_disabled_equivalence",
    }
    assert all(item["status"] == "PASS" for item in report["scenarios"])
    assert (tmp_path / acceptance.REPORT_JSON).is_file()
    assert (tmp_path / acceptance.REPORT_MARKDOWN).is_file()

    ordinary = next(
        item
        for item in report["scenarios"]
        if item["name"] == "ordinary_error_reschedule"
    )
    assert any(
        step["step"] == "dont_know_reschedules_without_relapse"
        for step in ordinary["steps"]
    )


def test_each_report_step_has_a_machine_readable_acceptance_contract(
    tmp_path: Path,
) -> None:
    report = acceptance.run_acceptance(report_dir=tmp_path, profile="ci")

    required = {
        "input_event",
        "generated_facts",
        "expected_state",
        "actual_state",
        "invariant_result",
        "failure_diff",
    }
    for scenario in report["scenarios"]:
        for step in scenario["steps"]:
            assert required <= set(step)
            assert step["invariant_result"] == "PASS"
            assert step["failure_diff"] == {}


def test_reports_are_byte_stable_and_do_not_leak_private_content(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    acceptance.run_acceptance(report_dir=first, profile="ci")
    acceptance.run_acceptance(report_dir=second, profile="ci")

    first_json = (first / acceptance.REPORT_JSON).read_bytes()
    second_json = (second / acceptance.REPORT_JSON).read_bytes()
    first_markdown = (first / acceptance.REPORT_MARKDOWN).read_bytes()
    second_markdown = (second / acceptance.REPORT_MARKDOWN).read_bytes()
    assert first_json == second_json
    assert first_markdown == second_markdown

    combined = (first_json + first_markdown).decode("utf-8").lower()
    for forbidden in (
        "synthetic-response",
        "synthetic-reference",
        "deterministic-fixture",
        "claim_token",
        "lease_token",
        "evidence_span",
    ):
        assert forbidden not in combined


def test_report_boundary_recursively_removes_private_fields() -> None:
    sanitized = acceptance._sanitize(
        {
            "status": "ok",
            "nested": {
                "user_answer": "private",
                "claim_token": "secret",
                "safe": 1,
            },
            "rows": [{"lease_token": "secret", "count": 2}],
        }
    )

    assert sanitized == {
        "status": "ok",
        "nested": {"safe": 1},
        "rows": [{"count": 2}],
    }


def test_main_returns_one_when_a_scenario_fails(
    monkeypatch, tmp_path: Path
) -> None:
    def failing_scenario(*_args):
        recorder = acceptance.ScenarioRecorder("expected_failure")
        recorder.check("invariant", expected=True, actual=False)
        return recorder.result()

    monkeypatch.setattr(acceptance, "SCENARIOS", (failing_scenario,))

    exit_code = acceptance.main(
        ["--profile", "ci", "--report-dir", str(tmp_path)]
    )

    assert exit_code == 1
    report = json.loads((tmp_path / acceptance.REPORT_JSON).read_text("utf-8"))
    assert report["summary"]["status"] == "FAIL"


def test_main_returns_two_for_tool_failure(monkeypatch, tmp_path: Path) -> None:
    def fail_to_run(**_kwargs):
        raise OSError("synthetic tool failure")

    monkeypatch.setattr(acceptance, "run_acceptance", fail_to_run)

    assert (
        acceptance.main(["--profile", "ci", "--report-dir", str(tmp_path)])
        == 2
    )


def test_runner_has_no_test_helper_network_or_sleep_dependency() -> None:
    source = Path(acceptance.__file__).read_text(encoding="utf-8")

    assert "from tests" not in source
    assert "import tests" not in source
    assert "sleep(" not in source
    assert "httpx" not in source
    assert "requests" not in source
