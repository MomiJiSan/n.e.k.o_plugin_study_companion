from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_material_learning_plan_document_bridge_test"


class _Logger:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


class _SdkError(RuntimeError):
    pass


class _Ok:
    def __init__(self, value: Any) -> None:
        self.value = value


def _count_tokens(text: object) -> int:
    return max(1, len(str(text)) // 4)


def _identity_decorator(*_args: object, **_kwargs: object):
    return lambda value: value


def _load_document_runtime(monkeypatch: pytest.MonkeyPatch):
    package = ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, PACKAGE_NAME, package)

    utils_package = ModuleType("utils")
    utils_package.__path__ = []  # type: ignore[attr-defined]
    tokenize_module = ModuleType("utils.tokenize")
    setattr(tokenize_module, "count_tokens", _count_tokens)
    monkeypatch.setitem(sys.modules, "utils", utils_package)
    monkeypatch.setitem(sys.modules, "utils.tokenize", tokenize_module)

    mode_manager = ModuleType(f"{PACKAGE_NAME}.mode_manager")
    setattr(mode_manager, "normalize_mode", lambda value: str(value or "companion"))
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    models = importlib.import_module(f"{PACKAGE_NAME}.models")

    entry_common = ModuleType(f"{PACKAGE_NAME}.entry_common")
    setattr(entry_common, "Ok", _Ok)
    setattr(entry_common, "SdkError", _SdkError)
    setattr(entry_common, "StudyEvent", SimpleNamespace)
    setattr(entry_common, "asyncio", asyncio)
    setattr(entry_common, "plugin_entry", _identity_decorator)
    setattr(entry_common, "tr", lambda _key, default="", **_kwargs: default)
    setattr(entry_common, "ui", SimpleNamespace(action=lambda: _identity_decorator()))
    monkeypatch.setitem(sys.modules, entry_common.__name__, entry_common)

    model_gateway = ModuleType(f"{PACKAGE_NAME}.study_model_gateway")
    setattr(
        model_gateway,
        "StudyModelError",
        type("StudyModelError", (RuntimeError,), {}),
    )
    monkeypatch.setitem(sys.modules, model_gateway.__name__, model_gateway)

    narration = ModuleType(f"{PACKAGE_NAME}._general_narration")
    setattr(
        narration,
        "prepare_general_narration_content",
        lambda value: str(value or ""),
    )
    monkeypatch.setitem(sys.modules, narration.__name__, narration)

    tutor_document = ModuleType(f"{PACKAGE_NAME}.tutor_llm_agent_document")

    class _DocumentModelResult:
        def __init__(self, text: str, output_limit_reached: bool = False) -> None:
            self.text = text
            self.output_limit_reached = output_limit_reached

    async def _unused(*_args: object, **_kwargs: object):
        raise AssertionError("chunked model helper must not be called")

    setattr(tutor_document, "_DocumentModelResult", _DocumentModelResult)
    setattr(tutor_document, "_analyze_document_chunk_result", _unused)
    setattr(tutor_document, "_merge_document_chunks_result", _unused)
    monkeypatch.setitem(sys.modules, tutor_document.__name__, tutor_document)

    return SimpleNamespace(
        document_entries=importlib.import_module(
            f"{PACKAGE_NAME}.entry_document_analysis_jobs"
        ),
        models=models,
    )


class _TopicStore:
    def __init__(self) -> None:
        self.topic = {
            "id": "algebra",
            "name": "Algebra",
            "subject": "math",
            "aliases": [],
            "prerequisites": [],
        }

    def list_topics(self, limit: int | None = 100, **_kwargs: object):
        assert limit is None
        return [dict(self.topic)]

    def get_topic(self, topic_id: str):
        return dict(self.topic) if topic_id == "algebra" else None

    def get_latest_mastery(self, _topic_id: str):
        return None


class _CapturingLogger(_Logger):
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: object, *_args: object, **_kwargs: object) -> None:
        self.messages.append(str(message))


