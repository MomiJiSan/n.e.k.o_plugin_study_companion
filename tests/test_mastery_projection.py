from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from adaptive_learning.mastery_projection import (
    MasteryV2Projector,
    evidence_from_mapping,
)

PROJECTION_TIME = "2026-09-25T03:00:00Z"


def _fact(
    attempt_id: str,
    *,
    submitted_at: str,
    score: int = 100,
    used_hint: bool | None = None,
) -> dict[str, object]:
    return {
        "attempt_id": attempt_id,
        "topic_id": "topic-1",
        "verdict": "correct" if score == 100 else "partial",
        "score": score,
        "difficulty": 3,
        "used_hint": used_hint,
        "response_time_ms": 8_000,
        "evaluator_confidence": 1.0,
        "submitted_at": submitted_at,
    }


class FakeStore:
    def __init__(self) -> None:
        self.claimed: list[dict[str, Any]] = []
        self.inputs: dict[str, dict[str, Any] | None] = {}
        self.evidence: dict[str, list[dict[str, Any]]] = {}
        self.wrong_counts: dict[str, int] = {}
        self.completed: list[dict[str, Any]] = []
        self.upserted: list[dict[str, Any]] = []
        self.failed: list[tuple[str, str]] = []
        self.claim_error: Exception | None = None
        self.complete_error_for: set[str] = set()
        self.attempt_ids: list[str] = []
        self.topics: list[dict[str, Any]] = []
        self.v1: list[dict[str, Any]] = []
        self.v2: list[dict[str, Any]] = []

    def claim_mastery_projections(self, *, limit: int = 1) -> list[dict[str, Any]]:
        if self.claim_error is not None:
            raise self.claim_error
        return self.claimed[:limit]

    def get_mastery_v2_projection_input(self, attempt_id: str) -> dict[str, Any] | None:
        return self.inputs.get(attempt_id)

    def complete_mastery_projection(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        if str(snapshot["source_attempt_id"]) in self.complete_error_for:
            raise RuntimeError("snapshot write failed")
        self.completed.append(snapshot)
        return snapshot

    def mark_mastery_projection_failed(self, *, attempt_id: str, error: str) -> bool:
        self.failed.append((attempt_id, error))
        return True

    def upsert_mastery_snapshot_v2(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self.upserted.append(snapshot)
        return snapshot

    def list_mastery_v2_evidence(
        self,
        *,
        topic_id: str,
        through_attempt_id: str | None = None,
    ) -> list[dict[str, Any]]:
        del through_attempt_id
        return self.evidence.get(topic_id, [])

    def count_active_wrong_questions(self, topic_id: str) -> int:
        return self.wrong_counts.get(topic_id, 0)

    def list_mastery_v2_attempt_ids(self, *, topic_id: str | None = None) -> list[str]:
        del topic_id
        return list(self.attempt_ids)

    def list_topics(self, limit: int | None = 100, **_scope: Any) -> list[dict[str, Any]]:
        del limit
        return list(self.topics)

    def list_latest_mastery_for_topics(self, _topic_ids: list[str]) -> list[dict[str, Any]]:
        return list(self.v1)

    def list_latest_mastery_v2_for_topics(
        self,
        _topic_ids: list[str],
        *,
        mastery_model_version: str,
    ) -> list[dict[str, Any]]:
        assert mastery_model_version == "mastery-v2-shadow-1"
        return list(self.v2)


def _projection_input(attempt_id: str, facts: list[dict[str, object]]) -> dict[str, object]:
    return {
        "topic_id": "topic-1",
        "source_attempt_id": attempt_id,
        "evidence": facts,
        "unresolved_wrong_count": 0,
    }


def test_process_pending_completes_valid_work_and_isolates_each_failure() -> None:
    store = FakeStore()
    store.claimed = [{"attempt_id": "attempt-1"}, {"attempt_id": "attempt-2"}]
    store.inputs["attempt-1"] = _projection_input(
        "attempt-1",
        [_fact("attempt-1", submitted_at="2026-08-26T03:00:00Z")],
    )
    store.inputs["attempt-2"] = None

    summary = MasteryV2Projector(store).process_pending(
        limit=10,
        as_of=PROJECTION_TIME,
    )

    assert summary.claimed == 2
    assert summary.completed == 1
    assert summary.failed == 1
    assert store.completed[0]["source_attempt_id"] == "attempt-1"
    assert store.completed[0]["computed_at"] == PROJECTION_TIME
    assert store.failed == [("attempt-2", "mastery projection input is unavailable")]


def test_process_pending_does_not_raise_claim_or_snapshot_write_failures() -> None:
    store = FakeStore()
    store.claim_error = RuntimeError("database unavailable")

    claim_summary = MasteryV2Projector(store).process_pending(
        as_of=PROJECTION_TIME
    )

    assert claim_summary.failed == 1
    assert claim_summary.failures[0].error == "database unavailable"

    store.claim_error = None
    store.claimed = [{"attempt_id": "attempt-1"}]
    store.inputs["attempt-1"] = _projection_input(
        "attempt-1",
        [_fact("attempt-1", submitted_at="2026-08-26T03:00:00Z")],
    )
    store.complete_error_for.add("attempt-1")

    write_summary = MasteryV2Projector(store).process_pending(
        as_of=PROJECTION_TIME
    )

    assert write_summary.failed == 1
    assert store.failed == [("attempt-1", "snapshot write failed")]


def test_projection_rejects_newer_facts_than_the_queued_source_attempt() -> None:
    store = FakeStore()
    store.claimed = [{"attempt_id": "attempt-1"}]
    store.inputs["attempt-1"] = _projection_input(
        "attempt-1",
        [
            _fact("attempt-1", submitted_at="2026-08-26T03:00:00Z"),
            _fact("attempt-2", submitted_at="2026-08-26T04:00:00Z"),
        ],
    )

    summary = MasteryV2Projector(store).process_pending(as_of=PROJECTION_TIME)

    assert summary.completed == 0
    assert summary.failed == 1
    assert "newer than its source" in summary.failures[0].error


def test_full_rebuild_uses_latest_fact_time_and_continues_past_empty_topics() -> None:
    store = FakeStore()
    store.evidence["topic-1"] = [
        _fact("attempt-2", submitted_at="2026-08-26T03:00:00Z"),
        _fact("attempt-1", submitted_at="2026-08-25T03:00:00Z", score=50),
    ]
    store.wrong_counts["topic-1"] = 1

    summary = MasteryV2Projector(store).rebuild_topics(
        ["topic-1", "topic-empty", "topic-1"],
        as_of=PROJECTION_TIME,
    )

    assert summary.requested == 2
    assert summary.rebuilt == 1
    assert summary.skipped == 1
    assert summary.failed == 0
    assert store.upserted[0]["source_attempt_id"] == "attempt-2"
    assert store.upserted[0]["computed_at"] == PROJECTION_TIME
    assert store.upserted[0]["unresolved_wrong_count"] == 1


def test_rebuild_all_replays_each_stable_attempt_and_matches_incremental_latest() -> None:
    incremental_store = FakeStore()
    first = _fact("attempt-1", submitted_at="2026-08-25T03:00:00Z", score=50)
    second = _fact("attempt-2", submitted_at="2026-08-26T03:00:00Z")
    incremental_store.claimed = [
        {"attempt_id": "attempt-1"},
        {"attempt_id": "attempt-2"},
    ]
    incremental_store.inputs["attempt-1"] = _projection_input("attempt-1", [first])
    incremental_store.inputs["attempt-2"] = _projection_input(
        "attempt-2", [first, second]
    )

    incremental_summary = MasteryV2Projector(incremental_store).process_pending(
        limit=10,
        as_of=PROJECTION_TIME,
    )

    rebuild_store = FakeStore()
    rebuild_store.attempt_ids = ["attempt-1", "attempt-2"]
    rebuild_store.inputs = dict(incremental_store.inputs)
    rebuild_summary = MasteryV2Projector(rebuild_store).rebuild_all(
        as_of=PROJECTION_TIME
    )

    assert incremental_summary.completed == 2
    assert rebuild_summary.requested == 2
    assert rebuild_summary.rebuilt == 2
    assert rebuild_summary.failed == 0
    assert [row["source_attempt_id"] for row in rebuild_store.upserted] == [
        "attempt-1",
        "attempt-2",
    ]
    assert rebuild_store.upserted[-1] == incremental_store.completed[-1]


def test_one_utc_clock_value_is_fixed_for_the_whole_batch_and_drives_decay() -> None:
    store = FakeStore()
    store.claimed = [{"attempt_id": "attempt-1"}, {"attempt_id": "attempt-2"}]
    old_fact = _fact("attempt-1", submitted_at="2026-06-27T03:00:00Z")
    recent_fact = _fact("attempt-2", submitted_at="2026-08-26T03:00:00Z")
    store.inputs["attempt-1"] = _projection_input("attempt-1", [old_fact])
    store.inputs["attempt-2"] = _projection_input(
        "attempt-2", [old_fact, recent_fact]
    )
    clock_calls: list[datetime] = []

    def clock() -> datetime:
        value = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
        clock_calls.append(value)
        return value

    summary = MasteryV2Projector(store, clock=clock).process_pending(limit=10)

    assert summary.completed == 2
    assert len(clock_calls) == 1
    assert {row["computed_at"] for row in store.completed} == {
        "2026-08-26T03:00:00Z"
    }
    assert store.completed[0]["recency"] == 0.5


def test_difference_report_is_read_only_and_groups_topic_stage_and_subject() -> None:
    store = FakeStore()
    store.topics = [
        {"id": "topic-1", "stage": "senior_high", "subject": "math"},
        {"id": "topic-2", "stage": "senior_high", "subject": "physics"},
        {"id": "topic-3", "stage": "junior_high", "subject": "math"},
    ]
    store.v1 = [
        {"topic_id": "topic-1", "mastery": 0.5},
        {"topic_id": "topic-2", "mastery": 0.4},
    ]
    store.v2 = [
        {"topic_id": "topic-1", "mastery": 0.7},
        {"topic_id": "topic-3", "mastery": 0.6},
    ]

    report = MasteryV2Projector(store).difference_report()

    assert report["mastery_model_version"] == "mastery-v2-shadow-1"
    assert report["overall"] == {
        "topic_count": 3,
        "v1_count": 2,
        "v2_count": 2,
        "compared_count": 1,
        "v1_only_count": 1,
        "v2_only_count": 1,
        "mean_delta_v2_minus_v1": 0.2,
        "mean_absolute_delta": 0.2,
        "max_absolute_delta": 0.2,
    }
    assert report["by_stage"]["senior_high"]["topic_count"] == 2
    assert report["by_subject"]["math"]["topic_count"] == 2
    assert report["topics"][0]["delta_v2_minus_v1"] == 0.2
    assert store.completed == []
    assert store.upserted == []


def test_fact_conversion_preserves_unknown_hint_and_rejects_fabricated_identity() -> None:
    evidence = evidence_from_mapping(
        _fact("attempt-1", submitted_at="2026-08-26T03:00:00Z", used_hint=None)
    )

    assert evidence.used_hint is None
    assert evidence.attempt_id == "attempt-1"

    try:
        evidence_from_mapping({"submitted_at": "2026-08-26T03:00:00Z"})
    except ValueError as exc:
        assert "attempt_id is required" in str(exc)
    else:
        raise AssertionError("missing attempt identity must not be manufactured")
