"""Offline-first quality evaluation for generated study questions.

This is deliberately a reporting tool, not a production validation gate.  The
default fixture mode is deterministic and makes no network calls.  The optional
``--live`` mode is a manual check: it uses the configured study model gateway
and generates each case the requested number of times (three by default).

Supported case schema (``static/question_generation_eval_cases.json``)::

    {"type": "question_generation_eval_cases", "version": 1, "cases": [{
        "id": "...", "topic_id": "...", "requested_question_type": "...",
        "planned_difficulty": 3, "expected": {"target_topic_id": "..."}
    }]}

Fixture files may be either ``{"outputs": {fixture_id: payload_or_list}}``
or the mapping itself.  A list represents repeated generations for one case.
The evaluator intentionally uses deterministic, inspectable proxies for
semantic quality; it cannot prove subject-matter truth without a human/model
judge.  Those assumptions are included in every report.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Sequence

ALLOWED_QUESTION_TYPES = frozenset({"short_answer", "math_exact", "math_reasoning"})
REPORT_VERSION = 1
DEFAULT_CASES_PATH = Path("static/question_generation_eval_cases.json")
DEFAULT_FIXTURES_PATH = Path("static/question_generation_eval_fixtures.json")
DEFAULT_REPORT_PATH = Path("artifacts/question_generation_eval_report.json")
THRESHOLDS: dict[str, float | int] = {
    "structural_contract_pass_rate": 0.99,
    "target_topic_relevance_rate": 0.97,
    "reference_answer_correct_rate": 0.98,
    "rubric_answer_consistency_rate": 0.98,
    "difficulty_within_one_rate": 0.90,
    "prompt_leakage_count": 0,
    "normalized_duplicate_question_rate": 0.10,
}
ASSUMPTIONS = (
    "Offline relevance is a target-topic binding check, not a semantic judgement.",
    "Reference-answer correctness is exact expected-answer matching when supplied; "
    "otherwise it is answer/reference-answer consistency.",
    "Rubric consistency checks positive rubric weights plus an answer anchor in accepted answers or key points.",
    "Prompt leakage is deterministic substring matching after whitespace/case normalization.",
    "Duplicate rate is duplicate non-empty normalized question instances divided by all non-empty questions.",
    "Threshold failures are reported only and intentionally do not make the command fail.",
)


class EvaluationInputError(ValueError):
    """An invalid local input/configuration, distinct from a quality failure."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalise_text(value: Any) -> str:
    return re.sub(r"\s+", "", _text(value)).casefold()


def _normalise_question(value: Any) -> str:
    text = _normalise_text(value)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text)


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _text(item))]


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 else None


