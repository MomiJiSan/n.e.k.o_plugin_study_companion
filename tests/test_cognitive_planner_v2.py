from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from test_cognitive_active_question_entry import _active_subject, _targeted_context
from test_targeted_question_contract import _load_entries

# isort: split

from adaptive_learning.cognitive_policy import CognitiveIntentPolicy
from adaptive_learning.cognitive_state import LearnerCognitiveStateView
from adaptive_learning.contracts import (
    HypothesisRef,
    LearningActionCandidate,
    PracticeSelection,
    QuestionPlan,
    TopicRef,
)
from adaptive_learning.planner import (
    build_question_plan,
    merge_learning_action_candidates,
    select_scope_fallback_topic,
)

NOW = datetime(2026, 3, 4, 12, tzinfo=timezone.utc)


def _plan(*, reason: str = "weak_topic", topic_id: str = "base") -> QuestionPlan:
    return QuestionPlan(
        plan_id="plan-1",
        selection=PracticeSelection(
            reason=reason,  # type: ignore[arg-type]
            target_topic=TopicRef(id=topic_id, name=topic_id),
            eligible_topic_ids=("base", "retention"),
        ),
        difficulty=3,
        question_type="math_reasoning",
    )


def _retention_candidate(
    *,
    topic_id: str = "retention",
    not_before: str = "2026-03-02T12:00:00Z",
    due_by: str = "2026-03-03T12:00:00Z",
    obligation_refs: tuple[str, ...] = ("obligation-1",),
) -> LearningActionCandidate:
    return LearningActionCandidate(
        source="cognitive_retention",
        topic_id=topic_id,
        intent="retention_check",
        urgency=0.8,
        expected_learning_gain=0.7,
        information_gain=0.6,
        evidence_refs=("episode-1",),
        satisfies=("retention",),
        not_before=not_before,
        due_by=due_by,
        expires_at="2026-03-08T12:00:00Z",
        obligation_refs=obligation_refs,
    )


def test_blocked_diagnostic_is_a_first_class_readiness_plan() -> None:
    plan = build_question_plan(
        {
            "target_topic_id": "blocked",
            "blocked_diagnostic": {
                "target_topic_id": "blocked",
                "blockers": [{"id": "prerequisite"}],
            },
        },
        plan_id="readiness-plan",
        topics_by_id={"blocked": {"id": "blocked", "name": "Blocked"}},
    )

    assert plan is not None
    assert plan.selection.reason == "blocked_diagnostic"
    assert plan.learning_intent == "readiness_probe"
    assert plan.hypothesis_target is None
    assert plan.obligation_refs == ()
    assert plan.cognitive_strategy == ""


def test_readiness_plan_cannot_be_cognitively_decorated() -> None:
    plan = build_question_plan(
        {"blocked_diagnostic": {"target_topic_id": "blocked"}},
        plan_id="readiness-plan",
        topics_by_id={"blocked": {"id": "blocked", "name": "Blocked"}},
    )
    assert plan is not None

    decision = CognitiveIntentPolicy(mode="on").decorate(
        plan,
        LearnerCognitiveStateView.empty(
            "blocked",
            "cognitive-v2.1-1",
            reason="missing_projection",
        ),
    )

    assert decision.applied is False
    assert decision.proposed_plan is None
    assert decision.action_candidate is None
    assert decision.fallback_reason == "planner_intent_already_set"


def test_readiness_merge_clears_all_cognitive_bindings() -> None:
    hypothesis = HypothesisRef(
        hypothesis_id="hypothesis-1",
        topic_id="base",
        code="omit_inner_derivative",
        status="supported",
        probability=0.9,
        model_version="cognitive-v2.1-1",
    )
    forged = replace(
        _plan(reason="blocked_diagnostic"),
        learning_intent="readiness_probe",
        hypothesis_target=hypothesis,
        repair_strategy="complete_inner_derivative",
        obligation_refs=("forged",),
        cognitive_strategy="forged",
    )

    merged = merge_learning_action_candidates(
        forged,
        (_retention_candidate(topic_id="base"),),
        now=NOW,
    )

    assert merged.learning_intent == "readiness_probe"
    assert merged.hypothesis_target is None
    assert merged.repair_strategy == ""
    assert merged.obligation_refs == ()
    assert merged.cognitive_strategy == ""


def test_entry_rejects_a_legacy_cognitive_override_of_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, _ = _active_subject(monkeypatch, "_readiness_cognitive_override_test")
    context = _targeted_context()
    context["selection_reason"] = "blocked_diagnostic"
    context["question_params"]["retry_wrong_question"] = {}

    payload = asyncio.run(
        subject._generate_question_payload(
            source_text="Generate the readiness question",
            source="targeted_question",
            targeted_context=context,
        )
    )

    assert payload["question"] == "Fallback ordinary chain-rule question"
    assert subject._knowledge_tracker.proposed == 1
    assert subject._agent.generated == 1
    assert subject._store.events == []
    assert subject.private_payload is not None
    binding = subject.private_payload["target_binding"]
    assert not {
        "cognitive_learning_intent",
        "cognitive_hypothesis_target",
        "cognitive_repair_strategy",
        "cognitive_decision_id",
        "diagnostic_validation_id",
    }.intersection(binding)


