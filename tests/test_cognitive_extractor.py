from __future__ import annotations

import asyncio
from typing import Any

import pytest

# isort: split
from adaptive_learning.cognitive_catalog import (
    CHAIN_RULE_TOPIC_ID,
    COGNITIVE_CATALOG_V1,
    COLLEGE_CHAIN_RULE_TOPIC_ID,
)
from adaptive_learning.cognitive_contracts import (
    COGNITIVE_EXTRACT_OPERATION,
    CognitiveExtractionInput,
    CognitiveModelRequest,
    CognitiveModelResponse,
)
from adaptive_learning.cognitive_extractor import CognitiveExtractor


def _count_tokens(text: str) -> int:
    return len(text)


def _truncate_to_tokens(text: str, limit: int) -> str:
    return text[:limit]


def _input(
    *, allowed_hypotheses: tuple[str, ...] = ()
) -> CognitiveExtractionInput:
    return CognitiveExtractionInput(
        topic_id=CHAIN_RULE_TOPIC_ID,
        question="Differentiate sin(x^2).",
        expected_answer="2x cos(x^2)",
        learner_answer="cos(x^2)",
        evaluation={
            "verdict": "wrong",
            "missing_points": ["inner derivative 2x"],
            "misconceptions": [],
            "step_feedback": [],
        },
        allowed_hypotheses=allowed_hypotheses,
    )


def _valid_payload() -> dict[str, Any]:
    return {
        "evidence": [
            {
                "hypothesis_code": "omit_inner_derivative",
                "direction": "support",
                "strength": 0.72,
                "extractor_confidence": 0.81,
                "evidence_span": "cos(x^2) is present without the factor 2x",
            }
        ]
    }


class _Gateway:
    def __init__(self, content: object) -> None:
        self.content = content
        self.calls: list[CognitiveModelRequest] = []

    async def complete_structured(
        self, request: CognitiveModelRequest
    ) -> CognitiveModelResponse:
        self.calls.append(request)
        if isinstance(self.content, BaseException):
            raise self.content
        return CognitiveModelResponse(content=self.content)  # type: ignore[arg-type]


def _extractor(
    gateway: object,
    **kwargs: Any,
) -> CognitiveExtractor:
    return CognitiveExtractor(
        gateway=gateway,  # type: ignore[arg-type]
        count_tokens=_count_tokens,
        truncate_to_tokens=_truncate_to_tokens,
        **kwargs,
    )


def test_v1_catalog_is_closed_to_one_topic_and_three_codes() -> None:
    expected = (
        "omit_inner_derivative",
        "differentiate_inner_incorrectly",
        "confuse_product_and_chain",
    )
    assert COGNITIVE_CATALOG_V1.allowed_codes(CHAIN_RULE_TOPIC_ID) == expected
    assert COGNITIVE_CATALOG_V1.allowed_codes(COLLEGE_CHAIN_RULE_TOPIC_ID) == expected
    assert COGNITIVE_CATALOG_V1.allowed_codes("algebra.linear_equation") == ()


@pytest.mark.asyncio
async def test_extracts_valid_evidence_through_bounded_neutral_request() -> None:
    gateway = _Gateway(_valid_payload())
    extractor = _extractor(
        gateway,
        max_input_tokens=1_800,
        max_output_tokens=321,
        timeout_seconds=2.5,
    )

    result = await extractor.extract(_input())

    assert result.succeeded is True
    assert result.failure_reason == ""
    assert len(result.evidence) == 1
    evidence = result.evidence[0]
    assert evidence.topic_id == CHAIN_RULE_TOPIC_ID
    assert evidence.hypothesis_code == "omit_inner_derivative"
    assert evidence.direction == "support"
    assert evidence.strength == 0.72
    assert evidence.extractor_confidence == 0.81
    request = gateway.calls[0]
    assert request.operation == COGNITIVE_EXTRACT_OPERATION
    assert request.max_input_tokens == 1_800
    assert request.max_output_tokens == 321
    assert request.timeout_seconds == 2.5
    assert request.payload["evaluation"]["verdict"] == "wrong"  # type: ignore[index]
    assert _count_tokens(__import__("json").dumps(
        request.payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )) <= request.max_input_tokens


@pytest.mark.asyncio
async def test_empty_evidence_is_a_successful_non_finding() -> None:
    result = await _extractor(_Gateway({"evidence": []})).extract(_input())

    assert result.succeeded is True
    assert result.evidence == ()


@pytest.mark.asyncio
async def test_seed_topic_alias_preserves_the_real_topic_id_in_request_and_evidence() -> None:
    gateway = _Gateway(_valid_payload())
    extraction_input = _input()
    extraction_input = CognitiveExtractionInput(
        topic_id=COLLEGE_CHAIN_RULE_TOPIC_ID,
        question=extraction_input.question,
        expected_answer=extraction_input.expected_answer,
        learner_answer=extraction_input.learner_answer,
        evaluation=extraction_input.evaluation,
    )

    result = await _extractor(gateway).extract(extraction_input)

    assert result.succeeded is True
    assert result.evidence[0].topic_id == COLLEGE_CHAIN_RULE_TOPIC_ID
    assert gateway.calls[0].payload["topic_id"] == COLLEGE_CHAIN_RULE_TOPIC_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        {"evidence": [{**_valid_payload()["evidence"][0], "evidence_span": ""}]},
        {"evidence": [{**_valid_payload()["evidence"][0], "strength": True}]},
        {"evidence": [{**_valid_payload()["evidence"][0], "strength": 1.1}]},
        {"evidence": [{**_valid_payload()["evidence"][0], "unexpected": "field"}]},
        {"evidence": "not-a-list"},
        {"evidence": [], "unexpected": True},
        "not-json",
    ],
)
async def test_invalid_model_structure_fails_the_whole_extraction(
    content: object,
) -> None:
    result = await _extractor(_Gateway(content)).extract(_input())

    assert result.succeeded is False
    assert result.evidence == ()
    assert result.failure_reason == "invalid_model_output"


