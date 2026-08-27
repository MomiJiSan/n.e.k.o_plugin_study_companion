from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "static" / "question_generation_eval_cases.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
question_eval = importlib.import_module("question_generation_eval")


def _case(
    case_id: str,
    *,
    topic_id: str = "addition",
    question_type: str = "math_exact",
    difficulty: int = 2,
    fixture_id: str | None = None,
    expected: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": case_id,
        "topic_id": topic_id,
        "subject": "math",
        "stage": "primary",
        "scenario": "normal_practice",
        "requested_question_type": question_type,
        "planned_difficulty": difficulty,
        "prompt_language": "zh-CN",
        "learner_context": {},
        "expected": expected
        or {
            "target_topic_id": topic_id,
            "question_type": question_type,
        },
        "fixture_id": fixture_id or case_id,
    }


def _passing_output(
    *,
    topic_id: str = "addition",
    question_type: str = "math_exact",
    difficulty: int = 2,
    question: str = "计算 2 + 3 的结果。",
    answer: str = "5",
) -> dict[str, object]:
    return {
        "question": question,
        "answer": answer,
        "reference_answer": answer,
        "accepted_answers": [answer],
        "key_points": [answer],
        "rubric": {"正确结果": 100},
        "question_type": question_type,
        "target_topic_id": topic_id,
        "difficulty": difficulty,
        "hint": "进行计算。",
    }


def test_bundled_case_set_locks_pr1_coverage_contract() -> None:
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert payload["type"] == "study_companion_question_generation_eval_cases"
    assert payload["version"] == 1
    cases = payload["cases"]
    assert isinstance(cases, list)
    assert 175 <= len(cases) <= 185

    required = {
        "id",
        "topic_id",
        "subject",
        "stage",
        "scenario",
        "requested_question_type",
        "planned_difficulty",
        "prompt_language",
        "source_text",
        "learner_context",
        "expected",
    }
    assert all(isinstance(case, dict) and required <= set(case) for case in cases)
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({case.get("fixture_id") or case["id"] for case in cases}) == len(cases)
    assert len({case["subject"] for case in cases}) == 11
    assert len({case["stage"] for case in cases}) == 4
    assert {case["requested_question_type"] for case in cases} == {
        "short_answer",
        "math_exact",
        "math_reasoning",
    }
    assert {
        "normal_practice",
            "error_retry",
        "due_review",
        "weak_topic",
        "prerequisite_blocked",
    } <= {case["scenario"] for case in cases}
    assert {case["planned_difficulty"] for case in cases} <= {2, 3, 4}
    english_count = sum(str(case["prompt_language"]).startswith("en") for case in cases)
    assert 0.15 <= english_count / len(cases) <= 0.25
    expected_required = {
        "target_topic_id",
        "question_type",
        "difficulty",
        "forbid_answer_leak",
        "forbid_prompt_leakage",
        "reference_answer_correct",
        "rubric_answer_consistent",
    }
    assert all(expected_required <= set(case["expected"]) for case in cases)


def test_fixture_evaluation_calculates_all_passing_metrics() -> None:
    case = _case("pass", expected={"target_topic_id": "addition", "question_type": "math_exact", "reference_answer": "5"})
    report = question_eval.evaluate_cases(
        [case],
        {"outputs": {"pass": _passing_output()}},
    )

    assert report["mode"] == "fixtures"
    assert report["case_count"] == 1
    assert report["run_count"] == 1
    assert report["quality_gate_passed"] is True
    for metric_name in (
        "structural_contract_pass_rate",
        "target_topic_relevance_rate",
        "reference_answer_correct_rate",
        "rubric_answer_consistency_rate",
        "difficulty_within_one_rate",
    ):
        assert report["metrics"][metric_name]["rate"] == 1.0
    assert report["metrics"]["prompt_leakage_count"]["count"] == 0
    assert report["metrics"]["normalized_duplicate_question_rate"]["rate"] == 0.0
    assert all(report["results"][0]["passed_checks"].values())
    assert report["results"][0]["failures"] == []


def test_fixture_evaluation_reports_quality_failures_without_raising() -> None:
    good = _case("good")
    broken = _case(
        "broken",
        fixture_id="broken",
        expected={
            "target_topic_id": "addition",
            "question_type": "math_exact",
            "reference_answer": "twelve",
        },
    )
    leaked_and_duplicate = _passing_output(question="计算错误答案的结果。", answer="wrong")
    leaked_and_duplicate["hint"] = "答案是 wrong。"
    leaked_and_duplicate["target_topic_id"] = "outside"
    leaked_and_duplicate["question_type"] = "short_answer"
    leaked_and_duplicate["difficulty"] = 5
    report = question_eval.evaluate_cases(
        [good, broken],
        {
            "outputs": {
                "good": _passing_output(question="计算 2 + 3 的结果。"),
                "broken": leaked_and_duplicate,
            }
        },
    )

    assert report["quality_gate_passed"] is False
    assert report["metrics"]["structural_contract_pass_rate"]["rate"] == 0.5
    assert report["metrics"]["target_topic_relevance_rate"]["rate"] == 0.5
    assert report["metrics"]["reference_answer_correct_rate"]["rate"] == 0.5
    assert report["metrics"]["difficulty_within_one_rate"]["rate"] == 0.5
    assert report["metrics"]["prompt_leakage_count"]["count"] == 1
    assert report["metrics"]["normalized_duplicate_question_rate"]["rate"] == 0.0
    failures = report["results"][1]["failures"]
    assert {"target_topic_relevance", "reference_answer_correct", "prompt_leakage_free"} <= set(failures)


def test_fixture_evaluation_detects_normalized_duplicate_questions() -> None:
    report = question_eval.evaluate_cases(
        [_case("first"), _case("second", fixture_id="second")],
        {
            "outputs": {
                "first": _passing_output(question="计算 2 + 3 的结果！"),
                "second": _passing_output(question="计算2+3的结果。"),
            }
        },
    )

    assert report["quality_gate_passed"] is False
    assert report["metrics"]["normalized_duplicate_question_rate"]["rate"] == 1.0
    assert report["metrics"]["normalized_duplicate_question_rate"]["duplicate_count"] == 2


def test_cli_writes_quality_failure_report_but_returns_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases_path = tmp_path / "cases.json"
    fixtures_path = tmp_path / "fixtures.json"
    report_path = tmp_path / "report.json"
    cases_path.write_text(json.dumps({"cases": [_case("bad")]}), encoding="utf-8")
    fixtures_path.write_text(
        json.dumps({"outputs": {"bad": {}}}), encoding="utf-8"
    )

    assert question_eval.main(
        [
            "--cases",
            str(cases_path),
            "--fixtures",
            str(fixtures_path),
            "--report",
            str(report_path),
        ]
    ) == 0
    assert report_path.exists()
    assert json.loads(report_path.read_text(encoding="utf-8"))["quality_gate_passed"] is False
    assert '"quality_gate_passed": false' in capsys.readouterr().out


def test_cli_returns_usage_error_for_missing_fixture_file(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps({"cases": [_case("pass")]}), encoding="utf-8")

    assert question_eval.main(
        ["--cases", str(cases_path), "--fixtures", str(tmp_path / "missing.json")]
    ) == 2
