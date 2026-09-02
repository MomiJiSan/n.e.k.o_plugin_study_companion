from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def debug(self, *_args, **_kwargs):
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


def _load(monkeypatch: pytest.MonkeyPatch, name: str):
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    mode_manager = ModuleType(f"{name}.mode_manager")
    setattr(mode_manager, "normalize_mode", lambda value: str(value or "companion"))
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    store_module = importlib.import_module(f"{name}.store")
    plan_module = importlib.import_module(f"{name}.learning_plan")
    return store_module.StudyStore, plan_module


def _store(tmp_path: Path, Store):
    store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    for topic_id in ("foundation", "core-a", "core-b"):
        store.upsert_topic(
            {
                "id": topic_id,
                "name": topic_id.title(),
                "subject": "math",
                "stage": "senior_high",
                "chapter": "algebra",
                "unit": "unit",
                "prerequisites": [],
                "related": [],
            }
        )
    return store


def _candidates(*topic_ids: str) -> list[dict]:
    return [
        {
            "topic_id": topic_id,
            "role": "prerequisite" if topic_id == "foundation" else "core",
            "mapping_score": 90.0 - index,
            "mapping_confidence": "high" if index == 0 else "medium",
            "reason_code": "material_exact_match",
            "required": True,
        }
        for index, topic_id in enumerate(topic_ids)
    ]


def _master(store, topic_id: str) -> None:
    store.append_mastery_snapshot(
        {
            "topic_id": topic_id,
            "mastery": 0.9,
            "accuracy": 0.9,
            "recency": 1.0,
            "consistency": 0.9,
            "confidence": 0.8,
            "level": "mastered",
            "attempts": 4,
            "flags": [],
        }
    )


def test_plan_persists_only_allowlisted_mapping_metadata_and_reopens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, plans = _load(monkeypatch, "_learning_plan_privacy")
    store = _store(tmp_path, Store)
    sentinel = "RAW-MATERIAL-MUST-NOT-PERSIST"
    try:
        service = plans.LearningPlanService(store)
        candidates = _candidates("foundation", "core-a")
        candidates[1]["raw_text"] = sentinel
        candidates[1]["chunk_memo"] = sentinel
        draft = service.create_draft(
            "document", "sha256:abcdef", candidates, unmatched_count=2
        )
        assert draft["display_title"] == plans.DEFAULT_LEARNING_PLAN_TITLE
        assert draft["unmatched_count"] == 2
        assert {item["topic_id"] for item in draft["items"]} == {
            "foundation",
            "core-a",
        }
        assert sentinel not in json.dumps(store.export_json(), ensure_ascii=False)
        plan_id = draft["id"]
        store.close()
        store.open()
        restored = service.get(plan_id)
        assert restored["source_digest"] == "sha256:abcdef"
        assert restored["status"] == "draft"
    finally:
        store.close()
    sqlite_bytes = b"".join(path.read_bytes() for path in tmp_path.glob("study.db*"))
    assert sentinel.encode() not in sqlite_bytes


def test_activation_rejects_topic_injection_and_enforces_one_active_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, plans = _load(monkeypatch, "_learning_plan_activation")
    store = _store(tmp_path, Store)
    try:
        service = plans.LearningPlanService(store)
        first = service.create_draft("document", "sha256:first", _candidates("core-a"))
        with pytest.raises(plans.LearningPlanError) as injected:
            service.activate(first["id"], first["revision"], ["core-a", "core-b"])
        assert injected.value.code == "LEARNING_PLAN_TOPIC_INJECTION_REJECTED"

        active = service.activate(first["id"], first["revision"], ["core-a"])
        assert active["status"] == "active"
        assert service.activate(first["id"], first["revision"], ["core-a"])["id"] == first["id"]

        second = service.create_draft("document", "sha256:second", _candidates("core-b"))
        with pytest.raises(plans.LearningPlanError) as conflict:
            service.activate(second["id"], second["revision"], ["core-b"])
        assert conflict.value.code == "ACTIVE_LEARNING_PLAN_EXISTS"
    finally:
        store.close()


