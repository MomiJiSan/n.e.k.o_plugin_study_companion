from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SUPPORTED_TARGETED_QUESTION_TYPES = frozenset(
    {"short_answer", "math_exact", "math_reasoning"}
)
_MATERIAL_LIST_MAX_ITEMS = 12
_MATERIAL_TEXT_MAX_CHARS = 500
_RUBRIC_KEY_MAX_CHARS = 200

TARGET_TOPIC_IDENTITY_FIELDS = (
    "id",
    "name",
    "title",
    "subject",
    "stage",
    "course_family",
    "chapter",
    "unit",
)
TARGET_TOPIC_KNOWLEDGE_FIELDS = (
    "skills",
    "typical_misconceptions",
    "question_types",
    "examples",
)
TARGET_TOPIC_EVIDENCE_FIELDS = (
    *TARGET_TOPIC_IDENTITY_FIELDS,
    *TARGET_TOPIC_KNOWLEDGE_FIELDS,
)


@dataclass(frozen=True)
class TargetedQuestionValidation:
    valid: bool
    errors: tuple[str, ...]


def project_target_topic_evidence(topic: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the single server-owned topic evidence contract used by LLMs."""

    source = topic if isinstance(topic, Mapping) else {}
    return {
        key: source[key]
        for key in TARGET_TOPIC_EVIDENCE_FIELDS
        if key in source and source[key] not in (None, "", [], {})
    }


def _text(value: object) -> str:
    return str(value or "").strip()


def _normalized(value: object) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", _text(value).casefold())


def _valid_material_list(value: object) -> bool:
    if not isinstance(value, list) or not value or len(value) > _MATERIAL_LIST_MAX_ITEMS:
        return False
    items = [_text(item) for item in value]
    return (
        all(isinstance(item, str) and item and len(item) <= _MATERIAL_TEXT_MAX_CHARS for item in value)
        and len({_normalized(item) for item in items}) == len(items)
    )


def _valid_rubric(value: object) -> bool:
    if not isinstance(value, Mapping) or not value or len(value) > _MATERIAL_LIST_MAX_ITEMS:
        return False
    total = 0.0
    for key, weight in value.items():
        name = _text(key)
        if not name or len(name) > _RUBRIC_KEY_MAX_CHARS:
            return False
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            return False
        numeric_weight = float(weight)
        if not math.isfinite(numeric_weight) or numeric_weight <= 0:
            return False
        total += numeric_weight
    return math.isfinite(total) and total > 0


def validate_targeted_question(
    payload: dict[str, Any],
    *,
    target_topic_id: str,
    target_topic_name: str,
    origin_wrong_question: dict[str, Any] | None = None,
    expected_difficulty: int | None = None,
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
    for field_name in ("accepted_answers", "key_points", "solution_steps"):
        if not _valid_material_list(payload.get(field_name)):
            errors.append(f"invalid_{field_name}")
    if not _valid_rubric(payload.get("rubric")):
        errors.append("invalid_rubric")
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
    elif expected_difficulty is not None and difficulty != expected_difficulty:
        # The optional bound lets the server own the targeted difficulty while
        # retaining the legacy contract for every caller that does not plan it.
        errors.append("planned_difficulty_mismatch")
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
