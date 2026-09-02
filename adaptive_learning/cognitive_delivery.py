"""Deterministic delivery helpers for the bounded V2 intervention loop.

The helpers in this module never choose a topic or infer a hypothesis.  They
turn an already-audited policy decision into one reviewed blueprint, immutable
ledger facts, and a validator input whose public half contains no answer
material.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .cognitive_catalog import (
    COGNITIVE_CATALOG_V1,
    CognitiveCatalog,
    CognitiveQuestionBlueprint,
)
from .cognitive_intervention import (
    CognitiveInterventionBinding,
    CognitiveInterventionEvent,
)
from .cognitive_policy import CognitivePolicyDecision, question_plan_ownership_fingerprint
from .cognitive_question_validation import (
    CognitiveQuestionArtifact,
    CognitiveQuestionValidationContext,
    CognitiveQuestionValidationResult,
    DiagnosticQuestionValidator,
)
from .contracts import QuestionInstance, QuestionPlan


@dataclass(frozen=True, slots=True)
class PreparedCognitiveIntervention:
    decision_id: str
    original_plan: QuestionPlan
    proposed_plan: QuestionPlan
    proposal_event: CognitiveInterventionEvent
    blueprint: CognitiveQuestionBlueprint | None = None

    @property
    def active(self) -> bool:
        return self.blueprint is not None


def prepare_cognitive_intervention(
    decision: CognitivePolicyDecision,
    *,
    catalog: CognitiveCatalog = COGNITIVE_CATALOG_V1,
    decision_id: str = "",
    created_at: str = "",
) -> PreparedCognitiveIntervention | None:
    """Prepare one Shadow proposal or one Active reviewed intervention."""

    proposed = decision.proposed_plan
    hypothesis = decision.selected_hypothesis
    if proposed is None or hypothesis is None or proposed.hypothesis_target != hypothesis:
        return None
    original = decision.original_plan
    if (
        proposed.target_topic.id != original.target_topic.id
        or question_plan_ownership_fingerprint(proposed)
        != question_plan_ownership_fingerprint(original)
        or decision.proposed_intent != proposed.learning_intent
        or decision.repair_strategy != proposed.repair_strategy
        or (decision.applied and decision.effective_plan != proposed)
        or (not decision.applied and decision.effective_plan != original)
    ):
        return None
    identity = str(decision_id or "").strip() or f"cognitive-decision:{uuid.uuid4().hex}"
    timestamp = str(created_at or "").strip() or _utc_iso()
    proposal = CognitiveInterventionEvent(
        event_id=f"cognitive-event:{uuid.uuid4().hex}",
        event_type="intent_proposed",
        decision_id=identity,
        hypothesis_target=hypothesis,
        learning_intent=proposed.learning_intent,
        repair_strategy=proposed.repair_strategy,
        binding=CognitiveInterventionBinding.from_plan(decision.original_plan),
        created_at=timestamp,
    )
    blueprint = None
    if decision.applied:
        if (
            hypothesis.status != "supported"
            or not hypothesis.source_snapshot_id
            or hypothesis.projection_generation <= 0
            or not catalog.is_active(hypothesis.topic_id, hypothesis.code)
        ):
            return None
        matches = tuple(
            item
            for item in catalog.blueprints(
                proposed.target_topic.id,
                hypothesis_code=hypothesis.code,
                learning_intent=proposed.learning_intent,
            )
            if item.repair_strategy == proposed.repair_strategy
        )
        if not matches:
            return None
        blueprint = sorted(matches, key=lambda item: item.blueprint_id)[0]
    return PreparedCognitiveIntervention(
        decision_id=identity,
        original_plan=decision.original_plan,
        proposed_plan=proposed,
        proposal_event=proposal,
        blueprint=blueprint,
    )


def reviewed_question_payload(
    prepared: PreparedCognitiveIntervention,
) -> dict[str, object]:
    """Build fixed, complete scoring material from a reviewed blueprint."""

    blueprint = prepared.blueprint
    if blueprint is None:
        raise ValueError("reviewed question payload requires an active blueprint")
    plan = prepared.proposed_plan
    return {
        "question": blueprint.question_text,
        "answer": blueprint.expected_answer,
        "reference_answer": blueprint.expected_answer,
        "accepted_answers": [blueprint.expected_answer],
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
        "question_type": plan.question_type,
        "difficulty": plan.difficulty,
        "target_topic_id": plan.target_topic.id,
        "hint": "",
        "math_expression": blueprint.math_expression,
        "diagnostic_signature": blueprint.diagnostic_signature,
        "cognitive_blueprint_id": blueprint.blueprint_id,
        "cognitive_question_family_id": blueprint.question_family_id,
        "competing_hypothesis_codes": list(
            blueprint.competing_hypothesis_codes
        ),
    }


def validate_reviewed_question(
    prepared: PreparedCognitiveIntervention,
    question: QuestionInstance,
    *,
    repair_question_family_id: str = "",
    validator: DiagnosticQuestionValidator | None = None,
) -> CognitiveQuestionValidationResult:
    """Validate protected mechanics without exposing private answer fields."""

    blueprint = prepared.blueprint
    if blueprint is None:
        raise ValueError("reviewed question validation requires an active blueprint")
    source_public = dict(question.public_payload)
    source_private = dict(question.private_payload)
    fixed_payload = reviewed_question_payload(prepared)

    def protected_value(key: str, default: object = "") -> object:
        private_present = key in source_private
        public_present = key in source_public
        if private_present and public_present and source_private[key] != source_public[key]:
            return None
        if private_present:
            return source_private[key]
        return source_public.get(key, default)

    expected_answer = str(protected_value("expected_answer") or "").strip()
    if not expected_answer:
        expected_answer = str(protected_value("answer") or "").strip()
    diagnostic_signature = str(
        protected_value("diagnostic_signature") or ""
    ).strip()
    competing_raw = protected_value("competing_hypothesis_codes", ())
    competing_hypotheses = (
        tuple(str(item or "").strip() for item in competing_raw)
        if isinstance(competing_raw, (tuple, list))
        else ()
    )
    blueprint_id = str(
        protected_value("cognitive_blueprint_id") or ""
    ).strip()
    question_family_id = str(
        protected_value("cognitive_question_family_id") or ""
    ).strip()
    math_expression = str(protected_value("math_expression") or "").strip()
    delivery_errors = [
        f"fixed_payload_mismatch:{key}"
        for key in (
            "answer",
            "reference_answer",
            "accepted_answers",
            "key_points",
            "rubric",
            "solution_steps",
            "math_equivalence_engine",
            "question_type",
            "difficulty",
            "target_topic_id",
            "math_expression",
            "diagnostic_signature",
            "cognitive_blueprint_id",
            "cognitive_question_family_id",
            "competing_hypothesis_codes",
        )
        if protected_value(key, None) != fixed_payload[key]
    ]
    public_payload = {
        key: value
        for key, value in dict(question.public_payload).items()
        if key
        not in {
            "answer",
            "reference_answer",
            "accepted_answers",
            "key_points",
            "rubric",
            "solution_steps",
            "math_equivalence_engine",
            "diagnostic_signature",
            "competing_hypothesis_codes",
        }
    }
    protected_question = replace(
        question,
        public_payload=public_payload,
        private_payload={
            "expected_answer": expected_answer,
            "diagnostic_signature": diagnostic_signature,
        },
    )
    artifact = CognitiveQuestionArtifact(
        decision_id=prepared.decision_id,
        blueprint_id=blueprint_id,
        question=protected_question,
        question_family_id=question_family_id,
        math_expression=math_expression,
        expected_answer=expected_answer,
        diagnostic_signature=diagnostic_signature,
        competing_hypothesis_codes=competing_hypotheses,
    )
    validated = (validator or DiagnosticQuestionValidator()).validate(
        CognitiveQuestionValidationContext(
            original_plan=prepared.original_plan,
            decorated_plan=prepared.proposed_plan,
            artifact=artifact,
            repair_question_family_id=repair_question_family_id,
        )
    )
    if not delivery_errors:
        return validated
    return CognitiveQuestionValidationResult(
        valid=False,
        errors=tuple(dict.fromkeys((*delivery_errors, *validated.errors))),
        validator_version=validated.validator_version,
    )


def committed_question_event(
    prepared: PreparedCognitiveIntervention,
    *,
    question_id: str,
    validation: CognitiveQuestionValidationResult,
    created_at: str = "",
) -> CognitiveInterventionEvent:
    blueprint = prepared.blueprint
    if blueprint is None or not validation.valid or not validation.validation_id:
        raise ValueError("question_committed requires a validated active blueprint")
    return replace(
        prepared.proposal_event,
        event_id=f"cognitive-event:{uuid.uuid4().hex}",
        event_type="question_committed",
        question_id=str(question_id or "").strip(),
        blueprint_id=blueprint.blueprint_id,
        question_family_id=blueprint.question_family_id,
        diagnostic_validation_id=validation.validation_id,
        validator_version=validation.validator_version,
        created_at=str(created_at or "").strip() or _utc_iso(),
    )


def abandoned_intervention_event(
    source: CognitiveInterventionEvent,
    *,
    reason: str,
    created_at: str = "",
) -> CognitiveInterventionEvent:
    return replace(
        source,
        event_id=f"cognitive-event:{uuid.uuid4().hex}",
        event_type="intervention_abandoned",
        attempt_id="",
        evaluation_verdict="",
        abandonment_reason=str(reason or "").strip(),
        created_at=str(created_at or "").strip() or _utc_iso(),
    )


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "PreparedCognitiveIntervention",
    "abandoned_intervention_event",
    "committed_question_event",
    "prepare_cognitive_intervention",
    "reviewed_question_payload",
    "validate_reviewed_question",
]