def _load_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationInputError(f"{label} file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationInputError(f"invalid {label} JSON: {path}: {exc.msg}") from exc


def _validate_case(case: Mapping[str, Any], index: int) -> dict[str, Any]:
    normalized = dict(case)
    case_id = _text(normalized.get("id"))
    if not case_id:
        raise EvaluationInputError(f"case at index {index} is missing id")
    topic_id = _text(normalized.get("topic_id"))
    if not topic_id:
        raise EvaluationInputError(f"case {case_id} is missing topic_id")
    question_type = _text(
        normalized.get("requested_question_type") or normalized.get("question_type")
    )
    if question_type not in ALLOWED_QUESTION_TYPES:
        raise EvaluationInputError(
            f"case {case_id} has unsupported requested_question_type: {question_type!r}"
        )
    difficulty = _positive_int(
        normalized.get("planned_difficulty", normalized.get("difficulty"))
    )
    if difficulty not in {2, 3, 4}:
        raise EvaluationInputError(
            f"case {case_id} planned_difficulty must be an integer from 2 to 4"
        )
    expected = _as_mapping(normalized.get("expected"))
    normalized["expected"] = expected
    normalized["id"] = case_id
    normalized["topic_id"] = topic_id
    normalized["requested_question_type"] = question_type
    normalized["planned_difficulty"] = difficulty
    normalized["fixture_id"] = _text(normalized.get("fixture_id")) or case_id
    return normalized


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate the stable, offline case contract."""

    payload = _load_json(Path(path), label="cases")
    if not isinstance(payload, Mapping):
        raise EvaluationInputError("cases JSON must be an object with a cases array")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvaluationInputError("cases JSON must contain a non-empty cases array")
    cases = [
        _validate_case(case, index)
        for index, case in enumerate(raw_cases)
        if isinstance(case, Mapping)
    ]
    if len(cases) != len(raw_cases):
        raise EvaluationInputError("every case must be a JSON object")
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise EvaluationInputError("case ids must be unique")
    return cases


def load_fixture_outputs(path: str | Path) -> dict[str, Any]:
    """Load fixed model outputs without contacting a provider."""

    payload = _load_json(Path(path), label="fixtures")
    if not isinstance(payload, Mapping):
        raise EvaluationInputError("fixture JSON must be an object")
    outputs = payload.get("outputs", payload)
    if not isinstance(outputs, Mapping):
        raise EvaluationInputError("fixture outputs must be an object keyed by fixture_id")
    return {str(key): value for key, value in outputs.items()}


def _expected_value(case: Mapping[str, Any], key: str, fallback: Any = "") -> Any:
    expected = _as_mapping(case.get("expected"))
    return expected.get(key, fallback)


def _answer_is_correct(case: Mapping[str, Any], output: Mapping[str, Any]) -> bool:
    answer = _normalise_text(output.get("answer"))
    reference = _normalise_text(output.get("reference_answer"))
    expected_answer = _normalise_text(
        _expected_value(case, "reference_answer", _expected_value(case, "answer", ""))
    )
    if expected_answer:
        return bool(answer or reference) and expected_answer in {answer, reference}
    keywords = [_normalise_text(item) for item in _expected_value(case, "answer_keywords", [])]
    keywords = [item for item in keywords if item]
    if keywords:
        return bool(reference) and all(item in reference for item in keywords)
    return bool(answer) and answer == reference


def _rubric_is_consistent(output: Mapping[str, Any]) -> bool:
    answer = _normalise_text(output.get("reference_answer") or output.get("answer"))
    rubric = _as_mapping(output.get("rubric"))
    if not answer or not rubric:
        return False
    weights: list[float] = []
    for key, value in rubric.items():
        if not _text(key) or isinstance(value, bool):
            return False
        try:
            weight = float(value)
        except (TypeError, ValueError):
            return False
        if weight <= 0:
            return False
        weights.append(weight)
    accepted = {_normalise_text(item) for item in _as_strings(output.get("accepted_answers"))}
    points = {_normalise_text(item) for item in _as_strings(output.get("key_points"))}
    return bool(weights) and (answer in accepted or answer in points)


def _prompt_leakage(case: Mapping[str, Any], output: Mapping[str, Any]) -> bool:
    expected = _as_mapping(case.get("expected"))
    forbidden = expected.get("forbid_answer_leak", expected.get("forbid_prompt_leakage", True))
    if forbidden is False:
        return False
    question = _normalise_text(output.get("question"))
    hint = _normalise_text(output.get("hint"))
    answers = [output.get("answer"), output.get("reference_answer")]
    answers.extend(_as_strings(output.get("accepted_answers")))
    for answer in {_normalise_text(value) for value in answers}:
        if len(answer) >= 2 and (answer in question or answer in hint):
            return True
    return False


def _one_result(case: Mapping[str, Any], output: Any, *, run_index: int) -> dict[str, Any]:
    payload = _as_mapping(output)
    expected_target = _text(_expected_value(case, "target_topic_id", case["topic_id"]))
    expected_type = _text(_expected_value(case, "question_type", case["requested_question_type"]))
    question = _text(payload.get("question"))
    actual_type = _text(payload.get("question_type") or payload.get("type"))
    actual_target = _text(payload.get("target_topic_id") or payload.get("topic_id"))
    difficulty = _positive_int(payload.get("difficulty"))
    checks = {
        "structural_contract": bool(question)
        and bool(_text(payload.get("answer")))
        and bool(_text(payload.get("reference_answer")))
        and bool(_as_strings(payload.get("accepted_answers")))
        and isinstance(payload.get("rubric"), Mapping)
        and actual_type in ALLOWED_QUESTION_TYPES
        and difficulty is not None
        and 1 <= difficulty <= 5,
        "target_topic_relevance": bool(expected_target) and actual_target == expected_target,
        "reference_answer_correct": _answer_is_correct(case, payload),
        "rubric_answer_consistency": _rubric_is_consistent(payload),
        "difficulty_within_one": difficulty is not None
        and abs(difficulty - int(case["planned_difficulty"])) <= 1,
        "prompt_leakage_free": not _prompt_leakage(case, payload),
    }
    if expected_type:
        checks["structural_contract"] = checks["structural_contract"] and actual_type == expected_type
    failures = [name for name, passed in checks.items() if not passed]
    return {
        "case_id": case["id"],
        "fixture_id": case["fixture_id"],
        "run_index": run_index,
        "scenario": _text(case.get("scenario")),
        "subject": _text(case.get("subject")),
        "question_type": actual_type,
        "target_topic_id": actual_target,
        "difficulty": difficulty,
        "normalized_question": _normalise_question(question),
        "passed_checks": checks,
        "failures": failures,
    }


def _outputs_for_case(fixture_outputs: Mapping[str, Any], case: Mapping[str, Any]) -> list[Any]:
    raw = fixture_outputs.get(str(case["fixture_id"]))
    if raw is None:
        return [{}]
    return list(raw) if isinstance(raw, list) else [raw]


def _rate_metric(results: Sequence[Mapping[str, Any]], check: str, threshold: float) -> dict[str, Any]:
    total = len(results)
    passed = sum(bool(_as_mapping(result.get("passed_checks")).get(check)) for result in results)
    rate = passed / total if total else 0.0
    return {"passed": passed, "total": total, "rate": rate, "threshold": threshold, "meets_threshold": rate >= threshold}


def _build_report(results: list[dict[str, Any]], *, case_count: int, mode: str) -> dict[str, Any]:
    metric_details: dict[str, Any] = {
        "structural_contract_pass_rate": _rate_metric(results, "structural_contract", 0.99),
        "target_topic_relevance_rate": _rate_metric(results, "target_topic_relevance", 0.97),
        "reference_answer_correct_rate": _rate_metric(results, "reference_answer_correct", 0.98),
        "rubric_answer_consistency_rate": _rate_metric(results, "rubric_answer_consistency", 0.98),
        "difficulty_within_one_rate": _rate_metric(results, "difficulty_within_one", 0.90),
    }
    leakage_count = sum(not _as_mapping(row.get("passed_checks")).get("prompt_leakage_free", False) for row in results)
    questions = [str(row.get("normalized_question") or "") for row in results]
    non_empty_questions = [question for question in questions if question]
    duplicate_count = sum(
        count for count in Counter(non_empty_questions).values() if count > 1
    )
    duplicate_rate = duplicate_count / len(non_empty_questions) if non_empty_questions else 0.0
    metric_details["prompt_leakage_count"] = {
        "count": leakage_count,
        "threshold": 0,
        "meets_threshold": leakage_count == 0,
    }
    metric_details["normalized_duplicate_question_rate"] = {
        "duplicate_count": duplicate_count,
        "total": len(non_empty_questions),
        "rate": duplicate_rate,
        "threshold": 0.10,
        "meets_threshold": duplicate_rate < 0.10,
    }
    seen_questions: set[str] = set()
    for result in results:
        normalized_question = str(result.get("normalized_question") or "")
        if normalized_question and normalized_question in seen_questions:
            result["failures"].append("normalized_duplicate_question")
            result["passed_checks"]["normalized_duplicate_question"] = False
        else:
            result["passed_checks"]["normalized_duplicate_question"] = True
        if normalized_question:
            seen_questions.add(normalized_question)
    quality_gate_passed = all(
        metric["meets_threshold"] for metric in metric_details.values()
    )
    return {
        "report_version": REPORT_VERSION,
        "mode": mode,
        "case_count": case_count,
        "run_count": len(results),
        "thresholds": THRESHOLDS,
        "metrics": metric_details,
        "quality_gate_passed": quality_gate_passed,
        "results": results,
        "assumptions": list(ASSUMPTIONS),
    }


def evaluate_cases(
    cases: list[dict[str, Any]], fixture_outputs: Mapping[str, Any], *, mode: str = "fixtures"
) -> dict[str, Any]:
    """Evaluate fixture or live payloads and return a non-gating JSON report."""

    outputs = _as_mapping(fixture_outputs).get("outputs", fixture_outputs)
    if not isinstance(outputs, Mapping):
        raise EvaluationInputError("fixture outputs must be a mapping")
    results: list[dict[str, Any]] = []
    for case in cases:
        for run_index, output in enumerate(_outputs_for_case(outputs, case), start=1):
            results.append(_one_result(case, output, run_index=run_index))
    return _build_report(results, case_count=len(cases), mode=mode)


def _load_live_dependencies() -> tuple[type[Any], type[Any]]:
    """Load package modules both as a plugin and as a direct script."""

    try:
        from .models import StudyConfig
        from .tutor_llm_agent import TutorLLMAgent
    except ImportError:  # pragma: no cover - only used by manual CLI execution.
        package_name = "_study_companion_question_eval"
        package = ModuleType(package_name)
        package.__path__ = [str(Path(__file__).resolve().parent)]  # type: ignore[attr-defined]
        sys.modules.setdefault(package_name, package)
        StudyConfig = importlib.import_module(f"{package_name}.models").StudyConfig
        TutorLLMAgent = importlib.import_module(f"{package_name}.tutor_llm_agent").TutorLLMAgent
    return StudyConfig, TutorLLMAgent


async def _generate_live_outputs(
    cases: Sequence[Mapping[str, Any]], *, runs_per_case: int
) -> dict[str, list[dict[str, Any]]]:
    StudyConfig, TutorLLMAgent = _load_live_dependencies()
    logger = logging.getLogger("question_generation_eval")
    agent = TutorLLMAgent(logger=logger, config=StudyConfig())
    outputs: dict[str, list[dict[str, Any]]] = {}
    try:
        runtime = await agent.resolve_model_runtime("agent")
        if not (
            _text(getattr(runtime, "model", ""))
            and _text(getattr(runtime, "base_url", ""))
            and _text(getattr(runtime, "api_key", ""))
            and _text(getattr(runtime, "transport", "")) != "unsupported"
        ):
            raise EvaluationInputError(
                "configured agent Qwen runtime is incomplete or unsupported"
            )
        for case in cases:
            language = _text(case.get("prompt_language")) or "zh-CN"
            source_text = _text(case.get("source_text") or case.get("prompt"))
            if not source_text:
                source_text = f"Target topic: {case['topic_id']}\nLearner context: {_text(case.get('learner_context'))}"
            context = {
                "targeted_question": True,
                "selected_topic_id": case["topic_id"],
                "selected_topic_name": _text(case.get("topic_name")) or case["topic_id"],
                "knowledge_question_params": {
                    "required_question_type": case["requested_question_type"],
                    "target_topic": {"id": case["topic_id"]},
                },
                "source_text": source_text,
            }
            agent._config.language = language  # Current plugin config supplies the gateway credentials.
            rows: list[dict[str, Any]] = []
            for _ in range(runs_per_case):
                reply = await agent.question_generate(source_text, context=context)
                payload = dict(reply.payload or {})
                if reply.degraded:
                    payload["_evaluation_model_degraded"] = True
                    payload.setdefault("_evaluation_diagnostic", reply.diagnostic)
                rows.append(payload)
            outputs[str(case["fixture_id"])] = rows
    finally:
        await agent.shutdown()
    return outputs


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate generated study-question quality.")
    parser.add_argument("cases", nargs="?", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--cases", dest="cases_option", type=Path)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--live", action="store_true", help="Manually call the currently configured Qwen gateway.")
    parser.add_argument("--runs-per-case", type=int, default=3, help="Live generations per case (default: 3).")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cases = load_cases(args.cases_option or args.cases)
        if args.live:
            if args.runs_per_case < 1:
                raise EvaluationInputError("--runs-per-case must be at least 1")
            fixtures = asyncio.run(_generate_live_outputs(cases, runs_per_case=args.runs_per_case))
            mode = "live"
        else:
            fixtures = load_fixture_outputs(args.fixtures)
            mode = "fixtures"
        report = evaluate_cases(cases, fixtures, mode=mode)
        _write_report(args.report, report)
    except EvaluationInputError as exc:
        print(f"question generation evaluation input error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # Live transport/config failures are real command failures.
        print(f"question generation evaluation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"report": str(args.report), "quality_gate_passed": report["quality_gate_passed"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a command.
    raise SystemExit(main())
