from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


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
        self.warnings: list[tuple[Any, ...]] = []

    def warning(self, *args: Any, **_kwargs: Any) -> None:
        self.warnings.append(args)


class _Ui:
    @staticmethod
    def action():
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


def _install_namespace(monkeypatch: pytest.MonkeyPatch, name: str) -> ModuleType:
    module = _install_module(monkeypatch, name)
    module.__path__ = []  # type: ignore[attr-defined]
    return module


def _package(monkeypatch: pytest.MonkeyPatch, prefix: str) -> str:
    name = f"{prefix}_{id(monkeypatch)}"
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    return name


def _install_sdk_and_io_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "plugin",
        "plugin.plugins",
        "plugin.plugins._shared",
        "plugin.plugins._shared.rapidocr",
        "plugin.sdk",
        "plugin.sdk.shared",
        "plugin.sdk.shared.transport",
        "plugin.server",
        "plugin.server.routes",
    ):
        _install_namespace(monkeypatch, name)

    class _NekoPluginBase:
        pass

    _install_module(
        monkeypatch,
        "plugin.sdk.plugin",
        Err=_Err,
        NekoPluginBase=_NekoPluginBase,
        Ok=_Ok,
        SdkError=_SdkError,
        lifecycle=lambda **_kwargs: lambda function: function,
        neko_plugin=lambda cls: cls,
        plugin_entry=lambda **_kwargs: lambda function: function,
        tr=lambda _key, **kwargs: kwargs.get("default", ""),
        ui=_Ui(),
    )
    rapidocr_support = _install_module(
        monkeypatch,
        "plugin.plugins._shared.rapidocr.rapidocr_support",
    )
    sys.modules["plugin.plugins._shared.rapidocr"].rapidocr_support = rapidocr_support

    class _I18n:
        def t(self, key: str, *, default: str = "", **_kwargs: Any) -> str:
            return default or key

    _install_module(
        monkeypatch,
        "plugin.sdk.shared.i18n",
        load_plugin_i18n_from_dir=lambda *_args, **_kwargs: _I18n(),
    )
    _install_module(
        monkeypatch,
        "plugin.sdk.shared.transport.message_plane",
        MessagePlaneTransport=object,
    )
    _install_module(
        monkeypatch,
        "plugin.server.routes._install_task_store",
        update_install_task_state=lambda *_args, **_kwargs: None,
    )


def _load_entries(
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    entry_name: str,
) -> SimpleNamespace:
    _install_sdk_and_io_boundaries(monkeypatch)
    package = _package(monkeypatch, prefix)
    _install_module(
        monkeypatch,
        f"{package}.tutor_llm_agent",
        TutorLLMAgent=object,
        diagnostic_code_for_exception=lambda _exc: "agent_error",
    )
    if entry_name == "explain":
        _install_module(
            monkeypatch,
            f"{package}.tutor_llm_agent_concept_explain",
            repair_solution_structure=lambda *_args, **_kwargs: None,
        )
    if entry_name == "question":
        _install_namespace(monkeypatch, "utils")
        _install_module(
            monkeypatch,
            "utils.tokenize",
            count_tokens=lambda text: len(str(text).split()),
            truncate_to_tokens=lambda text, *_args, **_kwargs: str(text),
        )
    common = importlib.import_module(f"{package}.entry_common")
    lifecycle = importlib.import_module(f"{package}.tutor_lifecycle")
    models = importlib.import_module(f"{package}.models")
    return SimpleNamespace(
        common=common,
        lifecycle=lifecycle,
        models=models,
        **{
            entry_name: importlib.import_module(
                f"{package}.entry_tutor_{entry_name}_entries"
            )
        },
    )


async def _async_value(value: Any) -> Any:
    return value


async def _assert_next_real_reservation_succeeds(lifecycle: ModuleType, owner: Any) -> None:
    assert await lifecycle.reserve_question_lifecycle(owner, "probe") == ""
    assert await lifecycle.reserve_question_lifecycle(owner, "second") == "probe"
    await lifecycle.release_question_lifecycle(owner, "probe")


def _explain_owner(entries: SimpleNamespace, agent: Any) -> Any:
    owner = entries.explain._TutorExplainEntriesMixin()
    owner._agent = agent
    owner._lock = asyncio.Lock()
    owner._state = SimpleNamespace(active_mode="companion", last_ocr_text="")
    owner._cfg = SimpleNamespace(
        language="zh-CN",
        llm_vision_enabled=True,
        communication=SimpleNamespace(enabled=False),
    )
    owner._event_bus = None
    owner.logger = _Logger()
    owner._resolve_study_target_lanlan = lambda _kwargs: None
    owner._is_current_ocr_text = lambda _text: _async_value(False)
    owner._build_learning_context = lambda *_args, **_kwargs: _async_value(
        {
            "screen_classification": {"type": "study"},
            "study_response_mode": "unknown",
            "study_semantic_status": "",
        }
    )
    owner._finalize_tutor_call = lambda _operation, reply, **_kwargs: _async_value(
        {"reply": reply.reply, "summary": reply.reply}
    )
    return owner


