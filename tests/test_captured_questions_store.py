from __future__ import annotations

import enum
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

    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None

    def error(self, *_args, **_kwargs):
        return None

    def exception(self, *_args, **_kwargs):
        return None


def _load_store(monkeypatch: pytest.MonkeyPatch, name: str):
    if not hasattr(enum, "StrEnum"):
        class _CompatStrEnum(str, enum.Enum):
            pass

        monkeypatch.setattr(enum, "StrEnum", _CompatStrEnum, raising=False)
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    mode_manager = ModuleType(f"{name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"{name}.mode_manager", mode_manager)
    return importlib.import_module(f"{name}.store").StudyStore


def _make_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    study_store = _load_store(monkeypatch, "_captured_questions_store_test")
    store = study_store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    return store


def test_captured_question_persists_only_allowlisted_metadata_and_deduplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(tmp_path, monkeypatch)
    try:
        first = store.save_captured_question(
            text="  What is\r\n  2 + 2?  ",
            consent_origin="explain",
            topic_id="math.addition",
            subject="Math",
            classification={
                "screen_type": "question",
                "confidence": 0.93,
                "window_title": "private title",
                "text_excerpt": "private excerpt",
            },
        )
        second = store.save_captured_question(
            text="What is\n2 + 2?",
            consent_origin="generate",
        )

        assert second["id"] == first["id"]
        assert first["question_text"] == "What is\n2 + 2?"
        assert first["question_type"] == "question"
        assert first["classification_confidence"] == pytest.approx(0.93)
        assert "window_title" not in first
        assert "text_excerpt" not in first
        assert len(store.list_captured_questions()) == 1
    finally:
        store.close()


def test_captured_question_delete_unlinks_qa_history_and_expiry_purges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(tmp_path, monkeypatch)
    try:
        captured = store.save_captured_question(
            text="Solve x + 1 = 2",
            consent_origin="evaluate",
        )
        store.ensure_session(session_id="s1", mode="companion")
        store.add_qa_record(
            session_id="s1",
            topic_id="",
            question={"prompt": "Solve x + 1 = 2"},
            user_answer="1",
            eval_result={"verdict": "correct"},
            mode="companion",
            source_question_id=captured["id"],
        )
        assert store.list_qa_records()[-1]["source_question_id"] == captured["id"]
        assert store.delete_captured_question(captured["id"]) is True
        assert store.get_captured_question(captured["id"]) is None
        assert store.list_qa_records()[-1]["source_question_id"] == ""

        expired = store.save_captured_question(
            text="Expired question",
            consent_origin="explicit_save",
            expires_at="2000-01-01 00:00:00",
        )
        assert store.purge_expired_captured_questions() == 1
        assert store.get_captured_question(expired["id"]) is None
    finally:
        store.close()


def test_clear_captured_questions_unlinks_all_associated_qa_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(tmp_path, monkeypatch)
    try:
        first = store.save_captured_question(
            text="Question one", consent_origin="explicit_save"
        )
        second = store.save_captured_question(
            text="Question two", consent_origin="explicit_save"
        )
        store.ensure_session(session_id="s-clear", mode="companion")
        store.add_qa_record(
            session_id="s-clear",
            topic_id="",
            question={"prompt": "Question one"},
            user_answer="answer",
            eval_result={"verdict": "correct"},
            mode="companion",
            source_question_id=first["id"],
        )
        assert store.clear_captured_questions() == 2
        assert store.get_captured_question(first["id"]) is None
        assert store.get_captured_question(second["id"]) is None
        assert store.list_qa_records()[-1]["source_question_id"] == ""
    finally:
        store.close()


def test_purge_all_includes_captured_questions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(tmp_path, monkeypatch)
    try:
        store.save_captured_question(
            text="Remove with all user data", consent_origin="explicit_save"
        )
        deleted = store.purge_all()
        assert deleted["captured_questions"] == 1
        assert store.list_captured_questions() == []
    finally:
        store.close()


def test_auto_save_requires_confident_question_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(tmp_path, monkeypatch)
    try:
        with pytest.raises(ValueError, match="confidence >= 0.80"):
            store.save_captured_question(
                text="An unconfirmed screen",
                consent_origin="auto_save",
                classification={"screen_type": "reading", "confidence": 0.99},
            )
        captured = store.save_captured_question(
            text="A confirmed question",
            consent_origin="auto_save",
            classification={"screen_type": "question", "confidence": 0.8},
        )
        assert captured["consent_origin"] == "auto_save"
    finally:
        store.close()


def test_schema_migrates_existing_qa_records_with_nullable_source_question_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "study.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE qa_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            topic_id TEXT,
            question TEXT,
            user_answer TEXT,
            eval_result TEXT,
            mode TEXT NOT NULL,
            response_time_ms INTEGER,
            created_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    study_store = _load_store(monkeypatch, "_captured_questions_migration_test")
    store = study_store(db_path, tmp_path / "seed.json", _Logger())
    store.open()
    try:
        columns = {
            str(row["name"])
            for row in store._require_conn().execute("PRAGMA table_info(qa_records)")
        }
        assert "source_question_id" in columns
        assert store._require_conn().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'captured_questions'"
        ).fetchone()
    finally:
        store.close()
