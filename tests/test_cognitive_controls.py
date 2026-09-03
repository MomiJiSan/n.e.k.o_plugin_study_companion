from __future__ import annotations

import importlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "calculus.chain_rule"
CODE = "omit_inner_derivative"
MODEL = "cognitive-v1"
EXTRACTOR = "cognitive-extractor-v1"


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
    Store = importlib.import_module(f"{name}.store").StudyStore
    cognitive = importlib.import_module(f"{name}.store_cognitive")
    setattr(
        Store,
        "list_cognitive_fact_timeline",
        cognitive.list_cognitive_fact_timeline,
    )
    return Store


def _store(tmp_path: Path, Store):
    store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    store.ensure_topic(topic_id=TOPIC, name="Chain rule")
    return store


def _write_attempt(store, attempt_id: str) -> None:
    store.batch_write_answer_data(
        session_id=f"session-{attempt_id}",
        mode="companion",
        topic_id=TOPIC,
        question={
            "question_id": f"question-{attempt_id}",
            "question": "Differentiate sin(x^2).",
            "answer": "2x cos(x^2)",
            "question_type": "math_exact",
            "difficulty": 3,
        },
        user_answer="cos(x^2)",
        eval_result={"verdict": "wrong", "score": 0},
        response_time_ms=100,
        attempt_id=attempt_id,
        enqueue_cognitive_projection=True,
        cognitive_extractor_version=EXTRACTOR,
    )


def _evidence(attempt_id: str) -> dict[str, object]:
    return {
        "attempt_id": attempt_id,
        "topic_id": TOPIC,
        "hypothesis_code": CODE,
        "direction": "support",
        "strength": 1.0,
        "extractor_confidence": 1.0,
        "diagnosticity": 0.8,
        "source_kind": "practice",
        "evidence_span": "missing 2x",
        "evidence_family_id": f"family-{attempt_id}",
        "session_id": f"session-{attempt_id}",
    }


def _snapshot(attempt_id: str) -> dict[str, object]:
    return {
        "hypothesis_id": f"{TOPIC}:{CODE}",
        "topic_id": TOPIC,
        "hypothesis_code": CODE,
        "status": "supported",
        "evidence_status": "supported",
        "intervention_stage": "idle",
        "user_override": "",
        "probability": 0.8,
        "support_count": 2,
        "counter_count": 0,
        "diagnostic_support_count": 0,
        "relapse_count": 0,
        "source_attempt_id": attempt_id,
        "model_version": MODEL,
        "computed_at": "2026-09-03T12:00:00Z",
    }


def _certified_transfer(attempt_id: str, occurred_at: datetime) -> dict[str, object]:
    return {
        "hypothesis_id": f"{TOPIC}:{CODE}",
        "topic_id": TOPIC,
        "hypothesis_code": CODE,
        "model_version": MODEL,
        "source_attempt_id": attempt_id,
        "source_event_id": f"transfer-event:{attempt_id}",
        "question_family_id": f"transfer-family:{attempt_id}",
        "evaluation_verdict": "correct",
        "certified": True,
        "used_hint": False,
        "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
    }


def _finish_extraction(store, attempt_id: str) -> dict[str, object]:
    claim = store.claim_cognitive_projections(
        limit=1,
        extractor_version=EXTRACTOR,
    )[0]
    assert claim["attempt_id"] == attempt_id
    return store.complete_cognitive_projection(
        attempt_id=attempt_id,
        extractor_version=EXTRACTOR,
        model_version=MODEL,
        lease_token=claim["lease_token"],
        evidence=[_evidence(attempt_id)],
    )


