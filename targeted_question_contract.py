from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SUPPORTED_TARGETED_QUESTION_TYPES = frozenset(
    {"short_answer", "math_exact", "math_reasoning"}
)


@dataclass(frozen=True)
class TargetedQuestionValidation:
    valid: bool
    errors: tuple[str, ...]


def _text(value: object) -> str:
    return str(value or "").strip()


def _normalized(value: object) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", _text(value).casefold())


def validate_targeted_question(
    payload: dict[str, Any],
    *,
    target_topic_id: str,
    target_topic_name: str,
    origin_wrong_question: dict[str, Any] | None = None,
) -> TargetedQuestionValidation:
    errors: list[str] = []
    question = _text(payload.get("question"))
    raw_answer = _text(payload.get("answer"))
    raw_reference = _text(payload.get("reference_answer"))
    # ``answer`` is the canonical hidden expected answer.  Older callers may
    # send only ``reference_answer``, so accept either field while generation
    # normalizes both fields to the canonical value before persistence.
    answer = raw_answer or raw_reference
    reference = raw_reference or raw_answer
    if not question:
        errors.append("missing_question")
    if not answer or not reference:
        errors.append("missing_reference_answer")
    if (
        payload.get("_answer_reference_answer_consistent") is False
        or (raw_answer and raw_reference and raw_answer != raw_reference)
    ):
        errors.append("answer_reference_answer_mismatch")
    if _text(payload.get("question_type")) not in SUPPORTED_TARGETED_QUESTION_TYPES:
        errors.append("unsupported_question_type")
    difficulty = payload.get("difficulty")
    if (
        payload.get("_targeted_difficulty_valid") is False
        or isinstance(difficulty, bool)
        or not isinstance(difficulty, int)
        or not 1 <= difficulty <= 5
    ):
        errors.append("invalid_difficulty")
    generated_target = _text(payload.get("target_topic_id"))
    if not generated_target or generated_target != _text(target_topic_id):
        errors.append("target_topic_mismatch")
    hint = _normalized(payload.get("hint"))
    for protected in (answer, reference, *(payload.get("accepted_answers") or [])):
        normalized_protected = _normalized(protected)
        raw_protected = _text(protected)
        raw_hint = _text(payload.get("hint"))
        short_answer_leaked = bool(
            len(normalized_protected) == 1
            and raw_protected
            and re.search(
                rf"(?<!\w){re.escape(raw_protected)}(?!\w)",
                raw_hint,
                flags=re.IGNORECASE,
            )
        )
        if (
            normalized_protected
            and hint
            and (
                (len(normalized_protected) >= 2 and normalized_protected in hint)
                or short_answer_leaked
            )
        ):
            errors.append("hint_leaks_answer")
            break
    wrong_payload = dict(origin_wrong_question or {})
    original_question = _text(
        (wrong_payload.get("question") or {}).get("question")
        if isinstance(wrong_payload.get("question"), dict)
        else wrong_payload.get("question")
    )
    if original_question and _normalized(original_question) == _normalized(question):
        errors.append("retry_copies_original_question")
    return TargetedQuestionValidation(valid=not errors, errors=tuple(errors))


def semantic_validation_passed(payload: dict[str, Any], *, degraded: bool) -> bool:
    return (
        not degraded
        and payload.get("relevant") is True
        and payload.get("answer_supported") is True
        and payload.get("retry") is False
    )
