from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_study_companion_coverage_persistence"
if PACKAGE_NAME not in sys.modules:
    package = ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = package
mode_manager = ModuleType(f"{PACKAGE_NAME}.mode_manager")
mode_manager.normalize_mode = lambda value: str(value or "companion")
sys.modules[mode_manager.__name__] = mode_manager

StudyStore = importlib.import_module(f"{PACKAGE_NAME}.store").StudyStore
create_card = importlib.import_module(f"{PACKAGE_NAME}.fsrs_bridge").create_card


class _Logger:
    def debug(self, *_args, **_kwargs) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


def _open_store(tmp_path: Path) -> StudyStore:
    store = StudyStore(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    return store


def _answer_payload(attempt_id: str) -> dict:
    return {
        "session_id": "session-writeback",
        "mode": "companion",
        "topic_id": "topic-writeback",
        "question": {
            "question_id": "question-writeback",
            "question": "What is atomicity?",
            "answer": "All or nothing",
            "question_type": "short_answer",
            "difficulty": 3,
        },
        "user_answer": "All or nothing",
        "eval_result": {
            "verdict": "correct",
            "score": 100,
            "final_answer_correct": True,
            "evaluator_type": "exact_short_answer",
            "evaluator_version": "exact-short-answer-v1",
            "confidence": 1.0,
            "fallback_reason": "",
        },
        "response_time_ms": 410,
        "attempt_id": attempt_id,
        "enqueue_mastery_v2": True,
        "mastery_snapshot": {
            "topic_id": "topic-writeback",
            "mastery": 0.75,
            "accuracy": 1.0,
            "recency": 1.0,
            "consistency": 0.8,
            "confidence": 0.9,
            "level": "progressing",
            "attempts": 4,
            "flags": [],
        },
        "fsrs_card": create_card("topic-writeback").to_dict(),
        "fsrs_rating": 3,
        "review_log_data": {
            "card_id": None,
            "rating": 3,
            "scheduled_days": 2,
            "actual_days": 1,
        },
    }


def test_store_transaction_rolls_back_all_statements_and_commits_success(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="forced transaction failure"):
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO notebooks (id, name) VALUES ('rolled-back', 'Draft')"
                )
                conn.execute(
                    """INSERT INTO kv (key, value, updated_at)
                       VALUES ('rolled-back', '{}', 1.0)"""
                )
                raise RuntimeError("forced transaction failure")

        conn = store._require_conn()
        assert conn.execute(
            "SELECT COUNT(*) FROM notebooks WHERE id = 'rolled-back'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM kv WHERE key = 'rolled-back'"
        ).fetchone()[0] == 0

        with store.transaction() as conn:
            conn.execute(
                """INSERT INTO kv (key, value, updated_at)
                   VALUES ('committed', '{}', 1.0)"""
            )
        assert store.get_raw("committed") == {}
    finally:
        store.close()


