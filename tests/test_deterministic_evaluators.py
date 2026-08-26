from __future__ import annotations

import asyncio

from adaptive_learning.assessment import AssessmentEngine, AssessmentRequest
from adaptive_learning.deterministic_evaluators import (
    ExactShortAnswerEvaluator,
    MathExpressionEvaluator,
    NumericToleranceEvaluator,
)
from evaluation_contract import canonicalize_evaluation, validate_evaluation


def _engine(*, flags: dict[str, object]) -> AssessmentEngine:
    return AssessmentEngine(
        deterministic_evaluators=(
            ExactShortAnswerEvaluator(),
            NumericToleranceEvaluator(),
            MathExpressionEvaluator(),
        ),
        feature_flags=flags,
    )


def test_all_deterministic_features_are_off_by_default() -> None:
    request = AssessmentRequest(
        question="What is the capital of France?",
        answer="Paris",
        expected_answer="Paris",
        context={"question_type": "short_answer", "accepted_answers": ["Paris"]},
    )

    assert asyncio.run(_engine(flags={}).try_assess(request)) is None


def test_exact_short_answer_returns_a_valid_private_answer_free_decision() -> None:
    request = AssessmentRequest(
        question="What is the capital of France?",
        answer="  PARIS ",
        expected_answer="Paris",
        context={
            "question_type": "short_answer",
            "accepted_answers": ["Paris", "Paris, France"],
        },
    )

    decision = asyncio.run(
        _engine(flags={"exact_short_answer_enabled": True}).try_assess(request)
    )

    assert decision is not None
    assert decision.evaluator_type == "exact_short_answer"
    assert decision.evaluator_version == "exact-short-answer-v1"
    assert decision.confidence == 1.0
    assert decision.fallback_reason == ""
    assert validate_evaluation(
        decision.as_payload(), learner_answer=request.answer
    ).valid
    assert canonicalize_evaluation(decision.as_payload())["verdict"] == "correct"
    serialized = repr(decision.as_payload())
    assert "Paris" not in serialized
    assert "accepted_answers" not in serialized


def test_short_answer_miss_falls_through_unless_question_is_explicitly_closed_world() -> None:
    base = AssessmentRequest(
        question="Name the capital.",
        answer="City of Light",
        expected_answer="Paris",
        context={"question_type": "short_answer", "accepted_answers": ["Paris"]},
    )
    engine = _engine(flags={"exact_short_answer_enabled": True})

    assert asyncio.run(engine.try_assess(base)) is None

    closed = AssessmentRequest(
        question=base.question,
        answer=base.answer,
        expected_answer=base.expected_answer,
        context={
            **base.context,
            "answer_spec": {"closed_world": True},
        },
    )
    decision = asyncio.run(engine.try_assess(closed))

    assert decision is not None
    assert decision.as_payload()["verdict"] == "wrong"
    assert validate_evaluation(decision.as_payload(), learner_answer=closed.answer).valid


def test_numeric_evaluator_uses_decimal_tolerance_and_declines_ambiguous_answers() -> None:
    request = AssessmentRequest(
        question="Compute one third rounded to two decimal places.",
        answer="0.334",
        expected_answer="0.333",
        context={
            "question_type": "math_exact",
            "answer_spec": {"numeric_tolerance": "0.001"},
        },
    )
    engine = _engine(flags={"numeric_tolerance_enabled": True})

    decision = asyncio.run(engine.try_assess(request))

    assert decision is not None
    assert decision.evaluator_type == "numeric_tolerance"
    assert decision.as_payload()["verdict"] == "correct"
    assert validate_evaluation(decision.as_payload(), learner_answer=request.answer).valid

    interval = AssessmentRequest(
        question=request.question,
        answer="[0.332, 0.334]",
        expected_answer=request.expected_answer,
        context=request.context,
    )
    assert asyncio.run(engine.try_assess(interval)) is None


def test_math_expression_uses_a_restricted_declared_grammar_without_python_execution() -> None:
    request = AssessmentRequest(
        question="Simplify the expression.",
        answer="y + x",
        expected_answer="x + y",
        context={
            "question_type": "math_exact",
            "math_equivalence_engine": {
                "enabled": True,
                "domain": "real",
                "variables": ["x", "y"],
            },
        },
    )
    engine = _engine(flags={"math_expression_enabled": True})

    decision = asyncio.run(engine.try_assess(request))

    assert decision is not None
    assert decision.evaluator_type == "math_expression"
    assert decision.as_payload()["verdict"] == "correct"

    unsafe = AssessmentRequest(
        question=request.question,
        answer="__import__('os').system('echo unsafe')",
        expected_answer=request.expected_answer,
        context=request.context,
    )
    assert asyncio.run(engine.try_assess(unsafe)) is None


def test_try_assess_never_invokes_the_default_llm_evaluator() -> None:
    class ExplodingDefault:
        async def assess(self, _request: AssessmentRequest):
            raise AssertionError("LLM should not be invoked by try_assess")

    engine = AssessmentEngine(
        ExplodingDefault(),
        deterministic_evaluators=(ExactShortAnswerEvaluator(),),
        feature_flags={"exact_short_answer_enabled": True},
    )
    request = AssessmentRequest(
        question="Q",
        answer="A",
        expected_answer="A",
        context={"question_type": "short_answer"},
    )

    decision = asyncio.run(engine.try_assess(request))

    assert decision is not None
    assert decision.as_payload()["verdict"] == "correct"
