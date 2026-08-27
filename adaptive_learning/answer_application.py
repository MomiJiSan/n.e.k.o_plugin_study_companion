"""Typed answer-assessment orchestration, independent of plugin entries."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, cast

from .assessment import AssessmentDecision, AssessmentEngine, AssessmentRequest
from .contracts import AssessmentResult, EvaluationResult, QuestionInstance


@dataclass(frozen=True, slots=True)
class AssessmentValidationResult:
    """Outcome from a structural or semantic assessment validator."""

    valid: bool
    errors: tuple[str, ...] = ()


class AssessmentValidator(Protocol):
    """Validate an evaluator decision before it reaches an entry adapter."""

    def validate(
        self,
        request: AssessmentRequest,
        decision: AssessmentDecision,
    ) -> AssessmentValidationResult | Awaitable[AssessmentValidationResult]: ...


AssessmentRepair: TypeAlias = Callable[
    [AssessmentRequest, AssessmentDecision, AssessmentValidationResult],
    AssessmentDecision | Awaitable[AssessmentDecision],
]


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    """Narrow a runtime mapping without leaking ``Unknown`` into strict code."""

    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


class AnswerAssessmentService:
    """Prefer certain deterministic decisions, then use the configured rubric.

    The service performs at most one validator-requested repair.  It does not
    choose models, translate entry errors, or expose private question payloads.
    """

    def __init__(
        self,
        engine: AssessmentEngine,
        *,
        validator: AssessmentValidator | None = None,
        repair: AssessmentRepair | None = None,
    ) -> None:
        if repair is not None and validator is None:
            raise ValueError("assessment repair requires a validator")
        self._engine = engine
        self._validator = validator
        self._repair = repair

    @staticmethod
    def request_for(
        question: QuestionInstance,
        learner_answer: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> AssessmentRequest:
        """Build an internal request from typed question fields only."""

        public_payload = dict(question.public_payload)
        private_payload = dict(question.private_payload)
        answer_spec = private_payload.get("answer_spec", public_payload.get("answer_spec"))
        equivalence_engine = private_payload.get(
            "math_equivalence_engine", public_payload.get("math_equivalence_engine")
        )
        request_context: dict[str, Any] = {
            "question_type": question.question_type,
            "accepted_answers": private_payload.get(
                "accepted_answers", public_payload.get("accepted_answers")
            ),
            "answer_spec": _mapping_or_empty(answer_spec),
            "closed_world": private_payload.get(
                "closed_world", public_payload.get("closed_world")
            ) is True,
            "math_equivalence_engine": _mapping_or_empty(equivalence_engine),
            **dict(context or {}),
        }
        return AssessmentRequest(
            question=str(public_payload.get("question") or public_payload.get("prompt") or ""),
            answer=learner_answer,
            expected_answer=str(
                private_payload.get("answer")
                or private_payload.get("expected_answer")
                or ""
            ),
            mode=question.mode,
            context=request_context,
        )

    async def assess(
        self,
        question: QuestionInstance,
        learner_answer: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> AssessmentResult:
        """Return the canonical evaluator outcome for one learner answer."""

        request = self.request_for(question, learner_answer, context=context)
        result = await self.try_assess_request(request)
        deterministic = result is not None
        decision = None if result is None else _decision_from_result(result)
        if decision is None:
            decision = await self._engine.assess(request)
        validation_errors: tuple[str, ...] = ()
        if self._validator is not None:
            validation = await _resolve(self._validator.validate(request, decision))
            if not isinstance(validation, AssessmentValidationResult):
                raise TypeError("assessment validator must return AssessmentValidationResult")
            validation_errors = validation.errors
            if not validation.valid:
                if self._repair is None:
                    raise ValueError(", ".join(validation.errors) or "assessment validation failed")
                decision = await _resolve(self._repair(request, decision, validation))
                if not isinstance(decision, AssessmentDecision):
                    raise TypeError("assessment repair must return AssessmentDecision")
                repaired = await _resolve(self._validator.validate(request, decision))
                if not isinstance(repaired, AssessmentValidationResult):
                    raise TypeError("assessment validator must return AssessmentValidationResult")
                validation_errors = repaired.errors
                if not repaired.valid:
                    raise ValueError(", ".join(repaired.errors) or "assessment repair failed")
        return _assessment_result(decision, deterministic=deterministic, validation_errors=validation_errors)

    async def try_assess_request(
        self, request: AssessmentRequest
    ) -> AssessmentResult | None:
        """Return a certain deterministic result without invoking an LLM.

        Entries use this for the existing opt-in deterministic branch; an
        uncertain result remains a normal fall-through to their configured
        rubric evaluator.
        """

        decision = await self._engine.try_assess(request)
        if decision is None:
            return None
        return _assessment_result(
            decision, deterministic=True, validation_errors=()
        )


def _assessment_result(
    decision: AssessmentDecision,
    *,
    deterministic: bool,
    validation_errors: tuple[str, ...],
) -> AssessmentResult:
    payload = decision.as_payload()
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in {"correct", "partial", "wrong", "dont_know"}:
        raise ValueError("assessment decision has an invalid verdict")
    score = payload.get("score")
    if isinstance(score, bool) or not isinstance(score, int):
        raise ValueError("assessment decision score must be an integer")
    final_answer_correct = payload.get("final_answer_correct")
    if not isinstance(final_answer_correct, bool):
        raise ValueError("assessment decision final_answer_correct must be a boolean")
    return AssessmentResult(
        evaluation=EvaluationResult(
            verdict=verdict,  # type: ignore[arg-type]
            score=score,
            feedback=str(payload.get("feedback") or ""),
            error_type=str(payload.get("error_type") or ""),
            final_answer_correct=final_answer_correct,
            confidence=decision.confidence,
            evaluator_type=decision.evaluator_type,
            evaluator_version=decision.evaluator_version,
            evaluator_metadata={
                "fallback_reason": decision.fallback_reason or "",
            },
            details=payload,
        ),
        payload=payload,
        deterministic=deterministic,
        validation_errors=validation_errors,
    )


def _decision_from_result(result: AssessmentResult) -> AssessmentDecision:
    """Rebuild the engine-neutral decision for the default evaluator branch."""

    evaluation = result.evaluation
    return AssessmentDecision(
        payload=result.payload,
        evaluator_type=evaluation.evaluator_type,
        evaluator_version=evaluation.evaluator_version,
        confidence=evaluation.confidence,
        fallback_reason=evaluation.fallback_reason or None,
    )
