from __future__ import annotations

from typing import Any

import pytest

from adaptive_learning.learner_state import (
    DEFAULT_MASTERY_V2_MODEL_VERSION,
    LearnerStateReader,
)


class _Store:
    def __init__(self) -> None:
        self.v1_by_topic: dict[str, dict[str, Any]] = {}
        self.v2_by_topic: dict[str, dict[str, Any]] = {}
        self.overview: list[dict[str, Any]] = []
        self.calls: list[tuple[Any, ...]] = []

    def get_latest_mastery(self, topic_id: str) -> dict[str, Any] | None:
        self.calls.append(("get_v1", topic_id))
        return self.v1_by_topic.get(topic_id)

    def list_latest_mastery_for_topics(
        self,
        topic_ids: list[str] | tuple[str, ...],
    ) -> list[dict[str, Any]]:
        keys = list(topic_ids)
        self.calls.append(("list_v1", keys))
        return [self.v1_by_topic[key] for key in keys if key in self.v1_by_topic]

    def list_mastery_overview(self, limit: int = 20) -> list[dict[str, Any]]:
        self.calls.append(("overview_v1", limit))
        return self.overview[:limit]

    def count_tracked_mastery_topics(self) -> int:
        self.calls.append(("count_v1",))
        return len(self.overview)

    def average_latest_mastery(self) -> float:
        self.calls.append(("average_v1",))
        if not self.overview:
            return 0.0
        return sum(float(row["mastery"]) for row in self.overview) / len(
            self.overview
        )

    def get_latest_mastery_v2(
        self,
        *,
        topic_id: str,
        mastery_model_version: str,
    ) -> dict[str, Any] | None:
        self.calls.append(("get_v2", topic_id, mastery_model_version))
        return self.v2_by_topic.get(topic_id)

    def list_latest_mastery_v2_for_topics(
        self,
        topic_ids: list[str] | tuple[str, ...],
        *,
        mastery_model_version: str,
    ) -> list[dict[str, Any]]:
        keys = list(topic_ids)
        self.calls.append(("list_v2", keys, mastery_model_version))
        return [self.v2_by_topic[key] for key in keys if key in self.v2_by_topic]


def _v1(topic_id: str, mastery: float) -> dict[str, Any]:
    return {
        "id": 1,
        "topic_id": topic_id,
        "topic_name": f"Topic {topic_id}",
        "chapter": "chapter",
        "subject": "math",
        "mastery": mastery,
        "accuracy": mastery,
        "recency": 1.0,
        "consistency": 0.8,
        "confidence": 0.7,
        "level": "learning",
        "attempts": 3,
        "flags": ["legacy-flag"],
        "updated_at": "2026-08-26 10:00:00",
        "v1_extension": "preserved",
    }


def _v2(topic_id: str, mastery: float) -> dict[str, Any]:
    return {
        "id": 9,
        "topic_id": topic_id,
        "mastery": mastery,
        "accuracy": 0.88,
        "recency": 0.77,
        "consistency": 0.66,
        "confidence": 0.55,
        "evidence_count": 4,
        "unresolved_wrong_count": 0,
        "mastery_model_version": DEFAULT_MASTERY_V2_MODEL_VERSION,
        "source_attempt_id": "attempt-9",
        "computed_at": "2026-08-26 11:00:00",
    }


def test_v1_is_default_and_preserves_store_rows_exactly() -> None:
    store = _Store()
    row = _v1("topic-a", 0.4)
    store.v1_by_topic["topic-a"] = row
    store.overview = [row]

    reader = LearnerStateReader(store)

    assert reader.read_model == "v1"
    assert reader.get_mastery("topic-a") is row
    assert reader.list_mastery(["topic-a"])[0] is row
    assert reader.list_overview(7)[0] is row
    assert not any(call[0].endswith("v2") for call in store.calls)


