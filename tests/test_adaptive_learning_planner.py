from __future__ import annotations

import pytest

# isort: split

from adaptive_learning.planner import build_question_plan, select_practice_selection, topic_ref_from_mapping


@pytest.fixture
def topics() -> dict[str, dict[str, str]]:
    return {
        "retry": {"id": "retry", "name": "错题知识点", "subject": "math"},
        "due": {"id": "due", "name": "复习知识点", "subject": "math"},
        "weak": {"id": "weak", "name": "薄弱知识点", "subject": "math"},
        "recommended": {"id": "recommended", "name": "推荐知识点", "subject": "math"},
        "fallback": {"id": "fallback", "name": "默认知识点", "subject": "math"},
    }


def test_retry_precedes_every_other_candidate(topics: dict[str, dict[str, str]]) -> None:
    selection = select_practice_selection(
        {
            "retry_wrong_question": {"id": "wrong-1", "topic_id": "retry"},
            "due_reviews": [{"topic_id": "due"}],
            "weak_topics": [{"topic_id": "weak"}],
            "candidate_evidence": [{"payload": {"topic_id": "recommended"}}],
        },
        eligible_topic_ids=topics,
        topics_by_id=topics,
    )

    assert selection is not None
    assert selection.reason == "wrong_retry"
    assert selection.target_topic.id == "retry"
    assert selection.origin_wrong_question_id == "wrong-1"


@pytest.mark.parametrize(
    ("params", "expected_reason", "expected_topic_id"),
    [
        ({"due_reviews": [{"topic_id": "due"}], "weak_topics": [{"topic_id": "weak"}]}, "due_review", "due"),
        ({"weak_topics": [{"topic_id": "weak"}]}, "weak_topic", "weak"),
        ({"candidate_evidence": [{"payload": {"topic_id": "recommended"}}]}, "recommended", "recommended"),
        ({"target_topic_id": "recommended"}, "recommended", "recommended"),
        ({}, "default", "fallback"),
    ],
)
def test_selection_follows_current_priority_order(
    topics: dict[str, dict[str, str]],
    params: dict[str, object],
    expected_reason: str,
    expected_topic_id: str,
) -> None:
    selection = select_practice_selection(
        params,
        eligible_topic_ids=("fallback", "recommended", "weak", "due"),
        topics_by_id=topics,
    )

    assert selection is not None
    assert selection.reason == expected_reason
    assert selection.target_topic.id == expected_topic_id


def test_out_of_scope_candidates_are_skipped_and_plan_carries_scope(topics: dict[str, dict[str, str]]) -> None:
    plan = build_question_plan(
        {
            "retry_wrong_question": {"id": "wrong-outside", "topic_id": "retry"},
            "due_reviews": [{"topic_id": "due"}],
            "suggested_difficulty": 4,
        },
        plan_id="plan-1",
        eligible_topic_ids=("due",),
        topics_by_id=topics,
        scope_key="stage:senior_high|subject:math",
        scope_revision=7,
        question_type="math_exact",
    )

    assert plan is not None
    assert plan.selection.reason == "due_review"
    assert plan.target_topic.id == "due"
    assert plan.difficulty == 4
    assert plan.question_type == "math_exact"
    assert plan.scope_revision == 7


def test_empty_candidates_do_not_invent_a_topic_outside_scope() -> None:
    assert select_practice_selection({}, eligible_topic_ids=("missing",), topics_by_id={}) is None
    assert topic_ref_from_mapping({}, topic_id="") is None
