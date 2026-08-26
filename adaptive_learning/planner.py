"""Pure practice-selection policy, intentionally not wired into entry points yet.

The current entry implementation prepares a dict of retry, review, weak-topic,
recommended, and fallback candidates.  This module expresses the same
precedence as a side-effect-free application-layer policy so it can be adopted
behind an adapter in a later PR.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .contracts import PracticeSelection, QuestionPlan, TopicRef


def topic_ref_from_mapping(topic: Mapping[str, Any] | None, *, topic_id: str = "") -> TopicRef | None:
    """Create a compact topic reference from current catalog-shaped payloads."""

    payload = dict(topic or {})
    resolved_id = str(payload.get("id") or topic_id or "").strip()
    if not resolved_id:
        return None
    name = str(payload.get("name") or payload.get("title") or resolved_id).strip()
    depth = payload.get("depth")
    return TopicRef(
        id=resolved_id,
        name=name or resolved_id,
        subject=str(payload.get("subject") or "").strip(),
        stage=str(payload.get("stage") or "").strip(),
        course_family=str(payload.get("course_family") or "").strip(),
        chapter=str(payload.get("chapter") or "").strip(),
        unit=str(payload.get("unit") or "").strip(),
        depth=int(depth) if isinstance(depth, int) and not isinstance(depth, bool) else 0,
        metadata=payload,
    )


def _topic_id(value: Mapping[str, Any] | None) -> str:
    payload = dict(value or {})
    nested = payload.get("topic")
    nested_payload = dict(nested) if isinstance(nested, Mapping) else {}
    return str(payload.get("topic_id") or nested_payload.get("id") or payload.get("id") or "").strip()


def _topic_for_candidate(
    candidate: Mapping[str, Any] | None,
    *,
    topics_by_id: Mapping[str, Mapping[str, Any]],
) -> TopicRef | None:
    candidate_payload = dict(candidate or {})
    topic_id = _topic_id(candidate_payload)
    catalog_topic = topics_by_id.get(topic_id)
    if catalog_topic is not None:
        return topic_ref_from_mapping(catalog_topic, topic_id=topic_id)
    nested = candidate_payload.get("topic")
    if not isinstance(nested, Mapping):
        return None
    return topic_ref_from_mapping(nested, topic_id=topic_id)


def _recommended_topic(
    candidate: Mapping[str, Any] | None,
    *,
    topics_by_id: Mapping[str, Mapping[str, Any]],
) -> TopicRef | None:
    payload = dict(candidate or {})
    evidence = payload.get("payload")
    summary = payload.get("payload_summary")
    for source in (evidence, summary, payload):
        if not isinstance(source, Mapping):
            continue
        topic_id = str(source.get("topic_id") or source.get("id") or "").strip()
        catalog_topic = topics_by_id.get(topic_id)
        if catalog_topic is not None:
            return topic_ref_from_mapping(catalog_topic, topic_id=topic_id)
        if any(key in source for key in ("name", "title", "subject", "stage")):
            topic = topic_ref_from_mapping(source, topic_id=topic_id)
            if topic is not None:
                return topic
    return None


def _eligible_ids(
    eligible_topic_ids: Iterable[str] | None,
    topics_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    if eligible_topic_ids is None:
        return tuple(topics_by_id)
    return tuple(dict.fromkeys(str(topic_id).strip() for topic_id in eligible_topic_ids if str(topic_id).strip()))


def _in_scope(topic: TopicRef, eligible_ids: tuple[str, ...]) -> bool:
    return not eligible_ids or topic.id in set(eligible_ids)


def _first_scoped_candidate(
    candidates: Iterable[Mapping[str, Any]],
    *,
    eligible_ids: tuple[str, ...],
    topics_by_id: Mapping[str, Mapping[str, Any]],
    recommended: bool = False,
) -> tuple[TopicRef, Mapping[str, Any]] | None:
    for candidate in candidates:
        topic = (
            _recommended_topic(candidate, topics_by_id=topics_by_id)
            if recommended
            else _topic_for_candidate(candidate, topics_by_id=topics_by_id)
        )
        if topic is not None and _in_scope(topic, eligible_ids):
            return topic, candidate
    return None


def select_practice_selection(
    question_params: Mapping[str, Any],
    *,
    eligible_topic_ids: Iterable[str] | None = None,
    topics_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> PracticeSelection | None:
    """Choose a topic with current precedence: retry > due > weak > recommended.

    The input names intentionally match the dict emitted by the current
    ``KnowledgeTracker.preview_next_question_params`` flow.  An empty result
    means the caller has no usable candidate, matching the existing
    ``no_data`` response rather than inventing a target outside the scope.
    """

    params = dict(question_params or {})
    catalog = {str(topic_id): dict(topic) for topic_id, topic in (topics_by_id or {}).items()}
    eligible_ids = _eligible_ids(eligible_topic_ids, catalog)

    retry = params.get("retry_wrong_question")
    if isinstance(retry, Mapping):
        found = _first_scoped_candidate((retry,), eligible_ids=eligible_ids, topics_by_id=catalog)
        if found is not None:
            topic, candidate = found
            return PracticeSelection(
                reason="wrong_retry",
                target_topic=topic,
                eligible_topic_ids=eligible_ids,
                origin_wrong_question_id=str(candidate.get("id") or "").strip() or None,
                explanation="retry active wrong question",
            )

    due_reviews = params.get("due_reviews")
    if isinstance(due_reviews, list):
        found = _first_scoped_candidate(due_reviews, eligible_ids=eligible_ids, topics_by_id=catalog)
        if found is not None:
            topic, _ = found
            return PracticeSelection(
                reason="due_review",
                target_topic=topic,
                eligible_topic_ids=eligible_ids,
                explanation="review due FSRS item",
            )

    weak_topics = params.get("weak_topics")
    if isinstance(weak_topics, list):
        found = _first_scoped_candidate(weak_topics, eligible_ids=eligible_ids, topics_by_id=catalog)
        if found is not None:
            topic, _ = found
            return PracticeSelection(
                reason="weak_topic",
                target_topic=topic,
                eligible_topic_ids=eligible_ids,
                explanation="practise weakest eligible topic",
            )

    evidence = params.get("candidate_evidence")
    if isinstance(evidence, list):
        found = _first_scoped_candidate(
            evidence,
            eligible_ids=eligible_ids,
            topics_by_id=catalog,
            recommended=True,
        )
        if found is not None:
            topic, _ = found
            return PracticeSelection(
                reason="recommended",
                target_topic=topic,
                eligible_topic_ids=eligible_ids,
                explanation="use knowledge candidate recommendation",
            )

    target_topic_id = str(params.get("target_topic_id") or "").strip()
    target_topic = topic_ref_from_mapping(params.get("target_topic"), topic_id=target_topic_id)
    if target_topic is None:
        target_topic = topic_ref_from_mapping(catalog.get(target_topic_id), topic_id=target_topic_id)
    if target_topic is not None and _in_scope(target_topic, eligible_ids):
        return PracticeSelection(
            reason="recommended",
            target_topic=target_topic,
            eligible_topic_ids=eligible_ids,
            explanation="use current target topic",
        )

    for topic_id in eligible_ids:
        raw_fallback = catalog.get(topic_id)
        fallback = topic_ref_from_mapping(raw_fallback, topic_id=topic_id) if raw_fallback is not None else None
        if fallback is not None:
            return PracticeSelection(
                reason="default",
                target_topic=fallback,
                eligible_topic_ids=eligible_ids,
                explanation="use first eligible fallback topic",
            )
    return None


def build_question_plan(
    question_params: Mapping[str, Any],
    *,
    plan_id: str,
    eligible_topic_ids: Iterable[str] | None = None,
    topics_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    scope_key: str = "",
    scope_revision: int = 0,
    question_type: str | None = None,
) -> QuestionPlan | None:
    """Convert current candidate dictionaries into a planner-owned question plan."""

    params = dict(question_params or {})
    selection = select_practice_selection(
        params,
        eligible_topic_ids=eligible_topic_ids,
        topics_by_id=topics_by_id,
    )
    if selection is None:
        return None
    raw_difficulty = params.get("suggested_difficulty") or 3
    difficulty = raw_difficulty if isinstance(raw_difficulty, int) and not isinstance(raw_difficulty, bool) else 3
    return QuestionPlan(
        plan_id=plan_id,
        selection=selection,
        difficulty=difficulty,
        question_type=str(question_type or params.get("question_type") or "math_reasoning"),
        learning_objective=str(params.get("learning_objective") or ""),
        misconception_target=str(params.get("misconception_target") or ""),
        scope_key=scope_key,
        scope_revision=scope_revision,
    )
