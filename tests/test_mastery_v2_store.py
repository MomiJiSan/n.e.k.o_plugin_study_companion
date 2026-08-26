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
    mode_manager.normalize_mode = lambda value: str(value or "companion")
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{name}.store").StudyStore


def _store(tmp_path: Path, Store):
    store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    store.ensure_topic(topic_id="topic-a", name="Topic A")
    return store


def _answer_kwargs(attempt_id: str) -> dict:
    return {
        "session_id": "session-a",
        "mode": "companion",
        "topic_id": "topic-a",
        "question": {
            "question_id": f"question-{attempt_id}",
            "question": "What is two plus two?",
            "answer": "4",
            "question_type": "math_exact",
            "difficulty": 3,
        },
        "user_answer": "4",
        "eval_result": {
            "verdict": "correct",
            "score": 100,
            "confidence": 0.95,
        },
        "response_time_ms": 420,
        "attempt_id": attempt_id,
    }


def _count(store, table: str) -> int:
    row = store._require_read_conn().execute(
        f"SELECT COUNT(*) FROM {table}"
    ).fetchone()
    return int(row[0])


def _snapshot(attempt_id: str) -> dict:
    return {
        "topic_id": "topic-a",
        "mastery": 0.8,
        "accuracy": 1.0,
        "recency": 0.9,
        "consistency": 0.75,
        "confidence": 0.6,
        "evidence_count": 1,
        "unresolved_wrong_count": 0,
        "mastery_model_version": "mastery-v2-shadow-1",
        "source_attempt_id": attempt_id,
        "computed_at": "2026-08-26T12:00:00+00:00",
    }