@pytest.mark.asyncio
async def test_explain_entry_uses_real_error_adapter_for_success_failure_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch, "_coverage_lifecycle_explain", "explain")
    assert entries.explain._entry_exception_error is entries.common._entry_exception_error

    class _SuccessAgent:
        async def concept_explain(self, text: str, **_kwargs: Any):
            return entries.models.TutorReply(
                operation="concept_explain",
                input_text=text,
                reply="explanation",
            )

    success_owner = _explain_owner(entries, _SuccessAgent())
    succeeded = await success_owner.study_explain_text("topic")
    assert isinstance(succeeded, _Ok)
    assert succeeded.value["reply"] == "explanation"
    assert succeeded.value["solution_narration_status"] == "not_applicable"

    class _FailingAgent:
        async def concept_explain(self, *_args: Any, **_kwargs: Any):
            raise RuntimeError("explain failed")

    failure_owner = _explain_owner(entries, _FailingAgent())
    failed = await failure_owner.study_explain_text("topic")
    assert isinstance(failed, _Err)
    assert str(failed.value) == "explain failed"
    assert failure_owner.logger.warnings

    started = asyncio.Event()

    class _BlockingAgent:
        async def concept_explain(self, *_args: Any, **_kwargs: Any):
            started.set()
            await asyncio.Event().wait()

    cancel_owner = _explain_owner(entries, _BlockingAgent())
    task = asyncio.create_task(cancel_owner.study_explain_text("topic"))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _question_owner(entries: SimpleNamespace, agent: Any) -> Any:
    owner = entries.question._TutorQuestionEntriesMixin()
    owner._agent = agent
    owner._lock = asyncio.Lock()
    owner._state = SimpleNamespace(active_mode="companion", last_ocr_text="")
    owner._cfg = SimpleNamespace(language="zh-CN")
    owner.logger = _Logger()
    owner._is_current_ocr_text = lambda _text: _async_value(False)
    owner._build_learning_context = lambda *_args, **_kwargs: _async_value(
        {"screen_classification": {"type": "study"}}
    )
    owner._finalize_tutor_call = lambda _operation, reply, **_kwargs: _async_value(
        {**reply.payload, "summary": reply.reply}
    )
    return owner


@pytest.mark.asyncio
async def test_question_entry_real_lifecycle_releases_after_success_failure_and_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch, "_coverage_lifecycle_question", "question")
    assert entries.question._entry_exception_error is entries.common._entry_exception_error

    class _SuccessAgent:
        async def question_generate(self, text: str, **_kwargs: Any):
            return entries.models.TutorReply(
                operation="question_generate",
                input_text=text,
                reply="2 + 2?",
                payload={"question": "2 + 2?", "answer": "4", "hint": "add"},
            )

    success_owner = _question_owner(entries, _SuccessAgent())
    succeeded = await success_owner.study_generate_question(text="arithmetic")
    assert isinstance(succeeded, _Ok)
    assert succeeded.value["question"] == "2 + 2?"
    await _assert_next_real_reservation_succeeds(entries.lifecycle, success_owner)

    class _FailingAgent:
        async def question_generate(self, *_args: Any, **_kwargs: Any):
            raise RuntimeError("question failed")

    failure_owner = _question_owner(entries, _FailingAgent())
    failed = await failure_owner.study_generate_question(text="arithmetic")
    assert isinstance(failed, _Err)
    assert str(failed.value) == "question failed"
    await _assert_next_real_reservation_succeeds(entries.lifecycle, failure_owner)

    started = asyncio.Event()

    class _BlockingAgent:
        async def question_generate(self, *_args: Any, **_kwargs: Any):
            started.set()
            await asyncio.Event().wait()

    cancel_owner = _question_owner(entries, _BlockingAgent())
    task = asyncio.create_task(cancel_owner.study_generate_question(text="arithmetic"))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _assert_next_real_reservation_succeeds(entries.lifecycle, cancel_owner)


@pytest.mark.asyncio
async def test_image_only_question_accepts_config_without_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch, "_coverage_image_only_locale", "question")
    captured: dict[str, Any] = {}

    class _SuccessAgent:
        async def question_generate(self, text: str, **_kwargs: Any):
            captured["text"] = text
            return entries.models.TutorReply(
                operation="question_generate",
                input_text=text,
                reply="What is shown?",
                payload={"question": "What is shown?", "answer": "diagram", "hint": "look"},
            )

    owner = _question_owner(entries, _SuccessAgent())
    owner._cfg = SimpleNamespace()
    monkeypatch.setattr(
        entries.question,
        "_validate_optional_vision_image_payload",
        lambda _owner, payload, **_kwargs: payload,
    )

    result = await owner.study_generate_question(
        vision_image_base64="image-payload",
        locale="en-US",
    )

    assert isinstance(result, _Ok)
    assert captured["text"] == entries.question.IMAGE_ONLY_QUESTION_PROMPT_EN


