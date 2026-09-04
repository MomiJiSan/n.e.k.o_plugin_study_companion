from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "calculus.chain_rule"
EXTRACTOR = "cognitive-extractor-v2"
MODEL = "cognitive-v2.1-1"


class _Logger:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


def _runtime(monkeypatch: pytest.MonkeyPatch, package_name: str):
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    setattr(mode_manager, "normalize_mode", lambda value: str(value or "companion"))
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    store_module = importlib.import_module(f"{package_name}.store")
    outbox_module = importlib.import_module(f"{package_name}.store_cognitive_outbox")
    cognitive_module = importlib.import_module(f"{package_name}.store_cognitive")
    return store_module, outbox_module, cognitive_module


def _store(tmp_path: Path, Store):
    store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    store.ensure_topic(topic_id=TOPIC, name="Chain rule")
    return store


def _answer(store, attempt_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": "session-outbox",
        "mode": "companion",
        "topic_id": TOPIC,
        "question": {
            "question_id": f"question-{attempt_id}",
            "question": "Differentiate sin(x^2).",
            "answer": "2x cos(x^2)",
            "question_type": "math_exact",
            "difficulty": 3,
        },
        "user_answer": "2x cos(x^2)",
        "eval_result": {"verdict": "correct", "score": 100},
        "response_time_ms": 100,
        "attempt_id": attempt_id,
    }
    payload.update(overrides)
    return store.batch_write_answer_data(**payload)


def test_projection_validation_failure_preserves_answer_and_legacy_result_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, _, _ = _runtime(monkeypatch, "_cognitive_outbox_config_failure")
    store = _store(tmp_path, store_module.StudyStore)
    try:
        result = _answer(
            store,
            "attempt-config-failure",
            enqueue_cognitive_projection=True,
            cognitive_extractor_version="",
            cognitive_model_version=MODEL,
        )

        assert result == {
            "ok": True,
            "wrong_question_id": "",
            "wrong_question_attempt": {},
        }
        assert store.get_attempt_fact("attempt-config-failure") is not None
        failed = store.list_cognitive_outbox(status="failed")
        assert len(failed) == 1
        assert failed[0]["operation"] == "projection_enqueue"
        assert failed[0]["retry_count"] == 1
        assert "projection outbox identities are required" in failed[0]["last_error"]
        assert "user_answer" not in failed[0]["payload"]
        assert "answer" not in failed[0]["payload"]
    finally:
        store.close()


def test_permanent_outbox_failure_is_discarded_at_retry_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, outbox_module, _ = _runtime(
        monkeypatch, "_cognitive_outbox_retry_ceiling"
    )
    store = _store(tmp_path, store_module.StudyStore)
    try:
        _answer(
            store,
            "attempt-retry-ceiling",
            enqueue_cognitive_projection=True,
            cognitive_extractor_version="",
            cognitive_model_version=MODEL,
        )

        for _ in range(outbox_module._COGNITIVE_OUTBOX_MAX_RETRIES - 1):
            store.process_cognitive_outbox(limit=1)

        discarded = store.list_cognitive_outbox(status="discarded")
        assert len(discarded) == 1
        assert discarded[0]["retry_count"] == outbox_module._COGNITIVE_OUTBOX_MAX_RETRIES
        assert store.claim_cognitive_outbox(limit=1) == []
    finally:
        store.close()


def test_expired_worker_is_fenced_after_lease_takeover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, outbox_module, _ = _runtime(
        monkeypatch, "_cognitive_outbox_worker_fence"
    )
    store = _store(tmp_path, store_module.StudyStore)
    try:
        _answer(store, "attempt-worker-fence")
        with store.transaction() as conn:
            outbox_id = outbox_module.enqueue_cognitive_projection_outbox(
                store,
                conn,
                attempt_id="attempt-worker-fence",
                extractor_version=EXTRACTOR,
                model_version=MODEL,
            )
        first = store.claim_cognitive_outbox(limit=1, lease_seconds=30)[0]
        conn = store._require_conn()
        conn.execute(
            """
            UPDATE cognitive_outbox
            SET lease_expires_at = datetime('now', '-1 second')
            WHERE outbox_id = ?
            """,
            (outbox_id,),
        )
        conn.commit()
        second = store.claim_cognitive_outbox(limit=1, lease_seconds=60)[0]
        assert second["lease_token"] != first["lease_token"]

        with store.transaction() as transaction:
            stale = outbox_module.deliver_cognitive_outbox_inline(
                store,
                transaction,
                outbox_id=outbox_id,
                lease_token=str(first["lease_token"]),
            )
        assert stale == {"recorded": False, "error": "lease_lost"}
        processing = store.list_cognitive_outbox(status="processing")
        assert processing[0]["lease_token"] == second["lease_token"]

        with store.transaction() as transaction:
            completed = outbox_module.deliver_cognitive_outbox_inline(
                store,
                transaction,
                outbox_id=outbox_id,
                lease_token=str(second["lease_token"]),
            )
        assert completed == {"recorded": True, "error": ""}
        assert store.list_cognitive_outbox(status="done")[0]["outbox_id"] == outbox_id
    finally:
        store.close()


