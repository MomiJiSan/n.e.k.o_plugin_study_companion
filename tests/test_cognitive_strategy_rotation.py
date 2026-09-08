from __future__ import annotations

from dataclasses import replace

from adaptive_learning.cognitive_policy import (
    CognitivePolicyDecision,
    question_plan_ownership_fingerprint,
)
from adaptive_learning.cognitive_strategy_rotation import (
    STRATEGY_ROTATION_EXPERIMENT_VERSION,
    apply_guarded_strategy_rotation,
)
from adaptive_learning.contracts import (
    HypothesisRef,
    PracticeSelection,
    QuestionPlan,
    TopicRef,
)


def _decision(*, source_attempt_id: str = "attempt-source") -> CognitivePolicyDecision:
    topic = TopicRef(id="college_chain_rule", name="Chain rule")
    original = QuestionPlan(
        plan_id="plan-rotation",
        selection=PracticeSelection(
            reason="wrong_retry",
            target_topic=topic,
            eligible_topic_ids=("college_chain_rule",),
        ),
        difficulty=3,
        question_type="math_reasoning",
    )
    hypothesis = HypothesisRef(
        hypothesis_id="hypothesis-active",
        topic_id="college_chain_rule",
        code="omit_inner_derivative",
        status="supported",
        probability=0.9,
        model_version="cognitive-v2.1-1",
        source_snapshot_id="snapshot-active",
        source_attempt_id=source_attempt_id,
        projection_generation=7,
    )
    proposed = replace(
        original,
        learning_intent="misconception_repair",
        hypothesis_target=hypothesis,
        repair_strategy="complete_inner_derivative",
    )
    return CognitivePolicyDecision(
        mode="on",
        original_plan=original,
        proposed_plan=proposed,
        effective_plan=proposed,
        proposed_intent="misconception_repair",
        selected_hypothesis=hypothesis,
        repair_strategy="complete_inner_derivative",
        applied=True,
        decision_trace=("policy:accepted",),
    )


def test_rotation_is_default_off_and_preserves_the_exact_decision() -> None:
    decision = _decision()

    result, assignment = apply_guarded_strategy_rotation(decision)

    assert result is decision
    assert assignment.to_metadata() == {
        "eligible": False,
        "applied": False,
        "reason": "disabled",
        "experiment_version": STRATEGY_ROTATION_EXPERIMENT_VERSION,
        "comparison_scope_id": "chain.omit-inner.repair",
        "bucket": None,
        "variant": "",
        "repair_strategy": "",
        "assignment_key_sha256": "",
    }


def test_rotation_deterministically_reaches_both_reviewed_variants() -> None:
    baseline, baseline_assignment = apply_guarded_strategy_rotation(
        _decision(source_attempt_id="attempt-source"), enabled=True
    )
    alternate, alternate_assignment = apply_guarded_strategy_rotation(
        _decision(source_attempt_id="attempt-a"), enabled=True
    )

    assert baseline_assignment.bucket == 0
    assert baseline_assignment.variant == "baseline"
    assert baseline.repair_strategy == "complete_inner_derivative"
    assert alternate_assignment.bucket == 1
    assert alternate_assignment.variant == "alternate"
    assert alternate.repair_strategy == "minimal_change"
    assert alternate.proposed_plan is not None
    assert alternate.proposed_plan.repair_strategy == "minimal_change"
    assert alternate.effective_plan == alternate.proposed_plan
    assert question_plan_ownership_fingerprint(alternate.proposed_plan) == (
        question_plan_ownership_fingerprint(alternate.original_plan)
    )
    assert len(alternate_assignment.assignment_key_sha256) == 64
    assert "attempt-a" not in str(alternate_assignment.to_metadata())


def test_rotation_replay_is_stable_for_the_same_frozen_identity() -> None:
    decision = _decision(source_attempt_id="stable-source")

    first = apply_guarded_strategy_rotation(decision, enabled=True)
    second = apply_guarded_strategy_rotation(decision, enabled=True)

    assert first == second


def test_rotation_reuses_a_verified_failed_repair_assignment() -> None:
    prior_decision = _decision(source_attempt_id="attempt-a")
    _, prior = apply_guarded_strategy_rotation(prior_decision, enabled=True)

    retried, assignment = apply_guarded_strategy_rotation(
        _decision(source_attempt_id="attempt-source"),
        enabled=True,
        retry_assignment=prior.to_metadata(),
    )

    assert retried.repair_strategy == "minimal_change"
    assert assignment.reason == "reused_retry_assignment"
    assert assignment.assignment_key_sha256 == prior.assignment_key_sha256


def test_rotation_ignores_a_tampered_retry_assignment() -> None:
    prior_decision = _decision(source_attempt_id="attempt-a")
    _, prior = apply_guarded_strategy_rotation(prior_decision, enabled=True)
    tampered = {**prior.to_metadata(), "repair_strategy": "complete_inner_derivative"}

    retried, assignment = apply_guarded_strategy_rotation(
        _decision(source_attempt_id="attempt-source"),
        enabled=True,
        retry_assignment=tampered,
    )

    assert retried.repair_strategy == "complete_inner_derivative"
    assert assignment.reason == "assigned"
    assert assignment.bucket == 0


def test_rotation_fails_closed_outside_the_frozen_scope() -> None:
    decision = _decision()
    hypothesis = replace(decision.selected_hypothesis, code="confuse_product_and_chain")
    proposed = replace(decision.proposed_plan, hypothesis_target=hypothesis)
    outside = replace(
        decision,
        proposed_plan=proposed,
        effective_plan=proposed,
        selected_hypothesis=hypothesis,
    )

    result, assignment = apply_guarded_strategy_rotation(outside, enabled=True)

    assert result is outside
    assert assignment.reason == "outside_frozen_scope"
    assert assignment.applied is False


def test_rotation_requires_preexisting_certified_assignment_identity() -> None:
    decision = _decision(source_attempt_id="")

    result, assignment = apply_guarded_strategy_rotation(decision, enabled=True)

    assert result is decision
    assert assignment.reason == "missing_assignment_identity"
    assert assignment.applied is False
