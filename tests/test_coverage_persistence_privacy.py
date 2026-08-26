from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_study_companion_coverage_persistence"
if PACKAGE_NAME not in sys.modules:
    package = ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = package
mode_manager = ModuleType(f"{PACKAGE_NAME}.mode_manager")
mode_manager.normalize_mode = lambda value: str(value or "companion")
sys.modules[mode_manager.__name__] = mode_manager

StudyStore = importlib.import_module(f"{PACKAGE_NAME}.store").StudyStore
StudyState = importlib.import_module(f"{PACKAGE_NAME}.models").StudyState


class _Logger:
    def debug(self, *_args, **_kwargs) -> None:
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


def _identity_decorator(*_args, **_kwargs):
    return lambda value: value


def _load_document_runtime(monkeypatch: pytest.MonkeyPatch):
    utils_package = ModuleType("utils")
    utils_package.__path__ = []  # type: ignore[attr-defined]
    tokenize_module = ModuleType("utils.tokenize")
    tokenize_module.count_tokens = _count_tokens
    monkeypatch.setitem(sys.modules, "utils", utils_package)
    monkeypatch.setitem(sys.modules, "utils.tokenize", tokenize_module)

    models = importlib.import_module(f"{PACKAGE_NAME}.models")
    entry_common = ModuleType(f"{PACKAGE_NAME}.entry_common")
    entry_common.LLM_OPERATION_ANSWER_EVALUATE = "answer_evaluate"
    entry_common.LLM_OPERATION_CONCEPT_EXPLAIN = "concept_explain"
    entry_common.LLM_OPERATION_KNOWLEDGE_TRACK = "knowledge_track"
    entry_common.LLM_OPERATION_QUESTION_GENERATE = "question_generate"
    entry_common.LLM_OPERATION_SUMMARIZE_SESSION = "summarize_session"
    entry_common.Any = Any
    entry_common.Ok = _Ok
    entry_common.SdkError = _SdkError
    entry_common.StudyEvent = SimpleNamespace
    entry_common.TutorReply = models.TutorReply
    entry_common._detect_mastery_threshold_crossed = lambda *_args: None
    entry_common._plugin_lock = lambda lock: lock
    entry_common.asyncio = asyncio
    entry_common.build_tutor_payload = lambda reply: reply.to_dict()
    entry_common.plugin_entry = _identity_decorator
    entry_common.time = time
    entry_common.tr = lambda _key, default="", **_kwargs: default
    entry_common.ui = SimpleNamespace(action=lambda: _identity_decorator())
    entry_common.utc_now_iso = models.utc_now_iso
    monkeypatch.setitem(sys.modules, entry_common.__name__, entry_common)

    tutor_common = ModuleType(f"{PACKAGE_NAME}.tutor_llm_agent_common")
    tutor_common.SdkError = _SdkError
    tutor_common.diagnostic_code_for_exception = lambda exc: str(
        getattr(exc, "diagnostic", "") or "llm_call_failed"
    )
    monkeypatch.setitem(sys.modules, tutor_common.__name__, tutor_common)

    model_gateway = ModuleType(f"{PACKAGE_NAME}.study_model_gateway")
    model_gateway.StudyModelError = type("StudyModelError", (RuntimeError,), {})
    monkeypatch.setitem(sys.modules, model_gateway.__name__, model_gateway)

    document_analysis = importlib.import_module(f"{PACKAGE_NAME}.document_analysis")
    document_chunking = importlib.import_module(f"{PACKAGE_NAME}.document_chunking")
    tutor_document = importlib.import_module(
        f"{PACKAGE_NAME}.tutor_llm_agent_document"
    )
    context_support = importlib.import_module(
        f"{PACKAGE_NAME}.entry_tutor_context_support"
    )
    document_entries = importlib.import_module(
        f"{PACKAGE_NAME}.entry_document_analysis_jobs"
    )
    return SimpleNamespace(
        context_support=context_support,
        document_analysis=document_analysis,
        document_chunking=document_chunking,
        document_entries=document_entries,
        tutor_document=tutor_document,
    )


def _private_source(analysis_mode: str, sentinel: str) -> str:
    if analysis_mode == "direct":
        return (
            f"{sentinel} direct private source prefix. "
            "This text must remain runtime-only."
        )
    paragraph = (
        f"{sentinel} chunk-private paragraph. "
        "Every possible persisted chunk must carry the sentinel. "
        + "x" * 60
    )
    return "\n\n".join(paragraph for _ in range(2_000))


def _assert_private_probes_absent(
    value: object, *, sentinel: str, source: str
) -> None:
    serialized = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, sort_keys=True)
    )
    for probe in (sentinel, source[:32], source[:64]):
        assert probe not in serialized


