from __future__ import annotations

from dataclasses import replace

import pytest

from adaptive_learning.cognitive_catalog import (
    COGNITIVE_CATALOG_V1,
    COLLEGE_CHAIN_RULE_TOPIC_ID,
)
from adaptive_learning.cognitive_intervention import (
    CognitiveInterventionBinding,
    CognitiveInterventionEvent,
)
from adaptive_learning.cognitive_question_validation import (
    DEFAULT_COGNITIVE_VALIDATOR_VERSION,
    CognitiveQuestionArtifact,
    CognitiveQuestionValidationContext,
    DiagnosticQuestionValidator,
)
from adaptive_learning.contracts import (
    HypothesisRef,
    PracticeSelection,
    QuestionInstance,
    QuestionPlan,
    TopicRef,
)


def _hypothesis(*, code: str = "omit_inner_derivative", status: str = "supported"):
    return HypothesisRef(
        hypothesis_id=f"hypothesis-{code}",
        topic_id=COLLEGE_CHAIN_RULE_TOPIC_ID,
        code=code,
        status=status,
        probability=0.91,
        model_version="cognitive-v2",
        source_snapshot_id="snapshot-7",
        source_attempt_id="attempt-source",
        projection_generation=7,
    )


def _plans(
    *,
    blueprint_id: str = "chain.omit-inner.compare-steps.v1",
) -> tuple[QuestionPlan, QuestionPlan]:
    blueprint = COGNITIVE_CATALOG_V1.get_blueprint(blueprint_id)
    assert blueprint is not None
    topic = TopicRef(id=COLLEGE_CHAIN_RULE_TOPIC_ID, name="Chain rule")
    original = QuestionPlan(
        plan_id="selection-1",
        selection=PracticeSelection(
            reason="wrong_retry",
            target_topic=topic,
            eligible_topic_ids=(COLLEGE_CHAIN_RULE_TOPIC_ID,),
            origin_wrong_question_id="wrong-1",
        ),
        difficulty=3,
        question_type="math_reasoning",
        scope_key="scope-a",
        scope_revision=9,
        mode="companion",
        source_question_id="wrong-1",
        target_binding={
            "learning_plan_id": "learning-plan-1",
            "learning_plan_revision": 4,
            "origin_wrong_question_id": "wrong-1",
        },
    )
    decorated = replace(
        original,
        learning_intent=blueprint.learning_intent,
        hypothesis_target=_hypothesis(),
        repair_strategy=blueprint.repair_strategy,
    )
    return original, decorated


def _context(
    *,
    blueprint_id: str = "chain.omit-inner.compare-steps.v1",
    repair_question_family_id: str = "",
) -> CognitiveQuestionValidationContext:
    original, decorated = _plans(blueprint_id=blueprint_id)
    blueprint = COGNITIVE_CATALOG_V1.get_blueprint(blueprint_id)
    assert blueprint is not None
    question = QuestionInstance(
        question_id="question-1",
        plan_id=decorated.plan_id,
        target_topic=decorated.target_topic,
        question_type=decorated.question_type,
        difficulty=decorated.difficulty,
        public_payload={
            "question": blueprint.question_text,
            "math_expression": blueprint.math_expression,
        },
        private_payload={
            "expected_answer": blueprint.expected_answer,
            "diagnostic_signature": blueprint.diagnostic_signature,
        },
        mode=decorated.mode,
        source_question_id=decorated.source_question_id,
        target_binding=decorated.target_binding,
        scope_key=decorated.scope_key,
        scope_revision=decorated.scope_revision,
        learning_intent=decorated.learning_intent,
        hypothesis_target=decorated.hypothesis_target,
        repair_strategy=decorated.repair_strategy,
        cognitive_decision_id="decision-1",
    )
    artifact = CognitiveQuestionArtifact(
        decision_id="decision-1",
        blueprint_id=blueprint.blueprint_id,
        question=question,
        question_family_id=blueprint.question_family_id,
        math_expression=blueprint.math_expression,
        expected_answer=blueprint.expected_answer,
        diagnostic_signature=blueprint.diagnostic_signature,
        competing_hypothesis_codes=blueprint.competing_hypothesis_codes,
    )
    return CognitiveQuestionValidationContext(
        original_plan=original,
        decorated_plan=decorated,
        artifact=artifact,
        repair_question_family_id=repair_question_family_id,
    )


