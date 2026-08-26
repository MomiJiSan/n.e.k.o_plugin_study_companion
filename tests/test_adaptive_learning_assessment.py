from __future__ import annotations

import asyncio

import pytest

from adaptive_learning.assessment import (
    AssessmentEngine,
    AssessmentRequest,
    LlmRubricEvaluator,
)


def test_llm_rubric_evaluator_passes_request_and_payload_through_unchanged() -> None:
    observed: list[AssessmentRequest] = []
    payload = {
        "verdict": "partial",
        "score": 60,
        "final_answer_correct": False,
        "feedback": "Keep the model result exactly as returned.",
    }

    def delegate(request: AssessmentRequest):
        observed.append(request)
        return payload

    request = AssessmentRequest(
        question="2 + 2 = ?",
        answer="3",
        expected_answer="4",
        mode="companion",
        context={"topic_id": "arithmetic"},
    )
    decision = asyncio.run(LlmRubricEvaluator(delegate).assess(request))

    assert observed == [request]
    assert decision.as_payload() == payload
    assert decision.evaluator_type == "llm_rubric"
    assert decision.evaluator_version == "v1"
    assert decision.as_payload() is not payload


def test_llm_rubric_evaluator_supports_async_delegate_without_rewriting_result() -> None:
    async def delegate(_request: AssessmentRequest):
        return {"verdict": "correct", "score": 100, "custom": {"kept": True}}

    decision = asyncio.run(
        LlmRubricEvaluator(delegate, evaluator_version="legacy-agent-v1").assess(
            AssessmentRequest(question="Q", answer="A")
        )
    )

    assert decision.as_payload() == {
        "verdict": "correct",
        "score": 100,
        "custom": {"kept": True},
    }
    assert decision.evaluator_version == "legacy-agent-v1"


def test_engine_uses_default_or_named_evaluator_without_fallback_policy() -> None:
    class Evaluator:
        def __init__(self, verdict: str) -> None:
            self.verdict = verdict

        async def assess(self, _request: AssessmentRequest):
            from adaptive_learning.assessment import AssessmentDecision

            return AssessmentDecision({"verdict": self.verdict}, self.verdict)

    default = Evaluator("default")
    engine = AssessmentEngine(default)
    engine.register("named", Evaluator("named"))
    request = AssessmentRequest(question="Q", answer="A")

    default_decision = asyncio.run(engine.assess(request))
    named_decision = asyncio.run(engine.assess(request, evaluator_name="named"))

    assert default_decision.as_payload() == {"verdict": "default"}
    assert named_decision.as_payload() == {"verdict": "named"}


def test_engine_rejects_missing_or_unknown_evaluator_explicitly() -> None:
    request = AssessmentRequest(question="Q", answer="A")
    engine = AssessmentEngine()

    with pytest.raises(RuntimeError, match="no default"):
        asyncio.run(engine.assess(request))
    with pytest.raises(KeyError, match="not registered: missing"):
        asyncio.run(engine.assess(request, evaluator_name="missing"))


def test_llm_rubric_evaluator_rejects_non_mapping_delegate_results() -> None:
    evaluator = LlmRubricEvaluator(lambda _request: "not a result")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="must return a mapping"):
        asyncio.run(evaluator.assess(AssessmentRequest(question="Q", answer="A")))
