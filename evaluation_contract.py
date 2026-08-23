from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ANSWER_VERDICTS = frozenset({"correct", "partial", "wrong", "dont_know"})


@dataclass(frozen=True)
class EvaluationValidation:
    valid: bool
    errors: tuple[str, ...]


def validate_evaluation(
    payload: dict[str, Any], *, learner_answer: str
) -> EvaluationValidation:
    errors: list[str] = []
    if payload.get("_evaluation_verdict_valid") is False:
        errors.append("invalid_verdict")
    if payload.get("_evaluation_score_valid") is False:
        errors.append("invalid_score")
    if payload.get("_evaluation_final_answer_correct_valid") is False:
        errors.append("invalid_final_answer_correct")
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in ANSWER_VERDICTS and "invalid_verdict" not in errors:
        errors.append("invalid_verdict")

    score = payload.get("score")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        if "invalid_score" not in errors:
            errors.append("invalid_score")
    elif verdict == "correct" and score < 80:
        errors.append("correct_score_mismatch")
    elif verdict == "partial" and not 40 <= score < 80:
        errors.append("partial_score_mismatch")
    elif verdict in {"wrong", "dont_know"} and score >= 40:
        errors.append("incorrect_score_mismatch")

    if not str(learner_answer or "").strip() and verdict != "dont_know":
        errors.append("empty_answer_verdict_mismatch")

    final_answer_correct = payload.get("final_answer_correct")
    if not isinstance(final_answer_correct, bool):
        if "invalid_final_answer_correct" not in errors:
            errors.append("invalid_final_answer_correct")
    elif verdict in ANSWER_VERDICTS and final_answer_correct != (verdict == "correct"):
        errors.append("final_answer_correct_mismatch")

    return EvaluationValidation(valid=not errors, errors=tuple(errors))


def canonicalize_evaluation(payload: dict[str, Any]) -> dict[str, Any]:
    canonical = dict(payload or {})
    verdict = str(canonical.get("verdict") or "").strip().lower()
    canonical["verdict"] = verdict
    canonical["final_answer_correct"] = verdict == "correct"
    canonical.pop("_evaluation_score_valid", None)
    canonical.pop("_evaluation_verdict_valid", None)
    canonical.pop("_evaluation_final_answer_correct_valid", None)
    return canonical