def test_delete_cutoff_survives_restore_and_only_accepts_new_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_permanent_cutoff")
    store = _store(tmp_path, Store)
    try:
        _write_attempt(store, "old")
        old_claim = store.claim_cognitive_projections(
            limit=1, extractor_version=EXTRACTOR
        )[0]
        conn = store._require_read_conn()
        question_seq = int(
            conn.execute(
                "SELECT root_fact_seq FROM question_instances WHERE question_id = ?",
                ("question-old",),
            ).fetchone()[0]
        )
        old_attempt_seq = int(
            conn.execute(
                "SELECT root_fact_seq FROM attempts WHERE attempt_id = 'old'"
            ).fetchone()[0]
        )
        assert 0 < question_seq < old_attempt_seq

        deleted = store.record_cognitive_user_control(
            topic_id=TOPIC, hypothesis_code=CODE, action="delete"
        )
        store.record_cognitive_user_control(
            topic_id=TOPIC, hypothesis_code=CODE, action="restore"
        )
        cutoff = conn.execute(
            """SELECT delete_cutoff_seq FROM cognitive_delete_cutoffs
            WHERE topic_id = ? AND hypothesis_code = ?""",
            (TOPIC, CODE),
        ).fetchone()[0]
        assert cutoff == deleted["root_fact_seq"]

        completed_old = store.complete_cognitive_projection(
            attempt_id="old",
            extractor_version=EXTRACTOR,
            model_version=MODEL,
            lease_token=old_claim["lease_token"],
            evidence=[_evidence("old")],
        )
        assert completed_old["evidence_inserted"] == 0

        _write_attempt(store, "new")
        completed_new = _finish_extraction(store, "new")
        assert completed_new["evidence_inserted"] == 1
        evidence = store.list_cognitive_evidence(topic_id=TOPIC)
        assert [item["attempt_id"] for item in evidence] == ["new"]
        timeline = store.list_cognitive_fact_timeline(
            topic_id=TOPIC,
            hypothesis_code=CODE,
            extractor_version=EXTRACTOR,
            model_version=MODEL,
        )
        assert [item["fact_kind"] for item in timeline] == ["evidence"]
        new_attempt_seq = conn.execute(
            "SELECT root_fact_seq FROM attempts WHERE attempt_id = 'new'"
        ).fetchone()[0]
        assert evidence[0]["root_fact_seq"] == new_attempt_seq
        assert timeline[0]["root_fact_seq"] == new_attempt_seq
        assert new_attempt_seq > cutoff
    finally:
        store.close()


def test_suppress_is_bounded_and_reader_resumes_without_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_live_controls")
    store = _store(tmp_path, Store)
    try:
        _write_attempt(store, "attempt")
        _finish_extraction(store, "attempt")
        claim = store.claim_cognitive_topic_projections(model_version=MODEL)[0]
        store.complete_cognitive_topic_projection(
            topic_id=TOPIC,
            model_version=MODEL,
            lease_token=claim["lease_token"],
            claimed_generation=claim["claimed_generation"],
            snapshots=[_snapshot("attempt")],
        )
        with pytest.raises(ValueError, match="require expires_at"):
            store.record_cognitive_user_control(
                topic_id=TOPIC, hypothesis_code=CODE, action="suppress"
            )
        with pytest.raises(ValueError, match="within 24 hours"):
            store.record_cognitive_user_control(
                topic_id=TOPIC,
                hypothesis_code=CODE,
                action="suppress",
                expires_at="2100-01-01T00:00:00Z",
            )

        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=1)
        store.record_cognitive_user_control(
            topic_id=TOPIC,
            hypothesis_code=CODE,
            action="suppress",
            expires_at=expires.isoformat().replace("+00:00", "Z"),
        )
        active = store.list_cognitive_hypothesis_current(
            topic_id=TOPIC,
            model_version=MODEL,
            as_of=now,
        )[0]
        resumed = store.list_cognitive_hypothesis_current(
            topic_id=TOPIC,
            model_version=MODEL,
            as_of=expires + timedelta(seconds=1),
        )[0]

        assert active["status"] == "dismissed"
        assert active["user_override"] == "suppressed"
        assert resumed["status"] == "supported"
        assert resumed["user_override"] == ""
    finally:
        store.close()


