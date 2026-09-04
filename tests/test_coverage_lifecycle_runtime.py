from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
real_bridge_module: Any = import_module("knowledge_dungeon.private_bridge")


class _Result:
    def __init__(self, value: Any):
        self.value = value


class _Ok(_Result):
    pass


class _Err(_Result):
    pass


class _SdkError(Exception):
    def __init__(self, message: str, *, code: str = ""):
        super().__init__(message)
        self.code = code


class _Logger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, tuple[Any, ...]]] = []

    def _record(self, level: str, *args: Any, **_kwargs: Any) -> None:
        self.messages.append((level, args))

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self._record("debug", *args, **kwargs)

    def info(self, *args: Any, **kwargs: Any) -> None:
        self._record("info", *args, **kwargs)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self._record("warning", *args, **kwargs)

    def error(self, *args: Any, **kwargs: Any) -> None:
        self._record("error", *args, **kwargs)

    def exception(self, *args: Any, **kwargs: Any) -> None:
        self._record("exception", *args, **kwargs)


def _decorator(**_kwargs: Any):
    return lambda function: function


def _install_module(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    **attributes: Any,
) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def _blank_class(name: str) -> type:
    return type(name, (), {})


def _load_runtime(monkeypatch: pytest.MonkeyPatch):
    package = f"_coverage_lifecycle_runtime_{id(monkeypatch)}"

    plugin = _install_module(monkeypatch, "plugin")
    plugin.__path__ = []  # type: ignore[attr-defined]
    sdk = _install_module(monkeypatch, "plugin.sdk")
    sdk.__path__ = []  # type: ignore[attr-defined]

    class _NekoPluginBase:
        pass

    class _OsActivitySnapshot:
        pass

    async def get_os_activity_snapshot(*_args: Any, **_kwargs: Any):
        return None

    _install_module(
        monkeypatch,
        "plugin.sdk.plugin",
        Err=_Err,
        NekoPluginBase=_NekoPluginBase,
        Ok=_Ok,
        OsActivitySnapshot=_OsActivitySnapshot,
        SdkError=_SdkError,
        custom_event=_decorator,
        get_os_activity_snapshot=get_os_activity_snapshot,
        lifecycle=_decorator,
        neko_plugin=lambda cls: cls,
    )

    _install_module(
        monkeypatch,
        "plugin.logging_config",
        get_logger=lambda _name: _Logger(),
    )

    constants = {
        "LLM_OPERATION_ANSWER_EVALUATE": "answer_evaluate",
        "LLM_OPERATION_CONCEPT_EXPLAIN": "concept_explain",
        "LLM_OPERATION_KNOWLEDGE_TRACK": "knowledge_track",
        "LLM_OPERATION_QUESTION_GENERATE": "question_generate",
        "LLM_OPERATION_SUMMARIZE_SESSION": "summarize_session",
        "MODE_COMPANION": "companion",
        "MODE_CONCEPT_EXPLAIN": "concept_explain",
        "MODE_INTERACTIVE": "interactive",
        "MODE_TEACHING": "teaching",
    }
    _install_module(monkeypatch, f"{package}.constants", **constants)

    class _ActivityBuffer:
        def __init__(self, *, window_seconds: float, snapshot_interval: float):
            self.window_seconds = window_seconds
            self.snapshot_interval = snapshot_interval

    class _StudyEvent:
        def __init__(self, *, name: str, payload: dict[str, Any]):
            self.name = name
            self.payload = payload

    local_modules: dict[str, dict[str, Any]] = {
        "_event_bus": {
            "StudyEvent": _StudyEvent,
            "StudyEventBus": _blank_class("StudyEventBus"),
        },
        "awareness_buffer": {"ActivityBuffer": _ActivityBuffer},
        "checkin_manager": {"CheckinManager": _blank_class("CheckinManager")},
        "doc_exporter": {
            "DocExporter": _blank_class("DocExporter"),
            "normalize_format": lambda value: value,
        },
        "knowledge_contribution": {
            "PublicGraphContributionBuilder": _blank_class("PublicGraphContributionBuilder")
        },
        "knowledge_tracker": {"KnowledgeTracker": _blank_class("KnowledgeTracker")},
        "memory_deck_store": {
            "MemoryDeckStore": _blank_class("MemoryDeckStore"),
            "MemoryItemNotFoundError": _blank_class("MemoryItemNotFoundError"),
        },
        "memory_habit_bridge": {"MemoryHabitBridge": _blank_class("MemoryHabitBridge")},
        "mode_manager": {
            "ModeManager": _blank_class("ModeManager"),
            "build_transition_phrase": lambda *_args, **_kwargs: "",
            "handle_user_intent": lambda *_args, **_kwargs: {},
            "normalize_mode": lambda value: value,
        },
        "pomodoro_timer": {"PomodoroTimer": _blank_class("PomodoroTimer")},
        "screen_classifier": {
            "classify_app_from_title": lambda _title, default="unknown": default,
            "classify_screen_from_ocr": lambda *_args, **_kwargs: "unknown",
        },
        "service": {
            "build_dependency_status": lambda *_args, **_kwargs: {},
            "build_explain_payload": lambda *_args, **_kwargs: {},
            "build_ocr_payload": lambda *_args, **_kwargs: {},
            "build_status_payload": lambda *_args, **_kwargs: {},
            "build_tutor_payload": lambda *_args, **_kwargs: {},
        },
        "state": {"build_initial_state": lambda **_kwargs: SimpleNamespace()},
        "store": {"StudyStore": _blank_class("StudyStore")},
        "store_notebook": {"NotebookStore": _blank_class("NotebookStore")},
        "study_habit_store": {"StudyHabitStore": _blank_class("StudyHabitStore")},
        "study_ocr_pipeline": {"StudyOcrPipeline": _blank_class("StudyOcrPipeline")},
        "supervision": {"SupervisionController": _blank_class("SupervisionController")},
        "tutor_llm_agent": {
            "TutorLLMAgent": _blank_class("TutorLLMAgent"),
            "diagnostic_code_for_exception": lambda _exc: "error",
        },
        "ui_api": {
            "STUDY_PANEL_SURFACE_ID": "study-panel",
            "build_contribution_settings_payload": lambda **_kwargs: {},
            "build_habit_dashboard_payload": lambda **_kwargs: {},
            "build_knowledge_map_payload": lambda **_kwargs: {},
            "build_open_ui_payload": lambda **_kwargs: {},
            "build_pomodoro_status_payload": lambda **_kwargs: {},
        },
        "voice_contracts": {
            "VOICE_TRANSCRIPT_EVENT_ID": "voice-transcript",
            "VOICE_TRANSCRIPT_EVENT_TYPE": "voice-transcript",
            "voice_transcript_cancel_response": lambda: {},
            "voice_transcript_noop": lambda: {},
            "voice_transcript_prime_context": lambda: {},
        },
        "voice_filter": {
            "VoiceFilter": _blank_class("VoiceFilter"),
            "_derive_subject": lambda *_args, **_kwargs: "",
            "build_context_for_catgirl": lambda *_args, **_kwargs: {},
        },
        "models": {
            "STATUS_ERROR": "error",
            "STATUS_READY": "ready",
            "STATUS_STOPPED": "stopped",
            "AdaptiveLoopConfig": _blank_class("AdaptiveLoopConfig"),
            "ActivitySnapshot": _blank_class("ActivitySnapshot"),
            "ActivitySummary": _blank_class("ActivitySummary"),
            "StudyConfig": _blank_class("StudyConfig"),
            "StudyState": _blank_class("StudyState"),
            "TutorReply": _blank_class("TutorReply"),
            "build_config": lambda _raw: SimpleNamespace(),
            "utc_now_iso": lambda: "now",
        },
    }
    knowledge_dungeon_package = _install_module(
        monkeypatch,
        f"{package}.knowledge_dungeon",
    )
    knowledge_dungeon_package.__path__ = []  # type: ignore[attr-defined]
    _install_module(
        monkeypatch,
        f"{package}.knowledge_dungeon.private_bridge",
        KnowledgeDungeonPrivateBridge=_blank_class("KnowledgeDungeonPrivateBridge"),
    )
    for module_name, attributes in local_modules.items():
        _install_module(monkeypatch, f"{package}.{module_name}", **attributes)

    _install_module(
        monkeypatch,
        f"{package}.entry_common",
        Any=Any,
        StudyEvent=_StudyEvent,
        StudyEventBus=_blank_class("StudyEventBus"),
        asyncio=asyncio,
    )
    _install_module(
        monkeypatch,
        f"{package}.fsrs_bridge",
        REVIEW_IS_DUE_AFTER_KEY="review_is_due_after",
        REVIEW_WAS_DUE_BEFORE_KEY="review_was_due_before",
    )

    mixins = {
        "entry_checkin_entries": "_CheckinEntriesMixin",
        "entry_cognitive_entries": "_CognitiveEntriesMixin",
        "entry_communication_pomodoro_events": "_CommunicationPomodoroEventsMixin",
        "entry_communication_tutor_events": "_CommunicationTutorEventsMixin",
        "entry_document_analysis_jobs": "_DocumentAnalysisJobsEntriesMixin",
        "entry_export_support": "_ExportSupportMixin",
        "entry_goal_entries": "_GoalEntriesMixin",
        "entry_knowledge_entries": "_KnowledgeEntriesMixin",
        "entry_learning_plan_entries": "_LearningPlanEntriesMixin",
        "entry_local_model_entries": "_LocalModelEntriesMixin",
        "entry_memory_card_entries": "_MemoryCardEntriesMixin",
        "entry_memory_deck_entries": "_MemoryDeckEntriesMixin",
        "entry_memory_import_entries": "_MemoryImportEntriesMixin",
        "entry_memory_review_entries": "_MemoryReviewEntriesMixin",
        "entry_mode_entries": "_ModeEntriesMixin",
        "entry_notebook": "_NotebookEntriesMixin",
        "entry_ocr_entries": "_OcrEntriesMixin",
        "entry_pomodoro_entries": "_PomodoroEntriesMixin",
        "entry_practice_scope_entries": "_PracticeScopeEntriesMixin",
        "entry_status_entries": "_StatusEntriesMixin",
        "entry_supervision_entries": "_SupervisionEntriesMixin",
        "entry_tutor_answer_entries": "_TutorAnswerEntriesMixin",
        "entry_tutor_explain_entries": "_TutorExplainEntriesMixin",
        "entry_tutor_question_entries": "_TutorQuestionEntriesMixin",
        "entry_tutor_summary_entries": "_TutorSummaryEntriesMixin",
    }
    for module_name, class_name in mixins.items():
        _install_module(
            monkeypatch,
            f"{package}.{module_name}",
            **{class_name: _blank_class(class_name)},
        )

    _install_module(
        monkeypatch,
        f"{package}.entry_neko_commands",
        _INTERRUPT_COMMANDS=set(),
        _NEKO_COMMAND_HANDLERS={"quiz": "handle_quiz"},
        _QUEUE_COMMANDS={"quiz"},
        _NekoCommandsMixin=_blank_class("_NekoCommandsMixin"),
    )

    async def _await_mastery_v2_projection_tasks(owner: Any) -> None:
        owner.projection_awaited = True

    _install_module(
        monkeypatch,
        f"{package}.entry_tutor_context_support",
        _TutorContextSupportMixin=_blank_class("_TutorContextSupportMixin"),
        _await_mastery_v2_projection_tasks=_await_mastery_v2_projection_tasks,
        _schedule_mastery_v2_projection=lambda _owner: None,
    )

    spec = importlib.util.spec_from_file_location(
        package,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package, module)
    spec.loader.exec_module(module)
    return module


