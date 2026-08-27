from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_tracker(monkeypatch: pytest.MonkeyPatch):
    package_name = "_study_companion_learner_state_contract"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(  # type: ignore[attr-defined]
        value or "companion"
    )
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{package_name}.knowledge_tracker")


class _Store:
    def __init__(self) -> None:
        self.topics = [
            {"id": "topic-a", "name": "Topic A", "prerequisites": []},
        ]
        self.v1 = {
            "topic-a": {
                "id": 1,
                "topic_id": "topic-a",
                "topic_name": "Topic A",
                "chapter": "chapter-a",
                "subject": "math",
                "mastery": 0.25,
                "accuracy": 0.25,
                "recency": 1.0,
                "consistency": 1.0,
                "confidence": 0.5,
                "level": "learning",
                "attempts": 2,
                "flags": ["low_confidence"],
                "updated_at": "v1-time",
            }
        }
        self.v2 = {
            "topic-a": {
                "id": 2,
                "topic_id": "topic-a",
                "mastery": 0.85,
                "accuracy": 0.9,
                "recency": 0.8,
                "consistency": 0.7,
                "confidence": 0.6,
                "evidence_count": 4,
                "unresolved_wrong_count": 0,
                "mastery_model_version": "mastery-v2-shadow-1",
                "source_attempt_id": "attempt-v2",
                "computed_at": "v2-time",
            }
        }
        self.calls: list[tuple[Any, ...]] = []
        self.batch_kwargs: dict[str, Any] = {}

    def list_topics(self, limit: int | None = None):
        return list(self.topics)

    def count_topics(self) -> int:
        return len(self.topics)

    def get_topic(self, topic_id: str):
        return next(
            (topic for topic in self.topics if topic["id"] == topic_id),
            None,
        )

    def find_topic_by_name(self, name: str):
        return next(
            (topic for topic in self.topics if topic["name"] == name),
            None,
        )

    def get_latest_mastery(self, topic_id: str):
        self.calls.append(("get_v1", topic_id))
        return self.v1.get(topic_id)

    def list_latest_mastery_for_topics(self, topic_ids):
        keys = list(topic_ids)
        self.calls.append(("list_v1", keys))
        return [self.v1[key] for key in keys if key in self.v1]

    def list_mastery_overview(self, limit: int = 20):
        self.calls.append(("overview_v1", limit))
        return list(self.v1.values())[:limit]

    def count_tracked_mastery_topics(self) -> int:
        return len(self.v1)

    def average_latest_mastery(self) -> float:
        if not self.v1:
            return 0.0
        return sum(float(row["mastery"]) for row in self.v1.values()) / len(self.v1)

    def get_latest_mastery_v2(
        self,
        *,
        topic_id: str,
        mastery_model_version: str,
    ):
        self.calls.append(("get_v2", topic_id, mastery_model_version))
        return self.v2.get(topic_id)

    def list_latest_mastery_v2_for_topics(
        self,
        topic_ids,
        *,
        mastery_model_version: str,
    ):
        keys = list(topic_ids)
        self.calls.append(("list_v2", keys, mastery_model_version))
        return [self.v2[key] for key in keys if key in self.v2]

    def load_answer_write_state(self, _topic_id: str, *, recent_limit: int):
        assert recent_limit == 10
        return {"recent": [], "fsrs_card": None}

    def batch_write_answer_data(self, **kwargs):
        self.batch_kwargs = dict(kwargs)
        return {
            "ok": True,
            "wrong_question_id": "",
            "wrong_question_attempt": {},
        }


