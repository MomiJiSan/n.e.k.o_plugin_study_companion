from __future__ import annotations

import importlib
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


def _load_models(monkeypatch: pytest.MonkeyPatch, name: str):
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    mode_manager = ModuleType(f"{name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{name}.models")


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
        "eval_result": {
            "verdict": "correct",
            "score": 100,
            "evaluator_type": "numeric_tolerance",
            "evaluator_version": "numeric-tolerance-v1",
            "confidence": 1.0,
            "fallback_reason": "",
        },
        "response_time_ms": 420,
        "attempt_id": attempt_id,
    }


def test_assessment_flags_default_off_and_reject_non_boolean_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _load_models(monkeypatch, "_assessment_config")

    defaults = models.build_config({}).assessment
    assert defaults.to_dict() == {
        "exact_short_answer_enabled": False,
        "numeric_tolerance_enabled": False,
        "math_expression_enabled": False,
    }
    enabled = models.build_config(
        {
            "assessment": {
                "exact_short_answer_enabled": True,
                "numeric_tolerance_enabled": True,
                "math_expression_enabled": True,
            }
        }
    ).assessment
    assert enabled.to_dict() == {
        "exact_short_answer_enabled": True,
        "numeric_tolerance_enabled": True,
        "math_expression_enabled": True,
    }
    invalid = models.build_config(
        {"assessment": {"exact_short_answer_enabled": "true"}}
    ).assessment
    assert invalid.exact_short_answer_enabled is False


def test_evaluation_metadata_is_dual_stored_without_rewriting_evaluation_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_assessment_metadata_write")
    store = _store(tmp_path, Store)
    try:
        evaluation = _answer_kwargs()["eval_result"]
        result = store.batch_write_answer_data(**_answer_kwargs())
        assert result["ok"] is True

        row = store._require_read_conn().execute(
            """SELECT evaluation_json, evaluator_type, evaluator_version,
                      confidence, fallback_reason
               FROM evaluations WHERE attempt_id = ?""",
            ("attempt-1",),
        ).fetchone()
        assert store._json_loads(row["evaluation_json"], {}) == evaluation
        assert dict(row) == {
            "evaluation_json": store._json_dumps(evaluation),
            "evaluator_type": "numeric_tolerance",
            "evaluator_version": "numeric-tolerance-v1",
            "confidence": 1.0,
            "fallback_reason": "",
        }
        fact = store.get_attempt_fact("attempt-1")
        assert fact is not None
        assert fact["eval_result"] == evaluation
        assert fact["evaluation_metadata"] == {
            "evaluator_type": "numeric_tolerance",
            "evaluator_version": "numeric-tolerance-v1",
            "confidence": 1.0,
            "fallback_reason": "",
        }
    finally:
        store.close()


def test_open_adds_evaluation_metadata_columns_with_legacy_llm_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_assessment_metadata_migration")
    store = _store(tmp_path, Store)
    try:
        store.ensure_session(session_id="session-a", mode="companion")
        conn = store._require_conn()
        conn.execute("DROP TABLE evaluations")
        conn.execute(
            """CREATE TABLE evaluations (
                attempt_id TEXT PRIMARY KEY,
                evaluation_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        conn.execute(
            """INSERT INTO evaluations (attempt_id, evaluation_json)
               VALUES (?, ?)""",
            ("legacy-evaluation", '{"verdict":"correct","score":100}'),
        )
        conn.commit()
    finally:
        store.close()

    reopened = _store(tmp_path, Store)
    try:
        row = reopened._require_read_conn().execute(
            """SELECT evaluator_type, evaluator_version, confidence, fallback_reason
               FROM evaluations WHERE attempt_id = ?""",
            ("legacy-evaluation",),
        ).fetchone()
        assert dict(row) == {
            "evaluator_type": "llm_rubric",
            "evaluator_version": "legacy-v1",
            "confidence": None,
            "fallback_reason": "",
        }
    finally:
        reopened.close()


def test_legacy_qa_fact_exposes_safe_evaluator_metadata_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_assessment_metadata_legacy_read")
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
        assert fact["eval_result"] == {"verdict": "partial", "score": 50}
        assert fact["evaluation_metadata"] == {
            "evaluator_type": "llm_rubric",
            "evaluator_version": "legacy-v1",
            "confidence": None,
            "fallback_reason": "",
        }
    finally:
        store.close()