def test_catalog_activates_only_omit_inner_derivative() -> None:
    assert COGNITIVE_CATALOG_V1.active_codes("calculus.chain_rule") == (
        "omit_inner_derivative",
    )
    assert COGNITIVE_CATALOG_V1.active_codes(COLLEGE_CHAIN_RULE_TOPIC_ID) == (
        "omit_inner_derivative",
    )
    assert not COGNITIVE_CATALOG_V1.is_active(
        COLLEGE_CHAIN_RULE_TOPIC_ID, "differentiate_inner_incorrectly"
    )
    assert not COGNITIVE_CATALOG_V1.is_active(
        COLLEGE_CHAIN_RULE_TOPIC_ID, "confuse_product_and_chain"
    )


def test_catalog_has_human_reviewed_probe_repair_and_transfer_blueprints() -> None:
    blueprints = COGNITIVE_CATALOG_V1.blueprints(
        COLLEGE_CHAIN_RULE_TOPIC_ID,
        hypothesis_code="omit_inner_derivative",
    )

    assert {blueprint.learning_intent for blueprint in blueprints} == {
        "misconception_probe",
        "misconception_repair",
        "transfer_check",
    }
    assert {blueprint.repair_strategy for blueprint in blueprints} == {
        "compare_steps",
        "complete_inner_derivative",
        "structure_classification",
        "minimal_change",
        "cross_form_transfer",
    }
    assert all(blueprint.expected_answer for blueprint in blueprints)
    assert all(blueprint.diagnostic_signature for blueprint in blueprints)


def test_v2_contract_fields_preserve_legacy_question_defaults() -> None:
    topic = TopicRef(id="legacy", name="Legacy")
    question = QuestionInstance(
        "question-legacy",
        "plan-legacy",
        topic,
        "short_answer",
        2,
        {"question": "Legacy question"},
    )
    hypothesis = HypothesisRef(
        "hypothesis-legacy",
        "legacy",
        "legacy_code",
        "hypothesized",
        0.5,
        "cognitive-v1",
    )

    assert question.learning_intent == "practice"
    assert question.hypothesis_target is None
    assert question.repair_strategy == ""
    assert question.cognitive_decision_id == ""
    assert question.cognitive_validator_version == ""
    assert hypothesis.source_snapshot_id == ""
    assert hypothesis.source_attempt_id == ""
    assert hypothesis.projection_generation == 0


def test_valid_human_blueprinted_probe_is_accepted_deterministically() -> None:
    result = DiagnosticQuestionValidator().validate(_context())

    assert result.valid
    assert result.errors == ()
    assert result.validation_id.startswith("cognitive-validation:")
    assert result.validator_version == DEFAULT_COGNITIVE_VALIDATOR_VERSION


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (lambda plan: replace(plan, difficulty=4), "ownership_changed:difficulty"),
        (
            lambda plan: replace(plan, scope_revision=10),
            "ownership_changed:scope_revision",
        ),
        (
            lambda plan: replace(
                plan,
                selection=replace(plan.selection, reason="recommended"),
            ),
            "ownership_changed:selection",
        ),
        (
            lambda plan: replace(
                plan,
                selection=replace(
                    plan.selection, origin_wrong_question_id="wrong-forged"
                ),
            ),
            "ownership_changed:selection",
        ),
        (
            lambda plan: replace(
                plan,
                target_binding={
                    **dict(plan.target_binding),
                    "learning_plan_revision": 5,
                },
            ),
            "ownership_changed:target_binding",
        ),
    ],
)
def test_validator_rejects_any_non_cognitive_plan_change(
    mutate, expected_error: str
) -> None:
    context = _context()
    changed = replace(context, decorated_plan=mutate(context.decorated_plan))

    result = DiagnosticQuestionValidator().validate(changed)

    assert not result.valid
    assert expected_error in result.errors


