from __future__ import annotations

from dataclasses import replace

import pytest

# isort: split
from adaptive_learning.answer_application import (
    AnswerAssessmentService,
    AssessmentValidationResult,
)
from adaptive_learning.assessment import AssessmentDecision, AssessmentEngine
from adaptive_learning.contracts import (
    EvaluatedAttempt,
    EvaluationResult,
    LearningCommitContext,
    PracticeSelection,
    QuestionInstance,
    QuestionPlan,
    TopicRef,
)
from adaptive_learning.production_adapters import StudyTrackerCommitAdapter
from adaptive_learning.question_application import (
    QuestionApplicationService,
    QuestionGenerationFailure,
)
from adaptive_learning.question_factory import (
    QuestionFactory,
    QuestionGenerationRequest,
    QuestionGenerationResult,
    QuestionValidationResult,
)


def _plan() -> QuestionPlan:
    topic = TopicRef(id="math.topic", name="Math topic")
    return QuestionPlan(
        plan_id="plan-1",
        selection=PracticeSelection(reason="weak_topic", target_topic=topic),
        difficulty=3,
        question_type="short_answer",
        mode="practice",
        source_question_id="source-1",
        target_binding={"origin_wrong_question_id": "wrong-1"},
    )


def _question(plan: QuestionPlan | None = None) -> QuestionInstance:
    plan = plan or _plan()
    return QuestionInstance(
        question_id="question-1",
        plan_id=plan.plan_id,
        target_topic=plan.target_topic,
        question_type=plan.question_type,
        difficulty=plan.difficulty,
        public_payload={"question": "2 + 2 = ?"},
        private_payload={"answer": "4"},
        mode=plan.mode,
        source_question_id=plan.source_question_id,
        target_binding=plan.target_binding,
        scope_key="stage:math",
        scope_revision=2,
        status="validated",
    )


@pytest.mark.asyncio
async def test_question_application_returns_first_valid_question_without_extra_call() -> None:
    plan = _plan()
    request = QuestionGenerationRequest(plan=plan)
    calls = 0

    def generator(_: QuestionGenerationRequest) -> QuestionGenerationResult:
        nonlocal calls
        calls += 1
        return QuestionGenerationResult(question=_question(plan))

    result = await QuestionApplicationService(
        QuestionFactory(generator=generator), max_attempts=2
    ).generate(request)

    assert result.question_id == "question-1"
    assert calls == 1


@pytest.mark.asyncio
async def test_question_application_retries_only_bounded_typed_failures() -> None:
    plan = _plan()
    request = QuestionGenerationRequest(plan=plan)
    calls = 0

    def generator(_: QuestionGenerationRequest) -> QuestionGenerationResult:
        nonlocal calls
        calls += 1
        return QuestionGenerationResult(question=_question(plan))

    def validator(
        _: QuestionGenerationRequest, __: QuestionGenerationResult
    ) -> QuestionValidationResult:
        return QuestionValidationResult(valid=calls == 2, errors=("retry",))

    result = await QuestionApplicationService(
        QuestionFactory(generator=generator, validator=validator), max_attempts=2
    ).generate(request)

    assert result.question_id == "question-1"
    assert calls == 2

    with pytest.raises(QuestionGenerationFailure, match="retry"):
        await QuestionApplicationService(
            QuestionFactory(generator=generator, validator=validator), max_attempts=1
        ).generate(request)


@pytest.mark.asyncio
async def test_answer_application_prefers_deterministic_result_without_llm() -> None:
    class Deterministic:
        async def try_assess(self, _request, *, feature_flags):
            assert feature_flags == {"enabled": True}
            return AssessmentDecision(
                {
                    "verdict": "correct",
                    "score": 100,
                    "final_answer_correct": True,
                    "feedback": "exact",
                },
                "exact",
            )

    class Llm:
        async def assess(self, _request):
            raise AssertionError("LLM must not run after a certain result")

    service = AnswerAssessmentService(
        AssessmentEngine(Llm(), deterministic_evaluators=(Deterministic(),), feature_flags={"enabled": True})
    )

    result = await service.assess(_question(), "4")

    assert result.deterministic is True
    assert result.evaluation.verdict == "correct"
    assert result.evaluation.evaluator_type == "exact"


