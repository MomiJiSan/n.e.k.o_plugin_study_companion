from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def debug(self, *_args, **_kwargs):
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
    store.ensure_topic(topic_id="calculus.chain_rule", name="Chain rule")
    return store


def _answer_kwargs(attempt_id: str) -> dict:
    return {
        "session_id": "session-a",
        "mode": "companion",
        "topic_id": "calculus.chain_rule",
        "question": {
            "question_id": f"question-{attempt_id}",
            "question": "Differentiate sin(x^2).",
            "answer": "2x cos(x^2)",
            "question_type": "math_exact",
            "difficulty": 3,
        },
        "user_answer": "cos(x^2)",
        "eval_result": {"verdict": "wrong", "score": 0, "confidence": 0.95},
        "response_time_ms": 420,
        "attempt_id": attempt_id,
    }


def _count(store, table: str) -> int:
    return int(
        store._require_read_conn()
        .execute(f"SELECT COUNT(*) FROM {table}")
        .fetchone()[0]
    )


def _evidence(attempt_id: str) -> dict:
    return {
        "attempt_id": attempt_id,
        "topic_id": "calculus.chain_rule",
        "hypothesis_code": "omit_inner_derivative",
        "direction": "support",
        "strength": 0.72,
        "extractor_confidence": 0.81,
        "diagnosticity": 0.4,
        "source_kind": "structured_attempt",
        "evidence_span": "cos(x^2) appears without 2x",
    }


def _snapshot(attempt_id: str, *, probability: float = 0.68) -> dict:
    return {
        "hypothesis_id": "calculus.chain_rule:omit_inner_derivative",
        "topic_id": "calculus.chain_rule",
        "hypothesis_code": "omit_inner_derivative",
        "status": "hypothesized",
        "probability": probability,
        "support_count": 1,
        "counter_count": 0,
        "diagnostic_support_count": 0,
        "relapse_count": 0,
        "source_attempt_id": attempt_id,
        "model_version": "cognitive-v1",
    }


def test_projection_disabled_is_a_strict_no_write_and_enabled_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_store_enqueue")
    store = _store(tmp_path, Store)
    try:
        store.batch_write_answer_data(**_answer_kwargs("attempt-disabled"))
        assert _count(store, "cognitive_projection_queue") == 0

        first = store.batch_write_answer_data(
            **_answer_kwargs("attempt-enabled"),
            enqueue_cognitive_projection=True,
            cognitive_extractor_version="extractor-v1",
        )
        duplicate = store.batch_write_answer_data(
            **_answer_kwargs("attempt-enabled"),
            enqueue_cognitive_projection=True,
            cognitive_extractor_version="extractor-v2",
        )
        assert first["ok"] is True
        assert duplicate["duplicate_attempt"] is True
        assert _count(store, "cognitive_projection_queue") == 1
        queue_item = store.list_cognitive_projection_queue()[0]
        assert queue_item["attempt_id"] == "attempt-enabled"
        assert queue_item["status"] == "pending"
        assert queue_item["retry_count"] == 0
        assert queue_item["last_error"] == ""
        assert queue_item["lease_token"] == ""
        assert queue_item["extractor_version"] == "extractor-v1"
    finally:
        store.close()

    reopened = _store(tmp_path, Store)
    try:
        queue = reopened.list_cognitive_projection_queue()
        assert len(queue) == 1
        assert queue[0]["attempt_id"] == "attempt-enabled"
        assert queue[0]["extractor_version"] == "extractor-v1"
    finally:
        reopened.close()


def test_queue_insert_rolls_back_with_answer_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_store_rollback")
    store = _store(tmp_path, Store)
    try:
        def fail_legacy_qa(*_args, **_kwargs):
            raise RuntimeError("injected QA failure")

        monkeypatch.setattr(store, "_batch_write_qa_record", fail_legacy_qa)
        with pytest.raises(RuntimeError, match="injected QA failure"):
            store.batch_write_answer_data(
                **_answer_kwargs("attempt-rollback"),
                enqueue_cognitive_projection=True,
            )

        assert _count(store, "attempts") == 0
        assert _count(store, "evaluations") == 0
        assert _count(store, "cognitive_projection_queue") == 0
    finally:
        store.close()