def test_unknown_projection_versions_are_read_only_and_never_claimed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_unknown_projection")
    store = _store(tmp_path, Store)
    try:
        conn = store._require_conn()
        conn.execute(
            """INSERT INTO cognitive_topic_projection_queue (
                topic_id, model_version, status, requested_generation,
                claimed_generation, projected_generation
            ) VALUES (?, 'unknown-v9', 'pending', 1, 0, 0)""",
            (TOPIC,),
        )
        conn.commit()

        rows = store.list_cognitive_topic_projection_queue(
            model_version="unknown-v9"
        )
        assert [item["model_version"] for item in rows] == ["unknown-v9"]
        assert store.claim_cognitive_topic_projections() == []
        with pytest.raises(ValueError, match="unsupported cognitive projection"):
            store.claim_cognitive_topic_projections(model_version="unknown-v9")
        with pytest.raises(ValueError, match="unsupported cognitive projection"):
            store.mark_cognitive_topic_projection_dirty(
                topic_id=TOPIC, model_version="unknown-v9"
            )

        row = conn.execute(
            """SELECT status, requested_generation FROM cognitive_topic_projection_queue
            WHERE topic_id = ? AND model_version = 'unknown-v9'""",
            (TOPIC,),
        ).fetchone()
        assert tuple(row) == ("pending", 1)
    finally:
        store.close()


def test_retention_facts_follow_permanent_delete_cutoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_retention_fact_cutoff")
    store = _store(tmp_path, Store)
    first_at = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    try:
        _write_attempt(store, "old-transfer")
        store.record_certified_transfer_success(
            _certified_transfer("old-transfer", first_at)
        )
        claim = store.claim_cognitive_obligations(
            worker_id="worker-old",
            lease_seconds=300,
            as_of=(first_at + timedelta(hours=24)).isoformat().replace(
                "+00:00", "Z"
            ),
            obligation_types=("retention",),
        )[0]
        _write_attempt(store, "old-retention")
        store.apply_cognitive_retention_disposition(
            obligation_id=claim["obligation_id"],
            claim_token=claim["claim_token"],
            worker_id="worker-old",
            attempt_id="old-retention",
            disposition="resolved",
            occurred_at=(first_at + timedelta(hours=24, minutes=1))
            .isoformat()
            .replace("+00:00", "Z"),
        )
        assert [
            fact["fact_kind"]
            for fact in store.list_cognitive_fact_timeline(
                topic_id=TOPIC,
                hypothesis_code=CODE,
                model_version=MODEL,
            )
        ] == ["obligation_satisfaction"]

        store.record_cognitive_user_control(
            topic_id=TOPIC, hypothesis_code=CODE, action="delete"
        )
        store.record_cognitive_user_control(
            topic_id=TOPIC, hypothesis_code=CODE, action="restore"
        )
        assert store.list_cognitive_fact_timeline(
            topic_id=TOPIC,
            hypothesis_code=CODE,
            model_version=MODEL,
        ) == []

        second_at = first_at + timedelta(days=10)
        _write_attempt(store, "new-transfer")
        store.record_certified_transfer_success(
            _certified_transfer("new-transfer", second_at)
        )
        store.expire_cognitive_monitoring_episodes(
            as_of=(second_at + timedelta(days=7, seconds=1))
            .isoformat()
            .replace("+00:00", "Z")
        )
        timeline = store.list_cognitive_fact_timeline(
            topic_id=TOPIC,
            hypothesis_code=CODE,
            model_version=MODEL,
        )

        assert [fact["fact_kind"] for fact in timeline] == ["episode"]
        assert timeline[0]["payload"]["event_type"] == "expired"
        cutoff = store._require_read_conn().execute(
            """SELECT delete_cutoff_seq FROM cognitive_delete_cutoffs
            WHERE topic_id = ? AND hypothesis_code = ?""",
            (TOPIC, CODE),
        ).fetchone()[0]
        assert timeline[0]["root_fact_seq"] > cutoff
    finally:
        store.close()


