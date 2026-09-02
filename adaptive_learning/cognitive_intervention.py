"""Provider-neutral contracts for the V2 cognitive intervention ledger.

These types describe immutable facts only.  They do not select a topic,
advance an intervention phase, or decide whether an answer confirms a
hypothesis; those responsibilities remain with Coach Policy and projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from .contracts import (
    HypothesisRef,
    LearningIntent,
    QuestionPlan,
    RepairStrategy,
    SelectionReason,
)

COGNITIVE_INTERVENTION_SCHEMA_VERSION = 1
DEFAULT_COGNITIVE_POLICY_VERSION = "cognitive-intent-policy-v2"

CognitiveInterventionEventType = Literal[
    "intent_proposed",
    "question_committed",
    "attempt_committed",
    "intervention_abandoned",
]
CognitiveInterventionIntent = Literal[
    "misconception_probe",
    "misconception_repair",
    "transfer_check",
]
CognitiveEvaluationVerdict = Literal["", "correct", "partial", "wrong", "dont_know"]


def _binding_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def hypothesis_ref_from_payload(
    value: object, *, topic_id: str
) -> HypothesisRef | None:
    """Strictly restore one hypothesis reference from private server data."""

    if isinstance(value, HypothesisRef):
        return value if value.topic_id == topic_id else None
    if not isinstance(value, Mapping):
        return None
    try:
        probability = float(value.get("probability"))
        generation = int(value.get("projection_generation") or 0)
    except (TypeError, ValueError):
        return None
    if (
        isinstance(value.get("probability"), bool)
        or isinstance(value.get("projection_generation"), bool)
        or not 0.0 <= probability <= 1.0
    ):
        return None
    text_fields = {
        key: str(value.get(key) or "").strip()
        for key in (
            "hypothesis_id",
            "topic_id",
            "code",
            "status",
            "model_version",
            "source_snapshot_id",
            "source_attempt_id",
        )
    }
    if (
        text_fields["topic_id"] != topic_id
        or not all(
            text_fields[key]
            for key in ("hypothesis_id", "code", "status", "model_version")
        )
        or generation < 0
    ):
        return None
    return HypothesisRef(
        hypothesis_id=text_fields["hypothesis_id"],
        topic_id=text_fields["topic_id"],
        code=text_fields["code"],
        status=text_fields["status"],
        probability=probability,
        model_version=text_fields["model_version"],
        source_snapshot_id=text_fields["source_snapshot_id"],
        source_attempt_id=text_fields["source_attempt_id"],
        projection_generation=generation,
    )


def hypothesis_ref_payload(value: HypothesisRef) -> dict[str, Any]:
    """Serialize exact hypothesis provenance for private persistence."""

    return {
        "hypothesis_id": value.hypothesis_id,
        "topic_id": value.topic_id,
        "code": value.code,
        "status": value.status,
        "probability": value.probability,
        "model_version": value.model_version,
        "source_snapshot_id": value.source_snapshot_id,
        "source_attempt_id": value.source_attempt_id,
        "projection_generation": value.projection_generation,
    }


@dataclass(frozen=True, slots=True)
class CognitiveInterventionBinding:
    """Ownership facts captured without transferring ownership to cognition."""

    plan_id: str
    topic_id: str
    selection_reason: SelectionReason
    eligible_topic_ids: tuple[str, ...] = ()
    learning_plan_id: str = ""
    learning_plan_revision: int = 0
    scope_key: str = ""
    scope_revision: int = 0
    origin_wrong_question_id: str = ""
    source_question_id: str = ""
    target_binding: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_plan(cls, plan: QuestionPlan) -> CognitiveInterventionBinding:
        target_binding = dict(plan.target_binding)
        return cls(
            plan_id=plan.plan_id,
            topic_id=plan.target_topic.id,
            selection_reason=plan.selection.reason,
            eligible_topic_ids=plan.selection.eligible_topic_ids,
            learning_plan_id=str(target_binding.get("learning_plan_id") or "").strip(),
            learning_plan_revision=_binding_int(
                target_binding.get("learning_plan_revision")
            ),
            scope_key=plan.scope_key,
            scope_revision=plan.scope_revision,
            origin_wrong_question_id=str(
                plan.selection.origin_wrong_question_id or ""
            ).strip(),
            source_question_id=plan.source_question_id,
            target_binding=target_binding,
        )


@dataclass(frozen=True, slots=True)
class CognitiveInterventionEvent:
    """One append-only event about a single hypothesis intervention.

    Validation is intentionally structural.  In particular,
    ``evaluation_verdict`` is a server evaluation fact, not a state transition
    chosen by an LLM.
    """

    event_id: str
    event_type: CognitiveInterventionEventType
    decision_id: str
    hypothesis_target: HypothesisRef
    learning_intent: CognitiveInterventionIntent
    repair_strategy: RepairStrategy
    binding: CognitiveInterventionBinding
    created_at: str
    question_id: str = ""
    attempt_id: str = ""
    blueprint_id: str = ""
    question_family_id: str = ""
    diagnostic_validation_id: str = ""
    evaluation_verdict: CognitiveEvaluationVerdict = ""
    abandonment_reason: str = ""
    policy_version: str = DEFAULT_COGNITIVE_POLICY_VERSION
    validator_version: str = ""
    schema_version: int = COGNITIVE_INTERVENTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.event_type not in {
            "intent_proposed",
            "question_committed",
            "attempt_committed",
            "intervention_abandoned",
        }:
            raise ValueError("invalid cognitive intervention event_type")
        if not is_cognitive_intervention_intent(self.learning_intent):
            raise ValueError("invalid cognitive intervention learning_intent")
        if not self.event_id.strip() or not self.decision_id.strip():
            raise ValueError("cognitive intervention event identity is required")
        if not self.created_at.strip():
            raise ValueError("cognitive intervention created_at is required")
        if not isinstance(self.hypothesis_target, HypothesisRef):
            raise ValueError("one cognitive intervention hypothesis is required")
        if self.binding.topic_id != self.hypothesis_target.topic_id:
            raise ValueError("intervention hypothesis must match the bound topic")
        if not self.repair_strategy:
            raise ValueError("cognitive intervention repair_strategy is required")
        if self.event_type == "intent_proposed":
            if self.attempt_id or self.evaluation_verdict:
                raise ValueError("intent_proposed cannot contain attempt results")
            return
        if self.event_type == "intervention_abandoned":
            if not self.abandonment_reason.strip():
                raise ValueError("intervention_abandoned requires a reason")
            if self.attempt_id or self.evaluation_verdict:
                raise ValueError("abandoned intervention cannot contain attempt results")
            return
        if not all(
            (
                self.question_id.strip(),
                self.blueprint_id.strip(),
                self.question_family_id.strip(),
                self.diagnostic_validation_id.strip(),
                self.validator_version.strip(),
            )
        ):
            raise ValueError("committed cognitive question provenance is required")
        if self.event_type == "question_committed":
            if self.attempt_id or self.evaluation_verdict:
                raise ValueError("question_committed cannot contain attempt results")
            return
        if not self.attempt_id.strip() or not self.evaluation_verdict:
            raise ValueError("attempt_committed requires an evaluated attempt")


def is_cognitive_intervention_intent(
    intent: LearningIntent,
) -> bool:
    """Return whether an intent belongs to the bounded V2 intervention flow."""

    return intent in {
        "misconception_probe",
        "misconception_repair",
        "transfer_check",
    }


__all__ = [
    "COGNITIVE_INTERVENTION_SCHEMA_VERSION",
    "DEFAULT_COGNITIVE_POLICY_VERSION",
    "CognitiveEvaluationVerdict",
    "CognitiveInterventionBinding",
    "CognitiveInterventionEvent",
    "CognitiveInterventionEventType",
    "CognitiveInterventionIntent",
    "hypothesis_ref_from_payload",
    "hypothesis_ref_payload",
    "is_cognitive_intervention_intent",
]
