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


class _Reply:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class _Logger:
    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _load_context_support(monkeypatch: pytest.MonkeyPatch):
    package_name = "_learning_commit_production_adapter"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    common = ModuleType(f"{package_name}.entry_common")
    for name, value in {
        "LLM_OPERATION_KNOWLEDGE_TRACK": "knowledge_track",
        "LLM_OPERATION_SUMMARIZE_SESSION": "summary",
        "LLM_OPERATION_CONCEPT_EXPLAIN": "concept_explain",
        "LLM_OPERATION_ANSWER_EVALUATE": "answer_evaluate",
        "LLM_OPERATION_QUESTION_GENERATE": "question_generate",
        "Any": Any,
        "asyncio": asyncio,
        "SdkError": RuntimeError,
        "StudyEvent": object,
        "TutorReply": _Reply,
        "_detect_mastery_threshold_crossed": lambda *_args: None,
        "_plugin_lock": None,
        "build_tutor_payload": lambda *_args, **_kwargs: {},
        "time": __import__("time"),
        "utc_now_iso": lambda: "now",
    }.items():
        setattr(common, name, value)
    monkeypatch.setitem(sys.modules, common.__name__, common)

    models = ModuleType(f"{package_name}.models")
    models.public_current_question_payload = lambda *_args, **_kwargs: {}
    monkeypatch.setitem(sys.modules, models.__name__, models)

    target_binding = ModuleType(f"{package_name}.target_binding")

    async def resolve_existing_target_topic_id(*_args: Any, **_kwargs: Any) -> str:
        return "topic-a"

    target_binding.resolve_existing_target_topic_id = resolve_existing_target_topic_id
    monkeypatch.setitem(sys.modules, target_binding.__name__, target_binding)
    return importlib.import_module(f"{package_name}.entry_tutor_context_support")


def test_record_answer_knowledge_delegates_to_existing_tracker_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _load_context_support(monkeypatch)
    received: list[dict[str, Any]] = []

    class Tracker:
        store = SimpleNamespace(get_topic=lambda _topic_id: {"id": "topic-a"})

        def get_mastery(self, _topic_id: str) -> float:
            return 0.5

        def on_answer(self, **kwargs: Any) -> dict[str, Any]:
            received.append(kwargs)
            return {"topic_id": "topic-a", "knowledge_tracking_status": "updated"}

    class Harness(support._TutorContextSupportMixin):
        _knowledge_tracker = Tracker()
        _state = SimpleNamespace(active_mode="companion", run_id="state-run")
        ctx = SimpleNamespace(run_id="ctx-run")
        _event_bus = None
        logger = _Logger()

        def _invalidate_knowledge_guidance_cache(self) -> None:
            return None

    question = {
        "question": "What is 2 + 2?",
        "answer": "4",
        "question_id": "question-1",
        "difficulty": 2,
        "target_binding": {
            "target_topic_id": "topic-a",
            "validation_status": "passed",
            "origin_wrong_question_id": "wrong-1",
        },
    }
    result = asyncio.run(
        Harness()._record_answer_knowledge(
            _Reply(
                input_text="3",
                payload={
                    "verdict": "wrong",
                    "score": 10,
                    "error_type": "calculation",
                    "final_answer_correct": False,
                },
                created_at="now",
            ),
            _Reply(payload={"topic": "topic-a"}),
            extra_context={
                "current_question": question,
                "question_payload": question,
                "question": question["question"],
                "expected_answer": question["answer"],
                "answer": "3",
                "session_id": "session-1",
                "attempt_id": "attempt-1",
                "mode": "companion",
                "response_time_ms": 1_234,
                "used_hint": True,
            },
        )
    )

    assert received == [
        {
            "topic_id": "topic-a",
                "question": {**question, "topic": "topic-a"},
            "user_answer": "3",
            "eval_result": {
                "verdict": "wrong",
                "score": 10,
                "error_type": "calculation",
                "final_answer_correct": False,
                "topic": "topic-a",
                "track": {"topic": "topic-a"},
            },
            "mode": "companion",
            "session_id": "session-1",
            "response_time_ms": 1_234,
            "used_hint": True,
            "allow_knowledge_update": True,
            "require_existing_topic": True,
            "origin_wrong_question_id": "wrong-1",
            "attempt_id": "attempt-1",
        }
    ]
    assert result == {
        "selected_topic_id": "topic-a",
        "mastery_before": 0.5,
        "mastery_after": 0.5,
        "mastery_delta": 0.0,
    }


