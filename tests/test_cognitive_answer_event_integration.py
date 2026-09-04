from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "calculus.chain_rule"
MODEL = "cognitive-v1"
HYPOTHESIS = "omit_inner_derivative"
SNAPSHOT = f"{TOPIC}:{HYPOTHESIS}:{MODEL}:generation-7"


class _Logger:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


class _UnusedExtractor:
    async def extract(self, _extraction_input: object) -> object:
        raise AssertionError("answer submission must not run cognitive extraction")


def _load_runtime(monkeypatch: pytest.MonkeyPatch, package_name: str):
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(  # type: ignore[attr-defined]
        value or "companion"
    )
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    store_module = importlib.import_module(f"{package_name}.store")
    tracker_module = importlib.import_module(f"{package_name}.knowledge_tracker")
    return store_module, tracker_module


def _open_store(tmp_path: Path, Store: type[Any]):
    store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    store.ensure_topic(topic_id=TOPIC, name="Chain rule")
    store.batch_write_answer_data(
        session_id="session-source",
        mode="companion",
        topic_id=TOPIC,
        question={
            "question_id": "question-source",
            "question": "Differentiate sin(x^2).",
            "answer": "2x cos(x^2)",
            "question_type": "math_exact",
            "difficulty": 3,
        },
        user_answer="cos(x^2)",
        eval_result={"verdict": "wrong", "score": 0},
        response_time_ms=100,
        attempt_id="attempt-source",
    )
    conn = store._require_conn()
    conn.execute(
        """
        INSERT INTO cognitive_topic_projection_queue (
            topic_id, model_version, status, requested_generation,
            claimed_generation, projected_generation
        ) VALUES (?, ?, 'done', 7, 7, 7)
        """,
        (TOPIC, MODEL),
    )
    conn.execute(
        """
        INSERT INTO cognitive_hypothesis_current (
            hypothesis_id, topic_id, hypothesis_code, evidence_status,
            intervention_stage, user_override, status, probability,
            support_count, counter_count, diagnostic_support_count,
            relapse_count, source_attempt_id, source_snapshot_id,
            model_version, projected_generation
        ) VALUES (?, ?, ?, 'supported', 'idle', '', 'supported', 0.91,
                  2, 0, 0, 0, 'attempt-source', ?, ?, 7)
        """,
        (f"{TOPIC}:{HYPOTHESIS}", TOPIC, HYPOTHESIS, SNAPSHOT, MODEL),
    )
    conn.commit()
    return store


def _question_event() -> dict[str, object]:
    return {
        "event_id": "event-question",
        "event_type": "question_committed",
        "decision_id": "decision-1",
        "hypothesis_target": {
            "hypothesis_id": f"{TOPIC}:{HYPOTHESIS}",
            "topic_id": TOPIC,
            "code": HYPOTHESIS,
            "status": "supported",
            "probability": 0.91,
            "model_version": MODEL,
            "source_snapshot_id": SNAPSHOT,
            "source_attempt_id": "attempt-source",
            "projection_generation": 7,
        },
        "learning_intent": "misconception_repair",
        "repair_strategy": "complete_inner_derivative",
        "binding": {
            "plan_id": "selection-1",
            "topic_id": TOPIC,
            "selection_reason": "wrong_retry",
            "eligible_topic_ids": [TOPIC],
            "learning_plan_id": "learning-plan-1",
            "learning_plan_revision": 3,
            "scope_key": "scope-a",
            "scope_revision": 4,
            "origin_wrong_question_id": "wrong-1",
            "source_question_id": "wrong-1",
            "target_binding": {"learning_plan_revision": 3},
        },
        "question_id": "question-intervention",
        "attempt_id": "",
        "blueprint_id": "chain.omit-inner.fill-factor.v1",
        "question_family_id": "chain.cos-cube.fill-factor",
        "diagnostic_validation_id": "validation-1",
        "evaluation_verdict": "",
        "policy_version": "cognitive-intent-policy-v2",
        "validator_version": "diagnostic-validator-v2",
        "schema_version": 1,
        "metadata": {"reviewed": True},
        "created_at": "2026-09-02T08:00:00Z",
    }


def _answer_question(*, decision_id: str = "decision-1") -> dict[str, object]:
    return {
        "question_id": "question-intervention",
        "question": "Complete the missing inner derivative.",
        "answer": "2x",
        "question_type": "math_exact",
        "difficulty": 3,
        "topic": TOPIC,
        "target_binding": {
            "target_topic_id": TOPIC,
            "validation_status": "passed",
            "cognitive_learning_intent": "misconception_repair",
            "cognitive_decision_id": decision_id,
            "diagnostic_validation_id": "validation-1",
            # These client-carried fields are deliberately forged. The tracker
            # must copy authoritative provenance from question_committed.
            "cognitive_repair_strategy": "forged-strategy",
            "cognitive_hypothesis_target": {
                "topic_id": "forged-topic",
                "code": "forged-code",
            },
        },
    }


