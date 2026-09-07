"""Exercise the production LLM adapter and real retention persistence together."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from test_cognitive_retention_answer_flow import (
    _claimed_retention,
    _Logger,
    _prepare_transfer_intervention,
    _question,
    _runtime,
    _store,
    _submit_transfer,
)
from test_study_inference_router import _agent_module


async def _evaluate(
    monkeypatch: pytest.MonkeyPatch, raw: dict[str, Any], *, retention: bool = False
):
    module = _agent_module(monkeypatch)
    agent = module.TutorLLMAgent(logger=_Logger(), config=module.StudyConfig())
    captured = []

    async def model(messages, **kwargs):
        captured.extend(messages)
        return json.dumps(raw)

    monkeypatch.setattr(agent, "_call_model", model)
    reply = await agent.answer_evaluate(
        question="Differentiate exp(5x - 2)." if retention else "Differentiate (x^2 + 1)^4.",
        answer="5*exp(5*x-2)" if retention else "8*x*(x^2+1)^3",
        expected_answer="5*exp(5*x-2)" if retention else "8*x*(x^2+1)^3",
    )
    assert not reply.degraded
    prompt = "\n".join(message["content"] for message in captured)
    assert '"confidence"' in prompt
    assert "not the learner's score" in prompt
    return reply.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", [0, 0.37, 1])
async def test_llm_verdict_preserves_reported_confidence_and_server_provenance(
    monkeypatch: pytest.MonkeyPatch, confidence: float
) -> None:
    payload = await _evaluate(monkeypatch, {
        "verdict": "correct", "score": 100, "confidence": confidence,
        "evaluator_type": "forged_deterministic", "evaluator_version": "forged",
        "reference_answer": "forged answer",
    })
    assert payload["evaluator_type"] == "llm_rubric"
    assert payload["evaluator_version"] == "llm-rubric-v2"
    assert payload["confidence"] == confidence
    assert payload["reference_answer"] == "8*x*(x^2+1)^3"


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [
    {}, {"confidence": None}, {"confidence": True}, {"confidence": "0.9"},
    {"confidence": -0.1}, {"confidence": 1.1}, {"confidence": float("nan")},
    {"confidence": float("inf")}, {"confidence": -float("inf")},
])
async def test_uncertified_llm_transfer_keeps_answer_without_opening_retention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reported: dict[str, Any]
) -> None:
    payload = await _evaluate(monkeypatch, {"verdict": "correct", "score": 100, **reported})
    assert payload["confidence"] is None
    store_module, _ = _runtime(monkeypatch, f"_llm_invalid_{id(monkeypatch)}")
    tracker = importlib.import_module(f"{store_module.__package__}.knowledge_tracker")
    store = _store(tmp_path, store_module.StudyStore)
    try:
        _prepare_transfer_intervention(store)
        _submit_transfer(store, tracker.KnowledgeTracker, retention_enabled=True,
                         attempt_id="uncertified", eval_overrides=payload)
        fact = store.get_attempt_fact("uncertified")
        assert fact is not None
        assert store._require_conn().execute(
            "SELECT confidence FROM evaluations WHERE attempt_id = 'uncertified'"
        ).fetchone()[0] is None
        assert store.list_cognitive_monitoring_episodes() == []
        assert store.list_cognitive_outbox(status="failed")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_real_llm_normalization_persists_and_opens_transfer_obligation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = await _evaluate(monkeypatch, {"verdict": "correct", "score": 100, "confidence": 0.93})
    assert payload["confidence"] == 0.93
    store_module, _ = _runtime(monkeypatch, "_llm_certified_transfer")
    tracker = importlib.import_module(f"{store_module.__package__}.knowledge_tracker")
    store = _store(tmp_path, store_module.StudyStore)
    try:
        _prepare_transfer_intervention(store)
        _submit_transfer(store, tracker.KnowledgeTracker, retention_enabled=True,
                         attempt_id="certified", eval_overrides=payload)
        row = store._require_conn().execute(
            "SELECT evaluation_json, evaluator_type, evaluator_version, confidence "
            "FROM evaluations WHERE attempt_id = 'certified'"
        ).fetchone()
        saved = json.loads(row[0])
        assert row[1:] == ("llm_rubric", "llm-rubric-v2", 0.93)
        assert tuple(saved[key] for key in ("evaluator_type", "evaluator_version", "confidence")) == row[1:]
        assert len(store.list_cognitive_monitoring_episodes()) == 1
        assert len(store.list_cognitive_learning_obligations()) == 1
        assert not store.list_cognitive_outbox(status="failed")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_llm_retention_evaluation_can_resolve_real_obligation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = await _evaluate(monkeypatch, {"verdict": "correct", "score": 100, "confidence": 0.91}, retention=True)
    store_module, _ = _runtime(monkeypatch, "_llm_certified_retention")
    store = _store(tmp_path, store_module.StudyStore)
    try:
        created, claim = _claimed_retention(store)
        store.batch_write_answer_data(
            session_id="llm-retention", mode="companion", topic_id="calculus.chain_rule",
            question=_question(created, claim), user_answer="5*exp(5*x-2)",
            eval_result=payload, response_time_ms=150, used_hint=False,
            attempt_id="llm-retention", enqueue_cognitive_projection=False,
        )
        assert store.list_cognitive_monitoring_episodes()[0]["status"] == "resolved"
        assert not store.list_cognitive_outbox(status="failed")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_failed_llm_call_cannot_certify_heuristic_correct_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _agent_module(monkeypatch)
    agent = module.TutorLLMAgent(logger=_Logger(), config=module.StudyConfig())

    async def unavailable(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(agent, "_call_model", unavailable)
    reply = await agent.answer_evaluate(
        question="What is 2 + 2?", answer="4", expected_answer="4"
    )
    assert reply.degraded
    assert reply.payload["verdict"] == "correct"
    assert reply.payload.get("confidence") is None
    assert not reply.payload.get("evaluator_version")
