from __future__ import annotations

import asyncio
import importlib
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


class _NeverCalledExtractor:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, _extraction_input: Any) -> Any:
        self.calls += 1
        raise AssertionError("extractor entered the synchronous answer path")


class _FailingExtractor:
    def __init__(self, contracts: Any) -> None:
        self._contracts = contracts
        self.calls = 0

    async def extract(self, _extraction_input: Any) -> Any:
        self.calls += 1
        return self._contracts.CognitiveExtractionOutcome(
            status="failed",
            failure_reason="injected extractor failure",
            extractor_version=self._contracts.DEFAULT_COGNITIVE_EXTRACTOR_VERSION,
            model_version=self._contracts.DEFAULT_COGNITIVE_MODEL_VERSION,
        )


def _install_package(monkeypatch: pytest.MonkeyPatch, name: str) -> str:
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    mode_manager = ModuleType(f"{name}.mode_manager")
    setattr(mode_manager, "normalize_mode", lambda value: str(value or "companion"))
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return name


def _load_runtime(monkeypatch: pytest.MonkeyPatch, name: str):
    package = _install_package(monkeypatch, name)
    store_module = importlib.import_module(f"{package}.store")
    tracker_module = importlib.import_module(f"{package}.knowledge_tracker")
    contracts = importlib.import_module(f"{package}.adaptive_learning.cognitive_contracts")
    planner = importlib.import_module(f"{package}.adaptive_learning.planner")
    return store_module.StudyStore, tracker_module.KnowledgeTracker, contracts, planner


def _load_context_support(monkeypatch: pytest.MonkeyPatch, name: str):
    package = _install_package(monkeypatch, name)
    common = ModuleType(f"{package}.entry_common")
    for attr, value in {
        "LLM_OPERATION_KNOWLEDGE_TRACK": "knowledge_track",
        "LLM_OPERATION_SUMMARIZE_SESSION": "summary",
        "LLM_OPERATION_CONCEPT_EXPLAIN": "concept_explain",
        "LLM_OPERATION_ANSWER_EVALUATE": "answer_evaluate",
        "LLM_OPERATION_QUESTION_GENERATE": "question_generate",
        "Any": Any,
        "asyncio": asyncio,
        "SdkError": RuntimeError,
        "StudyEvent": object,
        "TutorReply": object,
        "_detect_mastery_threshold_crossed": lambda *_args: None,
        "_plugin_lock": None,
        "build_tutor_payload": lambda *_args, **_kwargs: {},
        "time": __import__("time"),
        "utc_now_iso": lambda: "now",
    }.items():
        setattr(common, attr, value)
    monkeypatch.setitem(sys.modules, common.__name__, common)
    models = ModuleType(f"{package}.models")
    setattr(models, "public_current_question_payload", lambda *_args, **_kwargs: {})
    monkeypatch.setitem(sys.modules, models.__name__, models)
    binding = ModuleType(f"{package}.target_binding")

    async def resolve_existing_target_topic_id(*_args: Any, **_kwargs: Any) -> str:
        return "calculus.chain_rule"

    setattr(binding, "resolve_existing_target_topic_id", resolve_existing_target_topic_id)
    monkeypatch.setitem(sys.modules, binding.__name__, binding)
    return importlib.import_module(f"{package}.entry_tutor_context_support")


def _store(tmp_path: Path, Store, suffix: str = ""):
    store = Store(
        tmp_path / f"study{suffix}.db",
        tmp_path / f"seed{suffix}.json",
        _Logger(),
    )
    store.open()
    for topic_id, name in (
        ("calculus.chain_rule", "Chain rule"),
        ("college_chain_rule", "College chain rule"),
        ("algebra.linear_equation", "Linear equation"),
    ):
        store.ensure_topic(topic_id=topic_id, name=name)
    return store


def _cognitive_config(
    *,
    projection_enabled: bool = True,
    read_mode: str = "off",
    intent_policy: str = "off",
) -> SimpleNamespace:
    return SimpleNamespace(
        projection_enabled=projection_enabled,
        read_mode=read_mode,
        intent_policy=intent_policy,
        ui_enabled=False,
        model_version="cognitive-v1",
        supported_topics=("calculus.chain_rule",),
    )


