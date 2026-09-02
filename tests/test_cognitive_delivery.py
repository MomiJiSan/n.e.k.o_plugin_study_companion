from __future__ import annotations

from dataclasses import replace

import pytest

# isort: split
from adaptive_learning.cognitive_delivery import (
    abandoned_intervention_event,
    committed_question_event,
    prepare_cognitive_intervention,
    reviewed_question_payload,
    validate_reviewed_question,
)
from adaptive_learning.cognitive_policy import CognitivePolicyDecision
from adaptive_learning.contracts import (
    HypothesisRef,
    PracticeSelection,
    QuestionInstance,
    QuestionPlan,
    TopicRef,
)


def _original_plan() -> QuestionPlan:
    topic = TopicRef(id="college_chain_rule", name="Chain rule")
    return QuestionPlan(
        plan_id="selection-1",
        selection=PracticeSelection(
            reason="wrong_retry",
            target_topic=topic,
            eligible_topic_ids=("college_chain_rule",),
            origin_wrong_question_id="wrong-1",
        ),
        difficulty=3,
        question_type="math_reasoning",
        scope_key="scope-a",
        scope_revision=9,
        source_question_id="wrong-1",
        target_binding={
            "learning_plan_id": "plan-1",
            "learning_plan_revision": 4,
            "origin_wrong_question_id": "wrong-1",
        },
    )


def _hypothesis(*, status: str = "supported") -> HypothesisRef:
    return HypothesisRef(
        hypothesis_id="hypothesis-1",
        topic_id="college_chain_rule",
        code="omit_inner_derivative",
        status=status,
        probability=0.91,
        model_version="cognitive-v2",
        source_snapshot_id="snapshot-7",
        source_attempt_id="attempt-source",
        projection_generation=7,
    )


def _decision(
    *,
    mode: str = "on",
    intent: str = "misconception_repair",
    strategy: str = "complete_inner_derivative",
    applied: bool = True,
) -> CognitivePolicyDecision:
    original = _original_plan()
    hypothesis = _hypothesis(status="supported" if applied else "hypothesized")
    proposed = replace(
        original,
        learning_intent=intent,
        hypothesis_target=hypothesis,
        repair_strategy=strategy,
    )
    return CognitivePolicyDecision(
        mode=mode,  # type: ignore[arg-type]
        original_plan=original,
        proposed_plan=proposed,
        effective_plan=proposed if applied else original,
        proposed_intent=intent,  # type: ignore[arg-type]
        selected_hypothesis=hypothesis,
        repair_strategy=strategy,  # type: ignore[arg-type]
        applied=applied,
    )


def _prepared(*, transfer: bool = False):
    decision = (
        _decision(intent="transfer_check", strategy="cross_form_transfer")
        if transfer
        else _decision()
    )
    prepared = prepare_cognitive_intervention(
        decision,
        decision_id="decision-1",
        created_at="2026-03-11T10:00:00Z",
    )
    assert prepared is not None
    assert prepared.active
    return prepared


def _question(prepared) -> QuestionInstance:
    plan = prepared.proposed_plan
    return QuestionInstance(
        question_id="question-1",
        plan_id=plan.plan_id,
        target_topic=plan.target_topic,
        question_type=plan.question_type,
        difficulty=plan.difficulty,
        public_payload=reviewed_question_payload(prepared),
        mode=plan.mode,
        source_question_id=plan.source_question_id,
        target_binding=plan.target_binding,
        scope_key=plan.scope_key,
        scope_revision=plan.scope_revision,
        status="generated",
        learning_intent=plan.learning_intent,
        hypothesis_target=plan.hypothesis_target,
        repair_strategy=plan.repair_strategy,
        cognitive_decision_id=prepared.decision_id,
    )


def test_shadow_proposal_never_changes_the_effective_plan() -> None:
    decision = _decision(mode="shadow", applied=False)

    prepared = prepare_cognitive_intervention(
        decision,
        decision_id="decision-shadow",
        created_at="2026-03-11T10:00:00Z",
    )

    assert prepared is not None
    assert not prepared.active
    assert decision.effective_plan is decision.original_plan
    assert prepared.proposed_plan.learning_intent == "misconception_repair"
    assert prepared.proposal_event.event_type == "intent_proposed"
    assert prepared.proposal_event.decision_id == "decision-shadow"


def test_malformed_shadow_decision_that_changes_effective_plan_is_rejected() -> None:
    decision = _decision(mode="shadow", applied=False)
    forged = replace(decision, effective_plan=decision.proposed_plan)

    assert prepare_cognitive_intervention(forged) is None


def test_active_delivery_selects_only_the_matching_reviewed_blueprint() -> None:
    prepared = _prepared()

    assert prepared.blueprint is not None
    assert prepared.blueprint.blueprint_id == "chain.omit-inner.fill-factor.v1"
    assert prepared.blueprint.hypothesis_code == "omit_inner_derivative"
    assert prepared.blueprint.learning_intent == "misconception_repair"
    assert prepared.blueprint.repair_strategy == "complete_inner_derivative"


