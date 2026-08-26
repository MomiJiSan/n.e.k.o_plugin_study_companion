from __future__ import annotations

import asyncio
import importlib
import sys
from contextlib import asynccontextmanager
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


def _package(monkeypatch: pytest.MonkeyPatch, prefix: str) -> str:
    name = f"{prefix}_{id(monkeypatch)}"
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    return name


@asynccontextmanager
async def _plugin_lock(lock: Any):
    async with lock:
        yield


def _common(monkeypatch: pytest.MonkeyPatch, package: str) -> ModuleType:
    def entry_exception(_owner: Any, exc: BaseException, **_kwargs: Any) -> _Err:
        return _Err(_SdkError(str(exc)))

    return _install_module(
        monkeypatch,
        f"{package}.entry_common",
        Any=Any,
        Err=_Err,
        Ok=_Ok,
        SdkError=_SdkError,
        asyncio=asyncio,
        plugin_entry=lambda **_kwargs: lambda function: function,
        tr=lambda _key, **kwargs: kwargs.get("default", ""),
        ui=_Ui(),
        LLM_OPERATION_ANSWER_EVALUATE="answer_evaluate",
        LLM_OPERATION_CONCEPT_EXPLAIN="concept_explain",
        LLM_OPERATION_QUESTION_GENERATE="question_generate",
        LLM_OPERATION_SUMMARIZE_SESSION="summarize_session",
        MODE_COMPANION="companion",
        MODE_CONCEPT_EXPLAIN="concept_explain",
        TutorReply=object,
        _entry_exception_error=entry_exception,
        _normalize_submitted_image_payload=lambda value: value,
        _plugin_lock=_plugin_lock,
        _validate_optional_vision_image_payload=lambda _owner, value, **_kwargs: value,
        build_tutor_payload=lambda reply: {"reply": reply.reply},
        handle_user_intent=lambda *_args, **_kwargs: {
            "matched": False,
            "pure_switch": False,
            "kind": "",
            "remaining_text": "",
        },
        time=importlib.import_module("time"),
    )


def _load_explain_entries(monkeypatch: pytest.MonkeyPatch):
    package = _package(monkeypatch, "_coverage_lifecycle_explain")
    _common(monkeypatch, package)
    _install_module(
        monkeypatch,
        f"{package}._general_narration",
        prepare_general_narration_content=lambda value: value,
    )
    _install_module(
        monkeypatch,
        f"{package}._solution_structure",
        extract_solution_narration_sections=lambda _value: None,
        is_solution_structure_candidate=lambda _value: False,
        parse_solution_structure=lambda _value: None,
        render_solution_structure=lambda _value, **_kwargs: "",
    )
    _install_module(
        monkeypatch,
        f"{package}.entry_tutor_context_support",
        _TutorFinalizeProgress=type("_TutorFinalizeProgress", (), {}),
    )
    _install_module(
        monkeypatch,
        f"{package}.tutor_llm_agent_concept_explain",
        repair_solution_structure=lambda *_args, **_kwargs: None,
    )
    return importlib.import_module(f"{package}.entry_tutor_explain_entries")


def _load_summary_entries(monkeypatch: pytest.MonkeyPatch):
    package = _package(monkeypatch, "_coverage_lifecycle_summary")
    _common(monkeypatch, package)
    return importlib.import_module(f"{package}.entry_tutor_summary_entries")


def _load_question_entries(monkeypatch: pytest.MonkeyPatch):
    package = _package(monkeypatch, "_coverage_lifecycle_question")
    _common(monkeypatch, package)
    _install_module(
        monkeypatch,
        f"{package}.adaptive_learning.learner_state",
        tracker_list_mastery=lambda *_args, **_kwargs: [],
    )
    _install_module(
        monkeypatch,
        f"{package}.adaptive_learning.planner",
        build_question_plan=lambda *_args, **_kwargs: None,
    )
    _install_module(
        monkeypatch,
        f"{package}.adaptive_learning.question_factory",
        QuestionFactory=object,
    )
    _install_module(
        monkeypatch,
        f"{package}.knowledge_graph_guidance",
        _canonical_necessary_relations=lambda *_args, **_kwargs: [],
    )
    _install_module(
        monkeypatch,
        f"{package}.llm_prompts",
        ensure_targeted_prompt_context_fits=lambda _context: None,
    )
    _install_module(
        monkeypatch,
        f"{package}.models",
        public_current_question_payload=lambda value: dict(value or {}),
    )
    _install_module(
        monkeypatch,
        f"{package}.practice_scope",
        filter_question_params_to_scope=lambda params, _eligible: dict(params),
        ordered_scope_topics=lambda topics, **_kwargs: list(topics),
        practice_scope_matches_topic=lambda *_args, **_kwargs: True,
    )
    _install_module(
        monkeypatch,
        f"{package}.targeted_question_contract",
        project_target_topic_evidence=lambda value: dict(value or {}),
        semantic_validation_passed=lambda *_args, **_kwargs: True,
        validate_targeted_question=lambda *_args, **_kwargs: None,
    )

    async def reserve(*_args: Any, **_kwargs: Any) -> str:
        return ""

    async def release(*_args: Any, **_kwargs: Any) -> None:
        return None

    _install_module(
        monkeypatch,
        f"{package}.tutor_lifecycle",
        release_question_lifecycle=release,
        reserve_question_lifecycle=reserve,
    )
    return importlib.import_module(f"{package}.entry_tutor_question_entries")


