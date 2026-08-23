from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


def validated_target_topic_id(
    question_payload: dict[str, Any],
    current_question: dict[str, Any],
    *,
    question_source: str,
) -> str:
    binding = dict(question_payload.get("target_binding") or {})
    bound_topic_id = str(binding.get("target_topic_id") or "").strip()
    current_selected_topic_id = str(
        current_question.get("selected_topic_id")
        or current_question.get("topic_id")
        or current_question.get("topic")
        or ""
    ).strip()
    if not (
        str(question_source or "") == "current_question"
        and str(question_payload.get("source") or "") == "targeted_question"
        and str(binding.get("validation_status") or "") == "passed"
        and str(binding.get("generated_at") or "").strip()
        and bound_topic_id
        and bound_topic_id == current_selected_topic_id
    ):
        return ""
    return bound_topic_id


async def resolve_existing_target_topic_id(
    question_payload: dict[str, Any],
    current_question: dict[str, Any],
    *,
    question_source: str,
    get_topic: Callable[[str], Any],
    logger: Any,
) -> str:
    topic_id = validated_target_topic_id(
        question_payload,
        current_question,
        question_source=question_source,
    )
    if not topic_id:
        return ""
    try:
        topic = await asyncio.to_thread(get_topic, topic_id)
    except Exception as exc:
        logger.warning("study target binding lookup failed; recording QA only: {}", exc)
        return ""
    return topic_id if topic else ""