@pytest.mark.asyncio
async def test_answer_application_allows_exactly_one_validator_repair() -> None:
    class Llm:
        async def assess(self, _request):
            return AssessmentDecision(
                {"verdict": "wrong", "score": 0, "final_answer_correct": False}, "llm"
            )

    class Validator:
        def __init__(self) -> None:
            self.calls = 0

        async def validate(self, _request, decision):
            self.calls += 1
            return AssessmentValidationResult(
                valid=decision.payload["verdict"] == "partial",
                errors=("missing feedback",),
            )

    validator = Validator()
    repair_calls = 0

    async def repair(_request, _decision, _validation):
        nonlocal repair_calls
        repair_calls += 1
        return AssessmentDecision(
            {
                "verdict": "partial",
                "score": 50,
                "final_answer_correct": False,
                "feedback": "repaired",
            },
            "llm",
        )

    result = await AnswerAssessmentService(
        AssessmentEngine(Llm()), validator=validator, repair=repair
    ).assess(_question(), "3")

    assert result.evaluation.verdict == "partial"
    assert repair_calls == 1
    assert validator.calls == 2


@pytest.mark.asyncio
async def test_tracker_adapter_maps_every_tracker_input_from_typed_attempt() -> None:
    attempt = EvaluatedAttempt(
        attempt_id="attempt-1",
        question=_question(),
        learner_answer="4",
        evaluation=EvaluationResult(
            verdict="correct",
            score=100,
            feedback="good",
            final_answer_correct=True,
            details={
                "verdict": "correct",
                "score": 100,
                "feedback": "good",
                "final_answer_correct": True,
                "track": {"topic": "math.topic"},
            },
        ),
        session_id="run-1",
        response_time_ms=200,
        used_hint=True,
        commit_context=LearningCommitContext(
            mode="practice",
            source_question_id="source-override",
            target_binding={"origin_wrong_question_id": "wrong-1"},
            scope_key="scope-override",
            scope_revision=3,
            origin_wrong_question_id="wrong-1",
            require_existing_topic=True,
        ),
    )
    received: list[dict[str, object]] = []

    class Tracker:
        async def on_answer(self, **kwargs):
            received.append(kwargs)
            return {"knowledge_tracking_status": "updated"}

    result = await StudyTrackerCommitAdapter(Tracker()).commit_evaluated_attempt(attempt)

    assert result == {"knowledge_tracking_status": "updated"}
    assert len(received) == 1
    kwargs = received[0]
    assert kwargs["topic_id"] == "math.topic"
    assert kwargs["attempt_id"] == "attempt-1"
    assert kwargs["mode"] == "practice"
    assert kwargs["question"]["source_question_id"] == "source-override"
    assert kwargs["question"]["target_binding"] == {"origin_wrong_question_id": "wrong-1"}
    assert kwargs["eval_result"]["verdict"] == "correct"


@pytest.mark.asyncio
async def test_tracker_adapter_propagates_server_cognitive_provenance() -> None:
    question = replace(
        _question(),
        learning_intent="misconception_repair",
        repair_strategy="complete_inner_derivative",
        cognitive_decision_id="decision-1",
        cognitive_validator_version="validator-v2",
        diagnostic_validation_id="validation-1",
    )
    attempt = EvaluatedAttempt(
        attempt_id="attempt-cognitive",
        question=question,
        learner_answer="2*x*cos(x^2)",
        evaluation=EvaluationResult(
            verdict="correct",
            score=100,
            details={"verdict": "correct", "score": 100},
        ),
    )
    received: list[dict[str, object]] = []

    class Tracker:
        def on_answer(self, **kwargs):
            received.append(kwargs)
            return {"knowledge_tracking_status": "updated"}

    await StudyTrackerCommitAdapter(Tracker()).commit_evaluated_attempt(attempt)

    persisted = received[0]["question"]
    assert persisted["learning_intent"] == "misconception_repair"
    assert persisted["repair_strategy"] == "complete_inner_derivative"
    assert persisted["diagnostic_validation_id"] == "validation-1"
    assert persisted["cognitive_decision_id"] == "decision-1"
    assert persisted["cognitive_validator_version"] == "validator-v2"
