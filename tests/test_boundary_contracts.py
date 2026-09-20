from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_cognitive_answer_event_integration import (
    MODEL,
    TOPIC,
    _assert_ordinary_answer_committed,
    _load_runtime,
    _open_store,
    _submit,
    _UnusedExtractor,
)


def _tracker(tracker_type: type[Any], store: Any, *, cognitive_config: dict[str, object]) -> Any:
    return tracker_type(
        store,
        logger=type(
            "Logger",
            (),
            {name: lambda self, *_args, **_kwargs: None for name in ("debug", "info", "warning", "error", "exception")},
        )(),
        cognitive_config={
            "projection_enabled": True,
            "read_mode": "active",
            "intent_policy": "on",
            "model_version": MODEL,
            "supported_topics": [TOPIC],
            **cognitive_config,
        },
        cognitive_extractor=_UnusedExtractor(),
    )


def test_cognitive_disabled_keeps_ordinary_answer_without_exposure_or_outbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, tracker_module = _load_runtime(monkeypatch, "_boundary_contract_cognitive_disabled")
    store = _open_store(tmp_path, store_module.StudyStore)
    try:
        tracker = _tracker(
            tracker_module.KnowledgeTracker,
            store,
            cognitive_config={
                "projection_enabled": False,
                "read_mode": "off",
                "intent_policy": "off",
            },
        )
        result = _submit(tracker, attempt_id="attempt-cognitive-disabled")

        assert result["topic_id"] == TOPIC
        _assert_ordinary_answer_committed(store, "attempt-cognitive-disabled", cognitive=False)
        assert store.list_cognitive_intervention_events() == []
        assert store.list_cognitive_strategy_exposures() == []
        assert store.list_cognitive_strategy_facts() == []
        assert store.list_cognitive_outbox() == []
    finally:
        store.close()


def test_duplicate_attempt_is_idempotent_and_does_not_duplicate_cognitive_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, tracker_module = _load_runtime(monkeypatch, "_boundary_contract_duplicate_attempt")
    store = _open_store(tmp_path, store_module.StudyStore)
    try:
        tracker = _tracker(tracker_module.KnowledgeTracker, store, cognitive_config={})
        attempt_id = "attempt-duplicate"
        first = _submit(tracker, attempt_id=attempt_id)
        outbox_after_first = store.list_cognitive_outbox()
        second = _submit(tracker, attempt_id=attempt_id)

        assert first["topic_id"] == TOPIC
        assert second["knowledge_tracking_status"] == "duplicate_attempt"
        _assert_ordinary_answer_committed(store, attempt_id, cognitive=False)
        committed_qa = [
            row for row in store.list_qa_records(limit=20) if row["question"].get("attempt_id") == attempt_id
        ]
        assert len(committed_qa) == 1
        assert store.list_cognitive_outbox() == outbox_after_first
        assert store.list_cognitive_intervention_events() == []
        assert store.list_cognitive_strategy_exposures() == []
        assert store.list_cognitive_strategy_facts() == []
    finally:
        store.close()


def test_disabled_cognitive_entry_cannot_enqueue_retention_side_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, tracker_module = _load_runtime(
        monkeypatch, "_boundary_contract_retention_disabled"
    )
    store = _open_store(tmp_path, store_module.StudyStore)
    try:
        tracker = _tracker(
            tracker_module.KnowledgeTracker,
            store,
            cognitive_config={
                "projection_enabled": False,
                "read_mode": "off",
                "intent_policy": "off",
                "retention_enabled": False,
            },
        )
        question = {
            "question_id": "question-intervention",
            "question": "Complete the missing inner derivative.",
            "answer": "2x",
            "question_type": "math_exact",
            "difficulty": 3,
            "topic": TOPIC,
            "cognitive_strategy": "retention_check",
            "target_binding": {"cognitive_strategy": "retention_check"},
        }
        result = tracker.on_answer(
            topic_id=TOPIC,
            question=question,
            user_answer="2x",
            eval_result={
                "verdict": "correct",
                "score": 100,
                "evaluator_type": "deterministic",
                "evaluator_version": "integration-test-v1",
                "confidence": 1.0,
            },
            mode="companion",
            session_id="session-retention-disabled",
            require_existing_topic=True,
            attempt_id="attempt-retention-disabled",
        )

        assert result["topic_id"] == TOPIC
        _assert_ordinary_answer_committed(
            store, "attempt-retention-disabled", cognitive=False
        )
        assert store.list_cognitive_outbox() == []
    finally:
        store.close()