def test_invalid_read_model_fails_closed_to_v1() -> None:
    store = _Store()
    row = _v1("topic-a", 0.4)
    store.v1_by_topic["topic-a"] = row

    reader = LearnerStateReader(store, read_model="future")

    assert reader.read_model == "v1"
    assert reader.get_mastery("topic-a") is row
    assert store.calls == [("get_v1", "topic-a")]


def test_v2_get_adds_legacy_shape_and_retains_v2_provenance() -> None:
    store = _Store()
    legacy = _v1("topic-a", 0.4)
    store.v1_by_topic["topic-a"] = legacy
    store.v2_by_topic["topic-a"] = _v2("topic-a", 0.9)

    row = LearnerStateReader(store, read_model="v2").get_mastery("topic-a")

    assert row is not None
    assert row["mastery"] == 0.9
    assert row["attempts"] == 4
    assert row["updated_at"] == "2026-08-26 11:00:00"
    assert row["topic_name"] == legacy["topic_name"]
    assert row["chapter"] == legacy["chapter"]
    assert row["subject"] == legacy["subject"]
    assert row["level"] == ""
    assert row["flags"] == []
    assert row["mastery_model_version"] == DEFAULT_MASTERY_V2_MODEL_VERSION
    assert row["source_attempt_id"] == "attempt-9"
    assert store.calls == [
        ("get_v2", "topic-a", DEFAULT_MASTERY_V2_MODEL_VERSION),
        ("get_v1", "topic-a"),
    ]


def test_v2_get_falls_back_to_exact_v1_row_for_missing_projection() -> None:
    store = _Store()
    row = _v1("topic-a", 0.4)
    store.v1_by_topic["topic-a"] = row

    selected = LearnerStateReader(store, read_model="v2").get_mastery("topic-a")

    assert selected is row
    assert store.calls == [
        ("get_v2", "topic-a", DEFAULT_MASTERY_V2_MODEL_VERSION),
        ("get_v1", "topic-a"),
    ]


def test_v2_list_overlays_per_topic_and_preserves_v1_order_and_metadata() -> None:
    store = _Store()
    first = _v1("topic-a", 0.4)
    second = _v1("topic-b", 0.5)
    store.v1_by_topic = {"topic-a": first, "topic-b": second}
    store.v2_by_topic["topic-a"] = _v2("topic-a", 0.9)

    rows = LearnerStateReader(store, read_model="v2").list_mastery(
        ["topic-a", "topic-b"]
    )

    assert [row["topic_id"] for row in rows] == ["topic-a", "topic-b"]
    assert rows[0]["mastery"] == 0.9
    assert rows[0]["topic_name"] == first["topic_name"]
    assert rows[0]["chapter"] == first["chapter"]
    assert rows[1] is second


def test_v2_overview_keeps_v1_membership_limit_and_per_topic_fallback() -> None:
    store = _Store()
    first = _v1("topic-a", 0.4)
    second = _v1("topic-b", 0.5)
    store.overview = [first, second]
    store.v2_by_topic["topic-a"] = _v2("topic-a", 0.9)

    rows = LearnerStateReader(store, read_model="v2").list_overview(2)

    assert [row["topic_id"] for row in rows] == ["topic-a", "topic-b"]
    assert rows[0]["mastery"] == 0.9
    assert rows[1] is second
    assert store.calls == [
        ("overview_v1", 2),
        (
            "list_v2",
            ["topic-a", "topic-b"],
            DEFAULT_MASTERY_V2_MODEL_VERSION,
        ),
    ]


def test_aggregate_reads_preserve_v1_and_overlay_v2() -> None:
    store = _Store()
    first = _v1("topic-a", 0.4)
    second = _v1("topic-b", 0.6)
    store.overview = [first, second]
    store.v2_by_topic["topic-a"] = _v2("topic-a", 0.8)

    v1_reader = LearnerStateReader(store)
    assert v1_reader.count_tracked_topics() == 2
    assert v1_reader.average_mastery() == 0.5

    v2_reader = LearnerStateReader(store, read_model="v2")
    assert v2_reader.count_tracked_topics() == 2
    assert v2_reader.average_mastery() == pytest.approx(0.7)