def _harness(runtime: Any, service: object, *, summary: str = "Algebra summary"):
    models = runtime.models

    class Agent:
        async def resolve_model_runtime(self, _role: str):
            return None

        async def document_analyze(self, document):
            return models.TutorReply(
                operation="document_analyze",
                input_text=document.descriptor,
                reply=summary,
                payload={"document": document.public_metadata()},
                diagnostic="",
                created_at=models.utc_now_iso(),
            )

    class Harness(runtime.document_entries._DocumentAnalysisJobsEntriesMixin):
        def __init__(self) -> None:
            self._agent = Agent()
            self._cfg = SimpleNamespace(
                adaptive_loop=SimpleNamespace(
                    material_learning_plans_enabled=True,
                    auto_prepare_plan=True,
                ),
                communication=SimpleNamespace(
                    enabled=False, general_narration_enabled=True
                )
            )
            self._event_bus = None
            self._store = _TopicStore()
            self._learning_plan_service = service
            self.logger = _CapturingLogger()

        async def _finalize_tutor_call(
            self, _operation: str, reply: object, **kwargs: object
        ) -> dict[str, object]:
            public_payload = kwargs.get("public_payload")
            return {
                "reply": getattr(reply, "reply"),
                **(
                    dict(public_payload)
                    if isinstance(public_payload, dict)
                    else {}
                ),
            }

    return Harness()


async def _wait_for_job(harness: Any, started: Any) -> dict[str, object]:
    manager = harness._document_job_manager()
    job = manager._jobs[started.value["job_id"]]
    assert job.task is not None
    await asyncio.wait_for(asyncio.shield(job.task), timeout=5)
    return job.public_payload()


@pytest.mark.asyncio
async def test_document_completion_creates_safe_draft_before_runtime_text_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _load_document_runtime(monkeypatch)
    sentinel = "PRIVATE-DOCUMENT-BRIDGE-ZXQ"

    class Service:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def create_draft(self, **kwargs: object):
            self.calls.append(dict(kwargs))
            return {"id": "lp-safe", "status": "draft", "revision": 1}

    service = Service()
    harness = _harness(runtime, service)
    manager = harness._document_job_manager()
    try:
        started = await harness.study_start_document_analysis(
            document_name="private.txt",
            document_type="text/plain",
            document_text=f"{sentinel} source text",
            analysis_kind="course_material",
            locale="en",
        )
        assert hasattr(started, "value")
        completed = await _wait_for_job(harness, started)
    finally:
        await manager.shutdown()

    assert completed["status"] == "completed"
    assert completed["learning_plan_draft"] == {
        "id": "lp-safe",
        "status": "draft",
        "revision": 1,
    }
    assert completed["learning_plan_mapping"] == {
        "unmatched_count": 0,
        "truncated": False,
    }
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["source_kind"] == "document"
    assert len(str(call["source_digest"])) == 64
    assert call["display_title"] == ""
    candidates = call["candidates"]
    assert isinstance(candidates, list)
    candidate = candidates[0]
    assert isinstance(candidate, dict)
    assert candidate == {
        "topic_id": "algebra",
        "role": "core",
        "mapping_score": candidate["mapping_score"],
        "mapping_confidence": "high",
        "reason_code": "material_exact_match",
        "required": True,
    }
    assert candidate["mapping_score"] >= 40
    assert sentinel not in json.dumps(service.calls, ensure_ascii=False)
    assert sentinel not in json.dumps(completed, ensure_ascii=False)


@pytest.mark.asyncio
async def test_draft_failure_never_changes_successful_document_result_or_logs_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _load_document_runtime(monkeypatch)
    sentinel = "PRIVATE-DRAFT-FAILURE-ZXQ"

    class Service:
        def create_draft(self, **_kwargs: object):
            raise RuntimeError(sentinel)

    harness = _harness(runtime, Service())
    manager = harness._document_job_manager()
    try:
        started = await harness.study_start_document_analysis(
            document_name="private.txt",
            document_type="text/plain",
            document_text=f"{sentinel} source text",
            analysis_kind="course_material",
            locale="en",
        )
        completed = await _wait_for_job(harness, started)
    finally:
        await manager.shutdown()

    assert completed["status"] == "completed"
    assert completed["summary"] == "Algebra summary"
    assert "learning_plan_draft" not in completed
    assert completed["learning_plan_mapping"] == {
        "unmatched_count": 0,
        "truncated": False,
    }
    assert harness.logger.messages == [
        "material learning plan draft persistence failed"
    ]
    assert sentinel not in json.dumps(completed, ensure_ascii=False)
    assert sentinel not in repr(harness.logger.messages)