def _tracker(Tracker: type[Any], store: Any):
    return Tracker(
        store,
        logger=_Logger(),
        cognitive_config={
            "projection_enabled": True,
            "read_mode": "active",
            "intent_policy": "on",
            "model_version": MODEL,
            "supported_topics": [TOPIC],
        },
        cognitive_extractor=_UnusedExtractor(),
    )


def _submit(tracker: Any, *, attempt_id: str, decision_id: str = "decision-1"):
    return tracker.on_answer(
        topic_id=TOPIC,
        question=_answer_question(decision_id=decision_id),
        user_answer="2x",
        eval_result={
            "verdict": "correct",
            "score": 100,
            "evaluator_type": "deterministic",
            "evaluator_version": "integration-test-v1",
        },
        mode="companion",
        session_id="session-intervention",
        response_time_ms=125,
        used_hint=False,
        require_existing_topic=True,
        origin_wrong_question_id="",
        attempt_id=attempt_id,
    )


def _assert_ordinary_answer_committed(
    store: Any, attempt_id: str, *, cognitive: bool = False
) -> None:
    fact = store.get_attempt_fact(attempt_id)
    assert fact is not None
    assert fact["question_id"] == "question-intervention"
    assert fact["topic_id"] == TOPIC
    assert fact["eval_result"]["verdict"] == "correct"
    assert fact["evaluation_metadata"] == {
        "evaluator_type": "deterministic",
        "evaluator_version": "integration-test-v1",
        "confidence": None,
        "fallback_reason": "",
    }
    if not cognitive:
        assert "learning_intent" not in fact["question"]
        assert "diagnostic_validation_id" not in fact["question"]
        binding = fact["question"].get("target_binding") or {}
        assert not any(str(key).startswith("cognitive_") for key in binding)
    qa = store.list_qa_records(limit=1)[0]
    assert qa["question"]["attempt_id"] == attempt_id
    assert qa["eval_result"]["verdict"] == "correct"


def test_tracker_builds_attempt_event_from_persisted_question_and_commits_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, tracker_module = _load_runtime(
        monkeypatch, "_cognitive_answer_event_valid"
    )
    store = _open_store(tmp_path, store_module.StudyStore)
    try:
        committed = store.record_cognitive_intervention_event(_question_event())
        result = _submit(
            _tracker(tracker_module.KnowledgeTracker, store),
            attempt_id="attempt-intervention",
        )

        assert result["topic_id"] == TOPIC
        _assert_ordinary_answer_committed(
            store, "attempt-intervention", cognitive=True
        )
        events = store.list_cognitive_intervention_events(
            decision_id="decision-1", event_types=("attempt_committed",)
        )
        assert len(events) == 1
        attempt_event = events[0]
        assert attempt_event["attempt_id"] == "attempt-intervention"
        assert attempt_event["evaluation_verdict"] == "correct"
        assert attempt_event["question_id"] == "question-intervention"
        assert attempt_event["repair_strategy"] == committed["repair_strategy"]
        assert attempt_event["hypothesis_target"] == committed["hypothesis_target"]
        assert attempt_event["binding"] == committed["binding"]
    finally:
        store.close()


def test_forged_decision_skips_attempt_event_but_preserves_ordinary_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, tracker_module = _load_runtime(
        monkeypatch, "_cognitive_answer_event_forged"
    )
    store = _open_store(tmp_path, store_module.StudyStore)
    try:
        store.record_cognitive_intervention_event(_question_event())

        result = _submit(
            _tracker(tracker_module.KnowledgeTracker, store),
            attempt_id="attempt-forged",
            decision_id="decision-forged",
        )

        assert result["topic_id"] == TOPIC
        _assert_ordinary_answer_committed(store, "attempt-forged")
        events = store.list_cognitive_intervention_events()
        assert [event["event_type"] for event in events] == ["question_committed"]
    finally:
        store.close()


