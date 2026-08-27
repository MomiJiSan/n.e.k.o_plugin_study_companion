"""Typed orchestration for question generation, without entry-point coupling."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import QuestionInstance
from .question_factory import (
    QuestionFactory,
    QuestionFactoryResult,
    QuestionGenerationRequest,
)


@dataclass(frozen=True, slots=True)
class QuestionGenerationFailure(RuntimeError):
    """Generation failed after the caller-configured bounded attempt count."""

    attempts: tuple[QuestionFactoryResult, ...]

    def __str__(self) -> str:
        last = self.attempts[-1] if self.attempts else None
        if last is None:
            return "question generation did not run"
        if last.validation is not None and not last.validation.valid:
            return ", ".join(last.validation.errors) or "question validation failed"
        return last.generation.diagnostic or last.generation.error_code or "question generation failed"


class QuestionApplicationService:
    """Run the typed question factory with an explicit, bounded retry policy.

    The service has no dependency on plugin state, prompt construction, model
    routing, or entry error translation.  A future entry adapter owns those
    outer concerns and supplies a fully prepared request.
    """

    def __init__(self, factory: QuestionFactory, *, max_attempts: int = 1) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self._factory = factory
        self._max_attempts = max_attempts

    async def generate(self, request: QuestionGenerationRequest) -> QuestionInstance:
        """Generate and validate a question, retrying only on typed failure."""

        attempts: list[QuestionFactoryResult] = []
        for _ in range(self._max_attempts):
            result = await self._factory.generate(request)
            attempts.append(result)
            question = result.generation.question
            validation = result.validation
            if question is not None and (validation is None or validation.valid):
                return question
        raise QuestionGenerationFailure(tuple(attempts))
