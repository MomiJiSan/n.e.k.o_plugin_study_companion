"""Evaluation extension points.

This module deliberately does not choose or normalize an answer verdict.  It
provides a narrow adapter boundary so the existing LLM rubric evaluator can be
moved behind an explicit interface in a later change without changing its
inputs, outputs, errors, or model-routing behaviour.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeAlias


@dataclass(frozen=True, slots=True)
class AssessmentRequest:
    """The immutable inputs supplied to an answer evaluator."""

    question: str
    answer: str
    expected_answer: str = ""
    mode: str = "companion"
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AssessmentDecision:
    """An evaluator result preserved without verdict or score rewriting."""

    payload: Mapping[str, Any]
    evaluator_type: str
    evaluator_version: str = "v1"
    confidence: float | None = None
    fallback_reason: str | None = None

    def as_payload(self) -> dict[str, Any]:
        """Return a shallow copy suitable for the existing public contract."""

        return dict(self.payload)


class Assessor(Protocol):
    """Protocol used by ``AssessmentEngine``; no evaluator policy is implied."""

    async def assess(self, request: AssessmentRequest) -> AssessmentDecision: ...


class DeterministicAssessor(Protocol):
    """An optional evaluator that must decline uncertain inputs.

    Deterministic evaluators are deliberately separate from ``Assessor``.  A
    ``None`` result is a normal, safe fall-through to the existing LLM rubric
    rather than an assessment error or a negative verdict.
    """

    async def try_assess(
        self,
        request: AssessmentRequest,
        *,
        feature_flags: Mapping[str, Any],
    ) -> AssessmentDecision | None: ...


AssessmentDelegate: TypeAlias = Callable[
    [AssessmentRequest],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


class LlmRubricEvaluator:
    """Pass through to the current LLM-rubric callable without altering it.

    The delegate deliberately receives one value object.  An entry adapter can
    later map it to the current agent's keyword arguments while preserving the
    present operation, timeout, model routing, and error behaviour.
    """

    evaluator_type = "llm_rubric"

    def __init__(
        self,
        delegate: AssessmentDelegate,
        *,
        evaluator_version: str = "v1",
    ) -> None:
        self._delegate = delegate
        self._evaluator_version = evaluator_version

    async def assess(self, request: AssessmentRequest) -> AssessmentDecision:
        payload = self._delegate(request)
        if inspect.isawaitable(payload):
            payload = await payload
        if not isinstance(payload, Mapping):
            raise TypeError("assessment delegate must return a mapping")
        return AssessmentDecision(
            payload=dict(payload),
            evaluator_type=self.evaluator_type,
            evaluator_version=self._evaluator_version,
        )


class AssessmentEngine:
    """Named evaluator registry with no built-in assessment policy.

    Registering an evaluator does not change the existing evaluation path.  It
    becomes active only when a future entry explicitly calls ``assess``.
    """

    def __init__(
        self,
        default_evaluator: Assessor | None = None,
        *,
        deterministic_evaluators: Sequence[DeterministicAssessor] = (),
        feature_flags: Mapping[str, Any] | None = None,
    ) -> None:
        self._evaluators: dict[str, Assessor] = {}
        self._default_evaluator = default_evaluator
        # Keep a private copy so later caller mutations cannot silently turn a
        # feature on during an evaluation.  Absence means every deterministic
        # evaluator is disabled.
        self._deterministic_evaluators = tuple(deterministic_evaluators)
        self._feature_flags = dict(feature_flags or {})

    def register(self, name: str, evaluator: Assessor) -> None:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("evaluator name is required")
        self._evaluators[normalized_name] = evaluator

    async def assess(
        self,
        request: AssessmentRequest,
        *,
        evaluator_name: str | None = None,
    ) -> AssessmentDecision:
        evaluator = self._resolve(evaluator_name)
        return await evaluator.assess(request)

    async def try_assess(self, request: AssessmentRequest) -> AssessmentDecision | None:
        """Return the first certain deterministic result, else ``None``.

        This intentionally never invokes the default evaluator: the answer
        entry owns the existing LLM call, repair, lifecycle, and error path.
        Consequently an engine with no deterministic evaluators (the default)
        is a strict no-op.
        """

        for evaluator in self._deterministic_evaluators:
            decision = await evaluator.try_assess(
                request,
                feature_flags=self._feature_flags,
            )
            if decision is not None:
                return decision
        return None

    def _resolve(self, evaluator_name: str | None) -> Assessor:
        if evaluator_name is not None:
            normalized_name = str(evaluator_name).strip()
            try:
                return self._evaluators[normalized_name]
            except KeyError as exc:
                raise KeyError(
                    f"assessment evaluator not registered: {normalized_name}"
                ) from exc
        if self._default_evaluator is not None:
            return self._default_evaluator
        raise RuntimeError("no default assessment evaluator is configured")