def _answer_owner(entries: SimpleNamespace, agent: Any) -> Any:
    owner = entries.answer._TutorAnswerEntriesMixin()
    owner._agent = agent
    owner._lock = asyncio.Lock()
    owner._state = SimpleNamespace(current_question={}, active_mode="companion", last_ocr_text="")
    owner._cfg = SimpleNamespace(
        assessment=SimpleNamespace(
            exact_short_answer_enabled=False,
            numeric_tolerance_enabled=False,
            math_expression_enabled=False,
        )
    )
    owner.logger = _Logger()
    owner._resolve_study_target_lanlan = lambda _kwargs: None
    owner._resolve_current_run_id = lambda _kwargs: ""
    owner._build_learning_context = lambda *_args, **_kwargs: _async_value(
        {"screen_classification": {"type": "study"}}
    )
    owner._finalize_tutor_call = lambda _operation, reply, **_kwargs: _async_value(
        dict(reply.payload)
    )
    owner._emit_answer_evaluated_event = lambda **_kwargs: _async_value(True)
    return owner


@pytest.mark.asyncio
async def test_answer_entry_real_lifecycle_releases_after_success_failure_and_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch, "_coverage_lifecycle_answer", "answer")
    assert entries.answer._entry_exception_error is entries.common._entry_exception_error

    class _SuccessAgent:
        async def answer_evaluate(self, **_kwargs: Any):
            return entries.models.TutorReply(
                operation="answer_evaluate",
                input_text="4",
                reply="correct",
                payload={"verdict": "correct", "score": 100, "final_answer_correct": True},
            )

    success_owner = _answer_owner(entries, _SuccessAgent())
    succeeded = await success_owner.study_evaluate_answer(
        answer="4",
        question="2 + 2?",
        expected_answer="4",
    )
    assert isinstance(succeeded, _Ok)
    assert succeeded.value["verdict"] == "correct"
    await _assert_next_real_reservation_succeeds(entries.lifecycle, success_owner)

    class _FailingAgent:
        async def answer_evaluate(self, **_kwargs: Any):
            raise RuntimeError("answer failed")

    failure_owner = _answer_owner(entries, _FailingAgent())
    failed = await failure_owner.study_evaluate_answer(
        answer="4",
        question="2 + 2?",
        expected_answer="4",
    )
    assert isinstance(failed, _Err)
    assert str(failed.value) == "answer failed"
    await _assert_next_real_reservation_succeeds(entries.lifecycle, failure_owner)

    started = asyncio.Event()

    class _BlockingAgent:
        async def answer_evaluate(self, **_kwargs: Any):
            started.set()
            await asyncio.Event().wait()

    cancel_owner = _answer_owner(entries, _BlockingAgent())
    task = asyncio.create_task(
        cancel_owner.study_evaluate_answer(
            answer="4",
            question="2 + 2?",
            expected_answer="4",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _assert_next_real_reservation_succeeds(entries.lifecycle, cancel_owner)


def _summary_owner(entries: SimpleNamespace, agent: Any, emitted: list[dict[str, Any]]) -> Any:
    owner = entries.summary._TutorSummaryEntriesMixin()
    owner._agent = agent
    owner._lock = asyncio.Lock()
    owner._state = SimpleNamespace(active_mode="teaching")
    owner._cfg = SimpleNamespace(history_limit=12)
    owner._store = SimpleNamespace(list_interactions=lambda limit: [{"limit": limit}])
    owner.logger = _Logger()
    owner._build_learning_context = lambda *_args, **_kwargs: _async_value(
        {"screen_classification": {"type": "study"}}
    )
    owner._finalize_tutor_call = lambda _operation, reply, **_kwargs: _async_value(
        {"summary": reply.reply}
    )

    async def emit(payload: dict[str, Any]) -> None:
        emitted.append(payload)

    owner._emit_session_summarized_event = emit
    return owner


@pytest.mark.asyncio
async def test_summary_entry_preserves_success_internal_failure_and_caller_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch, "_coverage_lifecycle_summary", "summary")
    emitted: list[dict[str, Any]] = []

    class _SuccessAgent:
        async def summarize_session(self, history: list[dict[str, Any]], **_kwargs: Any):
            assert history == [{"limit": 12}]
            return entries.models.TutorReply(
                operation="summarize_session",
                input_text="session",
                reply="summary",
            )

    success_owner = _summary_owner(entries, _SuccessAgent(), emitted)
    succeeded = await success_owner.study_summarize_session("algebra")
    assert isinstance(succeeded, _Ok)
    assert succeeded.value["summary"] == "summary"
    assert emitted == [succeeded.value]

    class _FailingAgent:
        async def summarize_session(self, *_args: Any, **_kwargs: Any):
            raise RuntimeError("summary failed")

    failure_owner = _summary_owner(entries, _FailingAgent(), [])
    with pytest.raises(RuntimeError, match="summary failed"):
        await failure_owner.study_summarize_session()

    started = asyncio.Event()

    class _BlockingAgent:
        async def summarize_session(self, *_args: Any, **_kwargs: Any):
            started.set()
            await asyncio.Event().wait()

    cancel_owner = _summary_owner(entries, _BlockingAgent(), [])
    task = asyncio.create_task(cancel_owner.study_summarize_session())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
