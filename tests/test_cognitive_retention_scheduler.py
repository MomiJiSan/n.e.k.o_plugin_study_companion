from __future__ import annotations

import asyncio

import pytest
from test_cognitive_answer_event_integration import _load_runtime


class _Summary:
    def __init__(self, **payload: object) -> None:
        self.payload = payload

    def to_dict(self) -> dict[str, object]:
        return dict(self.payload)


def test_post_retention_outbox_completion_is_folded_in_same_scheduler_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, tracker_module = _load_runtime(
        monkeypatch,
        "_cognitive_retention_scheduler",
    )

    class Store:
        drains = 0

        def process_cognitive_outbox(self, *, limit, include_retention):
            assert limit == 5
            assert include_retention is True
            self.drains += 1
            return {"completed": 1 if self.drains == 2 else 0}

    class Projector:
        pending = 0
        dirty = 0

        async def process_pending(self, *, limit):
            assert limit == 5
            self.pending += 1
            return _Summary(claimed=0, completed=0, failed=0, failures=[])

        async def process_dirty_topics(self, *, limit):
            assert limit == 5
            self.dirty += 1
            return _Summary(
                requested=1,
                rebuilt=1,
                skipped=0,
                failed=0,
                failures=[],
            )

    tracker = object.__new__(tracker_module.KnowledgeTracker)
    tracker.store = Store()
    tracker._cognitive_projector = Projector()
    tracker._cognitive_projection_enabled = True
    tracker._cognitive_retention_enabled = True

    result = asyncio.run(tracker.project_cognitive_pending(limit=5))

    assert tracker.store.drains == 2
    assert tracker._cognitive_projector.pending == 1
    assert tracker._cognitive_projector.dirty == 1
    assert result["rebuilt"] == 1