def test_active_intent_requires_active_generation_consistent_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, Tracker, _, _ = _load_runtime(monkeypatch, "_cognitive_mode_fence")
    store = _store(tmp_path, Store)
    try:
        shadow = Tracker(
            store,
            logger=_Logger(),
            cognitive_config=_cognitive_config(read_mode="shadow", intent_policy="on"),
            cognitive_extractor=_NeverCalledExtractor(),
        )
        active = Tracker(
            store,
            logger=_Logger(),
            cognitive_config=_cognitive_config(read_mode="active", intent_policy="on"),
            cognitive_extractor=_NeverCalledExtractor(),
        )

        assert shadow.cognitive_intent_policy_mode == "shadow"
        assert active.cognitive_intent_policy_mode == "on"
        assert shadow.read_cognitive_state("calculus.chain_rule").usable is False
    finally:
        store.close()


def _answer(
    tracker: Any,
    *,
    topic_id: str,
    attempt_id: str,
    require_existing_topic: bool,
) -> dict[str, Any]:
    return tracker.on_answer(
        topic_id=topic_id,
        question={
            "question_id": f"question-{attempt_id}",
            "question": "Differentiate sin(x^2).",
            "answer": "2x cos(x^2)",
            "difficulty": 3,
        },
        user_answer="2x cos(x^2)",
        eval_result={"verdict": "correct", "score": 100},
        mode="companion",
        session_id=f"session-{attempt_id}",
        attempt_id=attempt_id,
        require_existing_topic=require_existing_topic,
    )


def test_all_cognitive_switches_off_preserve_existing_output_and_write_no_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, Tracker, _, _ = _load_runtime(monkeypatch, "_cognitive_shadow_off")
    store = _store(tmp_path, Store)
    try:
        baseline = Tracker(store, logger=_Logger())
        disabled = Tracker(
            store,
            logger=_Logger(),
            cognitive_config=SimpleNamespace(
                projection_enabled=False,
                read_mode="off",
                intent_policy="off",
                ui_enabled=False,
                model_version="cognitive-v1",
                supported_topics=("calculus.chain_rule",),
            ),
        )
        baseline_params = baseline.preview_next_question_params(
            "calculus.chain_rule",
            candidate_topic_ids=("calculus.chain_rule",),
        )
        disabled_params = disabled.preview_next_question_params(
            "calculus.chain_rule",
            candidate_topic_ids=("calculus.chain_rule",),
        )
        assert disabled.cognitive_projection_enabled is False
        assert disabled_params == baseline_params

        result = _answer(
            disabled,
            topic_id="calculus.chain_rule",
            attempt_id="attempt-disabled",
            require_existing_topic=True,
        )
        assert result["topic_id"] == "calculus.chain_rule"
        assert store.list_cognitive_projection_queue() == []
    finally:
        store.close()


def test_projection_enabled_does_not_enqueue_unvalidated_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, Tracker, _, _ = _load_runtime(monkeypatch, "_cognitive_shadow_unvalidated")
    store = _store(tmp_path, Store)
    extractor = _NeverCalledExtractor()
    try:
        tracker = Tracker(
            store,
            logger=_Logger(),
            cognitive_config=_cognitive_config(),
            cognitive_extractor=extractor,
        )
        result = _answer(
            tracker,
            topic_id="calculus.chain_rule",
            attempt_id="attempt-unvalidated",
            require_existing_topic=False,
        )
        assert result["topic_id"] == "calculus.chain_rule"
        assert store.list_cognitive_projection_queue() == []
        assert extractor.calls == 0
    finally:
        store.close()


def test_only_catalog_topic_and_alias_enqueue_without_waiting_for_extractor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, Tracker, _, _ = _load_runtime(monkeypatch, "_cognitive_shadow_scope")
    store = _store(tmp_path, Store)
    extractor = _NeverCalledExtractor()
    try:
        tracker = Tracker(
            store,
            logger=_Logger(),
            cognitive_config=_cognitive_config(),
            cognitive_extractor=extractor,
        )
        for topic_id, attempt_id in (
            ("calculus.chain_rule", "attempt-canonical"),
            ("college_chain_rule", "attempt-alias"),
            ("algebra.linear_equation", "attempt-outside"),
        ):
            result = _answer(
                tracker,
                topic_id=topic_id,
                attempt_id=attempt_id,
                require_existing_topic=True,
            )
            assert result["topic_id"] == topic_id

        queued = store.list_cognitive_projection_queue()
        assert {item["attempt_id"] for item in queued} == {
            "attempt-canonical",
            "attempt-alias",
        }
        assert all(item["extractor_version"] == "cognitive-extractor-v1" for item in queued)
        assert extractor.calls == 0
        assert store.get_attempt_fact("attempt-outside") is not None
    finally:
        store.close()