def test_same_topic_retention_merges_only_inside_its_time_window() -> None:
    plan = _plan(reason="wrong_retry")
    early = merge_learning_action_candidates(
        plan,
        (
            _retention_candidate(
                topic_id="base",
                not_before="2026-03-05T12:00:00Z",
                due_by="2026-03-06T12:00:00Z",
            ),
        ),
        now=NOW,
    )
    merged = merge_learning_action_candidates(
        plan,
        (_retention_candidate(topic_id="base"),),
        now=NOW,
    )
    different_topic_before_due = merge_learning_action_candidates(
        plan,
        (
            _retention_candidate(
                due_by="2026-03-06T12:00:00Z",
            ),
        ),
        topics_by_id={"retention": {"id": "retention", "name": "Retention"}},
        now=NOW,
    )

    assert early is plan
    assert different_topic_before_due is plan
    assert merged.selection.reason == "wrong_retry"
    assert merged.target_topic.id == "base"
    assert merged.learning_intent == "retention_check"
    assert merged.obligation_refs == ("obligation-1",)


@pytest.mark.parametrize("priority_reason", ["wrong_retry", "due_review"])
def test_overdue_retention_never_displaces_wrong_or_fsrs_priority(
    priority_reason: str,
) -> None:
    plan = _plan(reason=priority_reason)

    merged = merge_learning_action_candidates(
        plan,
        (_retention_candidate(),),
        topics_by_id={"retention": {"id": "retention", "name": "Retention"}},
        now=NOW,
    )

    assert merged is plan


def test_overdue_retention_can_become_a_coach_selected_candidate() -> None:
    plan = _plan(reason="weak_topic")

    merged = merge_learning_action_candidates(
        plan,
        (_retention_candidate(),),
        topics_by_id={"retention": {"id": "retention", "name": "Retention"}},
        now=NOW,
    )

    assert merged.target_topic.id == "retention"
    assert merged.selection.reason == "recommended"
    assert merged.learning_intent == "retention_check"
    assert merged.obligation_refs == ("obligation-1",)


def test_retention_requires_exactly_one_obligation_and_valid_timestamps() -> None:
    plan = _plan()

    no_obligation = merge_learning_action_candidates(
        plan,
        (_retention_candidate(obligation_refs=()),),
        topics_by_id={"retention": {"id": "retention", "name": "Retention"}},
        now=NOW,
    )
    invalid_time = merge_learning_action_candidates(
        plan,
        (_retention_candidate(due_by="not-a-time"),),
        topics_by_id={"retention": {"id": "retention", "name": "Retention"}},
        now=NOW,
    )
    missing_due_by = merge_learning_action_candidates(
        plan,
        (_retention_candidate(due_by=""),),
        topics_by_id={"retention": {"id": "retention", "name": "Retention"}},
        now=NOW,
    )
    missing_not_before = merge_learning_action_candidates(
        plan,
        (_retention_candidate(not_before=""),),
        topics_by_id={"retention": {"id": "retention", "name": "Retention"}},
        now=NOW,
    )

    assert no_obligation is plan
    assert invalid_time is plan
    assert missing_due_by is plan
    assert missing_not_before is plan


def test_scope_fallback_selection_is_owned_by_planner() -> None:
    selected = select_scope_fallback_topic(
        (
            {"id": "unattempted", "depth": 1, "difficulty": 0.2},
            {"id": "ready", "depth": 3, "difficulty": 0.8},
        ),
        {"ready": {"mastery": 0.9}},
        ready_topic_ids=("ready",),
    )

    assert selected is not None
    assert selected.id == "ready"


def test_entry_uses_planner_result_without_a_second_topic_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = _load_entries(monkeypatch, "_planner_single_source_test")
    chosen = _plan(reason="due_review", topic_id="retention")
    monkeypatch.setattr(entries, "build_question_plan", lambda *_args, **_kwargs: chosen)

    class Store:
        @staticmethod
        def get_topic(topic_id):
            return {"id": topic_id, "name": topic_id}

    class Subject(entries._TutorQuestionEntriesMixin):
        _knowledge_tracker = SimpleNamespace(store=Store())

    selection = Subject()._selection_from_question_params(
        {
            "retry_wrong_question": {"id": "wrong-1", "topic_id": "base"},
            "due_reviews": [{"topic_id": "retention"}],
        }
    )

    assert selection["selected_topic_id"] == "retention"
    assert selection["selection_reason"] == "due_review"
