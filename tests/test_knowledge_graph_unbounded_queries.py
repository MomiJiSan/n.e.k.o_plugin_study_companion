from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_tracker(monkeypatch: pytest.MonkeyPatch):
    package_name = "_study_companion_unbounded_graph_test"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    return importlib.import_module(f"{package_name}.knowledge_tracker")


class _Store:
    def __init__(self) -> None:
        self.list_topics_limits: list[int | None] = []
        self.mastery_requests: list[set[str]] = []
        self.topics = [
            {"id": f"topic-{index}", "prerequisites": []}
            for index in range(1001)
        ] + [
            {
                "id": "gated-topic",
                "prerequisites": [{"id": "old-prerequisite"}],
            }
        ]
        self.mastery = {"old-prerequisite": 0.9}

    def list_topics(self, limit: int | None = None):
        self.list_topics_limits.append(limit)
        return list(self.topics)

    def list_latest_mastery_for_topics(self, topic_ids: set[str]):
        self.mastery_requests.append(set(topic_ids))
        return [
            {"topic_id": topic_id, "mastery": mastery}
            for topic_id, mastery in self.mastery.items()
            if topic_id in topic_ids
        ]

    def get_topic(self, topic_id: str):
        return next((topic for topic in self.topics if topic["id"] == topic_id), None)


def test_ready_and_blocker_queries_do_not_truncate_at_one_thousand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker_module = _load_tracker(monkeypatch)
    store = _Store()
    graph = tracker_module.KnowledgeGraph(store)

    ready = graph.get_ready_topics(set())

    assert store.list_topics_limits == [None]
    assert "topic-1000" in ready
    assert "gated-topic" in ready
    assert store.mastery_requests == [{"old-prerequisite"}]

    store.mastery["old-prerequisite"] = 0.54
    assert graph.find_blocker("gated-topic") == ["old-prerequisite"]
    assert store.mastery_requests[-1] == {"old-prerequisite"}


def test_missing_threshold_uses_compatibility_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker_module = _load_tracker(monkeypatch)
    store = _Store()
    store.mastery["old-prerequisite"] = 0.0
    graph = tracker_module.KnowledgeGraph(store)

    assert graph.find_blocker("gated-topic") == ["old-prerequisite"]