def test_projector_failure_cannot_undo_completed_answer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    Store, Tracker, contracts, _ = _load_runtime(monkeypatch, "_cognitive_shadow_failure")
    store = _store(tmp_path, Store)
    extractor = _FailingExtractor(contracts)
    try:
        tracker = Tracker(
            store,
            logger=_Logger(),
            cognitive_config=_cognitive_config(),
            cognitive_extractor=extractor,
        )
        answer_result = _answer(
            tracker,
            topic_id="calculus.chain_rule",
            attempt_id="attempt-failure",
            require_existing_topic=True,
        )
        assert answer_result["topic_id"] == "calculus.chain_rule"
        assert store.get_attempt_fact("attempt-failure") is not None

        projection_result = asyncio.run(tracker.project_cognitive_pending(limit=100))
        assert projection_result["claimed"] == 1
        assert projection_result["completed"] == 0
        assert projection_result["failed"] == 1
        assert extractor.calls == 1
        assert store.get_attempt_fact("attempt-failure") is not None
        queue = store.list_cognitive_projection_queue()[0]
        assert queue["status"] == "failed"
        assert "injected extractor failure" in queue["last_error"]
    finally:
        store.close()


def test_answer_commit_returns_while_cognitive_projector_is_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _load_context_support(monkeypatch, "_cognitive_shadow_nonblocking")

    class Tracker:
        store = SimpleNamespace(get_topic=lambda _topic_id: {"id": "calculus.chain_rule"})
        mastery_v2_shadow_enabled = False
        cognitive_projection_enabled = True

        def __init__(self) -> None:
            self.projector_started = asyncio.Event()
            self.release_projector = asyncio.Event()

        def get_mastery(self, _topic_id: str) -> float:
            return 0.5

        def on_answer(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "topic_id": "calculus.chain_rule",
                "knowledge_tracking_status": "updated",
            }

        async def project_cognitive_pending(self, *, limit: int) -> dict[str, Any]:
            assert limit == 100
            self.projector_started.set()
            await self.release_projector.wait()
            return {"completed": 1, "failed": 0, "has_more": False}

    class Harness(support._TutorContextSupportMixin):
        _state = SimpleNamespace(active_mode="companion", run_id="state-run")
        ctx = SimpleNamespace(run_id="ctx-run")
        _event_bus = None
        logger = _Logger()

        def __init__(self) -> None:
            self._knowledge_tracker = Tracker()

        def _invalidate_knowledge_guidance_cache(self) -> None:
            return None

    async def scenario() -> None:
        harness = Harness()
        result = await asyncio.wait_for(
            harness._record_answer_knowledge(
                SimpleNamespace(
                    input_text="2x cos(x^2)",
                    payload={"verdict": "correct", "score": 100},
                    created_at="now",
                ),
                SimpleNamespace(payload={"topic": "calculus.chain_rule"}),
                extra_context={
                    "question_payload": {
                        "question_id": "question-nonblocking",
                        "question": "Differentiate sin(x^2).",
                        "answer": "2x cos(x^2)",
                        "difficulty": 3,
                    },
                    "answer": "2x cos(x^2)",
                    "attempt_id": "attempt-nonblocking",
                },
            ),
            1,
        )
        assert result["selected_topic_id"] == "calculus.chain_rule"
        await asyncio.wait_for(harness._knowledge_tracker.projector_started.wait(), 1)
        tasks = tuple(harness._cognitive_projection_tasks)
        assert len(tasks) == 1
        assert tasks[0].done() is False
        harness._knowledge_tracker.release_projector.set()
        await support._await_mastery_v2_projection_tasks(harness)

    asyncio.run(scenario())


def test_cognitive_scheduler_deduplicates_and_replays_dirty_wakeup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _load_context_support(monkeypatch, "_cognitive_shadow_scheduler")

    class Tracker:
        cognitive_projection_enabled = True
        calls = 0
        active = 0
        max_active = 0

        def __init__(self) -> None:
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.second_started = asyncio.Event()

        async def project_cognitive_pending(self, *, limit: int) -> dict[str, Any]:
            assert limit == 100
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if self.calls == 1:
                    self.first_started.set()
                    await self.release_first.wait()
                else:
                    self.second_started.set()
                return {"completed": 1, "failed": 0, "has_more": False}
            finally:
                self.active -= 1

    class Harness:
        logger = _Logger()

        def __init__(self) -> None:
            self._knowledge_tracker = Tracker()

    async def scenario() -> None:
        harness = Harness()
        support._schedule_cognitive_projection(harness)
        await asyncio.wait_for(harness._knowledge_tracker.first_started.wait(), 1)
        support._schedule_cognitive_projection(harness)
        assert len(getattr(harness, "_cognitive_projection_tasks")) == 1
        assert getattr(harness, "_cognitive_projection_dirty") is True
        harness._knowledge_tracker.release_first.set()
        await asyncio.wait_for(harness._knowledge_tracker.second_started.wait(), 1)
        await support._await_mastery_v2_projection_tasks(harness)
        assert harness._knowledge_tracker.calls == 2
        assert harness._knowledge_tracker.max_active == 1
        assert getattr(harness, "_cognitive_projection_tasks") == set()

    asyncio.run(scenario())