def test_status_is_derived_and_completion_reopens_when_review_is_due(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, plans = _load(monkeypatch, "_learning_plan_progress")
    store = _store(tmp_path, Store)
    try:
        service = plans.LearningPlanService(store)
        draft = service.create_draft(
            "document", "sha256:progress", _candidates("foundation", "core-a")
        )
        active = service.activate(
            draft["id"], draft["revision"], ["foundation", "core-a"]
        )
        initial = service.status(active["id"])
        assert initial["progress"] == {
            "total": 2,
            "mastered": 0,
            "progressing": 0,
            "pending": 2,
            "review_due": 0,
        }
        assert service.active_selection_scope()["eligible_topic_ids"] == [
            "foundation",
            "core-a",
        ]

        _master(store, "foundation")
        _master(store, "core-a")
        future = datetime.now(timezone.utc) + timedelta(days=3)
        store.upsert_fsrs_card(
            topic_id="core-a",
            card={"due": future.isoformat(), "state": "review"},
            last_rating=3,
        )
        completed = service.reconcile(active["id"])
        assert completed["status"] == "completed"
        assert completed["progress"]["mastered"] == 2
        assert service.active_selection_scope() is None

        store.close()
        store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
        store.open()
        visible_service = plans.LearningPlanService(store)
        assert visible_service.status()["status"] == "completed"
        assert visible_service.active_selection_scope() is None

        store.close()
        store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
        store.open()
        restarted_service = plans.LearningPlanService(store)
        after_due = future + timedelta(seconds=1)
        reopened_scope = restarted_service.active_selection_scope(now=after_due)
        assert reopened_scope is not None
        assert reopened_scope["learning_plan_id"] == active["id"]
        assert reopened_scope["progress"]["review_due"] == 1
        assert reopened_scope["eligible_topic_ids"] == [
            "core-a"
        ]
        assert restarted_service.status(now=after_due)["status"] == "active"
    finally:
        store.close()


def test_pause_resume_cancel_and_purge_preserve_then_remove_expected_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, plans = _load(monkeypatch, "_learning_plan_lifecycle")
    store = _store(tmp_path, Store)
    try:
        service = plans.LearningPlanService(store)
        draft = service.create_draft("text", "sha256:lifecycle", _candidates("core-a"))
        active = service.activate(draft["id"], draft["revision"], ["core-a"])
        paused = service.pause(active["id"], active["revision"])
        assert paused["status"] == "paused"
        assert service.active_selection_scope() is None

        store.close()
        store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
        store.open()
        restarted_service = plans.LearningPlanService(store)
        restored = restarted_service.status()
        assert restored["id"] == paused["id"]
        assert restored["status"] == "paused"
        assert restarted_service.active_selection_scope() is None
        resumed = restarted_service.activate(
            restored["id"], restored["revision"], ["core-a"]
        )
        assert resumed["status"] == "active"
        canceled = restarted_service.cancel(resumed["id"], resumed["revision"])
        assert canceled["status"] == "canceled"
        assert store.get_topic("core-a") is not None
        deleted = store.purge_all()
        assert deleted["learning_plan_items"] == 1
        assert deleted["learning_plans"] == 1
        assert restarted_service.list() == []
    finally:
        store.close()


def test_adaptive_loop_config_defaults_and_strict_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _ = _load(monkeypatch, "_learning_plan_config")
    models = importlib.import_module("_learning_plan_config.models")
    defaults = models.build_config({}).adaptive_loop
    assert defaults.next_step_preview_enabled is True
    assert defaults.material_learning_plans_enabled is False
    assert defaults.auto_generate_next_question is False
    configured = models.build_config(
        {
            "study": {
                "adaptive_loop": {
                    "material_learning_plans_enabled": True,
                    "max_core_topics": 20,
                    "max_prerequisite_topics": 8,
                    "auto_generate_next_question": "true",
                }
            }
        }
    ).adaptive_loop
    assert configured.material_learning_plans_enabled is True
    assert configured.max_core_topics == 12
    assert configured.max_prerequisite_topics == 5
    assert configured.auto_generate_next_question is False
    reloaded = models.build_config(models.StudyConfig(adaptive_loop=configured).to_dict())
    assert reloaded.adaptive_loop.material_learning_plans_enabled is True
    assert reloaded.adaptive_loop.max_core_topics == 12


def test_prepare_entry_consumes_only_server_registered_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_name = "_learning_plan_entries"
    Store, _ = _load(monkeypatch, package_name)

    class SdkError(Exception):
        def __init__(self, message: str, *, code: str = "") -> None:
            super().__init__(message)
            self.code = code

    class Result:
        def __init__(self, value=None, error=None) -> None:
            self.value = value
            self.error = error

    common = ModuleType(f"{package_name}.entry_common")
    setattr(common, "Err", lambda error: Result(error=error))
    setattr(common, "Ok", lambda value: Result(value=value))
    setattr(common, "SdkError", SdkError)
    setattr(
        common,
        "_entry_exception_error",
        lambda *_args, **_kwargs: Result(error=RuntimeError("unexpected")),
    )
    setattr(common, "asyncio", asyncio)
    setattr(common, "plugin_entry", lambda **_kwargs: lambda function: function)
    setattr(common, "ui", SimpleNamespace(action=lambda: lambda function: function))
    monkeypatch.setitem(sys.modules, common.__name__, common)
    entries = importlib.import_module(f"{package_name}.entry_learning_plan_entries")
    store = _store(tmp_path, Store)

    class Owner(entries._LearningPlanEntriesMixin):
        def __init__(self) -> None:
            self._store = store
            self._cfg = SimpleNamespace(
                adaptive_loop=SimpleNamespace(
                    material_learning_plans_enabled=True,
                    max_core_topics=12,
                    max_prerequisite_topics=5,
                )
            )

    owner = Owner()
    try:
        unavailable = asyncio.run(owner.study_learning_plan_prepare("unknown"))
        assert unavailable.error.code == "LEARNING_PLAN_PREPARE_INPUT_UNAVAILABLE"

        owner._register_learning_plan_prepare_input(
            "job-1",
            "document",
            "sha256:job-1",
            [{**_candidates("core-a")[0], "raw_text": "private material"}],
        )
        prepared = asyncio.run(owner.study_learning_plan_prepare("job-1"))
        assert prepared.value["plan"]["status"] == "draft"
        assert "candidates" not in inspect.signature(
            owner.study_learning_plan_prepare
        ).parameters
        consumed = asyncio.run(owner.study_learning_plan_prepare("job-1"))
        assert consumed.error.code == "LEARNING_PLAN_PREPARE_INPUT_UNAVAILABLE"
    finally:
        store.close()