@pytest.mark.parametrize(
    ("analysis_mode", "sentinel"),
    (("direct", "ZXQ-DIR-91"), ("chunked", "ZXQ-CHK-91")),
)
def test_document_source_is_absent_from_sqlite_state_and_json_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    analysis_mode: str,
    sentinel: str,
) -> None:
    runtime = _load_document_runtime(monkeypatch)
    source = _private_source(analysis_mode, sentinel)
    summary = f"A privacy-safe {analysis_mode} analysis result."
    chunks = runtime.document_chunking.split_document(source, "text/markdown")
    if analysis_mode == "direct":
        assert _count_tokens(source) <= runtime.document_chunking.DOCUMENT_DIRECT_MAX_TOKENS
    else:
        assert _count_tokens(source) > runtime.document_chunking.DOCUMENT_DIRECT_MAX_TOKENS
        assert len(chunks) > 1
        assert all(sentinel in chunk.text for chunk in chunks)

    store = StudyStore(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()

    class Agent:
        def __init__(self) -> None:
            self._logger = _Logger()
            self.model_calls: list[dict[str, Any]] = []

        def _new_operation_deadline(self, _operation, _messages) -> float:
            return time.monotonic() + 10

        async def _call_model_result(self, messages, *, operation, **_kwargs):
            self.model_calls.append(
                {"operation": operation, "messages": list(messages)}
            )
            text = (
                f"Privacy-safe chunk memo {len(self.model_calls)}."
                if operation == "document_chunk_analyze"
                else summary
            )
            return SimpleNamespace(text=text, output_limit_reached=False)

        async def document_analyze(self, document):
            return await runtime.tutor_document.document_analyze(self, document)

        def build_document_merge_messages(self, document, chunks, memos):
            return runtime.tutor_document.build_document_merge_messages(
                document, chunks, memos
            )

    class Harness(
        runtime.document_entries._DocumentAnalysisJobsEntriesMixin,
        runtime.context_support._TutorContextSupportMixin,
    ):
        def __init__(self) -> None:
            self._agent = Agent()
            self._cfg = SimpleNamespace(
                history_limit=10,
                communication=SimpleNamespace(
                    enabled=False, general_narration_enabled=True
                ),
            )
            self._event_bus = None
            self._lock = asyncio.Lock()
            self._state = StudyState()
            self._store = store
            self.logger = _Logger()

        async def _track_learning(self, *_args, **_kwargs) -> dict[str, Any]:
            return {}

        async def _persist_state(self) -> None:
            await asyncio.to_thread(self._store.save_state, self._state)

    async def run_document_path() -> tuple[
        dict[str, Any], dict[str, Any], list[dict[str, Any]]
    ]:
        harness = Harness()
        manager = harness._document_job_manager()
        try:
            started = await harness.study_start_document_analysis(
                document_name=f"private-{analysis_mode}.md",
                document_type="text/markdown",
                document_text=source,
                analysis_instruction="Summarize without quoting the source.",
                analysis_kind="general_notes",
                locale="en",
            )
            assert isinstance(started, _Ok)
            assert started.value["analysis_mode"] == analysis_mode
            job_id = started.value["job_id"]
            job = manager._jobs[job_id]
            job_task = job.task
            assert job_task is not None
            deadline = time.monotonic() + 5.0
            try:
                await asyncio.wait_for(
                    asyncio.shield(job_task),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            except TimeoutError:
                pytest.fail(
                    "document analysis job exceeded monotonic deadline: "
                    f"{job.public_payload()}"
                )
            job_state = job.public_payload()
            if job_state["status"] != "completed":
                pytest.fail(f"document analysis job did not complete: {job_state}")
            return dict(job.result), job_state, list(harness._agent.model_calls)
        finally:
            await manager.shutdown()

    try:
        completed, job_state, model_calls = asyncio.run(run_document_path())
        model_input = json.dumps(model_calls, ensure_ascii=False)
        assert sentinel in model_input
        _assert_private_probes_absent(
            completed, sentinel=sentinel, source=source
        )
        _assert_private_probes_absent(
            job_state, sentinel=sentinel, source=source
        )

        store.close()
        store.open()
        persisted_state = json.dumps(
            store.get_raw("state"), ensure_ascii=False, sort_keys=True
        )
        _assert_private_probes_absent(
            persisted_state, sentinel=sentinel, source=source
        )
        exported = store.export_json()
        exported_text = json.dumps(exported, ensure_ascii=False, sort_keys=True)
        _assert_private_probes_absent(
            exported_text, sentinel=sentinel, source=source
        )
        assert exported["interactions"][0]["input_text"].startswith(
            f"[document] private-{analysis_mode}.md"
        )
        assert exported["interactions"][0]["output_text"] == summary
        assert exported["interactions"][0]["metadata"]["source_retained"] is False
    finally:
        store.close()

    sqlite_bytes = b"".join(
        path.read_bytes() for path in sorted(tmp_path.glob("study.db*"))
    )
    for probe in (sentinel, source[:32], source[:64]):
        assert probe.encode("utf-8") not in sqlite_bytes
