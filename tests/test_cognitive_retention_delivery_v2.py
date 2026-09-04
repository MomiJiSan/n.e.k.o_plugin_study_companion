from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from adaptive_learning.cognitive_retention import (
    RETENTION_COGNITIVE_STRATEGY,
    build_retention_action_proposal,
    prepare_retention_question,
    retention_question_payload,
    validate_retention_question_payload,
)
from adaptive_learning.contracts import PracticeSelection, QuestionPlan, TopicRef

NOW = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)


def _episode(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "episode_id": "episode-1",
        "hypothesis_id": "hypothesis-1",
        "topic_id": "college_chain_rule",
        "hypothesis_code": "omit_inner_derivative",
        "model_version": "cognitive-v2.1-1",
        "source_attempt_id": "attempt-transfer",
        "source_event_id": "event-transfer",
        "transfer_question_family_id": "chain.polynomial-power.cross-form-transfer",
        "status": "open",
    }
    value.update(changes)
    return value


def _obligation(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "obligation_id": "obligation-1",
        "episode_id": "episode-1",
        "hypothesis_id": "hypothesis-1",
        "topic_id": "college_chain_rule",
        "hypothesis_code": "omit_inner_derivative",
        "obligation_type": "retention",
        "status": "pending",
        "not_before": "2026-09-03T00:00:00Z",
        "due_by": "2026-09-04T00:00:00Z",
        "eligibility_until": "2026-09-09T00:00:00Z",
    }
    value.update(changes)
    return value


def _proposal(**obligation_changes: object):
    proposal = build_retention_action_proposal(
        _obligation(**obligation_changes),
        _episode(),
        version_set="cognitive-v2.1-1",
        projection_current=True,
        as_of=NOW,
    )
    assert proposal is not None
    return proposal


def _plan(proposal) -> QuestionPlan:
    return QuestionPlan(
        plan_id="plan-1",
        selection=PracticeSelection(
            reason="recommended",
            target_topic=TopicRef("college_chain_rule", "Chain rule"),
            eligible_topic_ids=("college_chain_rule",),
        ),
        difficulty=3,
        question_type="math_reasoning",
        learning_intent="retention_check",
        obligation_refs=(proposal.obligation_id,),
        cognitive_strategy=RETENTION_COGNITIVE_STRATEGY,
    )


def _claim(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        **_obligation(status="claimed"),
        "claim_id": "claim-1",
        "claim_token": "secret-token",
        "worker_id": "question-worker",
        "lease_expires_at": "2026-09-03T12:05:00Z",
    }
    value.update(changes)
    return value


def test_open_v21_obligation_becomes_one_bounded_coach_candidate() -> None:
    proposal = _proposal()

    assert proposal.candidate.topic_id == "college_chain_rule"
    assert proposal.candidate.intent == "retention_check"
    assert proposal.candidate.obligation_refs == ("obligation-1",)
    assert proposal.candidate.evidence_refs == (
        "episode-1",
        "attempt-transfer",
        "event-transfer",
    )


def test_disabled_versions_stale_projection_and_nonactive_mechanisms_fail_closed() -> None:
    unknown = build_retention_action_proposal(
        _obligation(),
        _episode(),
        version_set="unknown",
        projection_current=True,
        as_of=NOW,
    )
    stale = build_retention_action_proposal(
        _obligation(),
        _episode(),
        version_set="cognitive-v2.1-1",
        projection_current=False,
        as_of=NOW,
    )
    other = build_retention_action_proposal(
        _obligation(hypothesis_code="differentiate_inner_incorrectly"),
        _episode(hypothesis_code="differentiate_inner_incorrectly"),
        version_set="cognitive-v2.1-1",
        projection_current=True,
        as_of=NOW,
    )

    assert unknown is None
    assert stale is None
    assert other is None


def test_claim_must_match_the_exact_planner_selected_obligation() -> None:
    proposal = _proposal()
    plan = _plan(proposal)

    assert prepare_retention_question(
        plan,
        proposal,
        _claim(obligation_id="different-obligation"),
    ) is None
    prepared = prepare_retention_question(plan, proposal, _claim())
    assert prepared is not None
    assert prepared.claim_token == "secret-token"


def test_retention_payload_is_fixed_and_tampering_is_rejected() -> None:
    proposal = _proposal()
    prepared = prepare_retention_question(_plan(proposal), proposal, _claim())
    assert prepared is not None

    payload = retention_question_payload(prepared, topic_id="college_chain_rule")
    assert payload["question"] == "Differentiate exp(5x - 2)."
    assert payload["answer"] == "5*exp(5*x-2)"
    assert payload["cognitive_independence_group"] == "chain.exponential-affine"
    assert validate_retention_question_payload(
        prepared,
        payload,
        topic_id="college_chain_rule",
    ) == ()

    tampered = {**payload, "answer": "exp(5*x-2)"}
    assert validate_retention_question_payload(
        prepared,
        tampered,
        topic_id="college_chain_rule",
    ) == ("fixed_payload_mismatch:answer",)


def test_rescheduled_retention_rotates_to_an_unused_reviewed_family_and_group() -> None:
    proposal = _proposal(
        previous_question_family_ids=("chain.exp-affine.retention",),
        previous_independence_groups=("chain.exponential-affine",),
    )

    assert proposal.blueprint.question_family_id == "chain.sin-affine.retention"
    assert proposal.blueprint.independence_group == "chain.trigonometric-affine"
    assert build_retention_action_proposal(
        _obligation(
            previous_question_family_ids=(
                "chain.exp-affine.retention",
                "chain.sin-affine.retention",
            ),
            previous_independence_groups=(
                "chain.exponential-affine",
                "chain.trigonometric-affine",
            ),
        ),
        _episode(),
        version_set="cognitive-v2.1-1",
        projection_current=True,
        as_of=NOW,
    ) is None


def test_rotated_retention_payload_uses_trigonometric_guidance() -> None:
    proposal = _proposal(
        previous_question_family_ids=("chain.exp-affine.retention",),
        previous_independence_groups=("chain.exponential-affine",),
    )
    prepared = prepare_retention_question(_plan(proposal), proposal, _claim())
    assert prepared is not None

    payload = retention_question_payload(prepared, topic_id="college_chain_rule")

    assert payload["question"] == "Differentiate sin(4x + 1)."
    assert payload["key_points"] == [
        "Differentiate the outer sine function to cosine.",
        "Multiply by the derivative of the inner affine expression.",
    ]
    assert payload["solution_steps"] == [
        "Keep the inner expression inside the cosine function.",
        "Multiply by the inner derivative.",
    ]
    assert validate_retention_question_payload(
        prepared,
        payload,
        topic_id="college_chain_rule",
    ) == ()


def test_non_retention_plan_cannot_bind_a_claim() -> None:
    proposal = _proposal()
    plan = replace(
        _plan(proposal),
        learning_intent="practice",
        cognitive_strategy="",
    )

    assert prepare_retention_question(plan, proposal, _claim()) is None