def test_open_additively_backfills_fact_roots_for_legacy_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_fact_root_migration")
    store = _store(tmp_path, Store)
    _write_attempt(store, "legacy")
    _finish_extraction(store, "legacy")
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    store.record_cognitive_user_control(
        topic_id=TOPIC,
        hypothesis_code=CODE,
        action="suppress",
        expires_at=expires.isoformat().replace("+00:00", "Z"),
    )
    store.close()

    connection = sqlite3.connect(tmp_path / "study.db")
    try:
        for trigger in (
            "trg_cognitive_root_question_insert",
            "trg_cognitive_root_attempt_insert",
            "trg_cognitive_root_evidence_insert",
            "trg_cognitive_root_control_insert",
            "trg_cognitive_root_intervention_insert",
            "trg_cognitive_control_expiry_validate",
        ):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        for index in (
            "idx_cognitive_evidence_root",
            "idx_cognitive_controls_root",
            "idx_cognitive_interventions_root",
            "idx_cognitive_fact_roots_source",
        ):
            connection.execute(f"DROP INDEX IF EXISTS {index}")
        connection.execute("DROP TABLE cognitive_delete_cutoffs")
        connection.execute("DROP TABLE cognitive_fact_roots")
        for table in (
            "question_instances",
            "attempts",
            "cognitive_evidence",
            "cognitive_user_controls",
            "cognitive_intervention_events",
        ):
            connection.execute(f"ALTER TABLE {table} DROP COLUMN root_fact_seq")
        connection.commit()
    finally:
        connection.close()

    reopened = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    reopened.open()
    try:
        conn = reopened._require_read_conn()
        question_seq = conn.execute(
            """SELECT root_fact_seq FROM question_instances
            WHERE question_id = 'question-legacy'"""
        ).fetchone()[0]
        attempt_seq = conn.execute(
            "SELECT root_fact_seq FROM attempts WHERE attempt_id = 'legacy'"
        ).fetchone()[0]
        evidence_seq = conn.execute(
            """SELECT root_fact_seq FROM cognitive_evidence
            WHERE attempt_id = 'legacy'"""
        ).fetchone()[0]
        control_seq = conn.execute(
            "SELECT root_fact_seq FROM cognitive_user_controls"
        ).fetchone()[0]

        assert 0 < question_seq < attempt_seq < control_seq
        assert evidence_seq == attempt_seq
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_fact_roots"
        ).fetchone()[0] == 3
    finally:
        reopened.close()


def test_open_backfills_immutable_fact_for_legacy_expired_episode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_episode_fact_migration")
    store = _store(tmp_path, Store)
    opened_at = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    _write_attempt(store, "legacy-transfer")
    store.record_certified_transfer_success(
        _certified_transfer("legacy-transfer", opened_at)
    )
    store.expire_cognitive_monitoring_episodes(
        as_of=(opened_at + timedelta(days=7, seconds=1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    store.close()

    connection = sqlite3.connect(tmp_path / "study.db")
    try:
        connection.execute("DROP TABLE cognitive_monitoring_episode_facts")
        connection.execute(
            "DELETE FROM cognitive_fact_roots WHERE fact_type = 'cognitive_episode'"
        )
        connection.commit()
    finally:
        connection.close()

    reopened = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    reopened.open()
    try:
        fact = reopened._require_read_conn().execute(
            "SELECT * FROM cognitive_monitoring_episode_facts"
        ).fetchone()
        assert fact["fact_type"] == "expired"
        assert fact["root_fact_seq"] > 0
        timeline = reopened.list_cognitive_fact_timeline(
            topic_id=TOPIC,
            hypothesis_code=CODE,
            model_version=MODEL,
        )
        assert [item["fact_kind"] for item in timeline] == ["episode"]
    finally:
        reopened.close()


def test_open_does_not_migrate_unknown_historical_projection_versions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_unknown_version_migration")
    store = _store(tmp_path, Store)
    _write_attempt(store, "historical")
    conn = store._require_conn()
    conn.execute(
        """INSERT INTO cognitive_hypothesis_snapshots (
            hypothesis_id, topic_id, hypothesis_code, status, probability,
            support_count, counter_count, diagnostic_support_count,
            relapse_count, source_attempt_id, model_version, computed_at
        ) VALUES (?, ?, ?, 'supported', 0.8, 2, 0, 0, 0,
                  'historical', 'unknown-v9', '2026-09-03T12:00:00Z')""",
        (f"{TOPIC}:{CODE}", TOPIC, CODE),
    )
    conn.commit()
    store.close()

    reopened = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    reopened.open()
    try:
        snapshots = reopened.list_cognitive_hypothesis_snapshots(
            topic_id=TOPIC,
            model_version="unknown-v9",
        )
        queue = reopened.list_cognitive_topic_projection_queue(
            model_version="unknown-v9"
        )

        assert [item["model_version"] for item in snapshots] == ["unknown-v9"]
        assert queue == []
    finally:
        reopened.close()