def test_answer_writeback_late_failure_rolls_back_every_learning_table(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    store.ensure_topic(topic_id="topic-writeback", name="Writeback")
    try:
        store._require_conn().execute(
            """CREATE TEMP TRIGGER fail_answer_review_log
               BEFORE INSERT ON review_log
               BEGIN
                   SELECT RAISE(ABORT, 'forced review-log failure');
               END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="forced review-log failure"):
            store.batch_write_answer_data(**_answer_payload("attempt-rollback"))

        conn = store._require_conn()
        checks = {
            "sessions": "id = 'session-writeback'",
            "question_instances": "question_id = 'question-writeback'",
            "attempts": "attempt_id = 'attempt-rollback'",
            "evaluations": "attempt_id = 'attempt-rollback'",
            "qa_records": "json_extract(question, '$.attempt_id') = 'attempt-rollback'",
            "mastery_snapshots": "topic_id = 'topic-writeback'",
            "fsrs_cards": "topic_id = 'topic-writeback'",
            "review_log": "topic_id = 'topic-writeback'",
            "mastery_projection_queue": "attempt_id = 'attempt-rollback'",
        }
        for table, predicate in checks.items():
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {predicate}"
            ).fetchone()[0]
            assert count == 0, table
    finally:
        store.close()


def test_assessment_mastery_and_fsrs_writeback_survive_reopen(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.ensure_topic(topic_id="topic-writeback", name="Writeback")
    try:
        result = store.batch_write_answer_data(**_answer_payload("attempt-committed"))
        assert result["ok"] is True

        evaluation = store._require_conn().execute(
            """SELECT evaluator_type, evaluator_version, confidence, fallback_reason
               FROM evaluations WHERE attempt_id = ?""",
            ("attempt-committed",),
        ).fetchone()
        assert dict(evaluation) == {
            "evaluator_type": "exact_short_answer",
            "evaluator_version": "exact-short-answer-v1",
            "confidence": 1.0,
            "fallback_reason": "",
        }
        assert store.get_latest_mastery("topic-writeback")["mastery"] == 0.75
        assert store.get_fsrs_card("topic-writeback")["last_rating"] == 3
        assert store.list_review_log()[-1]["scheduled_days"] == 2
        queue = store.list_mastery_projection_queue(statuses=("pending",))
        assert len(queue) == 1
        assert queue[0]["attempt_id"] == "attempt-committed"
        assert queue[0]["status"] == "pending"
        assert queue[0]["retry_count"] == 0
        assert queue[0]["last_error"] == ""
        assert queue[0]["lease_token"] == ""
        assert queue[0]["created_at"]
        assert queue[0]["updated_at"]

        store.close()
        store.open()
        fact = store.get_attempt_fact("attempt-committed")
        assert fact["evaluation_metadata"] == {
            "evaluator_type": "exact_short_answer",
            "evaluator_version": "exact-short-answer-v1",
            "confidence": 1.0,
            "fallback_reason": "",
        }
        assert store.get_latest_mastery("topic-writeback")["attempts"] == 4
        assert store.get_fsrs_card("topic-writeback")["last_rating"] == 3
        assert store.list_review_log()[-1]["scheduled_days"] == 2
        reopened_queue = store.list_mastery_projection_queue(statuses=("pending",))
        assert len(reopened_queue) == 1
        assert reopened_queue[0]["attempt_id"] == "attempt-committed"
        assert reopened_queue[0]["status"] == "pending"
    finally:
        store.close()


def test_open_migrates_legacy_topic_and_memory_card_tables_in_place(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    store.close()
    db_path = tmp_path / "study.db"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE topics")
        conn.execute(
            """CREATE TABLE topics (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                subject TEXT NOT NULL,
                chapter TEXT,
                depth INTEGER DEFAULT 1,
                difficulty REAL DEFAULT 0.5,
                prerequisites TEXT NOT NULL DEFAULT '[]',
                related TEXT NOT NULL DEFAULT '[]',
                typical_misconceptions TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'runtime',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        conn.execute(
            """INSERT INTO topics (
                id, name, subject, chapter, prerequisites, related,
                typical_misconceptions, source
            ) VALUES ('legacy-topic', 'Legacy topic', 'math', 'algebra',
                      '[]', '[]', '[]', 'legacy')"""
        )
        conn.execute(
            """INSERT INTO decks (
                id, name, deck_type, subject, language, source
            ) VALUES (
                'legacy-deck', 'Legacy deck', 'custom', 'history', 'en', 'legacy'
            )"""
        )
        conn.execute(
            """INSERT INTO memory_items (
                id, deck_id, item_type, prompt, answer, metadata_json,
                fsrs_card_id, status
            ) VALUES (
                'legacy-item', 'legacy-deck', 'custom', 'Legacy prompt',
                'Legacy answer', '{}', NULL, 'active'
            )"""
        )
        conn.execute("DROP TABLE memory_fsrs_cards")
        conn.execute(
            """CREATE TABLE memory_fsrs_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL UNIQUE REFERENCES memory_items(id) ON DELETE CASCADE,
                card_data TEXT NOT NULL,
                fsrs_state TEXT DEFAULT 'new',
                last_rating INTEGER,
                updated_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        legacy_card_data = (
            '{"sentinel":"LEGACY-CARD-SENTINEL-42","topic_id":"legacy-item"}'
        )
        conn.execute(
            """INSERT INTO memory_fsrs_cards (
                id, item_id, card_data, fsrs_state, last_rating
            ) VALUES (?, ?, ?, ?, ?)""",
            (42, "legacy-item", legacy_card_data, "review", 3),
        )
        conn.execute(
            "UPDATE memory_items SET fsrs_card_id = 42 WHERE id = 'legacy-item'"
        )
        conn.commit()
    finally:
        conn.close()

    reopened = _open_store(tmp_path)
    try:
        topic = reopened.get_topic("legacy-topic")
        assert topic is not None
        assert topic["stage"] == ""
        assert topic["unit"] == "algebra"
        assert topic["skills"] == []
        assert topic["course_family"] == ""
        columns = {
            row["name"]
            for row in reopened._require_conn().execute(
                "PRAGMA table_info(memory_fsrs_cards)"
            )
        }
        assert "next_due" in columns
        legacy_card = reopened._require_conn().execute(
            """SELECT id, item_id, card_data, fsrs_state, last_rating, next_due
               FROM memory_fsrs_cards WHERE item_id = ?""",
            ("legacy-item",),
        ).fetchone()
        assert dict(legacy_card) == {
            "id": 42,
            "item_id": "legacy-item",
            "card_data": legacy_card_data,
            "fsrs_state": "review",
            "last_rating": 3,
            "next_due": None,
        }
        legacy_item = reopened._require_conn().execute(
            "SELECT deck_id, fsrs_card_id FROM memory_items WHERE id = ?",
            ("legacy-item",),
        ).fetchone()
        assert dict(legacy_item) == {
            "deck_id": "legacy-deck",
            "fsrs_card_id": 42,
        }
    finally:
        reopened.close()
