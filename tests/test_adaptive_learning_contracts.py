from __future__ import annotations

from adaptive_learning import (
    DEFAULT_ASSESSMENT_POLICY_VERSION,
    DEFAULT_PRACTICE_POLICY_VERSION,
    DEFAULT_QUESTION_POLICY_VERSION,
    INTERNAL_SCHEMA_VERSION,
    EvaluatedAttempt,
    EvaluationResult,
    MapPage,
    MapQuery,
    PracticeSelection,
    QuestionInstance,
    QuestionPlan,
    TopicRef,
)


def test_question_plan_carries_server_owned_target_and_version_defaults() -> None:
    topic = TopicRef(id="math.linear-equation", name="一元一次方程", subject="math", stage="junior_high")
    selection = PracticeSelection(
        reason="weak_topic",
        target_topic=topic,
        eligible_topic_ids=(topic.id,),
    )

    plan = QuestionPlan(
        plan_id="plan-1",
        selection=selection,
        difficulty=3,
        question_type="math_reasoning",
    )

    assert plan.target_topic is topic
    assert plan.selection.reason == "weak_topic"
    assert plan.policy_version == DEFAULT_PRACTICE_POLICY_VERSION
    assert plan.schema_version == INTERNAL_SCHEMA_VERSION


def test_question_and_evaluation_contracts_preserve_private_and_audit_fields() -> None:
    topic = TopicRef(id="math.linear-equation", name="一元一次方程")
    question = QuestionInstance(
        question_id="question-1",
        plan_id="plan-1",
        target_topic=topic,
        question_type="math_exact",
        difficulty=2,
        public_payload={"question": "x + 1 = 2"},
        private_payload={"answer": "1"},
        status="validated",
    )
    evaluation = EvaluationResult(
        verdict="correct",
        score=100,
        final_answer_correct=True,
        confidence=0.99,
    )
    attempt = EvaluatedAttempt(
        attempt_id="attempt-1",
        question=question,
        learner_answer="1",
        evaluation=evaluation,
        response_time_ms=1234,
    )

    assert attempt.question.private_payload["answer"] == "1"
    assert attempt.evaluation.policy_version == DEFAULT_ASSESSMENT_POLICY_VERSION
    assert question.policy_version == DEFAULT_QUESTION_POLICY_VERSION


def test_map_contract_makes_page_and_boundary_truncation_explicit() -> None:
    query = MapQuery(stage="senior_high", subject="math", page_size=100, cursor="topic-100")
    page = MapPage(
        nodes=({"id": "topic-101"},),
        edges=({"source": "topic-100", "target": "topic-101"},),
        scope_total_count=1001,
        scope_returned_count=1,
        has_more=True,
        next_cursor="topic-101",
        boundary_returned_count=1,
        boundary_truncated=False,
        catalog_revision="catalog-42",
    )

    assert query.include_boundary is True
    assert page.scope_total_count == 1001
    assert page.has_more is True
    assert page.boundary_truncated is False