def test_record_answer_rebuilds_private_cognitive_question_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _load_context_support(monkeypatch)
    attempts: list[Any] = []

    class CommitService:
        def __init__(self, _port: Any) -> None:
            return None

        async def commit(self, attempt: Any) -> Any:
            attempts.append(attempt)
            return SimpleNamespace(
                as_payload=lambda: {
                    "topic_id": "topic-a",
                    "knowledge_tracking_status": "updated",
                }
            )

    monkeypatch.setattr(support, "LearningCommitService", CommitService)

    class Tracker:
        store = SimpleNamespace(get_topic=lambda _topic_id: {"id": "topic-a"})

        def get_mastery(self, _topic_id: str) -> float:
            return 0.5

    class Harness(support._TutorContextSupportMixin):
        _knowledge_tracker = Tracker()
        _state = SimpleNamespace(active_mode="companion", run_id="state-run")
        ctx = SimpleNamespace(run_id="ctx-run")
        _event_bus = None
        logger = _Logger()

        def _invalidate_knowledge_guidance_cache(self) -> None:
            return None

    hypothesis = {
        "hypothesis_id": "hypothesis-1",
        "topic_id": "topic-a",
        "code": "omit_inner_derivative",
        "status": "supported",
        "probability": 0.91,
        "model_version": "cognitive-v2",
        "source_snapshot_id": "snapshot-7",
        "source_attempt_id": "attempt-source",
        "projection_generation": 7,
    }
    question = {
        "question": "Differentiate sin(x^2).",
        "answer": "2*x*cos(x^2)",
        "question_id": "question-1",
        "difficulty": 2,
        "scope_key": "scope-a",
        "scope_revision": 9,
        "target_binding": {
            "target_topic_id": "topic-a",
            "validation_status": "passed",
            "plan_id": "selection-1",
            "origin_wrong_question_id": "wrong-1",
            "cognitive_learning_intent": "misconception_repair",
            "cognitive_hypothesis_target": hypothesis,
            "cognitive_repair_strategy": "complete_inner_derivative",
            "cognitive_decision_id": "decision-1",
            "cognitive_validator_version": "validator-v2",
            "diagnostic_validation_id": "validation-1",
        },
    }

    asyncio.run(
        Harness()._record_answer_knowledge(
            _Reply(
                input_text="cos(x^2)",
                payload={"verdict": "wrong", "score": 0},
                created_at="now",
            ),
            _Reply(payload={"topic": "topic-a"}),
            extra_context={
                "current_question": question,
                "question_payload": question,
                "answer": "cos(x^2)",
                "attempt_id": "attempt-1",
            },
        )
    )

    assert len(attempts) == 1
    rebuilt = attempts[0].question
    assert rebuilt.plan_id == "selection-1"
    assert rebuilt.target_topic.id == "topic-a"
    assert rebuilt.scope_key == "scope-a"
    assert rebuilt.scope_revision == 9
    assert rebuilt.learning_intent == "misconception_repair"
    assert rebuilt.hypothesis_target.code == "omit_inner_derivative"
    assert rebuilt.hypothesis_target.source_snapshot_id == "snapshot-7"
    assert rebuilt.hypothesis_target.projection_generation == 7
    assert rebuilt.repair_strategy == "complete_inner_derivative"
    assert rebuilt.cognitive_decision_id == "decision-1"
    assert rebuilt.cognitive_validator_version == "validator-v2"
    assert rebuilt.diagnostic_validation_id == "validation-1"