@pytest.mark.asyncio
async def test_one_out_of_scope_item_discards_an_otherwise_valid_batch() -> None:
    valid = _valid_payload()["evidence"][0]
    content = {
        "evidence": [
            valid,
            {
                **valid,
                "hypothesis_code": "invented_learning_style",
                "evidence_span": "unsupported claim",
            },
        ]
    }

    result = await _extractor(_Gateway(content)).extract(_input())

    assert result.succeeded is False
    assert result.evidence == ()


@pytest.mark.asyncio
async def test_request_subset_is_an_additional_model_output_boundary() -> None:
    gateway = _Gateway(
        {
            "evidence": [
                {
                    **_valid_payload()["evidence"][0],
                    "hypothesis_code": "confuse_product_and_chain",
                }
            ]
        }
    )

    result = await _extractor(gateway).extract(
        _input(allowed_hypotheses=("omit_inner_derivative",))
    )

    assert result.succeeded is False
    assert result.evidence == ()


@pytest.mark.asyncio
async def test_duplicate_hypothesis_and_more_than_three_items_are_rejected() -> None:
    item = _valid_payload()["evidence"][0]
    duplicate = await _extractor(
        _Gateway({"evidence": [item, item]})
    ).extract(_input())
    too_many = await _extractor(
        _Gateway({"evidence": [item, item, item, item]})
    ).extract(_input())

    assert duplicate.failure_reason == "invalid_model_output"
    assert duplicate.evidence == ()
    assert too_many.failure_reason == "invalid_model_output"
    assert too_many.evidence == ()


@pytest.mark.asyncio
async def test_invalid_input_scope_never_calls_the_model() -> None:
    gateway = _Gateway(_valid_payload())

    unsupported_topic = await _extractor(gateway).extract(
        CognitiveExtractionInput(
            topic_id="algebra.linear_equation",
            question="x + 1 = 2",
            expected_answer="x=1",
            learner_answer="x=3",
        )
    )
    invented_allowed_code = await _extractor(gateway).extract(
        _input(allowed_hypotheses=("invented",))
    )

    assert unsupported_topic.failure_reason == "unsupported_topic"
    assert invented_allowed_code.failure_reason == "invalid_input"
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_model_unavailable_is_a_safe_failure() -> None:
    result = await _extractor(
        _Gateway(ConnectionError("provider is offline"))
    ).extract(_input())

    assert result.succeeded is False
    assert result.evidence == ()
    assert result.failure_reason == "model_unavailable"


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_safe() -> None:
    class NeverReturns:
        async def complete_structured(
            self, _request: CognitiveModelRequest
        ) -> CognitiveModelResponse:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    result = await _extractor(
        NeverReturns(), timeout_seconds=0.01
    ).extract(_input())

    assert result.failure_reason == "timeout"
    assert result.evidence == ()


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_swallowed_as_model_failure() -> None:
    class Cancels:
        async def complete_structured(
            self, _request: CognitiveModelRequest
        ) -> CognitiveModelResponse:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _extractor(Cancels()).extract(_input())


@pytest.mark.asyncio
async def test_output_limit_is_never_accepted_as_partial_evidence() -> None:
    class Truncated:
        def complete_structured(
            self, _request: CognitiveModelRequest
        ) -> CognitiveModelResponse:
            return CognitiveModelResponse(
                content=_valid_payload(), output_limit_reached=True
            )

    result = await _extractor(Truncated()).extract(_input())

    assert result.failure_reason == "output_truncated"
    assert result.evidence == ()


@pytest.mark.asyncio
async def test_input_fields_are_truncated_before_reaching_gateway() -> None:
    gateway = _Gateway({"evidence": []})
    extractor = _extractor(gateway, max_input_tokens=1_800)
    long_input = CognitiveExtractionInput(
        topic_id=CHAIN_RULE_TOPIC_ID,
        question="Q" * 10_000,
        expected_answer="E" * 10_000,
        learner_answer="A" * 10_000,
        evaluation={"verdict": "wrong", "feedback": "F" * 10_000},
    )

    result = await extractor.extract(long_input)

    assert result.succeeded is True
    request = gateway.calls[0]
    serialized = __import__("json").dumps(
        request.payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert _count_tokens(serialized) <= 1_800
    assert len(str(request.payload["learner_answer"])) < 10_000


@pytest.mark.asyncio
async def test_token_budget_failure_never_reaches_the_model() -> None:
    gateway = _Gateway({"evidence": []})

    def broken_counter(_text: str) -> int:
        raise RuntimeError("tokenizer unavailable")

    extractor = CognitiveExtractor(
        gateway=gateway,
        count_tokens=broken_counter,
        truncate_to_tokens=_truncate_to_tokens,
    )

    result = await extractor.extract(_input())

    assert result.failure_reason == "input_budget_unavailable"
    assert result.evidence == ()
    assert gateway.calls == []