def _load_answer_entries(monkeypatch: pytest.MonkeyPatch):
    package = _package(monkeypatch, "_coverage_lifecycle_answer")
    _common(monkeypatch, package)
    _install_module(
        monkeypatch,
        f"{package}.adaptive_learning.assessment",
        AssessmentEngine=object,
        AssessmentRequest=object,
    )
    _install_module(
        monkeypatch,
        f"{package}.adaptive_learning.deterministic_evaluators",
        ExactShortAnswerEvaluator=object,
        MathExpressionEvaluator=object,
        NumericToleranceEvaluator=object,
    )
    _install_module(
        monkeypatch,
        f"{package}.evaluation_contract",
        canonicalize_evaluation=lambda value: dict(value),
        validate_evaluation=lambda *_args, **_kwargs: SimpleNamespace(valid=True),
    )
    _install_module(
        monkeypatch,
        f"{package}.models",
        public_current_question_payload=lambda value: dict(value or {}),
    )
    _install_module(
        monkeypatch,
        f"{package}.practice_outcome",
        build_practice_outcome=lambda **kwargs: kwargs,
    )
    _install_module(
        monkeypatch,
        f"{package}.target_binding",
        validated_target_topic_id=lambda *_args, **_kwargs: "",
    )

    async def reserve(*_args: Any, **_kwargs: Any) -> str:
        return ""

    async def release(*_args: Any, **_kwargs: Any) -> None:
        return None

    _install_module(
        monkeypatch,
        f"{package}.tutor_lifecycle",
        release_question_lifecycle=release,
        reserve_question_lifecycle=reserve,
    )
    return importlib.import_module(f"{package}.entry_tutor_answer_entries")


