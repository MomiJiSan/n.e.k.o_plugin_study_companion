from __future__ import annotations

import asyncio
import importlib
import sys
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