async def _forever() -> None:
    await asyncio.Event().wait()


def _assert_cancelled_tasks(tasks: dict[str, asyncio.Task[None]]) -> None:
    for name, task in tasks.items():
        assert task.done(), f"{name} task was not finished"
        assert task.cancelled(), f"{name} task was not cancelled"


async def _cleanup_tasks(tasks: dict[str, asyncio.Task[Any]]) -> None:
    for task in tasks.values():
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks.values(), return_exceptions=True)


class _Resource:
    def __init__(self, calls: list[str], name: str, *, fail: bool = False):
        self.calls = calls
        self.name = name
        self.fail = fail

    async def shutdown(self) -> None:
        self.calls.append(f"{self.name}.shutdown")
        if self.fail:
            raise RuntimeError(f"{self.name} failed")

    async def stop_worker(self) -> None:
        self.calls.append(f"{self.name}.stop_worker")
        if self.fail:
            raise RuntimeError(f"{self.name} failed")

    def close(self) -> None:
        self.calls.append(f"{self.name}.close")
        if self.fail:
            raise RuntimeError(f"{self.name} failed")


class _Store:
    def __init__(self, calls: list[str]):
        self.calls = calls
        self.saved_state: Any = None

    def save_state(self, state: Any) -> None:
        self.calls.append("store.save_state")
        self.saved_state = state

    def close(self) -> None:
        self.calls.append("store.close")