def test_record_answer_drops_forged_cross_topic_cognitive_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _load_context_support(monkeypatch)
    attempts: list[Any] = []

    class CommitService:
        def __init__(self, _port: Any) -> None:
            return None

        async def commit(self, attempt: Any) -> Any:
            attempts.append(attempt)
            return SimpleNamespace(
                as_payload=lambda: {
                    "topic_id": "topic-a",
                    "knowledge_tracking_status": "updated",
                }
            )

    monkeypatch.setattr(support, "LearningCommitService", CommitService)

    class Tracker:
        store = SimpleNamespace(get_topic=lambda _topic_id: {"id": "topic-a"})

        def get_mastery(self, _topic_id: str) -> float:
            return 0.5

    class Harness(support._TutorContextSupportMixin):
        _knowledge_tracker = Tracker()
        _state = SimpleNamespace(active_mode="companion", run_id="state-run")
        ctx = SimpleNamespace(run_id="ctx-run")
        _event_bus = None
        logger = _Logger()

        def _invalidate_knowledge_guidance_cache(self) -> None:
            return None

    question = {
        "question": "Question",
        "answer": "Answer",
        "target_binding": {
            "target_topic_id": "topic-a",
            "validation_status": "passed",
            "cognitive_learning_intent": "misconception_repair",
            "cognitive_hypothesis_target": {
                "hypothesis_id": "hypothesis-forged",
                "topic_id": "other-topic",
                "code": "omit_inner_derivative",
                "status": "supported",
                "probability": 0.99,
                "model_version": "cognitive-v2",
            },
            "cognitive_repair_strategy": "complete_inner_derivative",
            "cognitive_decision_id": "decision-forged",
        },
    }

    asyncio.run(
        Harness()._record_answer_knowledge(
            _Reply(input_text="Wrong", payload={"verdict": "wrong"}, created_at="now"),
            _Reply(payload={"topic": "topic-a"}),
            extra_context={
                "question_payload": question,
                "answer": "Wrong",
                "attempt_id": "attempt-forged",
            },
        )
    )

    rebuilt = attempts[0].question
    assert rebuilt.learning_intent == "practice"
    assert rebuilt.hypothesis_target is None
    assert rebuilt.repair_strategy == ""
    assert rebuilt.cognitive_decision_id == ""


def test_shadow_projection_is_scheduled_only_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _load_context_support(monkeypatch)
    projected: list[int] = []

    class Tracker:
        store = SimpleNamespace(get_topic=lambda _topic_id: {"id": "topic-a"})
        mastery_v2_shadow_enabled = True

        def get_mastery(self, _topic_id: str) -> float:
            return 0.5

        def on_answer(self, **_kwargs: Any) -> dict[str, Any]:
            return {"topic_id": "topic-a", "knowledge_tracking_status": "updated"}

        def project_mastery_v2_pending(self, *, limit: int) -> dict[str, Any]:
            projected.append(limit)
            return {"completed": 1, "failed": 0}

    class Harness(support._TutorContextSupportMixin):
        _knowledge_tracker = Tracker()
        _state = SimpleNamespace(active_mode="companion", run_id="state-run")
        ctx = SimpleNamespace(run_id="ctx-run")
        _event_bus = None
        logger = _Logger()

        def _invalidate_knowledge_guidance_cache(self) -> None:
            return None

    async def scenario() -> None:
        harness = Harness()
        await harness._record_answer_knowledge(
            _Reply(
                input_text="4",
                payload={"verdict": "correct", "score": 100},
                created_at="now",
            ),
            _Reply(payload={"topic": "topic-a"}),
            extra_context={
                "question_payload": {
                    "question_id": "question-1",
                    "question": "What is 2 + 2?",
                    "answer": "4",
                    "difficulty": 2,
                },
                "answer": "4",
                "attempt_id": "attempt-1",
            },
        )
        tasks = list(getattr(harness, "_mastery_v2_projection_tasks", ()))
        assert tasks
        await asyncio.gather(*tasks)

    asyncio.run(scenario())

    assert projected == [100]


def test_shadow_projection_reschedules_when_marked_dirty_while_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _load_context_support(monkeypatch)
    started = threading.Event()
    release = threading.Event()
    second_run = threading.Event()
    release_second = threading.Event()

    class Tracker:
        mastery_v2_shadow_enabled = True
        calls = 0

        def project_mastery_v2_pending(self, *, limit: int) -> dict[str, Any]:
            assert limit == 100
            self.calls += 1
            if self.calls == 1:
                started.set()
                assert release.wait(2)
            else:
                second_run.set()
                assert release_second.wait(2)
            return {"completed": 1, "failed": 0, "has_more": False}

    class Harness:
        _knowledge_tracker = Tracker()
        logger = _Logger()

    async def scenario() -> None:
        harness = Harness()
        support._schedule_mastery_v2_projection(harness)
        assert await asyncio.to_thread(started.wait, 1)
        support._schedule_mastery_v2_projection(harness)
        release.set()
        assert await asyncio.to_thread(second_run.wait, 2)
        waiter = asyncio.create_task(
            support._await_mastery_v2_projection_tasks(harness)
        )
        await asyncio.sleep(0)
        assert not waiter.done()
        release_second.set()
        await waiter
        assert harness._knowledge_tracker.calls == 2

    asyncio.run(scenario())
