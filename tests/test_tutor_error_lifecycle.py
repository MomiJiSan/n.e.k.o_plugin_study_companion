from __future__ import annotations

import asyncio
import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _Reply:
    operation: str = "concept_explain"
    input_text: str = "Explain this"
    reply: str = "Explanation"
    payload: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False
    diagnostic: str = ""
    created_at: str = "2026-09-02T00:00:00+00:00"


class _Store:
    def append_interaction(self, **_kwargs: Any) -> bool:
        return True


def _load_context_support(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    package_name = f"_tutor_error_lifecycle_{id(monkeypatch)}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    entry_common = ModuleType(f"{package_name}.entry_common")
    entry_common.LLM_OPERATION_ANSWER_EVALUATE = "answer_evaluate"
    entry_common.LLM_OPERATION_CONCEPT_EXPLAIN = "concept_explain"
    entry_common.LLM_OPERATION_KNOWLEDGE_TRACK = "knowledge_track"
    entry_common.LLM_OPERATION_QUESTION_GENERATE = "question_generate"
    entry_common.LLM_OPERATION_SUMMARIZE_SESSION = "summarize_session"
    entry_common.Any = Any
    entry_common.SdkError = RuntimeError
    entry_common.StudyEvent = SimpleNamespace
    entry_common.TutorReply = _Reply
    entry_common._detect_mastery_threshold_crossed = lambda *_args: None
    entry_common._plugin_lock = lambda lock: lock
    entry_common.asyncio = asyncio
    entry_common.build_tutor_payload = lambda reply: {
        "operation": reply.operation,
        "degraded": reply.degraded,
        "diagnostic": reply.diagnostic,
    }
    entry_common.time = __import__("time")
    entry_common.utc_now_iso = lambda: "2026-09-02T00:00:00+00:00"
    monkeypatch.setitem(sys.modules, entry_common.__name__, entry_common)

    models = ModuleType(f"{package_name}.models")
    models.public_current_question_payload = lambda value: dict(value or {})
    monkeypatch.setitem(sys.modules, models.__name__, models)

    return importlib.import_module(f"{package_name}.entry_tutor_context_support")


def _subject(context_support: ModuleType, *, last_error: str = "") -> Any:
    class Subject(context_support._TutorContextSupportMixin):
        def __init__(self) -> None:
            self._lock = asyncio.Lock()
            self._state = SimpleNamespace(last_error=last_error)
            self._store = _Store()
            self._cfg = SimpleNamespace(history_limit=10)
            self.persisted_errors: list[str] = []

        async def _record_tutor_result(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def _track_learning(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

        async def _persist_state(self) -> None:
            self.persisted_errors.append(self._state.last_error)

    return Subject()


async def _finalize(subject: Any, reply: _Reply) -> dict[str, Any]:
    return await subject._finalize_tutor_call(
        reply.operation,
        reply,
        history_kind="concept_explain",
        metadata={},
    )


def test_degraded_timeout_records_last_error(monkeypatch: pytest.MonkeyPatch) -> None:
    context_support = _load_context_support(monkeypatch)
    subject = _subject(context_support)

    asyncio.run(_finalize(subject, _Reply(degraded=True, diagnostic="timeout")))

    assert subject._state.last_error == "timeout"


def test_success_after_degraded_clears_last_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_support = _load_context_support(monkeypatch)
    subject = _subject(context_support)

    async def scenario() -> None:
        await _finalize(subject, _Reply(degraded=True, diagnostic="timeout"))
        assert subject._state.last_error == "timeout"
        await _finalize(subject, _Reply())

    asyncio.run(scenario())

    assert subject._state.last_error == ""
    assert subject.persisted_errors == [""]


def test_degraded_after_success_records_latest_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_support = _load_context_support(monkeypatch)
    subject = _subject(context_support, last_error="old_error")

    async def scenario() -> None:
        await _finalize(subject, _Reply())
        assert subject._state.last_error == ""
        await _finalize(
            subject,
            _Reply(degraded=True, diagnostic="model_unavailable"),
        )

    asyncio.run(scenario())

    assert subject._state.last_error == "model_unavailable"
    assert subject.persisted_errors == [""]


def test_persist_failure_restores_previous_last_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_support = _load_context_support(monkeypatch)
    subject = _subject(context_support, last_error="timeout")

    async def fail_persist() -> None:
        raise OSError("state persistence failed")

    subject._persist_state = fail_persist

    with pytest.raises(OSError, match="state persistence failed"):
        asyncio.run(_finalize(subject, _Reply()))

    assert subject._state.last_error == "timeout"


def test_success_and_degraded_writes_follow_finalize_completion_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_support = _load_context_support(monkeypatch)
    subject = _subject(context_support, last_error="timeout")

    async def scenario() -> None:
        persist_started = asyncio.Event()
        release_persist = asyncio.Event()

        async def blocked_persist() -> None:
            persist_started.set()
            await release_persist.wait()

        subject._persist_state = blocked_persist
        success = asyncio.create_task(_finalize(subject, _Reply()))
        await persist_started.wait()
        degraded = asyncio.create_task(
            _finalize(
                subject,
                _Reply(degraded=True, diagnostic="model_unavailable"),
            )
        )
        await asyncio.sleep(0)
        assert not degraded.done()

        release_persist.set()
        await success
        await degraded

    asyncio.run(scenario())

    assert subject._state.last_error == "model_unavailable"
