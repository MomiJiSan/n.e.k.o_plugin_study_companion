"""A side-effect-free shell around existing question generation delegates.

This module is intentionally not connected to entry points yet.  It owns no
prompt construction, retry policy, answer normalization, timeout, error-code
translation, or model-routing decisions.  Those remain the responsibility of
the existing delegates until a later migration explicitly moves them.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from .contracts import QuestionGenerationResult, QuestionPlan


@dataclass(frozen=True, slots=True)
class QuestionGenerationRequest:
    """The immutable request passed unchanged to an existing generator."""

    plan: QuestionPlan
    source_text: str = ""
    source: str = "manual"
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QuestionValidationResult:
    """Existing structural or semantic validation output without adaptation."""

    valid: bool
    errors: tuple[str, ...] = ()
    raw_result: Any = None


@dataclass(frozen=True, slots=True)
class QuestionFactoryResult:
    """The generator result and optional validator result from one delegation."""

    generation: QuestionGenerationResult
    validation: QuestionValidationResult | None = None


GenerationDelegate: TypeAlias = Callable[
    [QuestionGenerationRequest], QuestionGenerationResult | Awaitable[QuestionGenerationResult]
]
ValidationDelegate: TypeAlias = Callable[
    [QuestionGenerationRequest, QuestionGenerationResult],
    QuestionValidationResult | Awaitable[QuestionValidationResult],
]


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class QuestionFactory:
    """Delegate generation and validation exactly once, without policy changes."""

    def __init__(
        self,
        *,
        generator: GenerationDelegate,
        validator: ValidationDelegate | None = None,
    ) -> None:
        self._generator = generator
        self._validator = validator

    @staticmethod
    async def delegate(
        callback: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Call an existing sync or async delegate without adapting its result.

        This is the production migration seam for established generation and
        validation calls whose retry and validation policy is still owned by
        the entry layer. It intentionally neither catches exceptions nor
        inspects or mutates returned payloads.
        """

        return await _resolve(callback(*args, **kwargs))

    async def generate(self, request: QuestionGenerationRequest) -> QuestionFactoryResult:
        """Run the configured delegates once and preserve their exact outputs.

        A synchronous and an asynchronous delegate are both supported.  Any
        exception raised by either delegate deliberately propagates unchanged;
        translating it would alter the current public error contract.
        """

        generation = await _resolve(self._generator(request))
        if not isinstance(generation, QuestionGenerationResult):
            raise TypeError("question generator must return QuestionGenerationResult")
        if self._validator is None:
            return QuestionFactoryResult(generation=generation)
        validation = await _resolve(self._validator(request, generation))
        if not isinstance(validation, QuestionValidationResult):
            raise TypeError("question validator must return QuestionValidationResult")
        return QuestionFactoryResult(generation=generation, validation=validation)