def _owner(module: ModuleType, calls: list[str]):
    owner = object.__new__(module.StudyCompanionPlugin)
    owner.logger = _Logger()
    owner._lock = asyncio.Lock()
    owner._state = SimpleNamespace(
        status="ready",
        last_error="",
        clear_ocr_session=lambda: calls.append("state.clear_ocr_session"),
    )
    owner._store = _Store(calls)
    owner._document_jobs = _Resource(calls, "document_jobs")
    owner._event_bus = _Resource(calls, "event_bus")
    owner._agent = _Resource(calls, "agent")
    owner._local_model_manager = _Resource(calls, "local_model_manager")
    owner._ocr_pipeline = _Resource(calls, "ocr")
    owner._knowledge_tracker = object()
    owner._memory_deck_store = object()
    owner._habit_store = object()
    owner._checkin_manager = object()
    owner._pomodoro_timer = object()
    owner._supervision = object()
    owner._memory_habit_bridge = object()
    owner._buffer = object()
    owner._last_awareness_push_at = 5.0
    owner._awareness_idle_ticks = 2
    owner._consecutive_os_read_failures = 1
    owner._awareness_task = None
    owner._review_due_task = None
    owner._review_due_payload_future = None
    owner._command_queue = asyncio.Queue()
    owner._command_worker_task = None
    owner._interruptible_task = None
    owner._static_ui_config = {"path": "static"}
    owner._knowledge_dungeon_bridge = None
    owner.projection_awaited = False

    async def unsubscribe() -> None:
        calls.append("commands.unsubscribe")

    async def cancel_pomodoro() -> None:
        calls.append("pomodoro.cancel")

    async def shutdown_local_model() -> None:
        calls.append("local_model.shutdown")

    owner._unsubscribe_neko_commands = unsubscribe
    owner._cancel_pomodoro_watcher = cancel_pomodoro
    owner._shutdown_local_model_manager = shutdown_local_model
    owner.clear_list_actions = lambda: calls.append("actions.clear")
    owner.unregister_dynamic_entry = lambda entry_id: calls.append(f"entry.unregister:{entry_id}")
    return owner


