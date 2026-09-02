"""Fail-closed validation for bounded V2 cognitive questions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from typing import Any, Mapping

from .cognitive_catalog import COGNITIVE_CATALOG_V1, CognitiveCatalog
from .cognitive_intervention import is_cognitive_intervention_intent
from .contracts import HypothesisRef, QuestionInstance, QuestionPlan

DEFAULT_COGNITIVE_VALIDATOR_VERSION = "cognitive-question-validator-v2"

_COGNITIVE_PLAN_FIELDS = frozenset(
    {"learning_intent", "hypothesis_target", "repair_strategy"}
)
_PUBLIC_ANSWER_KEYS = frozenset(
    {
        "answer_reference",
        "correct_answer",
        "expected_answer",
        "private_payload",
        "reference_answer",
        "rubric",
        "solution",
    }
)


@dataclass(frozen=True, slots=True)
class CognitiveQuestionArtifact:
    """Protected generator output paired with one generated question."""

    decision_id: str
    blueprint_id: str
    question: QuestionInstance
    question_family_id: str
    math_expression: str
    expected_answer: str
    diagnostic_signature: str
    competing_hypothesis_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CognitiveQuestionValidationContext:
    """Original ownership decision and its cognitive-only decoration."""

    original_plan: QuestionPlan
    decorated_plan: QuestionPlan
    artifact: CognitiveQuestionArtifact
    repair_question_family_id: str = ""


@dataclass(frozen=True, slots=True)
class CognitiveQuestionValidationResult:
    valid: bool
    errors: tuple[str, ...] = ()
    validation_id: str = ""
    validator_version: str = DEFAULT_COGNITIVE_VALIDATOR_VERSION


def _mapping_value(mapping: Mapping[str, Any], key: str) -> str:
    return str(mapping.get(key) or "").strip()


def _find_public_answer_keys(value: Any, *, path: str = "public_payload") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            child_path = f"{path}.{key}" if key else path
            if key in _PUBLIC_ANSWER_KEYS:
                found.append(child_path)
            found.extend(_find_public_answer_keys(child, path=child_path))
    elif isinstance(value, (tuple, list)):
        for index, child in enumerate(value):
            found.extend(_find_public_answer_keys(child, path=f"{path}[{index}]"))
    return found


def _validation_id(
    context: CognitiveQuestionValidationContext,
    *,
    validator_version: str,
) -> str:
    hypothesis = context.decorated_plan.hypothesis_target
    payload = {
        "blueprint_id": context.artifact.blueprint_id,
        "decision_id": context.artifact.decision_id,
        "hypothesis_id": hypothesis.hypothesis_id if hypothesis else "",
        "plan_id": context.decorated_plan.plan_id,
        "projection_generation": (
            hypothesis.projection_generation if hypothesis is not None else 0
        ),
        "question_id": context.artifact.question.question_id,
        "validator_version": validator_version,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"cognitive-validation:{digest}"


class DiagnosticQuestionValidator:
    """Validate one human-blueprinted question without making policy choices."""

    def __init__(
        self,
        *,
        catalog: CognitiveCatalog = COGNITIVE_CATALOG_V1,
        validator_version: str = DEFAULT_COGNITIVE_VALIDATOR_VERSION,
    ) -> None:
        if not validator_version.strip():
            raise ValueError("cognitive validator_version is required")
        self._catalog = catalog
        self._validator_version = validator_version

    def validate(
        self, context: CognitiveQuestionValidationContext
    ) -> CognitiveQuestionValidationResult:
        errors: list[str] = []
        original = context.original_plan
        decorated = context.decorated_plan
        artifact = context.artifact
        question = artifact.question

        for contract_field in fields(QuestionPlan):
            if contract_field.name in _COGNITIVE_PLAN_FIELDS:
                continue
            if getattr(original, contract_field.name) != getattr(
                decorated, contract_field.name
            ):
                errors.append(f"ownership_changed:{contract_field.name}")

        if not is_cognitive_intervention_intent(decorated.learning_intent):
            errors.append("unsupported_learning_intent")
        hypothesis = decorated.hypothesis_target
        if hypothesis is None:
            errors.append("missing_hypothesis_target")
        elif not isinstance(hypothesis, HypothesisRef):
            errors.append("multiple_or_invalid_hypothesis_targets")
            hypothesis = None
        else:
            if hypothesis.topic_id != decorated.target_topic.id:
                errors.append("hypothesis_topic_mismatch")
            if hypothesis.status != "supported":
                errors.append("hypothesis_not_supported")
            if not self._catalog.is_active(hypothesis.topic_id, hypothesis.code):
                errors.append("hypothesis_not_active")
            if (
                not hypothesis.source_snapshot_id.strip()
                or hypothesis.projection_generation < 1
            ):
                errors.append("hypothesis_source_not_exact")

        blueprint = self._catalog.get_blueprint(artifact.blueprint_id)
        if blueprint is None:
            errors.append("unknown_blueprint")
        else:
            if not self._catalog.is_active(
                decorated.target_topic.id, blueprint.hypothesis_code
            ):
                errors.append("blueprint_not_active")
            if hypothesis is not None and blueprint.hypothesis_code != hypothesis.code:
                errors.append("blueprint_hypothesis_mismatch")
            if blueprint.learning_intent != decorated.learning_intent:
                errors.append("blueprint_intent_mismatch")
            if blueprint.repair_strategy != decorated.repair_strategy:
                errors.append("blueprint_repair_strategy_mismatch")
            if blueprint.question_family_id != artifact.question_family_id:
                errors.append("question_family_mismatch")
            if blueprint.math_expression != artifact.math_expression:
                errors.append("math_expression_mismatch")
            if blueprint.expected_answer != artifact.expected_answer:
                errors.append("expected_answer_mismatch")
            if blueprint.diagnostic_signature != artifact.diagnostic_signature:
                errors.append("diagnostic_signature_mismatch")
            if (
                blueprint.competing_hypothesis_codes
                != artifact.competing_hypothesis_codes
            ):
                errors.append("competing_hypotheses_mismatch")

        if not artifact.decision_id.strip():
            errors.append("missing_cognitive_decision_id")
        if question.cognitive_decision_id != artifact.decision_id:
            errors.append("cognitive_decision_id_mismatch")
        if question.plan_id != decorated.plan_id:
            errors.append("question_plan_id_mismatch")
        if question.target_topic.id != decorated.target_topic.id:
            errors.append("question_topic_mismatch")
        if question.question_type != decorated.question_type:
            errors.append("question_type_mismatch")
        if question.difficulty != decorated.difficulty:
            errors.append("question_difficulty_mismatch")
        if question.mode != decorated.mode:
            errors.append("question_mode_mismatch")
        if question.source_question_id != decorated.source_question_id:
            errors.append("question_source_binding_mismatch")
        if dict(question.target_binding) != dict(decorated.target_binding):
            errors.append("question_target_binding_mismatch")
        if question.scope_key != decorated.scope_key:
            errors.append("question_scope_key_mismatch")
        if question.scope_revision != decorated.scope_revision:
            errors.append("question_scope_revision_mismatch")
        if question.learning_intent != decorated.learning_intent:
            errors.append("question_intent_mismatch")
        if question.hypothesis_target != decorated.hypothesis_target:
            errors.append("question_hypothesis_mismatch")
        if question.repair_strategy != decorated.repair_strategy:
            errors.append("question_repair_strategy_mismatch")
        if question.cognitive_validator_version not in {
            "",
            self._validator_version,
        }:
            errors.append("stale_validator_version")

        if _mapping_value(question.public_payload, "math_expression") != (
            artifact.math_expression
        ):
            errors.append("public_math_expression_mismatch")
        if _mapping_value(question.private_payload, "expected_answer") != (
            artifact.expected_answer
        ):
            errors.append("private_expected_answer_mismatch")
        if _mapping_value(question.private_payload, "diagnostic_signature") != (
            artifact.diagnostic_signature
        ):
            errors.append("private_diagnostic_signature_mismatch")
        if _find_public_answer_keys(question.public_payload):
            errors.append("public_answer_material_exposed")

        if decorated.learning_intent == "transfer_check":
            if not context.repair_question_family_id.strip():
                errors.append("missing_repair_question_family")
            elif context.repair_question_family_id == artifact.question_family_id:
                errors.append("transfer_question_family_reused")

        unique_errors = tuple(dict.fromkeys(errors))
        if unique_errors:
            return CognitiveQuestionValidationResult(
                valid=False,
                errors=unique_errors,
                validator_version=self._validator_version,
            )
        return CognitiveQuestionValidationResult(
            valid=True,
            validation_id=_validation_id(
                context,
                validator_version=self._validator_version,
            ),
            validator_version=self._validator_version,
        )


__all__ = [
    "DEFAULT_COGNITIVE_VALIDATOR_VERSION",
    "CognitiveQuestionArtifact",
    "CognitiveQuestionValidationContext",
    "CognitiveQuestionValidationResult",
    "DiagnosticQuestionValidator",
]