@pytest.mark.asyncio
async def test_explain_entry_success_failure_and_cancellation_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_explain_entries(monkeypatch)
    owner = module._TutorExplainEntriesMixin()
    owner._lock = asyncio.Lock()
    owner._state = SimpleNamespace(active_mode="companion", last_ocr_text="")
    owner._cfg = SimpleNamespace(language="zh-CN", llm_vision_enabled=True)
    owner._resolve_study_target_lanlan = lambda _kwargs: None

    owner._agent = None
    unavailable = await owner.study_explain_text("topic")
    assert isinstance(unavailable, _Err)
    assert str(unavailable.value) == "study tutor agent is not initialized"

    owner._agent = object()
    monkeypatch.setattr(
        module,
        "handle_user_intent",
        lambda *_args, **_kwargs: {
            "matched": True,
            "kind": "mode_switch",
            "mode": "teaching",
            "keyword": "teach",
            "pure_switch": True,
            "remaining_text": "",
            "transition_phrase": "已切换",
        },
    )

    async def apply_mode(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"changed": True, "new_mode": "teaching", "transition_phrase": "已切换"}

    owner._apply_mode_switch = apply_mode
    switched = await owner.study_explain_text("切换教学模式")
    assert isinstance(switched, _Ok)
    assert switched.value["reply"] == "已切换"
    assert switched.value["degraded"] is False

    started = asyncio.Event()

    class _Agent:
        async def concept_explain(self, *_args: Any, **_kwargs: Any):
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(
        module,
        "handle_user_intent",
        lambda *_args, **_kwargs: {
            "matched": False,
            "pure_switch": False,
            "kind": "",
            "remaining_text": "",
        },
    )
    owner._agent = _Agent()
    owner._is_current_ocr_text = lambda _text: _async_value(False)
    owner._build_learning_context = lambda *_args, **_kwargs: _async_value(
        {"study_response_mode": "general_chat", "study_semantic_status": ""}
    )
    task = asyncio.create_task(owner.study_explain_text("topic"))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _async_value(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_summary_entry_returns_payload_and_degrades_only_event_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_summary_entries(monkeypatch)
    owner = module._TutorSummaryEntriesMixin()
    owner._lock = asyncio.Lock()
    owner._state = SimpleNamespace(active_mode="teaching")
    owner._cfg = SimpleNamespace(history_limit=12)
    owner._store = SimpleNamespace(list_interactions=lambda limit: [{"limit": limit}])
    owner.logger = _Logger()

    reply = SimpleNamespace(degraded=False, diagnostic="", payload={"highlights": ["x"]})

    class _Agent:
        async def summarize_session(self, history: list[dict[str, Any]], **_kwargs: Any):
            assert history == [{"limit": 12}]
            return reply

    owner._agent = _Agent()
    owner._build_learning_context = lambda *_args, **_kwargs: _async_value(
        {"screen_classification": {"type": "study"}}
    )
    owner._finalize_tutor_call = lambda *_args, **_kwargs: _async_value({"summary": "done"})

    async def fail_event(_payload: dict[str, Any]) -> None:
        raise RuntimeError("event bus unavailable")

    owner._emit_session_summarized_event = fail_event
    result = await owner.study_summarize_session(" algebra ")

    assert isinstance(result, _Ok)
    assert result.value == {
        "summary": "done",
        "screen_classification": {"type": "study"},
    }
    assert owner.logger.warnings


@pytest.mark.asyncio
async def test_summary_entry_failure_and_cancellation_keep_exception_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_summary_entries(monkeypatch)
    owner = module._TutorSummaryEntriesMixin()
    owner._lock = asyncio.Lock()
    owner._state = SimpleNamespace(active_mode="companion")
    owner._cfg = SimpleNamespace(history_limit=5)
    owner._store = SimpleNamespace(list_interactions=lambda _limit: [])

    owner._agent = None
    unavailable = await owner.study_summarize_session()
    assert isinstance(unavailable, _Err)

    started = asyncio.Event()

    class _Agent:
        async def summarize_session(self, *_args: Any, **_kwargs: Any):
            started.set()
            await asyncio.Event().wait()

    owner._agent = _Agent()
    owner._build_learning_context = lambda *_args, **_kwargs: _async_value({})
    task = asyncio.create_task(owner.study_summarize_session())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_question_entry_always_releases_generation_reservation(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    module = _load_question_entries(monkeypatch)
    owner = module._TutorQuestionEntriesMixin()
    owner._agent = object()
    owner._lock = asyncio.Lock()
    owner._state = SimpleNamespace(last_ocr_text="")
    releases: list[str] = []
    started = asyncio.Event()

    async def reserve(*_args: Any, **_kwargs: Any) -> str:
        return ""

    async def release(_owner: Any, operation: str) -> None:
        releases.append(operation)

    async def generate(**_kwargs: Any) -> dict[str, str]:
        if outcome == "failure":
            raise RuntimeError("generation failed")
        if outcome == "cancel":
            started.set()
            await asyncio.Event().wait()
        return {"question": "2 + 2?"}

    monkeypatch.setattr(module, "reserve_question_lifecycle", reserve)
    monkeypatch.setattr(module, "release_question_lifecycle", release)
    owner._generate_question_payload = generate
    owner._is_current_ocr_text = lambda _text: _async_value(False)

    task = asyncio.create_task(owner.study_generate_question(text="2 + 2"))
    if outcome == "cancel":
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert isinstance(result, _Ok if outcome == "success" else _Err)

    assert releases == ["question_generation"]


@pytest.mark.asyncio
async def test_answer_entry_success_busy_failure_and_cancellation_reservation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_answer_entries(monkeypatch)
    owner = module._TutorAnswerEntriesMixin()
    releases: list[str] = []
    active_operation = ""

    async def reserve(*_args: Any, **_kwargs: Any) -> str:
        return active_operation

    async def release(_owner: Any, operation: str) -> None:
        releases.append(operation)

    monkeypatch.setattr(module, "reserve_question_lifecycle", reserve)
    monkeypatch.setattr(module, "release_question_lifecycle", release)
    owner._study_evaluate_answer_impl = lambda **_kwargs: _async_value(_Ok({"score": 1.0}))

    succeeded = await owner.study_evaluate_answer(answer="4")
    assert isinstance(succeeded, _Ok)
    assert releases == ["answer_evaluation"]

    active_operation = "question_generation"
    busy = await owner.study_evaluate_answer(answer="4")
    assert isinstance(busy, _Err)
    assert busy.value.code == "QUESTION_GENERATION_IN_PROGRESS"
    assert releases == ["answer_evaluation"]

    active_operation = ""
    started = asyncio.Event()

    async def block(**_kwargs: Any) -> _Ok:
        started.set()
        await asyncio.Event().wait()
        return _Ok({})

    owner._study_evaluate_answer_impl = block
    task = asyncio.create_task(owner.study_evaluate_answer(answer="4"))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert releases == ["answer_evaluation", "answer_evaluation"]
