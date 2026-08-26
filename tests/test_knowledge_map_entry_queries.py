from __future__ import annotations

import asyncio
import importlib
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_entries(monkeypatch: pytest.MonkeyPatch):
    # pytest-randomly can run this loader after another dynamic-import test.
    # A per-call package name prevents a stale submodule from retaining that
    # test's mocked entry_common dependencies.
    package_name = f"_study_companion_knowledge_map_entry_test_{id(monkeypatch)}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    ui_api = importlib.import_module(f"{package_name}.ui_api")
    common = ModuleType(f"{package_name}.entry_common")
    common.Ok = lambda payload: payload
    common.PublicGraphContributionBuilder = object
    common.StudyConfig = object
    def _raise_entry_exception(_owner, exc, **_kwargs):
        raise exc

    common._entry_exception_error = _raise_entry_exception
    common.asyncio = asyncio
    common.build_contribution_settings_payload = lambda **_kwargs: {}
    common.build_knowledge_map_page_payload = ui_api.build_knowledge_map_page_payload
    common.build_knowledge_map_payload = ui_api.build_knowledge_map_payload
    common.plugin_entry = lambda **_kwargs: lambda function: function
    common.tr = lambda _key, *, default: default

    class _Ui:
        @staticmethod
        def action():
            return lambda function: function

    common.ui = _Ui()
    monkeypatch.setitem(sys.modules, common.__name__, common)

    guidance = ModuleType(f"{package_name}.knowledge_graph_guidance")
    guidance.build_knowledge_guidance_payload = lambda **_kwargs: {}
    monkeypatch.setitem(sys.modules, guidance.__name__, guidance)

    quality = ModuleType(f"{package_name}.knowledge_quality")
    quality.KnowledgeCandidateStatus = type("Status", (), {"TRUSTED": type("T", (), {"value": "trusted"})})
    quality.KnowledgeCandidateType = type("Type", (), {})
    quality.KnowledgeEvidenceType = type("Evidence", (), {})
    monkeypatch.setitem(sys.modules, quality.__name__, quality)
    return importlib.import_module(f"{package_name}.entry_knowledge_entries")


class _Store:
    def __init__(self) -> None:
        self.topic_limits: list[int | None] = []
        self.mastery_requests: list[set[str]] = []
        self.wrong_requests: list[dict[str, object]] = []
        self.scope_topic = {
            "id": "scope-topic",
            "name": "范围主题",
            "subject": "math",
            "stage": "junior_high",
            "prerequisites": [],
            "related": [],
        }

    def list_topics(self, limit, subject=None, stage=None):
        self.topic_limits.append(limit)
        if subject or stage:
            return [self.scope_topic]
        return [self.scope_topic, *({"id": f"catalog-{index}", "name": str(index)} for index in range(1001))]

    def list_latest_mastery_for_topics(self, topic_ids):
        self.mastery_requests.append(set(topic_ids))
        return [{"topic_id": "scope-topic", "mastery": 0.9, "flags": []}]

    def list_wrong_questions(self, **kwargs):
        self.wrong_requests.append(dict(kwargs))
        return [{"id": "wrong", "topic_id": "scope-topic", "status": "retrying"}]

    def query_knowledge_map_page(self, **kwargs):
        self.map_v2_request = dict(kwargs)
        return {
            "schema_version": 2,
            "catalog_revision": "edge-v1",
            "scope": {"stage": "senior_high", "subject": "math"},
            "scope_total_count": 1_001,
            "scope_returned_count": 1,
            "has_more": True,
            "next_cursor": "next-page",
            "nodes": [self.scope_topic],
            "edges": [],
            "boundary": {"nodes": [], "returned_count": 0, "truncated": False},
        }


class _Tracker:
    def get_weak_topics(self, **_kwargs):
        return []


def test_knowledge_map_reads_mastery_and_active_wrong_questions_by_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)
    store = _Store()

    class Harness(entries._KnowledgeEntriesMixin):
        _store = store
        _knowledge_tracker = _Tracker()

    payload = asyncio.run(Harness().study_knowledge_map(limit=10, subject="math"))

    assert store.topic_limits == [10, None]
    assert store.mastery_requests == [{"scope-topic"}]
    assert store.wrong_requests == [
        {
            "limit": None,
            "topic_ids": {"scope-topic"},
            "statuses": ("active", "retrying"),
        }
    ]
    node = next(item for item in payload["nodes"] if item["id"] == "scope-topic")
    assert node["mastery"] == 0.9
    assert node["mastery_status"] == "progress"
    assert node["weak"] is True


def test_knowledge_graph_builds_leave_the_async_entry_responsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heartbeat must run while the deliberately blocked graph worker runs."""
    entries = _load_entries(monkeypatch)
    store = _Store()
    started = threading.Event()
    release = threading.Event()
    heartbeat = threading.Event()

    def blocked_map_builder(**_kwargs):
        started.set()
        assert release.wait(2)
        return {"heartbeat_seen_while_building": heartbeat.is_set()}

    entries.build_knowledge_map_payload = blocked_map_builder

    class Harness(entries._KnowledgeEntriesMixin):
        _store = store
        _knowledge_tracker = _Tracker()

    async def run_map():
        # This timer makes a synchronous regression complete deterministically:
        # its result then proves whether the loop could service the heartbeat.
        threading.Timer(0.5, release.set).start()
        task = asyncio.create_task(Harness().study_knowledge_map(limit=10, subject="math"))
        assert await asyncio.to_thread(started.wait, 1)
        asyncio.get_running_loop().call_soon(heartbeat.set)
        return await task

    assert asyncio.run(run_map()) == {"heartbeat_seen_while_building": True}


def test_knowledge_map_v2_reads_a_bounded_page_and_reports_real_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)
    store = _Store()

    class Harness(entries._KnowledgeEntriesMixin):
        _store = store
        _knowledge_tracker = _Tracker()

    payload = asyncio.run(
        Harness().study_query_knowledge_map(
            scope={"stage": "senior_high", "subject": "math"},
            page_size=100,
            cursor="",
            include_boundary=True,
        )
    )

    assert store.map_v2_request == {
        "stage": "senior_high",
        "subject": "math",
        "course_family": "",
        "chapter": "",
        "unit": "",
        "page_size": 100,
        "cursor": "",
        "include_boundary": True,
    }
    assert payload["schema_version"] == 2
    assert payload["scope_total_count"] == 1_001
    assert payload["scope_returned_count"] == 1
    assert payload["has_more"] is True
    assert payload["next_cursor"] == "next-page"


def test_knowledge_guidance_build_leaves_the_async_entry_responsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)
    started = threading.Event()
    release = threading.Event()
    heartbeat = threading.Event()

    def blocked_guidance_builder(**_kwargs):
        started.set()
        assert release.wait(2)
        return {"heartbeat_seen_while_building": heartbeat.is_set()}

    entries.build_knowledge_guidance_payload = blocked_guidance_builder

    class Harness(entries._KnowledgeEntriesMixin):
        _store = _Store()

    async def run_guidance():
        threading.Timer(0.5, release.set).start()
        task = asyncio.create_task(Harness().study_knowledge_guidance(topic_id="scope-topic"))
        assert await asyncio.to_thread(started.wait, 1)
        asyncio.get_running_loop().call_soon(heartbeat.set)
        return await task

    assert asyncio.run(run_guidance()) == {"heartbeat_seen_while_building": True}
