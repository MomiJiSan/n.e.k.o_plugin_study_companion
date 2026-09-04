"""Bounded V2.1 retention contract for the reviewed chain-rule mechanism."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from .cognitive_catalog import COGNITIVE_CATALOG_V1
from .cognitive_versions import (
    DEFAULT_COGNITIVE_VERSION_SET,
    get_cognitive_version_set,
)
from .contracts import LearningActionCandidate, QuestionPlan

RETENTION_BLUEPRINT_VERSION = "cognitive-retention-blueprints-v1"
RETENTION_VALIDATOR_VERSION = "cognitive-retention-validator-v1"
ACTIVE_RETENTION_HYPOTHESIS = "omit_inner_derivative"
RETENTION_COGNITIVE_STRATEGY = "independent_delayed_retention"

RetentionDisposition = Literal[
    "resolved",
    "relapse",
    "reschedule",
    "ordinary_evidence",
]


@dataclass(frozen=True, slots=True)
class RetentionBlueprint:
    blueprint_id: str
    topic_id: str
    hypothesis_code: str
    question_family_id: str
    independence_group: str
    question_text: str
    math_expression: str
    expected_answer: str
    diagnostic_signature: str
    key_points: tuple[str, ...] = ()
    solution_steps: tuple[str, ...] = ()
    blueprint_version: str = RETENTION_BLUEPRINT_VERSION


CHAIN_RULE_RETENTION_BLUEPRINT = RetentionBlueprint(
    blueprint_id="chain.omit-inner.retention-exp-affine.v1",
    topic_id="calculus.chain_rule",
    hypothesis_code=ACTIVE_RETENTION_HYPOTHESIS,
    question_family_id="chain.exp-affine.retention",
    independence_group="chain.exponential-affine",
    question_text="Differentiate exp(5x - 2).",
    math_expression="d/dx exp(5*x-2)",
    expected_answer="5*exp(5*x-2)",
    diagnostic_signature=(
        "composition:exp(5*x-2)|outer:exp(5*x-2)|inner:5|retention:true"
    ),
    key_points=(
        "Differentiate the outer exponential function.",
        "Multiply by the derivative of the inner affine expression.",
    ),
    solution_steps=(
        "Keep the inner expression inside the exponential.",
        "Multiply by the inner derivative.",
    ),
)

CHAIN_RULE_RETENTION_BLUEPRINT_TRIG = RetentionBlueprint(
    blueprint_id="chain.omit-inner.retention-sin-affine.v1",
    topic_id="calculus.chain_rule",
    hypothesis_code=ACTIVE_RETENTION_HYPOTHESIS,
    question_family_id="chain.sin-affine.retention",
    independence_group="chain.trigonometric-affine",
    question_text="Differentiate sin(4x + 1).",
    math_expression="d/dx sin(4*x+1)",
    expected_answer="4*cos(4*x+1)",
    diagnostic_signature=(
        "composition:sin(4*x+1)|outer:cos(4*x+1)|inner:4|retention:true"
    ),
    key_points=(
        "Differentiate the outer sine function to cosine.",
        "Multiply by the derivative of the inner affine expression.",
    ),
    solution_steps=(
        "Keep the inner expression inside the cosine function.",
        "Multiply by the inner derivative.",
    ),
)

CHAIN_RULE_RETENTION_BLUEPRINTS = (
    CHAIN_RULE_RETENTION_BLUEPRINT,
    CHAIN_RULE_RETENTION_BLUEPRINT_TRIG,
)


@dataclass(frozen=True, slots=True)
class RetentionValidationInput:
    episode_id: str
    obligation_id: str
    hypothesis_code: str
    verdict: str
    observed_hypothesis_code: str = ""
    used_hint: bool | None = None
    evaluator_type: str = ""
    evaluator_version: str = ""
    evaluator_confidence: float | None = None
    answered_at: str = ""
    not_before: str = ""
    eligibility_until: str = ""
    question_family_id: str = ""
    transfer_question_family_id: str = ""
    independence_group: str = ""
    previous_question_family_ids: tuple[str, ...] = ()
    previous_independence_groups: tuple[str, ...] = ()
    blueprint_version: str = ""
    validator_version: str = ""


@dataclass(frozen=True, slots=True)
class RetentionValidationResult:
    disposition: RetentionDisposition
    certified: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetentionActionProposal:
    """A store fact converted into a bounded Coach candidate.

    This object is deliberately not a claim.  The obligation is leased only
    after the Coach accepts ``candidate`` so an unselected proposal never
    occupies worker capacity.
    """

    candidate: LearningActionCandidate
    episode_id: str
    obligation_id: str
    hypothesis_id: str
    hypothesis_code: str
    model_version: str
    transfer_question_family_id: str
    blueprint: RetentionBlueprint = CHAIN_RULE_RETENTION_BLUEPRINT


@dataclass(frozen=True, slots=True)
class PreparedRetentionQuestion:
    proposal: RetentionActionProposal
    claim_id: str
    claim_token: str
    worker_id: str
    lease_expires_at: str


def _utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_retention_action_proposal(
    obligation: Mapping[str, Any],
    episode: Mapping[str, Any],
    *,
    version_set: str,
    projection_current: bool,
    as_of: datetime | None = None,
) -> RetentionActionProposal | None:
    """Fail closed unless one pending fact is safe to offer to Coach."""

    versions = get_cognitive_version_set(version_set)
    if (
        version_set != DEFAULT_COGNITIVE_VERSION_SET
        or versions is None
        or not projection_current
        or str(obligation.get("status") or "") != "pending"
        or str(obligation.get("obligation_type") or "") != "retention"
        or str(episode.get("status") or "") != "open"
    ):
        return None
    obligation_id = str(obligation.get("obligation_id") or "").strip()
    episode_id = str(obligation.get("episode_id") or "").strip()
    hypothesis_id = str(obligation.get("hypothesis_id") or "").strip()
    topic_id = str(obligation.get("topic_id") or "").strip()
    hypothesis_code = str(obligation.get("hypothesis_code") or "").strip()
    if (
        not all((obligation_id, episode_id, hypothesis_id, topic_id))
        or episode_id != str(episode.get("episode_id") or "").strip()
        or hypothesis_id != str(episode.get("hypothesis_id") or "").strip()
        or topic_id != str(episode.get("topic_id") or "").strip()
        or hypothesis_code != ACTIVE_RETENTION_HYPOTHESIS
        or hypothesis_code != str(episode.get("hypothesis_code") or "").strip()
        or COGNITIVE_CATALOG_V1.canonical_topic_id(topic_id)
        != CHAIN_RULE_RETENTION_BLUEPRINT.topic_id
    ):
        return None
    model_version = str(episode.get("model_version") or "").strip()
    if model_version != versions.projection_version:
        return None
    not_before = str(obligation.get("not_before") or "").strip()
    due_by = str(obligation.get("due_by") or "").strip()
    eligibility_until = str(obligation.get("eligibility_until") or "").strip()
    available = _utc(not_before)
    due = _utc(due_by)
    expires = _utc(eligibility_until)
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if (
        available is None
        or due is None
        or expires is None
        or not available < due < expires
        or now < available
        or now >= expires
    ):
        return None
    transfer_family = str(
        episode.get("transfer_question_family_id") or ""
    ).strip()
    prior_families = {
        str(item or "").strip()
        for item in (obligation.get("previous_question_family_ids") or ())
        if str(item or "").strip()
    }
    prior_groups = {
        str(item or "").strip()
        for item in (obligation.get("previous_independence_groups") or ())
        if str(item or "").strip()
    }
    blueprint = next(
        (
            item
            for item in CHAIN_RULE_RETENTION_BLUEPRINTS
            if item.question_family_id != transfer_family
            and item.question_family_id not in prior_families
            and item.independence_group not in prior_groups
        ),
        None,
    )
    if not transfer_family or blueprint is None:
        return None
    overdue = now >= due
    candidate = LearningActionCandidate(
        source="cognitive_retention",
        topic_id=topic_id,
        intent="retention_check",
        urgency=1.0 if overdue else 0.6,
        expected_learning_gain=0.6,
        information_gain=0.7,
        evidence_refs=tuple(
            value
            for value in (
                episode_id,
                str(episode.get("source_attempt_id") or "").strip(),
                str(episode.get("source_event_id") or "").strip(),
            )
            if value
        ),
        satisfies=(f"cognitive_retention:{hypothesis_id}",),
        not_before=not_before,
        due_by=due_by,
        expires_at=eligibility_until,
        obligation_refs=(obligation_id,),
    )
    return RetentionActionProposal(
        candidate=candidate,
        episode_id=episode_id,
        obligation_id=obligation_id,
        hypothesis_id=hypothesis_id,
        hypothesis_code=hypothesis_code,
        model_version=model_version,
        transfer_question_family_id=transfer_family,
        blueprint=blueprint,
    )


def prepare_retention_question(
    plan: QuestionPlan,
    proposal: RetentionActionProposal,
    claim: Mapping[str, Any],
) -> PreparedRetentionQuestion | None:
    """Bind an accepted plan to the exact CAS claim that it selected."""

    if (
        plan.learning_intent != "retention_check"
        or plan.obligation_refs != (proposal.obligation_id,)
        or plan.cognitive_strategy != RETENTION_COGNITIVE_STRATEGY
        or COGNITIVE_CATALOG_V1.canonical_topic_id(plan.target_topic.id)
        != proposal.blueprint.topic_id
        or str(claim.get("obligation_id") or "").strip()
        != proposal.obligation_id
        or str(claim.get("episode_id") or "").strip() != proposal.episode_id
        or str(claim.get("hypothesis_code") or "").strip()
        != proposal.hypothesis_code
        or str(claim.get("status") or "").strip() != "claimed"
    ):
        return None
    claim_id = str(claim.get("claim_id") or "").strip()
    claim_token = str(claim.get("claim_token") or "").strip()
    worker_id = str(claim.get("worker_id") or "").strip()
    lease_expires_at = str(claim.get("lease_expires_at") or "").strip()
    if not all((claim_id, claim_token, worker_id, lease_expires_at)):
        return None
    return PreparedRetentionQuestion(
        proposal=proposal,
        claim_id=claim_id,
        claim_token=claim_token,
        worker_id=worker_id,
        lease_expires_at=lease_expires_at,
    )


def retention_question_payload(
    prepared: PreparedRetentionQuestion,
    *,
    topic_id: str,
) -> dict[str, Any]:
    """Return the fixed, human-reviewed retention question contract."""

    blueprint = prepared.proposal.blueprint
    if COGNITIVE_CATALOG_V1.canonical_topic_id(topic_id) != blueprint.topic_id:
        raise ValueError("retention blueprint topic mismatch")
    return {
        "question": blueprint.question_text,
        "answer": blueprint.expected_answer,
        "reference_answer": blueprint.expected_answer,
        "accepted_answers": [blueprint.expected_answer],
        "key_points": list(blueprint.key_points),
        "rubric": {"chain_rule_retention": 1.0},
        "solution_steps": list(blueprint.solution_steps),
        "math_equivalence_engine": {"enabled": False},
        "question_type": "math_reasoning",
        "difficulty": 3,
        "target_topic_id": topic_id,
        "hint": "",
        "math_expression": blueprint.math_expression,
        "diagnostic_signature": blueprint.diagnostic_signature,
        "cognitive_blueprint_id": blueprint.blueprint_id,
        "cognitive_question_family_id": blueprint.question_family_id,
        "cognitive_independence_group": blueprint.independence_group,
    }


def validate_retention_question_payload(
    prepared: PreparedRetentionQuestion,
    payload: Mapping[str, Any],
    *,
    topic_id: str,
) -> tuple[str, ...]:
    """Reject any generated or mutated retention mechanics."""

    try:
        expected = retention_question_payload(prepared, topic_id=topic_id)
    except ValueError:
        return ("retention_topic_mismatch",)
    protected = (
        "question",
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
        "hint",
        "math_expression",
        "diagnostic_signature",
        "cognitive_blueprint_id",
        "cognitive_question_family_id",
        "cognitive_independence_group",
    )
    return tuple(
        f"fixed_payload_mismatch:{field}"
        for field in protected
        if payload.get(field) != expected[field]
    )


class RetentionValidator:
    """Certify only delayed, independent, unassisted reviewed questions."""

    def validate(self, item: RetentionValidationInput) -> RetentionValidationResult:
        errors: list[str] = []
        answered_at = _utc(item.answered_at)
        not_before = _utc(item.not_before)
        eligibility_until = _utc(item.eligibility_until)
        if not item.episode_id or not item.obligation_id:
            errors.append("missing_episode_or_obligation")
        if item.hypothesis_code != ACTIVE_RETENTION_HYPOTHESIS:
            errors.append("hypothesis_not_active")
        if item.used_hint is not False:
            errors.append("hint_used_or_unknown")
        if not item.evaluator_type or not item.evaluator_version:
            errors.append("evaluator_provenance_missing")
        if item.evaluator_confidence is None or not 0.0 <= item.evaluator_confidence <= 1.0:
            errors.append("evaluator_confidence_invalid")
        if item.blueprint_version != RETENTION_BLUEPRINT_VERSION:
            errors.append("blueprint_version_mismatch")
        if item.validator_version != RETENTION_VALIDATOR_VERSION:
            errors.append("validator_version_mismatch")
        if answered_at is None or not_before is None or eligibility_until is None:
            errors.append("retention_window_invalid")
        elif answered_at < not_before:
            errors.append("retention_too_early")
        elif answered_at > eligibility_until:
            errors.append("retention_window_expired")
        if not item.question_family_id:
            errors.append("question_family_missing")
        if item.question_family_id == item.transfer_question_family_id:
            errors.append("transfer_question_family_reused")
        if item.question_family_id in item.previous_question_family_ids:
            errors.append("retention_question_family_reused")
        if (
            not item.independence_group
            or item.independence_group in item.previous_independence_groups
        ):
            errors.append("independence_group_reused")
        if errors:
            return RetentionValidationResult(
                disposition="ordinary_evidence",
                certified=False,
                reasons=tuple(dict.fromkeys(errors)),
            )
        if item.observed_hypothesis_code == item.hypothesis_code:
            return RetentionValidationResult("relapse", True)
        if item.verdict == "correct":
            return RetentionValidationResult("resolved", True)
        if item.verdict in {"partial", "dont_know"} or item.observed_hypothesis_code:
            return RetentionValidationResult("reschedule", True)
        return RetentionValidationResult(
            "reschedule", True, ("uncertified_wrong_mechanism",)
        )


__all__ = [
    "ACTIVE_RETENTION_HYPOTHESIS",
    "CHAIN_RULE_RETENTION_BLUEPRINT",
    "CHAIN_RULE_RETENTION_BLUEPRINTS",
    "CHAIN_RULE_RETENTION_BLUEPRINT_TRIG",
    "RETENTION_BLUEPRINT_VERSION",
    "RETENTION_COGNITIVE_STRATEGY",
    "RETENTION_VALIDATOR_VERSION",
    "PreparedRetentionQuestion",
    "RetentionActionProposal",
    "RetentionBlueprint",
    "RetentionDisposition",
    "RetentionValidationInput",
    "RetentionValidationResult",
    "RetentionValidator",
    "build_retention_action_proposal",
    "prepare_retention_question",
    "retention_question_payload",
    "validate_retention_question_payload",
]
