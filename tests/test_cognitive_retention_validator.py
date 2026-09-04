from __future__ import annotations

from dataclasses import replace

from adaptive_learning.cognitive_retention import (
    CHAIN_RULE_RETENTION_BLUEPRINT,
    RETENTION_BLUEPRINT_VERSION,
    RETENTION_VALIDATOR_VERSION,
    RetentionValidationInput,
    RetentionValidator,
)


def _input(**changes: object) -> RetentionValidationInput:
    blueprint = CHAIN_RULE_RETENTION_BLUEPRINT
    base = RetentionValidationInput(
        episode_id="episode-1",
        obligation_id="obligation-1",
        hypothesis_code="omit_inner_derivative",
        verdict="correct",
        used_hint=False,
        evaluator_type="deterministic_math",
        evaluator_version="chain-rule-v1",
        evaluator_confidence=1.0,
        answered_at="2026-09-02T02:00:00Z",
        not_before="2026-09-02T00:00:00Z",
        eligibility_until="2026-09-08T00:00:00Z",
        question_family_id=blueprint.question_family_id,
        transfer_question_family_id="chain.polynomial-power.cross-form-transfer",
        independence_group=blueprint.independence_group,
        blueprint_version=RETENTION_BLUEPRINT_VERSION,
        validator_version=RETENTION_VALIDATOR_VERSION,
    )
    return replace(base, **changes)


def test_delayed_independent_unassisted_correct_answer_resolves() -> None:
    result = RetentionValidator().validate(_input())
    assert result.certified is True
    assert result.disposition == "resolved"


def test_same_hypothesis_support_is_relapse_not_generic_wrong() -> None:
    result = RetentionValidator().validate(
        _input(verdict="wrong", observed_hypothesis_code="omit_inner_derivative")
    )
    assert result.disposition == "relapse"


def test_early_hint_or_repeated_family_is_only_ordinary_evidence() -> None:
    result = RetentionValidator().validate(
        _input(
            answered_at="2026-09-01T12:00:00Z",
            used_hint=True,
            question_family_id="chain.polynomial-power.cross-form-transfer",
        )
    )
    assert result.certified is False
    assert result.disposition == "ordinary_evidence"
    assert {"retention_too_early", "hint_used_or_unknown", "transfer_question_family_reused"} <= set(result.reasons)


def test_other_mechanism_and_partial_are_rescheduled_without_relapse() -> None:
    other = RetentionValidator().validate(
        _input(verdict="wrong", observed_hypothesis_code="differentiate_inner_incorrectly")
    )
    partial = RetentionValidator().validate(_input(verdict="partial"))
    assert other.disposition == "reschedule"
    assert partial.disposition == "reschedule"


def test_prior_retention_family_is_not_recertified() -> None:
    result = RetentionValidator().validate(
        _input(
            previous_question_family_ids=(
                CHAIN_RULE_RETENTION_BLUEPRINT.question_family_id,
            )
        )
    )

    assert result.certified is False
    assert result.disposition == "ordinary_evidence"
    assert "retention_question_family_reused" in result.reasons