@pytest.mark.asyncio
async def test_knowledge_dungeon_bridge_switch_false_creates_no_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_runtime(monkeypatch)
    owner = _owner(module, [])
    constructed = 0

    class _Bridge:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            nonlocal constructed
            constructed += 1

    monkeypatch.setattr(module, "KnowledgeDungeonPrivateBridge", _Bridge)
    owner.data_path = lambda filename: tmp_path / filename
    await owner._start_knowledge_dungeon_bridge(enabled=False)

    assert constructed == 0
    assert owner._knowledge_dungeon_bridge is None
    assert not (tmp_path / "runtime" / "bridge-v1.json").exists()
    assert not any(
        thread.name == "study-companion-knowledge-dungeon-bridge" and thread.is_alive()
        for thread in threading.enumerate()
    )


@pytest.mark.asyncio
async def test_optional_knowledge_dungeon_bridge_failure_does_not_fail_plugin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_runtime(monkeypatch)
    calls: list[str] = []
    owner = _owner(module, calls)

    class _Bridge:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            calls.append("bridge.start")
            raise RuntimeError("bind failed")

        def stop(self) -> bool:
            calls.append("bridge.stop")
            return True

    monkeypatch.setattr(module, "KnowledgeDungeonPrivateBridge", _Bridge)
    owner.data_path = lambda filename: tmp_path / filename
    await owner._start_knowledge_dungeon_bridge(enabled=True)

    assert calls == ["bridge.start", "bridge.stop"]
    assert owner._knowledge_dungeon_bridge is None
    assert any(level == "warning" for level, _args in owner.logger.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_thread_name",
    [
        "study-companion-knowledge-dungeon-bridge",
        "study-companion-knowledge-dungeon-rendezvous-renewal",
    ],
)
async def test_real_bridge_thread_start_failure_remains_optional_during_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_thread_name: str,
) -> None:
    module = _load_runtime(monkeypatch)
    owner = _owner(module, [])
    runtime_dir = tmp_path / "runtime"
    owner.data_path = lambda filename: tmp_path / filename
    monkeypatch.setattr(
        module,
        "KnowledgeDungeonPrivateBridge",
        real_bridge_module.KnowledgeDungeonPrivateBridge,
    )
    monkeypatch.setenv(
        "NEKO_KNOWLEDGE_DUNGEON_RUNTIME_DIR",
        str(runtime_dir),
    )
    original_start = threading.Thread.start
    failure_injected = False

    def fail_one_start(thread: threading.Thread) -> None:
        nonlocal failure_injected
        if thread.name == failed_thread_name and not failure_injected:
            failure_injected = True
            raise RuntimeError(f"injected {failed_thread_name} start failure")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_one_start)

    await owner._start_knowledge_dungeon_bridge(enabled=True)

    assert failure_injected is True
    assert owner._knowledge_dungeon_bridge is None
    assert not (runtime_dir / "bridge-v1.json").exists()
    assert any(level == "warning" for level, _args in owner.logger.messages)


