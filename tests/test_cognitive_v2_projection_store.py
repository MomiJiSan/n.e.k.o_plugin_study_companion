from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

from adaptive_learning.cognitive_contracts import (
    CognitiveEvidenceDraft,
    CognitiveExtractionOutcome,
)
from adaptive_learning.cognitive_projection import CognitiveProjector

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "calculus.chain_rule"
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
    for method_name in (
        "mark_cognitive_topic_projection_dirty",
        "get_cognitive_topic_projection_state",
        "list_cognitive_topic_projection_queue",
        "claim_cognitive_topic_projections",
        "complete_cognitive_topic_projection",
        "mark_cognitive_topic_projection_failed",
        "list_cognitive_hypothesis_current",
    ):
        setattr(Store, method_name, getattr(cognitive, method_name))
    return Store, cognitive


def _store(tmp_path: Path, Store):
    store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    store.ensure_topic(topic_id=TOPIC, name="Chain rule")
    return store


def _write_attempt(
    store,
    attempt_id: str = "attempt-1",
    *,
    session_id: str = "session-a",
) -> None:
    store.batch_write_answer_data(
        session_id=session_id,
        mode="companion",
        topic_id=TOPIC,
        question={
            "question_id": f"question-{attempt_id}",
            "question": "Differentiate sin(x^2).",
            "answer": "2x cos(x^2)",
            "question_type": "math_exact",
            "difficulty": 3,
        },
        user_answer=attempt_id,
        eval_result={"verdict": "wrong", "score": 0},
        response_time_ms=100,
        attempt_id=attempt_id,
        enqueue_cognitive_projection=True,
        cognitive_extractor_version=EXTRACTOR,
    )


def _evidence(attempt_id: str = "attempt-1") -> dict[str, object]:
    return {
        "attempt_id": attempt_id,
        "topic_id": TOPIC,
        "hypothesis_code": "omit_inner_derivative",
        "direction": "support",
        "strength": 0.9,
        "extractor_confidence": 0.9,
        "diagnosticity": 0.6,
        "source_kind": "practice",
        "evidence_span": "cos(x^2) without 2x",
        "evidence_family_id": "question:question-family-a:omit_inner_derivative",
        "question_id": "question-family-a",
        "session_id": "session-a",
        "diagnostic_validation_id": "",
    }


def _snapshot(attempt_id: str = "attempt-1") -> dict[str, object]:
    return {
        "hypothesis_id": f"{TOPIC}:omit_inner_derivative",
        "topic_id": TOPIC,
        "hypothesis_code": "omit_inner_derivative",
        "status": "hypothesized",
        "evidence_status": "hypothesized",
        "intervention_stage": "idle",
        "user_override": "",
        "probability": 0.7,
        "support_count": 1,
        "counter_count": 0,
        "diagnostic_support_count": 0,
        "relapse_count": 0,
        "source_attempt_id": attempt_id,
        "model_version": MODEL,
        "computed_at": "2026-09-02T08:00:00Z",
    }


def test_v1_evidence_migration_marks_topic_for_current_state_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, _ = _load_store(monkeypatch, "_cognitive_v1_current_migration")
    store = _store(tmp_path, Store)
    try:
        _write_attempt(store)
        claimed = store.claim_cognitive_projections(
            limit=1,
            extractor_version=EXTRACTOR,
        )[0]
        store.complete_cognitive_projection(
            attempt_id="attempt-1",
            extractor_version=EXTRACTOR,
            lease_token=claimed["lease_token"],
            evidence=[_evidence()],
            snapshots=[_snapshot()],
        )
    finally:
        store.close()

    connection = __import__("sqlite3").connect(tmp_path / "study.db")
    try:
        connection.execute("DROP TABLE cognitive_hypothesis_current")
        connection.execute("DROP TABLE cognitive_topic_projection_queue")
        connection.commit()
    finally:
        connection.close()

    reopened = _store(tmp_path, Store)
    try:
        queue = reopened.get_cognitive_topic_projection_state(
            topic_id=TOPIC,
            model_version=MODEL,
        )
        assert queue is not None
        assert queue["status"] == "pending"
        assert queue["requested_generation"] == 1
        assert queue["projected_generation"] == 0
        assert reopened.list_cognitive_hypothesis_current(topic_id=TOPIC) == []
    finally:
        reopened.close()


