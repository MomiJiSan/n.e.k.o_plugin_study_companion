"""Pure Coach-owned topic selection and learning-action merge policy."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .contracts import (
    HypothesisRef,
    LearningActionCandidate,
    PracticeSelection,
    QuestionPlan,
    RepairStrategy,
    TopicRef,
)


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
    if isinstance(nested, Mapping):
        return topic_ref_from_mapping(nested, topic_id=topic_id)
    if not topic_id:
        return None
    return topic_ref_from_mapping(
        {
            "id": topic_id,
            "name": candidate_payload.get("topic_name")
            or candidate_payload.get("name")
            or topic_id,
        }
    )


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
        if topic_id:
            return topic_ref_from_mapping({"id": topic_id, "name": topic_id})
    return None


def candidate_topic_ids(question_params: Mapping[str, Any]) -> tuple[str, ...]:
    """Return all topic ids the Planner may consider, without choosing one."""

    params = dict(question_params or {})
    topic_ids: list[str] = []

    def append(value: object) -> None:
        topic_id = str(value or "").strip()
        if topic_id and topic_id not in topic_ids:
            topic_ids.append(topic_id)

    append(params.get("target_topic_id"))
    target = params.get("target_topic")
    if isinstance(target, Mapping):
        append(target.get("id"))
    retry = params.get("retry_wrong_question")
    if isinstance(retry, Mapping):
        append(_topic_id(retry))
    for key in ("due_reviews", "weak_topics"):
        candidates = params.get(key)
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, Mapping):
                    append(_topic_id(candidate))
    diagnostic = params.get("blocked_diagnostic")
    if isinstance(diagnostic, Mapping):
        append(diagnostic.get("target_topic_id"))
    evidence = params.get("candidate_evidence")
    if isinstance(evidence, list):
        for candidate in evidence:
            if not isinstance(candidate, Mapping):
                continue
            payload = candidate.get("payload")
            summary = candidate.get("payload_summary")
            for source in (payload, summary, candidate):
                if isinstance(source, Mapping):
                    topic_id = str(source.get("topic_id") or source.get("id") or "").strip()
                    if topic_id:
                        append(topic_id)
                        break
    return tuple(topic_ids)


def select_scope_fallback_topic(
    topics: Iterable[Mapping[str, Any]],
    mastery_by_topic: Mapping[str, Mapping[str, Any]],
    *,
    ready_topic_ids: Iterable[str] | None = None,
    explicit_topic_id: str = "",
) -> TopicRef | None:
    """Choose the scoped fallback without leaking topic ownership to entries."""

    catalog = [dict(topic) for topic in topics if str(topic.get("id") or "").strip()]
    explicit = str(explicit_topic_id or "").strip()
    if explicit:
        return next(
            (
                topic_ref_from_mapping(topic, topic_id=explicit)
                for topic in catalog
                if str(topic.get("id") or "").strip() == explicit
            ),
            None,
        )
    ready = (
        {str(topic_id or "").strip() for topic_id in ready_topic_ids if str(topic_id or "").strip()}
        if ready_topic_ids is not None
        else None
    )
    selectable = [
        topic
        for topic in catalog
        if ready is None or not ready or str(topic.get("id") or "").strip() in ready
    ]
    if not selectable:
        return None
    attempted = {str(topic_id or "").strip() for topic_id in mastery_by_topic}
    ordered = sorted(
        selectable,
        key=lambda topic: (
            str(topic.get("id") or "").strip() in attempted,
            _finite_number(topic.get("depth"), 1.0),
            _finite_number(topic.get("difficulty"), 0.5),
            str(topic.get("id") or "").strip(),
        ),
    )
    unattempted = [
        topic
        for topic in ordered
        if str(topic.get("id") or "").strip() not in attempted
    ]
    selected = (
        unattempted[0]
        if unattempted
        else min(
            ordered,
            key=lambda topic: (
                _finite_number(
                    mastery_by_topic.get(
                        str(topic.get("id") or "").strip(),
                        {},
                    ).get("mastery"),
                    0.0,
                ),
                str(topic.get("id") or "").strip(),
            ),
        )
    )
    return topic_ref_from_mapping(selected)


def apply_readiness_policy(
    question_params: Mapping[str, Any],
    *,
    ready_topic_ids: Iterable[str],
    blockers_by_topic: Mapping[str, Iterable[Mapping[str, Any]]],
    fallback_topic_id: str,
    eligible_topic_ids: Iterable[str],
) -> dict[str, Any]:
    """Filter automatic recommendations and emit a Planner-owned probe."""

    params = dict(question_params or {})
    ready = {
        str(topic_id or "").strip()
        for topic_id in ready_topic_ids
        if str(topic_id or "").strip()
    }
    evidence = params.get("candidate_evidence")
    params["candidate_evidence"] = [
        dict(candidate)
        for candidate in evidence
        if isinstance(candidate, Mapping)
        and (resolved := _recommended_topic(candidate, topics_by_id={})) is not None
        and resolved.id in ready
    ] if isinstance(evidence, list) else []
    if (
        not ready
        and not params.get("retry_wrong_question")
        and not params.get("due_reviews")
        and not params.get("weak_topics")
    ):
        topic_id = str(fallback_topic_id or "").strip()
        params["blocked_diagnostic"] = {
            "target_topic_id": topic_id,
            "blockers": [
                dict(item)
                for item in blockers_by_topic.get(topic_id, ())
                if isinstance(item, Mapping)
            ],
            "scope_topic_ids": sorted(
                {
                    str(item or "").strip()
                    for item in eligible_topic_ids
                    if str(item or "").strip()
                }
            ),
        }
    return params


def _finite_number(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


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

    diagnostic = params.get("blocked_diagnostic")
    if isinstance(diagnostic, Mapping):
        diagnostic_topic_id = str(diagnostic.get("target_topic_id") or "").strip()
        diagnostic_topic = topic_ref_from_mapping(
            catalog.get(diagnostic_topic_id),
            topic_id=diagnostic_topic_id,
        )
        if diagnostic_topic is not None and _in_scope(diagnostic_topic, eligible_ids):
            return PracticeSelection(
                reason="blocked_diagnostic",
                target_topic=diagnostic_topic,
                eligible_topic_ids=eligible_ids,
                explanation="diagnose readiness before advancing",
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


def selection_reason_payload(
    selection: PracticeSelection,
    question_params: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the Planner decision back to the legacy public reason payload."""

    params = dict(question_params or {})
    topic_id = selection.target_topic.id
    if selection.reason == "wrong_retry":
        retry = params.get("retry_wrong_question")
        return {"wrong_question": dict(retry)} if isinstance(retry, Mapping) else {}
    if selection.reason == "due_review":
        candidates = params.get("due_reviews")
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, Mapping) and _topic_id(candidate) == topic_id:
                    return {"due_review": dict(candidate)}
    if selection.reason == "weak_topic":
        candidates = params.get("weak_topics")
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, Mapping) and _topic_id(candidate) == topic_id:
                    return {"weak_topic": dict(candidate)}
    if selection.reason == "blocked_diagnostic":
        diagnostic = params.get("blocked_diagnostic")
        return {"blocked_diagnostic": dict(diagnostic)} if isinstance(diagnostic, Mapping) else {}
    if selection.reason == "recommended":
        candidates = params.get("candidate_evidence")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    continue
                resolved = _recommended_topic(candidate, topics_by_id={})
                if resolved is not None and resolved.id == topic_id:
                    return {"candidate": dict(candidate)}
        target = params.get("target_topic")
        return {"target_topic": dict(target)} if isinstance(target, Mapping) else {}
    return {}


