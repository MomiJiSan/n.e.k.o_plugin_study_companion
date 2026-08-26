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


def _answer_kwargs(attempt_id: str = "attempt-1") -> dict:
    return {
        "session_id": "session-a",
        "mode": "companion",
        "topic_id": "topic-a",
        "question": {
            "question_id": "question-a",
            "question": "What is two plus two?",
            "answer": "4",
            "question_type": "math_exact",
            "difficulty": 3,
        },
        "user_answer": "4",
        "eval_result": {"verdict": "correct", "score": 100},
        "response_time_ms": 420,
        "attempt_id": attempt_id,
    }


def _count(store, table: str) -> int:
    return int(store._require_read_conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_answer_batch_dual_writes_immutable_facts_before_legacy_qa(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_attempt_facts_dual_write")
    store = _store(tmp_path, Store)
    try:
        result = store.batch_write_answer_data(**_answer_kwargs())
        assert result["ok"] is True
        assert {
            "question_instances": _count(store, "question_instances"),
            "attempts": _count(store, "attempts"),
            "evaluations": _count(store, "evaluations"),
            "qa_records": _count(store, "qa_records"),
        } == {
            "question_instances": 1,
            "attempts": 1,
            "evaluations": 1,
            "qa_records": 1,
        }
        fact = store.get_attempt_fact("attempt-1")
        assert fact is not None
        assert fact["storage"] == "attempt_facts"
        assert fact["question_id"] == "question-a"
        assert fact["question"]["answer"] == "4"
        assert "attempt_id" not in fact["question"]
        assert fact["eval_result"] == {"verdict": "correct", "score": 100}
        assert store.list_qa_records(limit=1)[0]["question"]["attempt_id"] == "attempt-1"
    finally:
        store.close()


def test_attempt_id_remains_idempotent_without_duplicate_fact_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_attempt_facts_idempotent")
    store = _store(tmp_path, Store)
    try:
        first = store.batch_write_answer_data(**_answer_kwargs())
        second = store.batch_write_answer_data(
            **{
                **_answer_kwargs(),
                "user_answer": "different retry",
                "eval_result": {"verdict": "wrong", "score": 0},
            }
        )
        assert first["ok"] is True
        assert second["duplicate_attempt"] is True
        assert second["existing_eval_result"] == {"verdict": "correct", "score": 100}
        assert _count(store, "question_instances") == 1
        assert _count(store, "attempts") == 1
        assert _count(store, "evaluations") == 1
        assert _count(store, "qa_records") == 1
    finally:
        store.close()


def test_attempt_fact_reader_falls_back_to_legacy_qa_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_attempt_facts_fallback")
    store = _store(tmp_path, Store)
    try:
        store.ensure_session(session_id="legacy-session", mode="companion")
        store.add_qa_record(
            session_id="legacy-session",
            topic_id="topic-a",
            question={"question": "legacy", "attempt_id": "legacy-attempt"},
            user_answer="answer",
            eval_result={"verdict": "partial", "score": 50},
            mode="companion",
        )
        fact = store.get_attempt_fact("legacy-attempt")
        assert fact is not None
        assert fact["storage"] == "legacy_qa_record"
        assert fact["question_id"] == "legacy-attempt"
        assert fact["eval_result"]["verdict"] == "partial"
    finally:
        store.close()


def test_legacy_qa_failure_rolls_back_new_fact_rows_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_attempt_facts_rollback")
    store = _store(tmp_path, Store)
    try:
        def fail_legacy_qa(*_args, **_kwargs):
            raise RuntimeError("injected legacy QA failure")

        monkeypatch.setattr(store, "_batch_write_qa_record", fail_legacy_qa)
        with pytest.raises(RuntimeError, match="injected legacy QA failure"):
            store.batch_write_answer_data(**_answer_kwargs("attempt-rollback"))
        assert _count(store, "question_instances") == 0
        assert _count(store, "attempts") == 0
        assert _count(store, "evaluations") == 0
        assert _count(store, "qa_records") == 0
        assert store.get_attempt_fact("attempt-rollback") is None
    finally:
        store.close()


def test_open_adds_fact_tables_to_an_existing_legacy_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_attempt_facts_migration")
    first = _store(tmp_path, Store)
    try:
        first.ensure_session(session_id="legacy-session", mode="companion")
        first.add_qa_record(
            session_id="legacy-session",
            topic_id="topic-a",
            question={"question": "legacy", "attempt_id": "legacy-attempt"},
            user_answer="answer",
            eval_result={"verdict": "wrong", "score": 0},
            mode="companion",
        )
    finally:
        first.close()

    connection = sqlite3.connect(tmp_path / "study.db")
    try:
        connection.execute("DROP TABLE evaluations")
        connection.execute("DROP TABLE attempts")
        connection.execute("DROP TABLE question_instances")
        connection.commit()
    finally:
        connection.close()

    reopened = _store(tmp_path, Store)
    try:
        tables = {
            row[0]
            for row in reopened._require_read_conn()
            .execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            .fetchall()
        }
        assert {"question_instances", "attempts", "evaluations"} <= tables
        assert reopened.get_attempt_fact("legacy-attempt")["storage"] == "legacy_qa_record"
        reopened.batch_write_answer_data(**_answer_kwargs("migrated-attempt"))
        assert reopened.get_attempt_fact("migrated-attempt")["storage"] == "attempt_facts"
    finally:
        reopened.close()