def _finish_extraction(store) -> None:
    claim = store.claim_cognitive_projections(extractor_version=EXTRACTOR)[0]
    store.complete_cognitive_projection(
        attempt_id="attempt-1",
        extractor_version=EXTRACTOR,
        model_version=MODEL,
        lease_token=claim["lease_token"],
        evidence=[_evidence()],
    )


def test_extraction_queue_accepts_multiple_versions_for_one_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, cognitive = _load_store(monkeypatch, "_cognitive_v2_versions")
    store = _store(tmp_path, Store)
    try:
        _write_attempt(store)
        conn = store._require_conn()
        assert cognitive.enqueue_cognitive_projection(
            store,
            conn,
            attempt_id="attempt-1",
            extractor_version="cognitive-extractor-v2",
        )
        conn.commit()
        rows = store._require_read_conn().execute(
            "SELECT attempt_id, extractor_version FROM cognitive_extraction_queue"
        ).fetchall()
        assert {(row[0], row[1]) for row in rows} == {
            ("attempt-1", EXTRACTOR),
            ("attempt-1", "cognitive-extractor-v2"),
        }
        assert store._require_read_conn().execute(
            "SELECT COUNT(*) FROM cognitive_projection_queue"
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_dirty_during_rebuild_cannot_be_acknowledged_by_old_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, _ = _load_store(monkeypatch, "_cognitive_v2_generation")
    store = _store(tmp_path, Store)
    try:
        _write_attempt(store)
        queued = store.get_cognitive_topic_projection_state(
            topic_id=TOPIC, model_version=MODEL
        )
        assert queued is not None
        assert queued["requested_generation"] == 1
        assert queued["projected_generation"] == 0
        _finish_extraction(store)
        first = store.claim_cognitive_topic_projections(model_version=MODEL)[0]
        first_generation = first["claimed_generation"]
        assert first_generation == 2

        assert store.mark_cognitive_topic_projection_dirty(
            topic_id=TOPIC, model_version=MODEL
        ) == 3
        completed = store.complete_cognitive_topic_projection(
            topic_id=TOPIC,
            model_version=MODEL,
            lease_token=first["lease_token"],
            claimed_generation=first_generation,
            snapshots=[_snapshot()],
        )
        assert completed["status"] == "pending"
        assert completed["projected_generation"] == first_generation
        state = store.get_cognitive_topic_projection_state(
            topic_id=TOPIC, model_version=MODEL
        )
        assert state is not None
        assert state["requested_generation"] == 3
        assert state["projected_generation"] == first_generation

        second = store.claim_cognitive_topic_projections(model_version=MODEL)[0]
        store.complete_cognitive_topic_projection(
            topic_id=TOPIC,
            model_version=MODEL,
            lease_token=second["lease_token"],
            claimed_generation=second["claimed_generation"],
            snapshots=[_snapshot()],
        )
        current = store.list_cognitive_hypothesis_current(
            topic_id=TOPIC, model_version=MODEL
        )[0]
        assert current["projected_generation"] == 3
        assert current["source_snapshot_id"].endswith("generation-3")
    finally:
        store.close()


def test_expired_topic_lease_is_reclaimed_and_stale_worker_is_fenced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, _ = _load_store(monkeypatch, "_cognitive_v2_topic_lease")
    store = _store(tmp_path, Store)
    try:
        _write_attempt(store)
        _finish_extraction(store)
        first = store.claim_cognitive_topic_projections(model_version=MODEL)[0]
        store._require_conn().execute(
            """UPDATE cognitive_topic_projection_queue
            SET updated_at = datetime('now', '-20 minutes')
            WHERE topic_id = ? AND model_version = ?""",
            (TOPIC, MODEL),
        )
        store._require_conn().commit()
        second = store.claim_cognitive_topic_projections(model_version=MODEL)[0]
        assert second["lease_token"] != first["lease_token"]
        with pytest.raises(ValueError, match="lease is no longer active"):
            store.complete_cognitive_topic_projection(
                topic_id=TOPIC,
                model_version=MODEL,
                lease_token=first["lease_token"],
                claimed_generation=first["claimed_generation"],
                snapshots=[_snapshot()],
            )
    finally:
        store.close()


def test_delete_erases_only_derivations_and_suppress_expiry_is_honored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, _ = _load_store(monkeypatch, "_cognitive_v2_controls")
    store = _store(tmp_path, Store)
    try:
        _write_attempt(store)
        _finish_extraction(store)
        claim = store.claim_cognitive_topic_projections(model_version=MODEL)[0]
        store.complete_cognitive_topic_projection(
            topic_id=TOPIC,
            model_version=MODEL,
            lease_token=claim["lease_token"],
            claimed_generation=claim["claimed_generation"],
            snapshots=[_snapshot()],
        )
        store.record_cognitive_user_control(
            topic_id=TOPIC,
            hypothesis_code="omit_inner_derivative",
            action="delete",
        )
        conn = store._require_read_conn()
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cognitive_evidence").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_hypothesis_snapshots"
        ).fetchone()[0] == 0
        assert store.list_cognitive_hypothesis_current(topic_id=TOPIC) == []
        assert store.is_cognitive_hypothesis_suppressed(
            topic_id=TOPIC, hypothesis_code="omit_inner_derivative"
        )

        store.record_cognitive_user_control(
            topic_id=TOPIC,
            hypothesis_code="omit_inner_derivative",
            action="suppress",
            expires_at="2026-01-01T00:00:00Z",
        )
        assert not store.is_cognitive_hypothesis_suppressed(
            topic_id=TOPIC,
            hypothesis_code="omit_inner_derivative",
            as_of="2026-09-02T00:00:00Z",
        )
    finally:
        store.close()