def test_shadow_disabled_does_not_enqueue_and_unknown_hint_stays_null(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_mastery_v2_disabled")
    store = _store(tmp_path, Store)
    try:
        store.batch_write_answer_data(**_answer_kwargs("attempt-disabled"))

        assert _count(store, "mastery_projection_queue") == 0
        assert store.get_attempt_fact("attempt-disabled")["used_hint"] is None
    finally:
        store.close()


def test_shadow_enqueue_is_atomic_idempotent_and_persists_trusted_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_mastery_v2_enqueue")
    store = _store(tmp_path, Store)
    try:
        first = store.batch_write_answer_data(
            **_answer_kwargs("attempt-queued"),
            used_hint=True,
            enqueue_mastery_v2=True,
        )
        duplicate = store.batch_write_answer_data(
            **_answer_kwargs("attempt-queued"),
            used_hint=False,
            enqueue_mastery_v2=True,
        )

        assert first["ok"] is True
        assert duplicate["duplicate_attempt"] is True
        assert _count(store, "mastery_projection_queue") == 1
        assert store.get_attempt_fact("attempt-queued")["used_hint"] is True
        assert store.list_mastery_projection_queue() == [
            {
                **store.list_mastery_projection_queue()[0],
                "attempt_id": "attempt-queued",
                "status": "pending",
                "retry_count": 0,
                "last_error": "",
            }
        ]
    finally:
        store.close()


def test_later_answer_failure_rolls_back_projection_queue_with_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_mastery_v2_rollback")
    store = _store(tmp_path, Store)
    try:
        def fail_legacy_qa(*_args, **_kwargs):
            raise RuntimeError("injected QA failure")

        monkeypatch.setattr(store, "_batch_write_qa_record", fail_legacy_qa)
        with pytest.raises(RuntimeError, match="injected QA failure"):
            store.batch_write_answer_data(
                **_answer_kwargs("attempt-rollback"),
                enqueue_mastery_v2=True,
            )

        assert _count(store, "attempts") == 0
        assert _count(store, "evaluations") == 0
        assert _count(store, "mastery_projection_queue") == 0
    finally:
        store.close()


def test_projection_retry_and_completion_are_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_mastery_v2_worker")
    store = _store(tmp_path, Store)
    try:
        store.batch_write_answer_data(
            **_answer_kwargs("attempt-worker"), enqueue_mastery_v2=True
        )

        assert store.claim_mastery_projections()[0]["status"] == "processing"
        assert store.mark_mastery_projection_failed(
            attempt_id="attempt-worker", error="temporary failure"
        )
        failed = store.list_mastery_projection_queue(statuses=("failed",))[0]
        assert failed["retry_count"] == 1
        assert failed["last_error"] == "temporary failure"

        conn = store._require_conn()
        conn.execute(
            """
            UPDATE mastery_projection_queue
            SET updated_at = datetime('now', '-10 minutes')
            WHERE attempt_id = ?
            """,
            ("attempt-worker",),
        )
        conn.commit()
        assert store.claim_mastery_projections()[0]["status"] == "processing"
        completed = store.complete_mastery_projection(_snapshot("attempt-worker"))
        repeated = store.complete_mastery_projection(_snapshot("attempt-worker"))

        assert completed["mastery"] == pytest.approx(0.8)
        assert repeated["id"] == completed["id"]
        assert _count(store, "mastery_snapshots_v2") == 1
        queue = store.list_mastery_projection_queue()[0]
        assert queue["status"] == "done"
        assert queue["retry_count"] == 1
        assert queue["last_error"] == ""
    finally:
        store.close()


def test_stale_processing_projection_lease_is_reclaimed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_mastery_v2_stale_lease")
    store = _store(tmp_path, Store)
    try:
        store.batch_write_answer_data(
            **_answer_kwargs("attempt-stale"), enqueue_mastery_v2=True
        )
        assert store.claim_mastery_projections()[0]["status"] == "processing"
        conn = store._require_conn()
        conn.execute(
            """
            UPDATE mastery_projection_queue
            SET updated_at = datetime('now', '-10 minutes')
            WHERE attempt_id = ?
            """,
            ("attempt-stale",),
        )
        conn.commit()

        reclaimed = store.claim_mastery_projections()

        assert reclaimed[0]["attempt_id"] == "attempt-stale"
        assert reclaimed[0]["status"] == "processing"
        assert reclaimed[0]["retry_count"] == 1
    finally:
        store.close()


def test_latest_v2_snapshot_follows_source_attempt_order_not_retry_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_mastery_v2_source_order")
    store = _store(tmp_path, Store)
    try:
        store.batch_write_answer_data(**_answer_kwargs("attempt-old"))
        store.batch_write_answer_data(**_answer_kwargs("attempt-new"))
        conn = store._require_conn()
        conn.execute(
            "UPDATE attempts SET submitted_at = ? WHERE attempt_id = ?",
            ("2026-08-24 12:00:00", "attempt-old"),
        )
        conn.execute(
            "UPDATE attempts SET submitted_at = ? WHERE attempt_id = ?",
            ("2026-08-25 12:00:00", "attempt-new"),
        )
        conn.commit()
        old_snapshot = {
            **_snapshot("attempt-old"),
            "mastery": 0.2,
            "evidence_count": 1,
            "computed_at": "2026-08-26T13:00:00+00:00",
        }
        new_snapshot = {
            **_snapshot("attempt-new"),
            "mastery": 0.9,
            "evidence_count": 2,
            "computed_at": "2026-08-26T12:00:00+00:00",
        }
        store.upsert_mastery_snapshot_v2(new_snapshot)
        store.upsert_mastery_snapshot_v2(old_snapshot)

        latest = store.get_latest_mastery_v2(
            topic_id="topic-a",
            mastery_model_version="mastery-v2-shadow-1",
        )
        listed = store.list_latest_mastery_v2_for_topics(
            ["topic-a"], mastery_model_version="mastery-v2-shadow-1"
        )

        assert latest is not None
        assert latest["source_attempt_id"] == "attempt-new"
        assert latest["evidence_count"] == 2
        assert listed[0]["source_attempt_id"] == "attempt-new"
    finally:
        store.close()


def test_real_projector_consumes_store_queue_without_touching_v1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_name = "_mastery_v2_real_projector"
    Store = _load_store(monkeypatch, package_name)
    projector_module = importlib.import_module(
        f"{package_name}.adaptive_learning.mastery_projection"
    )
    store = _store(tmp_path, Store)
    try:
        store.batch_write_answer_data(
            **_answer_kwargs("attempt-project"), enqueue_mastery_v2=True
        )
        v1_before = _count(store, "mastery_snapshots")

        summary = projector_module.MasteryV2Projector(store).process_pending()

        assert summary.completed == 1
        assert summary.failed == 0
        assert _count(store, "mastery_snapshots_v2") == 1
        assert _count(store, "mastery_snapshots") == v1_before
        assert store.list_mastery_projection_queue()[0]["status"] == "done"
    finally:
        store.close()


def test_evidence_is_fact_only_ordered_and_supports_attempt_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_mastery_v2_evidence")
    store = _store(tmp_path, Store)
    try:
        store.batch_write_answer_data(
            **_answer_kwargs("attempt-b"), enqueue_mastery_v2=True
        )
        store.batch_write_answer_data(
            **_answer_kwargs("attempt-a"), enqueue_mastery_v2=True
        )
        conn = store._require_conn()
        conn.execute(
            "UPDATE attempts SET submitted_at = '2026-08-26 10:00:00'"
        )
        conn.commit()

        all_evidence = store.list_mastery_v2_evidence(topic_id="topic-a")
        prefix = store.get_mastery_v2_projection_input("attempt-a")

        assert [item["attempt_id"] for item in all_evidence] == [
            "attempt-a",
            "attempt-b",
        ]
        assert store.list_mastery_v2_attempt_ids() == ["attempt-a", "attempt-b"]
        assert store.list_mastery_v2_attempt_ids(topic_id="topic-a") == [
            "attempt-a",
            "attempt-b",
        ]
        assert prefix is not None
        assert [item["attempt_id"] for item in prefix["evidence"]] == ["attempt-a"]
        assert prefix["unresolved_wrong_count"] == 0
        assert all_evidence[0]["difficulty"] == 3
        assert all_evidence[0]["evaluator_confidence"] == pytest.approx(0.95)
        assert all_evidence[0]["used_hint"] is None
    finally:
        store.close()


def test_open_adds_v2_tables_and_nullable_hint_to_legacy_attempts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_mastery_v2_migration")
    database = tmp_path / "study.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            CREATE TABLE attempts (
                attempt_id TEXT PRIMARY KEY,
                question_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                topic_id TEXT,
                user_answer TEXT NOT NULL,
                mode TEXT NOT NULL,
                response_time_ms INTEGER,
                submitted_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = _store(tmp_path, Store)
    try:
        columns = {
            str(row["name"])
            for row in store._require_read_conn()
            .execute("PRAGMA table_info(attempts)")
            .fetchall()
        }
        tables = {
            str(row["name"])
            for row in store._require_read_conn()
            .execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            .fetchall()
        }

        assert "used_hint" in columns
        assert {"mastery_snapshots_v2", "mastery_projection_queue"} <= tables
    finally:
        store.close()