@pytest.mark.parametrize(
    ("shadow_enabled", "requested_model", "expected_model", "expected_mastery"),
    [
        (False, "v2", "v1", 0.25),
        (True, "v1", "v1", 0.25),
        (True, "v2", "v2", 0.85),
    ],
)
def test_tracker_read_model_requires_both_v2_switches(
    monkeypatch: pytest.MonkeyPatch,
    shadow_enabled: bool,
    requested_model: str,
    expected_model: str,
    expected_mastery: float,
) -> None:
    tracker_module = _load_tracker(monkeypatch)
    store = _Store()
    tracker = tracker_module.KnowledgeTracker(
        store,
        mastery_config=SimpleNamespace(
            v2_shadow_enabled=shadow_enabled,
            read_model=requested_model,
            model_version="mastery-v2-shadow-1",
        ),
    )

    snapshot = tracker.get_mastery_snapshot("topic-a")

    assert tracker.learner_state.read_model == expected_model
    assert tracker.graph._learner_state is tracker.learner_state
    assert snapshot is not None
    assert snapshot["mastery"] == expected_mastery
    if expected_model == "v2":
        assert snapshot["topic_name"] == "Topic A"
        assert snapshot["chapter"] == "chapter-a"
        assert snapshot["updated_at"] == "v2-time"
    else:
        assert not any(call[0].endswith("v2") for call in store.calls)


@pytest.mark.parametrize("shadow_enabled", [False, True])
def test_answer_batch_enqueues_shadow_projection_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    shadow_enabled: bool,
) -> None:
    tracker_module = _load_tracker(monkeypatch)
    store = _Store()
    tracker = tracker_module.KnowledgeTracker(
        store,
        mastery_config={
            "v2_shadow_enabled": shadow_enabled,
            "read_model": "v1",
            "model_version": "mastery-v2-shadow-1",
        },
    )
    tracker._prepare_answer_topic_data = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: ("topic-a", True, None, None)
    )
    tracker._build_positive_candidate_data = (  # type: ignore[method-assign]
        lambda **_kwargs: None
    )

    tracker._on_answer_batch(
        topic_id="topic-a",
        question={"question_id": "question-a", "difficulty": 3},
        user_answer="answer",
        eval_result={"verdict": "correct", "score": 100},
        mode="companion",
        session_id="session-a",
        response_time_ms=500,
        used_hint=True,
        attempt_id="attempt-a",
    )

    assert store.batch_kwargs["used_hint"] is True
    assert store.batch_kwargs["enqueue_mastery_v2"] is shadow_enabled


def test_projection_worker_drains_more_than_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker_module = _load_tracker(monkeypatch)
    tracker = tracker_module.KnowledgeTracker(
        _Store(),
        mastery_config={"v2_shadow_enabled": True},
    )

    class Projector:
        calls = 0

        def process_pending(self, *, limit: int):
            self.calls += 1
            claimed = 100 if self.calls == 1 else 1 if self.calls == 2 else 0
            return SimpleNamespace(
                to_dict=lambda: {
                    "claimed": claimed,
                    "completed": claimed,
                    "failed": 0,
                    "skipped": 0,
                    "failures": [],
                }
            )

    projector = Projector()
    tracker._mastery_v2_projector = projector

    result = tracker.project_mastery_v2_pending(limit=100)

    assert projector.calls == 2
    assert result["claimed"] == 101
    assert result["completed"] == 101
    assert result["has_more"] is False


def test_projection_worker_continues_after_poison_item_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker_module = _load_tracker(monkeypatch)
    tracker = tracker_module.KnowledgeTracker(
        _Store(),
        mastery_config={"v2_shadow_enabled": True},
    )

    class Projector:
        calls = 0

        def process_pending(self, *, limit: int):
            self.calls += 1
            payloads = (
                {
                    "claimed": limit,
                    "completed": limit - 1,
                    "failed": 1,
                    "skipped": 0,
                    "failures": [{"attempt_id": "poison", "error": "bad fact"}],
                },
                {
                    "claimed": 1,
                    "completed": 1,
                    "failed": 0,
                    "skipped": 0,
                    "failures": [],
                },
            )
            return SimpleNamespace(to_dict=lambda: payloads[self.calls - 1])

    projector = Projector()
    tracker._mastery_v2_projector = projector

    result = tracker.project_mastery_v2_pending(limit=100)

    assert projector.calls == 2
    assert result["claimed"] == 101
    assert result["completed"] == 100
    assert result["failed"] == 1