def merge_learning_action_candidates(
    original_plan: QuestionPlan,
    candidates: Iterable[LearningActionCandidate],
    *,
    topics_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    hypothesis_target: HypothesisRef | None = None,
    repair_strategy: RepairStrategy = "",
    cognitive_strategy: str = "",
) -> QuestionPlan:
    """Let Coach accept at most one available action candidate.

    Same-topic candidates may merge once ``not_before`` is reached. A
    different-topic retention candidate is eligible only after ``due_by`` and
    never displaces a wrong-question retry or overdue FSRS review. Invalid
    timestamps, expired candidates, and ambiguous hypothesis bindings fail
    closed to the original plan.
    """

    if original_plan.learning_intent == "readiness_probe" or original_plan.selection.reason == "blocked_diagnostic":
        readiness_plan = replace(
            original_plan,
            learning_intent="readiness_probe",
            hypothesis_target=None,
            repair_strategy="",
            obligation_refs=(),
            cognitive_strategy="",
        )
        return original_plan if readiness_plan == original_plan else readiness_plan
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)
    catalog = {str(key): dict(value) for key, value in (topics_by_id or {}).items()}
    eligible = set(original_plan.selection.eligible_topic_ids)
    accepted: list[tuple[tuple[object, ...], LearningActionCandidate, bool]] = []
    for candidate in candidates:
        if not isinstance(candidate, LearningActionCandidate):
            continue
        if candidate.intent == "practice" or not candidate.topic_id:
            continue
        if eligible and candidate.topic_id not in eligible:
            continue
        available_valid, available = _candidate_time(candidate.not_before)
        expires_valid, expires = _candidate_time(candidate.expires_at)
        due_valid, due = _candidate_time(candidate.due_by)
        if not available_valid or not expires_valid or not due_valid:
            continue
        if available is not None and effective_now < available:
            continue
        if expires is not None and effective_now >= expires:
            continue
        same_topic = candidate.topic_id == original_plan.target_topic.id
        overdue = due is not None and effective_now >= due
        if candidate.intent == "retention_check":
            if (
                available is None
                or due is None
                or expires is None
                or not available < due < expires
                or len(candidate.obligation_refs) != 1
                or not candidate.obligation_refs[0].strip()
            ):
                continue
        if not same_topic and (
            candidate.intent != "retention_check"
            or not overdue
            or original_plan.selection.reason in {"wrong_retry", "due_review"}
        ):
            continue
        priority = (
            0 if overdue else 1,
            -float(candidate.urgency),
            -float(candidate.expected_learning_gain),
            -float(candidate.information_gain),
            candidate.topic_id,
            candidate.intent,
            candidate.obligation_refs,
        )
        accepted.append((priority, candidate, same_topic))
    if not accepted:
        return original_plan
    _, candidate, same_topic = min(accepted, key=lambda item: item[0])
    if hypothesis_target is not None and hypothesis_target.topic_id != candidate.topic_id:
        return original_plan
    if original_plan.hypothesis_target is not None and original_plan.hypothesis_target != hypothesis_target:
        return original_plan
    if candidate.intent in {"misconception_probe", "misconception_repair", "transfer_check"} and hypothesis_target is None:
        return original_plan
    selection = original_plan.selection
    if not same_topic:
        topic = topic_ref_from_mapping(catalog.get(candidate.topic_id), topic_id=candidate.topic_id)
        if topic is None:
            return original_plan
        selection = PracticeSelection(
            reason="recommended",
            target_topic=topic,
            eligible_topic_ids=selection.eligible_topic_ids,
            explanation="select overdue Coach learning-action candidate",
            policy_version=selection.policy_version,
            schema_version=selection.schema_version,
        )
    return replace(
        original_plan,
        selection=selection,
        learning_intent=candidate.intent,
        hypothesis_target=hypothesis_target,
        repair_strategy=repair_strategy if hypothesis_target is not None else "",
        obligation_refs=candidate.obligation_refs,
        cognitive_strategy=(
            str(cognitive_strategy).strip()
            if hypothesis_target is not None
            or candidate.intent == "retention_check"
            else ""
        ),
    )


def _candidate_time(value: str) -> tuple[bool, datetime | None]:
    text = str(value or "").strip()
    if not text:
        return True, None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False, None
    if parsed.tzinfo is None:
        return False, None
    return True, parsed.astimezone(timezone.utc)


def build_question_plan(
    question_params: Mapping[str, Any],
    *,
    plan_id: str,
    eligible_topic_ids: Iterable[str] | None = None,
    topics_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    scope_key: str = "",
    scope_revision: int = 0,
    question_type: str | None = None,
    learning_action_candidates: Iterable[LearningActionCandidate] = (),
    now: datetime | None = None,
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
    plan = QuestionPlan(
        plan_id=plan_id,
        selection=selection,
        difficulty=difficulty,
        question_type=str(question_type or params.get("question_type") or "math_reasoning"),
        learning_objective=str(params.get("learning_objective") or ""),
        misconception_target=str(params.get("misconception_target") or ""),
        scope_key=scope_key,
        scope_revision=scope_revision,
        learning_intent=(
            "readiness_probe"
            if selection.reason == "blocked_diagnostic"
            else "practice"
        ),
    )
    return merge_learning_action_candidates(
        plan,
        learning_action_candidates,
        topics_by_id=topics_by_id,
        now=now,
    )