@pytest.mark.parametrize(
    ("field_name", "replacement", "expected_error"),
    [
        ("math_expression", "d/dx sin(x^3)", "math_expression_mismatch"),
        ("expected_answer", "cos(x^2)", "expected_answer_mismatch"),
        (
            "diagnostic_signature",
            "model-invented-signature",
            "diagnostic_signature_mismatch",
        ),
        (
            "competing_hypothesis_codes",
            ("invented_hypothesis",),
            "competing_hypotheses_mismatch",
        ),
        ("question_family_id", "model.family", "question_family_mismatch"),
    ],
)
def test_model_cannot_change_protected_blueprint_fields(
    field_name: str, replacement, expected_error: str
) -> None:
    context = _context()
    changed_artifact = replace(context.artifact, **{field_name: replacement})

    result = DiagnosticQuestionValidator().validate(
        replace(context, artifact=changed_artifact)
    )

    assert not result.valid
    assert expected_error in result.errors


def test_non_active_or_unconfirmed_hypothesis_cannot_change_a_real_question() -> None:
    context = _context()
    shadow = replace(
        context.decorated_plan,
        hypothesis_target=_hypothesis(code="differentiate_inner_incorrectly"),
    )
    hypothesized = replace(
        context.decorated_plan,
        hypothesis_target=_hypothesis(status="hypothesized"),
    )

    shadow_result = DiagnosticQuestionValidator().validate(
        replace(context, decorated_plan=shadow)
    )
    hypothesized_result = DiagnosticQuestionValidator().validate(
        replace(context, decorated_plan=hypothesized)
    )

    assert "hypothesis_not_active" in shadow_result.errors
    assert "hypothesis_not_supported" in hypothesized_result.errors


def test_active_hypothesis_requires_exact_projection_source() -> None:
    context = _context()
    target = context.decorated_plan.hypothesis_target
    assert target is not None
    plan = replace(
        context.decorated_plan,
        hypothesis_target=replace(
            target, source_snapshot_id="", projection_generation=0
        ),
    )

    result = DiagnosticQuestionValidator().validate(
        replace(context, decorated_plan=plan)
    )

    assert "hypothesis_source_not_exact" in result.errors


def test_question_plan_cannot_target_multiple_hypotheses() -> None:
    context = _context()
    plan = replace(
        context.decorated_plan,
        hypothesis_target=(_hypothesis(), _hypothesis()),  # type: ignore[arg-type]
    )

    result = DiagnosticQuestionValidator().validate(
        replace(context, decorated_plan=plan)
    )

    assert not result.valid
    assert "multiple_or_invalid_hypothesis_targets" in result.errors


def test_question_must_preserve_scope_plan_topic_difficulty_and_wrong_binding() -> None:
    context = _context()
    question = replace(
        context.artifact.question,
        plan_id="plan-forged",
        target_topic=TopicRef(id="other-topic", name="Other"),
        difficulty=5,
        scope_revision=10,
        target_binding={"origin_wrong_question_id": "other-wrong"},
    )
    artifact = replace(context.artifact, question=question)

    result = DiagnosticQuestionValidator().validate(
        replace(context, artifact=artifact)
    )

    assert {
        "question_plan_id_mismatch",
        "question_topic_mismatch",
        "question_difficulty_mismatch",
        "question_scope_revision_mismatch",
        "question_target_binding_mismatch",
    }.issubset(result.errors)


def test_answer_material_must_remain_private() -> None:
    context = _context()
    question = replace(
        context.artifact.question,
        public_payload={
            **dict(context.artifact.question.public_payload),
            "solution": context.artifact.expected_answer,
        },
    )

    result = DiagnosticQuestionValidator().validate(
        replace(context, artifact=replace(context.artifact, question=question))
    )

    assert "public_answer_material_exposed" in result.errors


