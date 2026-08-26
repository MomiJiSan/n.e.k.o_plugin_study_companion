from __future__ import annotations

import pytest
from adaptive_learning import PracticeSelection, QuestionInstance, QuestionPlan, TopicRef
from adaptive_learning.question_factory import (
    QuestionFactory,
    QuestionGenerationRequest,
    QuestionGenerationResult,
    QuestionValidationResult,
)


def _plan() -> QuestionPlan:
    topic = TopicRef(id="math.linear-equation", name="一元一次方程")
    return QuestionPlan(
        plan_id="plan-1",
        selection=PracticeSelection(reason="weak_topic", target_topic=topic),
        difficulty=3,
        question_type="math_reasoning",
    )


def _question(plan: QuestionPlan) -> QuestionInstance:
    return QuestionInstance(
        question_id="question-1",
        plan_id=plan.plan_id,
        target_topic=plan.target_topic,
        question_type=plan.question_type,
        difficulty=plan.difficulty,
        public_payload={"question": "Solve x + 1 = 2."},
        private_payload={"answer": "1"},
        status="validated",
    )


@pytest.mark.asyncio
async def test_factory_preserves_sync_delegate_outputs_and_calls_each_once() -> None:
    request = QuestionGenerationRequest(plan=_plan(), source_text="source", context={"keep": "value"})
    generation = QuestionGenerationResult(question=_question(request.plan), payload={"unchanged": True})
    validation = QuestionValidationResult(valid=True, errors=(), raw_result={"checked": True})
    calls: list[object] = []

    def generator(received: QuestionGenerationRequest) -> QuestionGenerationResult:
        calls.append(received)
        return generation

    def validator(
        received_request: QuestionGenerationRequest,
        received_generation: QuestionGenerationResult,
    ) -> QuestionValidationResult:
        calls.extend((received_request, received_generation))
        return validation

    result = await QuestionFactory(generator=generator, validator=validator).generate(request)

    assert calls == [request, request, generation]
    assert result.generation is generation
    assert result.validation is validation
    assert result.generation.question is generation.question
    assert result.generation.question is not None
    assert result.generation.question.private_payload["answer"] == "1"


@pytest.mark.asyncio
async def test_factory_supports_async_delegate_and_propagates_its_error_unchanged() -> None:
    request = QuestionGenerationRequest(plan=_plan())

    async def generator(_: QuestionGenerationRequest) -> QuestionGenerationResult:
        return QuestionGenerationResult(question=None, error_code="EXISTING_GENERATION_ERROR")

    result = await QuestionFactory(generator=generator).generate(request)
    assert result.generation.error_code == "EXISTING_GENERATION_ERROR"
    assert result.validation is None

    async def failing_generator(_: QuestionGenerationRequest) -> QuestionGenerationResult:
        raise RuntimeError("existing failure")

    with pytest.raises(RuntimeError, match="existing failure"):
        await QuestionFactory(generator=failing_generator).generate(request)


@pytest.mark.asyncio
async def test_factory_does_not_add_retries_when_validation_returns_failure() -> None:
    request = QuestionGenerationRequest(plan=_plan())
    call_count = 0

    def generator(_: QuestionGenerationRequest) -> QuestionGenerationResult:
        nonlocal call_count
        call_count += 1
        return QuestionGenerationResult(question=_question(request.plan))

    def validator(
        _: QuestionGenerationRequest,
        __: QuestionGenerationResult,
    ) -> QuestionValidationResult:
        return QuestionValidationResult(valid=False, errors=("existing_validation_error",))

    result = await QuestionFactory(generator=generator, validator=validator).generate(request)

    assert call_count == 1
    assert result.validation is not None
    assert result.validation.errors == ("existing_validation_error",)


@pytest.mark.asyncio
async def test_delegate_preserves_sync_and_async_arguments_results_and_errors() -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    expected = object()

    def sync_delegate(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return expected

    assert await QuestionFactory.delegate(sync_delegate, "source", mode="companion") is expected
    assert calls == [(("source",), {"mode": "companion"})]

    async def async_delegate() -> object:
        raise RuntimeError("existing delegate failure")

    with pytest.raises(RuntimeError, match="existing delegate failure"):
        await QuestionFactory.delegate(async_delegate)