def test_unbound_attempt_is_not_cognitive_projection_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_store_unbound")
    store = _store(tmp_path, Store)
    try:
        payload = _answer_kwargs("attempt-unbound")
        payload["topic_id"] = ""
        store.batch_write_answer_data(
            **payload,
            enqueue_cognitive_projection=True,
        )
        assert _count(store, "attempts") == 1
        assert _count(store, "cognitive_projection_queue") == 0
        assert store.get_cognitive_projection_input("attempt-unbound") is None
    finally:
        store.close()


def test_claim_input_and_complete_are_lease_fenced_and_atomic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_store_complete")
    store = _store(tmp_path, Store)
    try:
        store.batch_write_answer_data(
            **_answer_kwargs("attempt-complete"),
            enqueue_cognitive_projection=True,
        )
        claimed = store.claim_cognitive_projections()[0]
        projection_input = store.get_cognitive_projection_input("attempt-complete")
        assert projection_input is not None
        assert projection_input["topic_id"] == "calculus.chain_rule"
        assert projection_input["question_text"] == "Differentiate sin(x^2)."
        assert projection_input["expected_answer"] == "2x cos(x^2)"
        assert projection_input["learner_answer"] == "cos(x^2)"
        assert projection_input["evaluation"]["verdict"] == "wrong"
        assert projection_input["extractor_version"] == "cognitive-extractor-v1"

        completed = store.complete_cognitive_projection(
            attempt_id="attempt-complete",
            lease_token=claimed["lease_token"],
            evidence=[_evidence("attempt-complete")],
            snapshots=[_snapshot("attempt-complete")],
        )
        assert completed["status"] == "done"
        assert completed["evidence_inserted"] == 1
        assert _count(store, "cognitive_evidence") == 1
        assert _count(store, "cognitive_hypothesis_snapshots") == 1
        assert store.list_cognitive_projection_queue()[0]["status"] == "done"
        with pytest.raises(ValueError, match="lease is no longer active"):
            store.complete_cognitive_projection(
                attempt_id="attempt-complete",
                lease_token=claimed["lease_token"],
                evidence=[_evidence("attempt-complete")],
            )
        assert _count(store, "cognitive_evidence") == 1
    finally:
        store.close()


def test_invalid_projection_output_writes_nothing_and_keeps_active_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_store_invalid_output")
    store = _store(tmp_path, Store)
    try:
        store.batch_write_answer_data(
            **_answer_kwargs("attempt-invalid"),
            enqueue_cognitive_projection=True,
        )
        claimed = store.claim_cognitive_projections()[0]
        duplicate = _evidence("attempt-invalid")
        with pytest.raises(ValueError, match="only one evidence item"):
            store.complete_cognitive_projection(
                attempt_id="attempt-invalid",
                lease_token=claimed["lease_token"],
                evidence=[duplicate, dict(duplicate)],
                snapshots=[_snapshot("attempt-invalid")],
            )

        assert _count(store, "cognitive_evidence") == 0
        assert _count(store, "cognitive_hypothesis_snapshots") == 0
        queue = store.list_cognitive_projection_queue()[0]
        assert queue["status"] == "processing"
        assert queue["lease_token"] == claimed["lease_token"]
    finally:
        store.close()


def test_snapshot_database_failure_rolls_back_evidence_and_queue_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_store_db_rollback")
    store = _store(tmp_path, Store)
    try:
        store.batch_write_answer_data(
            **_answer_kwargs("attempt-db-rollback"),
            enqueue_cognitive_projection=True,
        )
        claimed = store.claim_cognitive_projections()[0]
        store._require_conn().execute(
            """
            CREATE TRIGGER fail_cognitive_snapshot
            BEFORE INSERT ON cognitive_hypothesis_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'injected snapshot failure');
            END
            """
        )
        store._require_conn().commit()
        with pytest.raises(sqlite3.IntegrityError, match="injected snapshot failure"):
            store.complete_cognitive_projection(
                attempt_id="attempt-db-rollback",
                lease_token=claimed["lease_token"],
                evidence=[_evidence("attempt-db-rollback")],
                snapshots=[_snapshot("attempt-db-rollback")],
            )

        assert _count(store, "cognitive_evidence") == 0
        assert _count(store, "cognitive_hypothesis_snapshots") == 0
        queue = store.list_cognitive_projection_queue()[0]
        assert queue["status"] == "processing"
        assert queue["lease_token"] == claimed["lease_token"]
    finally:
        store.close()