def test_delete_tombstone_fences_late_in_flight_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, _ = _load_store(monkeypatch, "_cognitive_v2_delete_fence")
    store = _store(tmp_path, Store)
    try:
        _write_attempt(store)
        claimed = store.claim_cognitive_projections(
            limit=1,
            extractor_version=EXTRACTOR,
        )[0]
        store.record_cognitive_user_control(
            topic_id=TOPIC,
            hypothesis_code="omit_inner_derivative",
            action="delete",
        )

        completed = store.complete_cognitive_projection(
            attempt_id="attempt-1",
            extractor_version=EXTRACTOR,
            lease_token=claimed["lease_token"],
            evidence=[_evidence()],
            snapshots=[_snapshot()],
        )

        assert completed["evidence_inserted"] == 0
        assert completed["snapshots_upserted"] == 0
        assert store.list_cognitive_evidence(topic_id=TOPIC) == []
        assert store.list_cognitive_hypothesis_snapshots(topic_id=TOPIC) == []
        assert store.is_cognitive_hypothesis_suppressed(
            topic_id=TOPIC,
            hypothesis_code="omit_inner_derivative",
        )
    finally:
        store.close()


def test_late_retry_rebuilds_topic_from_all_evidence_in_attempt_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _RetryingExtractor:
        def __init__(self) -> None:
            self.failed_once = False

        async def extract(self, extraction_input):
            if extraction_input.learner_answer == "attempt-early" and not self.failed_once:
                self.failed_once = True
                raise RuntimeError("temporary model failure")
            return CognitiveExtractionOutcome(
                status="success",
                evidence=(
                    CognitiveEvidenceDraft(
                        topic_id=TOPIC,
                        hypothesis_code="omit_inner_derivative",
                        direction="support",
                        strength=1.0,
                        extractor_confidence=1.0,
                        evidence_span="missing 2x",
                    ),
                ),
            )

    Store, _ = _load_store(monkeypatch, "_cognitive_v2_late_retry")
    store = _store(tmp_path, Store)
    try:
        _write_attempt(store, "attempt-early", session_id="session-early")
        _write_attempt(store, "attempt-late", session_id="session-late")
        projector = CognitiveProjector(store, _RetryingExtractor())

        first = asyncio.run(projector.process_pending(limit=2))
        assert first.failed == 1
        assert store.list_cognitive_hypothesis_current(topic_id=TOPIC)[0][
            "support_count"
        ] == 1

        store._require_conn().execute(
            """UPDATE cognitive_extraction_queue
            SET updated_at = datetime('now', '-20 minutes')
            WHERE attempt_id = 'attempt-early'"""
        )
        store._require_conn().commit()
        second = asyncio.run(projector.process_pending(limit=1))
        assert second.failed == 0
        current = store.list_cognitive_hypothesis_current(topic_id=TOPIC)[0]
        assert current["support_count"] == 2
        assert current["status"] == "supported"
    finally:
        store.close()