@pytest.mark.asyncio
async def test_bridge_start_cancellation_waits_for_start_then_stops_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_runtime(monkeypatch)
    calls: list[str] = []
    owner = _owner(module, calls)
    start_entered = threading.Event()
    release_start = threading.Event()

    class _Bridge:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            calls.append("bridge.start")
            start_entered.set()
            assert release_start.wait(2)
            calls.append("bridge.started")

        def stop(self) -> bool:
            calls.append("bridge.stop")
            return True

    monkeypatch.setattr(module, "KnowledgeDungeonPrivateBridge", _Bridge)
    owner.data_path = lambda filename: tmp_path / filename
    task = asyncio.create_task(owner._start_knowledge_dungeon_bridge(enabled=True))
    assert await asyncio.to_thread(start_entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release_start.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert calls == ["bridge.start", "bridge.started", "bridge.stop"]
    assert owner._knowledge_dungeon_bridge is None


@pytest.mark.asyncio
async def test_startup_failure_cleans_partial_runtime_and_reports_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runtime(monkeypatch)
    calls: list[str] = []
    owner = _owner(module, calls)

    class _Config:
        async def dump(self, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("config unavailable")

    owner.config = _Config()
    tasks = {
        "awareness": asyncio.create_task(_forever()),
        "command_worker": asyncio.create_task(_forever()),
        "interruptible": asyncio.create_task(_forever()),
        "review": asyncio.create_task(_forever()),
    }
    owner._awareness_task = tasks["awareness"]
    owner._command_worker_task = tasks["command_worker"]
    owner._interruptible_task = tasks["interruptible"]
    owner._review_due_task = tasks["review"]
    owner._command_queue.put_nowait(("quiz", {"id": 1}))

    try:
        result = await owner.startup()

        assert isinstance(result, _Err)
        assert str(result.value) == "failed to start study_companion"
        assert owner._state.status == "error"
        assert owner._state.last_error == "startup_failed"
        assert owner._event_bus is None
        assert owner._agent is None
        assert owner._local_model_manager is None
        assert owner._ocr_pipeline is None
        assert owner._static_ui_config is None
        assert owner._command_queue.empty()
        assert owner._awareness_task is None
        assert owner._command_worker_task is None
        assert owner._interruptible_task is None
        assert owner._review_due_task is None
        _assert_cancelled_tasks(tasks)
        assert owner.projection_awaited is True
        assert {
            "document_jobs.shutdown",
            "commands.unsubscribe",
            "event_bus.stop_worker",
            "actions.clear",
            "entry.unregister:study_export_notes",
            "agent.shutdown",
            "local_model_manager.shutdown",
            "ocr.close",
            "store.close",
        } <= set(calls)
    finally:
        await _cleanup_tasks(tasks)


@pytest.mark.asyncio
async def test_cancelled_startup_finishes_bridge_cleanup_despite_repeat_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runtime(monkeypatch)
    calls: list[str] = []
    owner = _owner(module, calls)
    stop_entered = threading.Event()
    release_stop = threading.Event()

    class _Bridge:
        def stop(self) -> bool:
            calls.append("bridge.stop")
            stop_entered.set()
            assert release_stop.wait(2)
            calls.append("bridge.stopped")
            return True

    owner._knowledge_dungeon_bridge = _Bridge()

    async def cleanup() -> None:
        await owner._stop_knowledge_dungeon_bridge()
        calls.append("cleanup.done")

    owner._cleanup_after_failed_startup = cleanup
    cleanup_task = asyncio.create_task(owner._finish_cancelled_startup_cleanup())
    assert await asyncio.to_thread(stop_entered.wait, 1)
    cleanup_task.cancel()
    await asyncio.sleep(0)
    cleanup_task.cancel()
    await asyncio.sleep(0)
    release_stop.set()
    await asyncio.wait_for(cleanup_task, timeout=2)

    assert calls == ["bridge.stop", "bridge.stopped", "cleanup.done"]
    assert owner._knowledge_dungeon_bridge is None


@pytest.mark.asyncio
async def test_shutdown_releases_resources_despite_best_effort_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runtime(monkeypatch)
    calls: list[str] = []
    owner = _owner(module, calls)
    owner._event_bus = _Resource(calls, "event_bus", fail=True)
    owner._ocr_pipeline = _Resource(calls, "ocr", fail=True)

    def fail_unregister(_entry_id: str) -> None:
        calls.append("entry.unregister.failed")
        raise RuntimeError("registry unavailable")

    owner.unregister_dynamic_entry = fail_unregister
    tasks = {
        "awareness": asyncio.create_task(_forever()),
        "command_worker": asyncio.create_task(_forever()),
        "interruptible": asyncio.create_task(_forever()),
        "review": asyncio.create_task(_forever()),
    }
    owner._awareness_task = tasks["awareness"]
    owner._command_worker_task = tasks["command_worker"]
    owner._interruptible_task = tasks["interruptible"]
    owner._review_due_task = tasks["review"]
    owner._command_queue.put_nowait(("quiz", {"id": 1}))

    try:
        result = await owner.shutdown()

        assert isinstance(result, _Ok)
        assert result.value == {"status": "stopped"}
        assert owner._state.status == "stopped"
        assert owner._event_bus is None
        assert owner._ocr_pipeline is None
        assert owner._store.saved_state is owner._state
        assert owner._command_queue.empty()
        _assert_cancelled_tasks(tasks)
        assert owner.projection_awaited is True
        assert calls[-3:] == ["state.clear_ocr_session", "store.save_state", "store.close"]
        assert "agent.shutdown" in calls
        assert "local_model.shutdown" in calls
    finally:
        await _cleanup_tasks(tasks)


@pytest.mark.asyncio
async def test_shutdown_propagates_cancellation_without_claiming_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runtime(monkeypatch)
    calls: list[str] = []
    owner = _owner(module, calls)

    class _CancelledJobs:
        async def shutdown(self) -> None:
            calls.append("document_jobs.cancelled")
            raise asyncio.CancelledError

    owner._document_jobs = _CancelledJobs()

    with pytest.raises(asyncio.CancelledError):
        await owner.shutdown()

    assert owner._state.status == "ready"
    assert calls == ["document_jobs.cancelled"]


@pytest.mark.asyncio
async def test_command_worker_survives_handler_failure_then_cancels_and_drains_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runtime(monkeypatch)
    calls: list[str] = []
    owner = _owner(module, calls)
    handled: list[str] = []
    third_seen = asyncio.Event()

    async def execute(command: str, _payload: dict[str, Any]) -> None:
        handled.append(command)
        if command == "broken":
            raise RuntimeError("handler failed")
        if command == "third":
            third_seen.set()

    owner._execute_command = execute
    worker = asyncio.create_task(owner._run_command_worker())
    owner._command_worker_task = worker
    for command in ("first", "broken", "third"):
        owner._command_queue.put_nowait((command, {}))

    await asyncio.wait_for(third_seen.wait(), timeout=1.0)
    owner._command_queue.put_nowait(("never", {}))
    await owner._cancel_command_worker()

    assert handled == ["first", "broken", "third"]
    assert worker.cancelled()
    assert owner._command_worker_task is None
    assert owner._command_queue.empty()
    assert any(level == "exception" for level, _args in owner.logger.messages)


@pytest.mark.asyncio
async def test_awareness_and_review_loops_are_periodic_and_cancellation_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runtime(monkeypatch)
    calls: list[str] = []
    owner = _owner(module, calls)
    owner._cfg = SimpleNamespace(
        awareness=SimpleNamespace(
            context_window_minutes=2,
            snapshot_interval_seconds=1,
            push_to_llm_interval_seconds=30,
        )
    )
    owner._ocr_pipeline = object()
    awareness_seen = asyncio.Event()
    build_started = [threading.Event(), threading.Event()]
    build_release = [threading.Event(), threading.Event()]
    build_count = 0

    class _EventBus:
        def __init__(self) -> None:
            self.events: list[Any] = []

        def schedule_emit(self, event: Any) -> None:
            self.events.append(event)

    event_bus = _EventBus()
    owner._event_bus = event_bus

    async def awareness_tick() -> None:
        awareness_seen.set()

    owner.awareness_tick = awareness_tick

    def build_review_due_payload() -> dict[str, int]:
        nonlocal build_count
        index = build_count
        build_count += 1
        build_started[index].set()
        assert build_release[index].wait(timeout=1.0)
        return {"due_count": index + 1}

    owner._build_review_due_payload = build_review_due_payload
    owner._resolve_study_target_lanlan = lambda: None
    monkeypatch.setattr(module, "_REVIEW_DUE_INTERVAL_SECONDS", 0.0)

    owner.start_awareness_loop()
    owner._start_review_due_task()
    awareness_task = owner._awareness_task
    review_task = owner._review_due_task
    assert awareness_task is not None
    assert review_task is not None
    tasks = {"awareness": awareness_task, "review": review_task}
    try:
        await asyncio.wait_for(awareness_seen.wait(), timeout=1.0)
        assert await asyncio.to_thread(build_started[0].wait, 1.0)
        first_future = owner._review_due_payload_future
        assert first_future is not None
        assert not first_future.done()

        build_release[0].set()
        assert await asyncio.to_thread(build_started[1].wait, 1.0)
        second_future = owner._review_due_payload_future
        assert second_future is not None
        assert second_future is not first_future
        assert build_count == 2
        assert len(event_bus.events) == 1

        owner.stop_awareness_loop()
        await owner._await_awareness_stop()
        cancel_review = asyncio.create_task(owner._cancel_review_due_task())
        tasks["review_cancel"] = cancel_review
        await asyncio.sleep(0)
        assert not cancel_review.done()
        assert owner._review_due_payload_future is second_future

        build_release[1].set()
        await cancel_review

        assert awareness_task.cancelled()
        assert review_task.cancelled()
        assert second_future.done()
        assert owner._review_due_payload_future is None
        assert owner._awareness_task is None
        assert owner._review_due_task is None
        assert owner._buffer is None
        assert owner._last_awareness_push_at == 0.0
        assert owner._awareness_idle_ticks == 0
        assert owner._consecutive_os_read_failures == 0
    finally:
        build_release[0].set()
        build_release[1].set()
        await _cleanup_tasks(tasks)