def test_sqlite_delivery_failure_rolls_back_whole_answer_transaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, _, cognitive_module = _runtime(
        monkeypatch, "_cognitive_outbox_sqlite_failure"
    )
    store = _store(tmp_path, store_module.StudyStore)
    try:
        def fail_sqlite(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("injected disk failure")

        monkeypatch.setattr(cognitive_module, "enqueue_cognitive_projection", fail_sqlite)
        with pytest.raises(sqlite3.OperationalError, match="injected disk failure"):
            _answer(
                store,
                "attempt-sqlite-failure",
                enqueue_cognitive_projection=True,
                cognitive_extractor_version=EXTRACTOR,
                cognitive_model_version=MODEL,
            )

        assert store.get_attempt_fact("attempt-sqlite-failure") is None
        assert store.list_cognitive_outbox() == []
    finally:
        store.close()


def test_event_id_collision_cannot_attach_existing_outbox_to_another_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, outbox_module, _ = _runtime(
        monkeypatch, "_cognitive_outbox_identity_collision"
    )
    store = _store(tmp_path, store_module.StudyStore)
    try:
        _answer(store, "attempt-collision-a")
        _answer(store, "attempt-collision-b")
        event = {"event_id": "shared-event", "attempt_id": "attempt-collision-a"}
        with store.transaction() as conn:
            first_id = outbox_module.enqueue_cognitive_outbox(
                store,
                conn,
                attempt_id="attempt-collision-a",
                event=event,
            )
        with pytest.raises(ValueError, match="identity collision"):
            with store.transaction() as conn:
                outbox_module.enqueue_cognitive_outbox(
                    store,
                    conn,
                    attempt_id="attempt-collision-b",
                    event={**event, "attempt_id": "attempt-collision-b"},
                )

        rows = store.list_cognitive_outbox()
        assert len(rows) == 1
        assert rows[0]["outbox_id"] == first_id
        assert rows[0]["attempt_id"] == "attempt-collision-a"
    finally:
        store.close()


def test_existing_outbox_check_migrates_without_losing_row_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, _, _ = _runtime(monkeypatch, "_cognitive_outbox_migration")
    db_path = tmp_path / "study.db"
    seed_path = tmp_path / "seed.json"
    store = store_module.StudyStore(db_path, seed_path, _Logger())
    store.open()
    store.ensure_topic(topic_id=TOPIC, name="Chain rule")
    _answer(
        store,
        "attempt-existing-outbox",
        enqueue_cognitive_projection=True,
        cognitive_extractor_version=EXTRACTOR,
        cognitive_model_version=MODEL,
    )
    before = store.list_cognitive_outbox()[0]
    store.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "ALTER TABLE cognitive_outbox RENAME TO cognitive_outbox_current"
        )
        conn.execute(
            """
            CREATE TABLE cognitive_outbox (
                outbox_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                event_id TEXT NOT NULL UNIQUE,
                operation TEXT NOT NULL CHECK(operation IN (
                    'intervention_event', 'projection_enqueue'
                )),
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
                    'pending', 'processing', 'done', 'failed', 'discarded'
                )),
                retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
                last_error TEXT NOT NULL DEFAULT '',
                lease_token TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """INSERT INTO cognitive_outbox
            SELECT * FROM cognitive_outbox_current"""
        )
        conn.execute("DROP TABLE cognitive_outbox_current")
        conn.commit()
    finally:
        conn.close()

    reopened = store_module.StudyStore(db_path, seed_path, _Logger())
    reopened.open()
    try:
        assert reopened.list_cognitive_outbox()[0] == before
        sql = reopened._require_read_conn().execute(
            """SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'cognitive_outbox'"""
        ).fetchone()["sql"]
        assert "retention_disposition" in sql
        with reopened.transaction() as transaction:
            transaction.execute(
                """INSERT INTO cognitive_outbox (
                    outbox_id, attempt_id, event_id, operation, payload_json
                ) VALUES (?, ?, ?, 'retention_disposition', '{}')""",
                (
                    "outbox-retention-proof",
                    "attempt-existing-outbox",
                    "event-retention-proof",
                ),
            )
    finally:
        reopened.close()