def test_cognitive_projection_request_is_gated_and_failure_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _load_context_support(monkeypatch, "_cognitive_request_projection")

    class DisabledTracker:
        cognitive_projection_enabled = False

    class DisabledHarness(support._TutorContextSupportMixin):
        logger = _Logger()
        _knowledge_tracker = DisabledTracker()

    disabled = DisabledHarness()
    assert disabled._request_cognitive_projection() is False
    assert not hasattr(disabled, "_cognitive_projection_tasks")

    class BrokenTracker:
        @property
        def cognitive_projection_enabled(self) -> bool:
            raise RuntimeError("injected scheduler gate failure")

    class BrokenHarness(support._TutorContextSupportMixin):
        logger = _Logger()
        _knowledge_tracker = BrokenTracker()

    broken = BrokenHarness()
    assert broken._request_cognitive_projection() is False
    assert not hasattr(broken, "_cognitive_projection_tasks")


def test_shutdown_wait_helper_drains_mastery_and_cognitive_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _load_context_support(monkeypatch, "_cognitive_shadow_shutdown")

    class Tracker:
        mastery_v2_shadow_enabled = True
        cognitive_projection_enabled = True

        def __init__(self) -> None:
            self.mastery_started = threading.Event()
            self.release_mastery = threading.Event()
            self.cognitive_started = asyncio.Event()
            self.release_cognitive = asyncio.Event()

        def project_mastery_v2_pending(self, *, limit: int) -> dict[str, Any]:
            assert limit == 100
            self.mastery_started.set()
            assert self.release_mastery.wait(2)
            return {"completed": 1, "failed": 0, "has_more": False}

        async def project_cognitive_pending(self, *, limit: int) -> dict[str, Any]:
            assert limit == 100
            self.cognitive_started.set()
            await self.release_cognitive.wait()
            return {"completed": 1, "failed": 0, "has_more": False}

    class Harness:
        logger = _Logger()

        def __init__(self) -> None:
            self._knowledge_tracker = Tracker()

    async def scenario() -> None:
        harness = Harness()
        support._schedule_mastery_v2_projection(harness)
        assert await asyncio.to_thread(harness._knowledge_tracker.mastery_started.wait, 1)
        await asyncio.wait_for(harness._knowledge_tracker.cognitive_started.wait(), 1)
        waiter = asyncio.create_task(support._await_mastery_v2_projection_tasks(harness))
        await asyncio.sleep(0)
        assert waiter.done() is False
        harness._knowledge_tracker.release_mastery.set()
        await asyncio.sleep(0.01)
        assert waiter.done() is False
        harness._knowledge_tracker.release_cognitive.set()
        await asyncio.wait_for(waiter, 1)
        assert getattr(harness, "_mastery_v2_projection_tasks") == set()
        assert getattr(harness, "_cognitive_projection_tasks") == set()

    asyncio.run(scenario())


def test_cognitive_config_does_not_change_existing_question_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, Tracker, _, planner = _load_runtime(monkeypatch, "_cognitive_shadow_selection")
    store = _store(tmp_path, Store)
    try:
        disabled = Tracker(store, logger=_Logger())
        enabled = Tracker(
            store,
            logger=_Logger(),
            cognitive_config=_cognitive_config(),
            cognitive_extractor=_NeverCalledExtractor(),
        )
        candidate_ids = ("calculus.chain_rule", "algebra.linear_equation")
        topics = {topic_id: store.get_topic(topic_id) for topic_id in candidate_ids}
        disabled_params = disabled.preview_next_question_params(
            "calculus.chain_rule",
            candidate_topic_ids=candidate_ids,
            candidate_topics_by_id=topics,
        )
        enabled_params = enabled.preview_next_question_params(
            "calculus.chain_rule",
            candidate_topic_ids=candidate_ids,
            candidate_topics_by_id=topics,
        )
        assert enabled_params == disabled_params
        common = {
            "plan_id": "plan-1",
            "eligible_topic_ids": candidate_ids,
            "topics_by_id": topics,
            "scope_key": "subject:math",
            "scope_revision": 1,
            "question_type": "math_exact",
        }
        assert planner.build_question_plan(enabled_params, **common) == (
            planner.build_question_plan(disabled_params, **common)
        )
    finally:
        store.close()