def test_transfer_must_use_a_different_family_from_repair() -> None:
    context = _context(
        blueprint_id="chain.omit-inner.cross-form-transfer.v1",
        repair_question_family_id="chain.polynomial-power.cross-form-transfer",
    )

    result = DiagnosticQuestionValidator().validate(context)

    assert not result.valid
    assert "transfer_question_family_reused" in result.errors


def test_transfer_with_distinct_family_is_valid() -> None:
    context = _context(
        blueprint_id="chain.omit-inner.cross-form-transfer.v1",
        repair_question_family_id="chain.cos-cube.fill-factor",
    )

    result = DiagnosticQuestionValidator().validate(context)

    assert result.valid


def test_intervention_binding_captures_but_does_not_own_plan_facts() -> None:
    _, plan = _plans()

    binding = CognitiveInterventionBinding.from_plan(plan)

    assert binding.plan_id == "selection-1"
    assert binding.topic_id == COLLEGE_CHAIN_RULE_TOPIC_ID
    assert binding.selection_reason == "wrong_retry"
    assert binding.eligible_topic_ids == (COLLEGE_CHAIN_RULE_TOPIC_ID,)
    assert binding.learning_plan_id == "learning-plan-1"
    assert binding.learning_plan_revision == 4
    assert binding.scope_key == "scope-a"
    assert binding.scope_revision == 9
    assert binding.origin_wrong_question_id == "wrong-1"


def _event(event_type: str, **changes) -> CognitiveInterventionEvent:
    _, plan = _plans()
    values = {
        "event_id": f"event-{event_type}",
        "event_type": event_type,
        "decision_id": "decision-1",
        "hypothesis_target": _hypothesis(),
        "learning_intent": "misconception_probe",
        "repair_strategy": "compare_steps",
        "binding": CognitiveInterventionBinding.from_plan(plan),
        "created_at": "2026-03-11T10:00:00Z",
    }
    values.update(changes)
    return CognitiveInterventionEvent(**values)


def test_intervention_event_contract_separates_proposal_question_and_attempt() -> None:
    proposed = _event("intent_proposed")
    question = _event(
        "question_committed",
        question_id="question-1",
        blueprint_id="chain.omit-inner.compare-steps.v1",
        question_family_id="chain.sin-square.compare-steps",
        diagnostic_validation_id="validation-1",
        validator_version=DEFAULT_COGNITIVE_VALIDATOR_VERSION,
    )
    attempt = _event(
        "attempt_committed",
        question_id="question-1",
        attempt_id="attempt-1",
        blueprint_id="chain.omit-inner.compare-steps.v1",
        question_family_id="chain.sin-square.compare-steps",
        diagnostic_validation_id="validation-1",
        validator_version=DEFAULT_COGNITIVE_VALIDATOR_VERSION,
        evaluation_verdict="wrong",
    )

    assert proposed.attempt_id == ""
    assert question.evaluation_verdict == ""
    assert attempt.evaluation_verdict == "wrong"
    assert attempt.hypothesis_target.source_snapshot_id == "snapshot-7"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _event("intervention_abandoned"),
        lambda: _event("question_committed", question_id="question-1"),
        lambda: _event(
            "attempt_committed",
            question_id="question-1",
            blueprint_id="chain.omit-inner.compare-steps.v1",
            question_family_id="chain.sin-square.compare-steps",
            diagnostic_validation_id="validation-1",
            validator_version=DEFAULT_COGNITIVE_VALIDATOR_VERSION,
        ),
    ],
)
def test_intervention_events_fail_closed_when_required_facts_are_missing(
    factory,
) -> None:
    with pytest.raises(ValueError):
        factory()


def test_abandoned_event_records_reason_without_claiming_an_attempt() -> None:
    event = _event(
        "intervention_abandoned",
        question_id="question-1",
        abandonment_reason="scope_revision_changed",
    )

    assert event.abandonment_reason == "scope_revision_changed"
    assert event.attempt_id == ""
    assert event.evaluation_verdict == ""