def test_active_delivery_rejects_unreviewed_strategy_and_unconfirmed_source() -> None:
    unreviewed = _decision(strategy="compare_steps")
    unsupported = _decision()
    hypothesis = _hypothesis(status="hypothesized")
    proposed = replace(unsupported.proposed_plan, hypothesis_target=hypothesis)
    unsupported = replace(
        unsupported,
        proposed_plan=proposed,
        effective_plan=proposed,
        selected_hypothesis=hypothesis,
    )

    assert prepare_cognitive_intervention(unreviewed) is None
    assert prepare_cognitive_intervention(unsupported) is None


def test_reviewed_payload_contains_complete_fixed_scoring_contract() -> None:
    prepared = _prepared()

    payload = reviewed_question_payload(prepared)

    assert payload == {
        "question": "Complete the missing factor: d/dx cos(x^3) = -sin(x^3) * ____.",
        "answer": "3*x^2",
        "reference_answer": "3*x^2",
        "accepted_answers": ["3*x^2"],
        "key_points": [
            "Identify the outer and inner functions.",
            "Include the derivative of the inner function.",
        ],
        "rubric": {"chain_rule_structure": 1.0},
        "solution_steps": [
            "Differentiate the outer function while keeping the inner expression.",
            "Multiply by the derivative of the inner expression.",
        ],
        "math_equivalence_engine": {"enabled": False},
        "question_type": "math_reasoning",
        "difficulty": 3,
        "target_topic_id": "college_chain_rule",
        "hint": "",
        "math_expression": "d/dx cos(x^3)",
        "diagnostic_signature": (
            "composition:cos(x^3)|outer:-sin(x^3)|inner:3*x^2|blank:inner"
        ),
        "cognitive_blueprint_id": "chain.omit-inner.fill-factor.v1",
        "cognitive_question_family_id": "chain.cos-cube.fill-factor",
        "competing_hypothesis_codes": ["differentiate_inner_incorrectly"],
    }


def test_reviewed_question_passes_the_domain_validator() -> None:
    prepared = _prepared()

    result = validate_reviewed_question(prepared, _question(prepared))

    assert result.valid
    assert result.errors == ()
    assert result.validation_id.startswith("cognitive-validation:")


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (
            lambda question: replace(
                question,
                target_topic=TopicRef(id="other-topic", name="Other"),
            ),
            "question_topic_mismatch",
        ),
        (
            lambda question: replace(question, difficulty=5),
            "question_difficulty_mismatch",
        ),
        (
            lambda question: replace(
                question,
                public_payload={**dict(question.public_payload), "answer": "cos(x^3)"},
            ),
            "fixed_payload_mismatch:answer",
        ),
        (
            lambda question: replace(
                question,
                public_payload={
                    **dict(question.public_payload),
                    "competing_hypothesis_codes": ["invented_hypothesis"],
                },
            ),
            "competing_hypotheses_mismatch",
        ),
    ],
)
def test_tampered_reviewed_question_fails_closed(mutate, expected_error: str) -> None:
    prepared = _prepared()

    result = validate_reviewed_question(prepared, mutate(_question(prepared)))

    assert not result.valid
    assert result.validation_id == ""
    assert expected_error in result.errors


def test_transfer_question_cannot_reuse_the_repair_question_family() -> None:
    prepared = _prepared(transfer=True)
    assert prepared.blueprint is not None

    result = validate_reviewed_question(
        prepared,
        _question(prepared),
        repair_question_family_id=prepared.blueprint.question_family_id,
    )

    assert not result.valid
    assert "transfer_question_family_reused" in result.errors


def test_committed_and_abandoned_events_keep_exact_validated_provenance() -> None:
    prepared = _prepared()
    validation = validate_reviewed_question(prepared, _question(prepared))

    committed = committed_question_event(
        prepared,
        question_id="question-1",
        validation=validation,
        created_at="2026-03-11T10:01:00Z",
    )
    abandoned = abandoned_intervention_event(
        committed,
        reason="scope_revision_changed",
        created_at="2026-03-11T10:02:00Z",
    )

    assert committed.event_type == "question_committed"
    assert committed.question_id == "question-1"
    assert committed.blueprint_id == "chain.omit-inner.fill-factor.v1"
    assert committed.diagnostic_validation_id == validation.validation_id
    assert committed.hypothesis_target.source_snapshot_id == "snapshot-7"
    assert abandoned.event_type == "intervention_abandoned"
    assert abandoned.question_id == "question-1"
    assert abandoned.attempt_id == ""
    assert abandoned.evaluation_verdict == ""
    assert abandoned.abandonment_reason == "scope_revision_changed"


def test_committed_and_abandoned_events_reject_missing_required_facts() -> None:
    prepared = _prepared()
    validation = validate_reviewed_question(prepared, _question(prepared))

    with pytest.raises(ValueError):
        committed_question_event(
            prepared,
            question_id="",
            validation=validation,
        )
    committed = committed_question_event(
        prepared,
        question_id="question-1",
        validation=validation,
    )
    with pytest.raises(ValueError):
        abandoned_intervention_event(committed, reason="")
