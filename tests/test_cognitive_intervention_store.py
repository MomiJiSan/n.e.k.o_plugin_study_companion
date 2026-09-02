from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

# isort: split
from adaptive_learning.cognitive_projection import CognitiveProjector

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "calculus.chain_rule"
MODEL = "cognitive-v1"
CODE = "omit_inner_derivative"
SNAPSHOT = f"{TOPIC}:{CODE}:{MODEL}:generation-7"


class _Logger:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


def _load_store(monkeypatch: pytest.MonkeyPatch, name: str):
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    mode_manager = ModuleType(f"{name}.mode_manager")
    setattr(mode_manager, "normalize_mode", lambda value: str(value or "companion"))
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{name}.store").StudyStore


def _store(tmp_path: Path, Store):
    store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    store.ensure_topic(topic_id=TOPIC, name="Chain rule")
    store.batch_write_answer_data(
        session_id="session-repair",
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
        (f"{TOPIC}:{CODE}", TOPIC, CODE, SNAPSHOT, MODEL),
    )
    conn.commit()
    return store


def _event(
    event_id: str,
    event_type: str,
    *,
    intent: str = "misconception_repair",
    attempt_id: str = "",
    verdict: str = "",
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "decision_id": "decision-1",
        "hypothesis_target": {
            "hypothesis_id": f"{TOPIC}:{CODE}",
            "topic_id": TOPIC,
            "code": CODE,
            "status": "supported",
            "probability": 0.91,
            "model_version": MODEL,
            "source_snapshot_id": SNAPSHOT,
            "source_attempt_id": "attempt-source",
            "projection_generation": 7,
        },
        "learning_intent": intent,
        "repair_strategy": "complete_inner_derivative",
        "binding": {
            "plan_id": "plan-1",
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
        "question_id": "question-source",
        "attempt_id": attempt_id,
        "blueprint_id": "chain.omit-inner.fill-factor.v1",
        "question_family_id": "chain.cos-cube.fill-factor",
        "diagnostic_validation_id": "validation-1",
        "evaluation_verdict": verdict,
        "policy_version": "cognitive-intent-policy-v2",
        "validator_version": "diagnostic-validator-v2",
        "schema_version": 1,
        "metadata": {"safe": True},
        "created_at": "2026-09-02T08:00:00Z",
    }


def _answer_kwargs(attempt_id: str, event: dict[str, object]) -> dict[str, object]:
    return {
        "session_id": "session-repair",
        "mode": "companion",
        "topic_id": TOPIC,
        "question": {
            "question_id": "question-source",
            "question": "Complete the missing inner derivative.",
            "answer": "2x",
            "question_type": "math_exact",
            "difficulty": 3,
        },
        "user_answer": "x",
        "eval_result": {"verdict": "wrong", "score": 0},
        "response_time_ms": 120,
        "attempt_id": attempt_id,
        "cognitive_intervention_event": event,
    }


def test_question_and_attempt_events_are_idempotent_and_dirty_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_intervention_idempotent")
    store = _store(tmp_path, Store)
    try:
        question = _event("event-question", "question_committed")
        first = store.record_cognitive_intervention_event(question)
        duplicate = store.record_cognitive_intervention_event(question)
        assert first == duplicate
        state = store.get_cognitive_topic_projection_state(
            topic_id=TOPIC, model_version=MODEL
        )
        assert state is not None and state["requested_generation"] == 8

        with pytest.raises(ValueError, match="identity collision"):
            store.record_cognitive_intervention_event(
                {**question, "repair_strategy": "compare_steps"}
            )
        attempt = _event(
            "event-attempt",
            "attempt_committed",
            attempt_id="attempt-source",
            verdict="wrong",
        )
        stored = store.record_cognitive_intervention_event(attempt)
        assert stored["session_id"] == "session-repair"
        assert store.get_cognitive_topic_projection_state(
            topic_id=TOPIC, model_version=MODEL
        )["requested_generation"] == 9
    finally:
        store.close()


def test_attempt_requires_matching_question_and_transaction_helper_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_intervention_atomic")
    store = _store(tmp_path, Store)
    try:
        with pytest.raises(ValueError, match="matching committed question"):
            store.record_cognitive_intervention_event(
                _event(
                    "event-orphan",
                    "attempt_committed",
                    attempt_id="attempt-source",
                    verdict="correct",
                )
            )
        with pytest.raises(RuntimeError, match="rollback"):
            with store.transaction() as conn:
                store.insert_cognitive_intervention_event(
                    conn,
                    _event("event-question", "question_committed"),
                )
                raise RuntimeError("rollback")
        assert store.list_cognitive_intervention_events() == []
        assert store.get_cognitive_topic_projection_state(
            topic_id=TOPIC, model_version=MODEL
        )["requested_generation"] == 7
    finally:
        store.close()


def test_abandoned_event_requires_and_records_a_committed_question(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_intervention_abandoned")
    store = _store(tmp_path, Store)
    try:
        orphan = {
            **_event("event-orphan", "intervention_abandoned"),
            "abandonment_reason": "scope_revision_changed",
        }
        with pytest.raises(ValueError, match="matching prior event"):
            store.record_cognitive_intervention_event(orphan)
        store.record_cognitive_intervention_event(
            _event("event-question", "question_committed")
        )
        abandoned = {
            **_event("event-abandoned", "intervention_abandoned"),
            "abandonment_reason": "scope_revision_changed",
        }
        stored = store.record_cognitive_intervention_event(abandoned)

        assert stored["abandonment_reason"] == "scope_revision_changed"
        assert len(store.list_cognitive_intervention_events()) == 2
        assert store.get_cognitive_topic_projection_state(
            topic_id=TOPIC, model_version=MODEL
        )["requested_generation"] == 9
    finally:
        store.close()


def test_ledger_contains_no_original_answer_and_delete_keeps_tombstone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_intervention_delete")
    store = _store(tmp_path, Store)
    try:
        columns = {
            str(row["name"])
            for row in store._require_read_conn()
            .execute("PRAGMA table_info(cognitive_intervention_events)")
            .fetchall()
        }
        assert not {"user_answer", "learner_answer", "evaluation_json"} & columns
        store.record_cognitive_intervention_event(
            _event("event-question", "question_committed")
        )
        store.record_cognitive_user_control(
            topic_id=TOPIC, hypothesis_code=CODE, action="delete"
        )
        assert store.list_cognitive_intervention_events() == []
        assert store.list_cognitive_user_controls(
            topic_id=TOPIC, hypothesis_code=CODE, limit=1
        )[0]["action"] == "delete"
        assert store._require_read_conn().execute(
            "SELECT COUNT(*) FROM attempts"
        ).fetchone()[0] == 1
        proposal = _event("event-proposal", "intent_proposed")
        store.record_cognitive_intervention_event(proposal)
        deleted = store.purge_all()
        assert deleted["cognitive_intervention_events"] == 1
    finally:
        store.close()


def test_open_migrates_database_missing_intervention_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_intervention_migration")
    original = _store(tmp_path, Store)
    original.close()
    connection = sqlite3.connect(tmp_path / "study.db")
    try:
        connection.execute("DROP TABLE cognitive_intervention_events")
        connection.commit()
    finally:
        connection.close()
    reopened = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    reopened.open()
    try:
        tables = {
            row[0]
            for row in reopened._require_read_conn()
            .execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            .fetchall()
        }
        assert "cognitive_intervention_events" in tables
    finally:
        reopened.close()


def test_topic_projector_folds_committed_repair_result_into_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _UnusedExtractor:
        async def extract(self, _input):
            raise AssertionError("dirty topic projection must not call extractor")

    Store = _load_store(monkeypatch, "_cognitive_intervention_projection")
    store = _store(tmp_path, Store)
    try:
        store._require_conn().execute(
            """
            INSERT INTO cognitive_evidence (
                evidence_id, attempt_id, topic_id, hypothesis_code, direction,
                strength, extractor_confidence, diagnosticity, source_kind,
                evidence_span, extractor_version, evidence_family_id,
                question_id, session_id, diagnostic_validation_id
            ) VALUES (
                'evidence-1', 'attempt-source', ?, ?, 'support',
                1.0, 1.0, 1.0, 'misconception_probe', 'missing 2x',
                'cognitive-extractor-v1', 'diagnostic:probe-1',
                'question-source', 'session-repair', 'validation-probe'
            )
            """,
            (TOPIC, CODE),
        )
        store._require_conn().commit()
        store.record_cognitive_intervention_event(
            _event("event-question", "question_committed")
        )
        store.record_cognitive_intervention_event(
            _event(
                "event-attempt",
                "attempt_committed",
                attempt_id="attempt-source",
                verdict="correct",
            )
        )

        summary = asyncio.run(
            CognitiveProjector(store, _UnusedExtractor()).process_dirty_topics()
        )
        current = store.list_cognitive_hypothesis_current(
            topic_id=TOPIC, hypothesis_code=CODE, model_version=MODEL
        )[0]

        assert summary.rebuilt == 1
        assert current["status"] == "provisionally_resolved"
        assert current["intervention_stage"] == "provisionally_resolved"
        assert current["last_intent"] == "misconception_repair"
        assert current["last_outcome"] == "correct"
        assert current["consecutive_repair_failures"] == 0
    finally:
        store.close()


def test_answer_transaction_records_intervention_once_and_duplicate_does_not_repeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_answer_intervention")
    store = _store(tmp_path, Store)
    try:
        store.record_cognitive_intervention_event(
            _event("event-question", "question_committed")
        )
        attempt_event = _event(
            "event-attempt",
            "attempt_committed",
            attempt_id="attempt-new",
            verdict="wrong",
        )
        kwargs = _answer_kwargs("attempt-new", attempt_event)

        first = store.batch_write_answer_data(**kwargs)
        duplicate = store.batch_write_answer_data(**kwargs)

        assert first["cognitive_intervention_event"] == {
            "recorded": True,
            "error": "",
        }
        assert duplicate["duplicate_attempt"] is True
        assert duplicate["cognitive_intervention_event"] == {
            "recorded": False,
            "error": "duplicate_attempt",
        }
        assert len(store.list_cognitive_intervention_events()) == 2
    finally:
        store.close()


def test_answer_without_intervention_keeps_legacy_result_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_answer_no_intervention")
    store = _store(tmp_path, Store)
    try:
        payload = _answer_kwargs("attempt-plain", {})
        payload.pop("cognitive_intervention_event")
        result = store.batch_write_answer_data(**payload)

        assert "cognitive_intervention_event" not in result
    finally:
        store.close()


def test_invalid_intervention_rolls_back_answer_facts_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_answer_intervention_invalid")
    store = _store(tmp_path, Store)
    try:
        store.record_cognitive_intervention_event(
            _event("event-question", "question_committed")
        )
        detached = _event(
            "event-attempt",
            "attempt_committed",
            attempt_id="another-attempt",
            verdict="wrong",
        )

        with pytest.raises(ValueError, match="detached"):
            store.batch_write_answer_data(**_answer_kwargs("attempt-new", detached))

        assert store.get_attempt_fact("attempt-new") is None
        assert len(store.list_cognitive_intervention_events()) == 1
    finally:
        store.close()


def test_later_answer_failure_rolls_back_successful_intervention_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_answer_intervention_rollback")
    store = _store(tmp_path, Store)
    try:
        store.record_cognitive_intervention_event(
            _event("event-question", "question_committed")
        )
        attempt_event = _event(
            "event-attempt",
            "attempt_committed",
            attempt_id="attempt-new",
            verdict="wrong",
        )

        def fail_legacy_qa(*_args, **_kwargs):
            raise RuntimeError("injected later failure")

        monkeypatch.setattr(store, "_batch_write_qa_record", fail_legacy_qa)
        with pytest.raises(RuntimeError, match="injected later failure"):
            store.batch_write_answer_data(
                **_answer_kwargs("attempt-new", attempt_event)
            )

        assert store.get_attempt_fact("attempt-new") is None
        events = store.list_cognitive_intervention_events()
        assert [item["event_type"] for item in events] == ["question_committed"]
        assert store.get_cognitive_topic_projection_state(
            topic_id=TOPIC, model_version=MODEL
        )["requested_generation"] == 8
    finally:
        store.close()