def test_abandoned_question_cannot_commit_attempt_event_or_diagnostic_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, tracker_module = _load_runtime(
        monkeypatch, "_cognitive_answer_event_abandoned"
    )
    store = _open_store(tmp_path, store_module.StudyStore)
    try:
        committed = store.record_cognitive_intervention_event(_question_event())
        abandoned = dict(committed)
        abandoned.update(
            {
                "event_id": "event-abandoned",
                "event_type": "intervention_abandoned",
                "attempt_id": "",
                "evaluation_verdict": "",
                "abandonment_reason": "question_commit_not_published",
            }
        )
        abandoned.pop("event_seq", None)
        store.record_cognitive_intervention_event(abandoned)

        result = _submit(
            _tracker(tracker_module.KnowledgeTracker, store),
            attempt_id="attempt-after-abandonment",
        )

        assert result["topic_id"] == TOPIC
        _assert_ordinary_answer_committed(store, "attempt-after-abandonment")
        assert store.list_cognitive_intervention_events(
            event_types=("attempt_committed",)
        ) == []
    finally:
        store.close()


def test_attempt_commit_is_terminal_and_cannot_be_abandoned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, tracker_module = _load_runtime(
        monkeypatch, "_cognitive_answer_event_terminal"
    )
    store = _open_store(tmp_path, store_module.StudyStore)
    try:
        committed = store.record_cognitive_intervention_event(_question_event())
        _submit(
            _tracker(tracker_module.KnowledgeTracker, store),
            attempt_id="attempt-terminal",
        )
        abandoned = dict(committed)
        abandoned.update(
            {
                "event_id": "event-terminal-abandon",
                "event_type": "intervention_abandoned",
                "attempt_id": "",
                "evaluation_verdict": "",
                "abandonment_reason": "late_cancel",
            }
        )
        abandoned.pop("event_seq", None)
        with pytest.raises(ValueError, match="terminal"):
            store.record_cognitive_intervention_event(abandoned)
    finally:
        store.close()


@pytest.mark.parametrize("action", ["dismiss", "suppress", "delete"])
def test_user_override_skips_attempt_event_but_preserves_ordinary_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str
) -> None:
    package_name = f"_cognitive_answer_event_control_{action}"
    store_module, tracker_module = _load_runtime(monkeypatch, package_name)
    store = _open_store(tmp_path, store_module.StudyStore)
    try:
        store.record_cognitive_intervention_event(_question_event())
        store.record_cognitive_user_control(
            topic_id=TOPIC,
            hypothesis_code=HYPOTHESIS,
            action=action,
            reason="integration test",
            expires_at=(
                (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
                if action == "suppress"
                else ""
            ),
        )

        attempt_id = f"attempt-{action}"
        result = _submit(
            _tracker(tracker_module.KnowledgeTracker, store),
            attempt_id=attempt_id,
        )

        assert result["topic_id"] == TOPIC
        _assert_ordinary_answer_committed(store, attempt_id)
        assert store.list_cognitive_intervention_events(
            event_types=("attempt_committed",)
        ) == []
    finally:
        store.close()


def test_cognitive_event_insert_failure_preserves_answer_and_failed_outbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, tracker_module = _load_runtime(
        monkeypatch, "_cognitive_answer_event_insert_failure"
    )
    store = _open_store(tmp_path, store_module.StudyStore)
    try:
        store.record_cognitive_intervention_event(_question_event())

        def fail_insert(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected cognitive event failure")

        intervention_module = importlib.import_module(
            f"{store_module.__package__}.store_cognitive_intervention"
        )
        original_insert = intervention_module.insert_cognitive_intervention_event
        monkeypatch.setattr(intervention_module, "insert_cognitive_intervention_event", fail_insert)
        result = _submit(
            _tracker(tracker_module.KnowledgeTracker, store),
            attempt_id="attempt-insert-failure",
        )

        assert result["topic_id"] == TOPIC
        _assert_ordinary_answer_committed(store, "attempt-insert-failure", cognitive=True)
        events = store.list_cognitive_intervention_events()
        assert [event["event_type"] for event in events] == ["question_committed"]
        failed = store.list_cognitive_outbox(status="failed")
        assert len(failed) == 1
        assert failed[0]["attempt_id"] == "attempt-insert-failure"
        assert failed[0]["retry_count"] == 1
        assert "injected cognitive event failure" in failed[0]["last_error"]
        assert "user_answer" not in failed[0]["payload"]
        assert "expected_answer" not in failed[0]["payload"]

        monkeypatch.setattr(
            intervention_module,
            "insert_cognitive_intervention_event",
            original_insert,
        )
        assert store.process_cognitive_outbox() == {
            "claimed": 1,
            "completed": 1,
            "failed": 0,
            "lease_lost": 0,
        }
        assert len(
            [
                row
                for row in store.list_cognitive_outbox(status="done")
                if row["operation"] == "intervention_event"
            ]
        ) == 1
        assert [event["event_type"] for event in store.list_cognitive_intervention_events()] == [
            "question_committed",
            "attempt_committed",
        ]
    finally:
        store.close()