@pytest.mark.asyncio
async def test_all_low_confidence_matches_return_count_without_creating_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _load_document_runtime(monkeypatch)

    class Service:
        calls = 0

        def create_draft(self, **_kwargs: object):
            self.calls += 1
            return {"id": "must-not-exist"}

    service = Service()
    harness = _harness(runtime, service, summary="plan")
    harness._store.topic = {
        "id": "study-plan",
        "name": "Study Plan",
        "subject": "general",
        "aliases": ["plan"],
        "prerequisites": [],
    }
    manager = harness._document_job_manager()
    try:
        started = await harness.study_start_document_analysis(
            document_name="unmatched.txt",
            document_type="text/plain",
            document_text="private source",
            analysis_kind="course_material",
            locale="en",
        )
        completed = await _wait_for_job(harness, started)
    finally:
        await manager.shutdown()

    assert completed["status"] == "completed"
    assert "learning_plan_draft" not in completed
    assert completed["learning_plan_mapping"] == {
        "unmatched_count": 1,
        "truncated": False,
    }
    assert service.calls == 0


@pytest.mark.asyncio
async def test_document_bridge_is_fail_closed_when_material_plans_are_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _load_document_runtime(monkeypatch)

    class Service:
        calls = 0

        def create_draft(self, **_kwargs: object):
            self.calls += 1
            return {"id": "must-not-exist"}

    service = Service()
    harness = _harness(runtime, service)
    harness._cfg.adaptive_loop.material_learning_plans_enabled = False
    manager = harness._document_job_manager()
    try:
        started = await harness.study_start_document_analysis(
            document_name="disabled.txt",
            document_type="text/plain",
            document_text="private source",
            analysis_kind="course_material",
            locale="en",
        )
        completed = await _wait_for_job(harness, started)
    finally:
        await manager.shutdown()

    assert completed["status"] == "completed"
    assert "learning_plan_draft" not in completed
    assert service.calls == 0


@pytest.mark.asyncio
async def test_cancel_before_document_success_never_creates_a_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _load_document_runtime(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()

    class Service:
        calls = 0

        def create_draft(self, **_kwargs: object):
            self.calls += 1
            return {"id": "must-not-exist"}

    service = Service()
    harness = _harness(runtime, service)

    async def blocked_document_analyze(document: object):
        del document
        entered.set()
        await release.wait()
        raise AssertionError("canceled document analysis must not resume")

    harness._agent.document_analyze = blocked_document_analyze
    manager = harness._document_job_manager()
    try:
        started = await harness.study_start_document_analysis(
            document_name="cancel.txt",
            document_type="text/plain",
            document_text="private cancel source",
            analysis_kind="course_material",
            locale="en",
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        canceled = await harness.study_cancel_document_analysis(
            started.value["job_id"]
        )
    finally:
        release.set()
        await manager.shutdown()

    assert hasattr(canceled, "value")
    assert canceled.value["status"] == "canceled"
    assert service.calls == 0


@pytest.mark.asyncio
async def test_cancel_after_finalization_drains_draft_and_preserves_completed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _load_document_runtime(monkeypatch)

    class Service:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()

        def create_draft(self, **_kwargs: object):
            self.entered.set()
            assert self.release.wait(timeout=3)
            return {"id": "lp-race", "status": "draft", "revision": 1}

    service = Service()
    harness = _harness(runtime, service)
    manager = harness._document_job_manager()
    try:
        started = await harness.study_start_document_analysis(
            document_name="race.txt",
            document_type="text/plain",
            document_text="private source",
            analysis_kind="course_material",
            locale="en",
        )
        assert await asyncio.to_thread(service.entered.wait, 2)
        cancel_task = asyncio.create_task(
            harness.study_cancel_document_analysis(started.value["job_id"])
        )
        await asyncio.sleep(0)
        service.release.set()
        canceled = await asyncio.wait_for(cancel_task, timeout=5)
    finally:
        service.release.set()
        await manager.shutdown()

    assert hasattr(canceled, "value")
    assert canceled.value["status"] == "completed"
    assert canceled.value["learning_plan_draft"]["id"] == "lp-race"