def test_expired_lease_is_reclaimed_and_stale_worker_is_fenced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_store_lease")
    store = _store(tmp_path, Store)
    try:
        store.batch_write_answer_data(
            **_answer_kwargs("attempt-lease"),
            enqueue_cognitive_projection=True,
        )
        first = store.claim_cognitive_projections()[0]
        store._require_conn().execute(
            """UPDATE cognitive_projection_queue
            SET updated_at = datetime('now', '-20 minutes')
            WHERE attempt_id = 'attempt-lease'"""
        )
        store._require_conn().commit()

        reclaimed = store.claim_cognitive_projections()[0]
        assert reclaimed["lease_token"] != first["lease_token"]
        assert reclaimed["retry_count"] == 1
        assert not store.mark_cognitive_projection_failed(
            attempt_id="attempt-lease",
            lease_token=first["lease_token"],
            error="stale worker",
        )
        assert store.mark_cognitive_projection_failed(
            attempt_id="attempt-lease",
            lease_token=reclaimed["lease_token"],
            error="temporary failure",
        )
    finally:
        store.close()


def test_snapshot_rebuild_and_user_suppression_controls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_store_rebuild")
    store = _store(tmp_path, Store)
    try:
        store.batch_write_answer_data(
            **_answer_kwargs("attempt-rebuild"),
            enqueue_cognitive_projection=True,
        )
        written = store.upsert_cognitive_hypothesis_snapshot(
            _snapshot("attempt-rebuild", probability=0.6)
        )
        assert written["probability"] == pytest.approx(0.6)
        rebuilt = store.replace_cognitive_hypothesis_snapshots(
            topic_id="calculus.chain_rule",
            model_version="cognitive-v1",
            snapshots=[_snapshot("attempt-rebuild", probability=0.72)],
        )
        assert len(rebuilt) == 1
        assert rebuilt[0]["probability"] == pytest.approx(0.72)

        store.record_cognitive_user_control(
            topic_id="calculus.chain_rule",
            hypothesis_code="omit_inner_derivative",
            action="delete",
            reason="not applicable",
        )
        assert store.is_cognitive_hypothesis_suppressed(
            topic_id="calculus.chain_rule",
            hypothesis_code="omit_inner_derivative",
        )
        store.record_cognitive_user_control(
            topic_id="calculus.chain_rule",
            hypothesis_code="omit_inner_derivative",
            action="restore",
        )
        assert not store.is_cognitive_hypothesis_suppressed(
            topic_id="calculus.chain_rule",
            hypothesis_code="omit_inner_derivative",
        )
    finally:
        store.close()


def test_open_migrates_an_existing_database_with_additive_cognitive_tables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_store_migration")
    original = _store(tmp_path, Store)
    original.close()
    connection = sqlite3.connect(tmp_path / "study.db")
    try:
        connection.execute("DROP TABLE cognitive_hypothesis_snapshots")
        connection.execute("DROP TABLE cognitive_evidence")
        connection.execute("DROP TABLE cognitive_projection_queue")
        connection.execute("DROP TABLE cognitive_user_controls")
        connection.commit()
    finally:
        connection.close()

    reopened = _store(tmp_path, Store)
    try:
        tables = {
            str(row[0])
            for row in reopened._require_read_conn()
            .execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            .fetchall()
        }
        assert {
            "cognitive_projection_queue",
            "cognitive_evidence",
            "cognitive_hypothesis_snapshots",
            "cognitive_user_controls",
        } <= tables
        queue_columns = {
            str(row["name"])
            for row in reopened._require_read_conn()
            .execute("PRAGMA table_info(cognitive_projection_queue)")
            .fetchall()
        }
        assert {"lease_token", "extractor_version"} <= queue_columns
    finally:
        reopened.close()
