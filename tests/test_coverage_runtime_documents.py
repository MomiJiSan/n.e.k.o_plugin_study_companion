from __future__ import annotations

import asyncio
import importlib
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def document_modules(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    package_name = f"_coverage_runtime_documents_{time.time_ns()}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    utils = ModuleType("utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    tokenize = ModuleType("utils.tokenize")
    tokenize.count_tokens = lambda value: len(str(value))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "utils", utils)
    monkeypatch.setitem(sys.modules, "utils.tokenize", tokenize)

    return SimpleNamespace(
        analysis=importlib.import_module(f"{package_name}.document_analysis"),
        jobs=importlib.import_module(f"{package_name}.document_analysis_jobs"),
        chunking=importlib.import_module(f"{package_name}.document_chunking"),
    )


def test_document_validation_messages_and_echo_guard(document_modules: Any) -> None:
    analysis = document_modules.analysis
    document = analysis.validate_document(
        document_name=r"C:\uploads\lesson.md",
        document_type="text/markdown",
        document_text="# Lesson\n\nA compact source about vectors and matrices.",
        analysis_instruction="Emphasize prerequisites.",
        locale="zh_hans",
        analysis_kind="course_material",
    )

    assert document.name == "lesson.md"
    assert document.locale == "zh-CN"
    assert document.descriptor.startswith("[document] lesson.md")
    assert document.public_metadata()["source_retained"] is False
    messages = analysis.build_document_analysis_messages(document)
    assert messages[0]["role"] == "system"
    assert "course_material" in messages[0]["content"]
    assert "<untrusted_document>" in messages[1]["content"]

    auto = analysis.validate_document(
        document_name="notes.txt",
        document_type="",
        document_text="short notes",
        locale="en-US",
    )
    assert auto.locale == "en"
    assert "Localized structures:" in analysis.build_document_analysis_messages(auto)[0]["content"]
    assert analysis.contains_full_document_source("short notes", "short notes") is True
    assert analysis.contains_full_document_source("summary", "short notes") is False

    long_source = " ".join(f"word-{index}" for index in range(96))
    copied = "prefix " + " ".join(long_source.split()[:48])
    assert analysis.contains_full_document_source(copied, long_source) is True
    cjk_source = "甲乙丙丁" * 100
    assert analysis.contains_full_document_source(cjk_source[:200], cjk_source) is True


@pytest.mark.parametrize(
    ("kwargs", "diagnostic"),
    [
        ({"document_name": "", "document_text": "x"}, "invalid_document_name"),
        ({"document_name": "a.exe", "document_text": "x"}, "unsupported_document_type"),
        (
            {"document_name": "a.txt", "document_type": "text/markdown", "document_text": "x"},
            "document_type_mismatch",
        ),
        ({"document_name": "a.txt", "document_text": ""}, "empty_document"),
        ({"document_name": "a.txt", "document_text": "a\x00b"}, "binary_document"),
        ({"document_name": "a.txt", "document_text": "a�b"}, "invalid_document_encoding"),
        ({"document_name": "a.txt", "document_text": "x", "locale": "xx"}, "unsupported_locale"),
        (
            {"document_name": "a.txt", "document_text": "x", "analysis_kind": "unknown"},
            "unsupported_document_kind",
        ),
        (
            {"document_name": "a.txt", "document_text": "1234", "max_tokens": 3},
            "document_too_long",
        ),
        (
            {"document_name": "a.txt", "document_text": "x", "analysis_instruction": "y" * 1001},
            "analysis_instruction_too_long",
        ),
    ],
)
def test_document_validation_rejects_invalid_input(
    document_modules: Any, kwargs: dict[str, object], diagnostic: str
) -> None:
    parameters = {"document_type": "", **kwargs}
    with pytest.raises(document_modules.analysis.DocumentValidationError) as raised:
        document_modules.analysis.validate_document(**parameters)
    assert raised.value.diagnostic == diagnostic


def test_document_validation_rejects_oversized_and_embedded_payloads(document_modules: Any) -> None:
    analysis = document_modules.analysis
    cases = [
        ("x" * (analysis.DOCUMENT_MAX_BYTES + 1), "document_too_large"),
        ("x" * 32_769, "unsafe_document_content"),
        ("A" * 8_192, "unsafe_document_content"),
        ("data:image/png;base64," + "A" * 4_096, "unsafe_document_content"),
    ]
    for text, diagnostic in cases:
        with pytest.raises(analysis.DocumentValidationError) as raised:
            analysis.validate_document(
                document_name="payload.txt",
                document_type="text/plain",
                document_text=text,
                max_tokens=len(text) + 1,
            )
        assert raised.value.diagnostic == diagnostic


def test_chunking_preserves_markdown_structure_and_compacts(document_modules: Any) -> None:
    chunking = document_modules.chunking
    text = (
        "preface\n\n# One ###\nalpha alpha. beta beta.\n\n"
        "```\n# not a heading\n```\n\n## Two\ngamma gamma. delta delta.\n"
    )
    chunks = chunking.split_document(
        text,
        "text/markdown",
        token_counter=len,
        target_tokens=35,
        max_tokens=55,
        min_preferred_tokens=10,
        max_chunks=3,
    )

    assert "".join(chunk.text for chunk in chunks) == text
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert chunks[-1].end_char == len(text)
    paths = {path for chunk in chunks for path in chunk.heading_paths}
    assert ("One",) in paths
    assert ("One", "Two") in paths
    assert ("not a heading",) not in paths

    chapter_text = "Chapter I: Start\n\nFirst. Second.\n\n第二章：继续\n\nThird."
    chapter_chunks = chunking.split_document(
        chapter_text,
        "text/plain",
        token_counter=len,
        target_tokens=24,
        max_tokens=35,
        min_preferred_tokens=5,
        max_chunks=4,
    )
    assert "".join(chunk.text for chunk in chapter_chunks) == chapter_text


@pytest.mark.parametrize(
    "call",
    [
        lambda module: module.split_document("", "text/plain"),
        lambda module: module.split_document("x", "application/json"),
        lambda module: module.split_document("one indivisible sentence", "text/plain", token_counter=len, max_tokens=5, target_tokens=5, min_preferred_tokens=1),
        lambda module: module.split_document("a. b. c.", "text/plain", token_counter=len, max_tokens=3, target_tokens=2, min_preferred_tokens=1, max_chunks=1),
    ],
)
def test_chunking_reports_unrecoverable_splits(document_modules: Any, call: Any) -> None:
    with pytest.raises(document_modules.chunking.DocumentChunkingError) as raised:
        call(document_modules.chunking)
    assert raised.value.diagnostic == "document_split_failed"


def test_chunking_rejects_invalid_budgets(document_modules: Any) -> None:
    split = document_modules.chunking.split_document
    with pytest.raises(ValueError, match="budgets"):
        split("text", "text/plain", min_preferred_tokens=2, target_tokens=1)
    with pytest.raises(ValueError, match="positive"):
        split("text", "text/plain", max_chunks=0)


async def _wait_for_terminal(manager: Any, job_id: str, owner_id: str = "owner") -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1.0
    while loop.time() < deadline:
        payload = await manager.status(job_id, owner_id=owner_id)
        if payload["status"] != "running":
            return payload
        await asyncio.sleep(0.001)
    raise AssertionError("document job did not reach a terminal state")


@pytest.mark.asyncio
async def test_document_job_success_busy_acknowledge_and_callback_failure_isolated(
    document_modules: Any,
) -> None:
    jobs = document_modules.jobs
    manager = jobs.DocumentAnalysisJobManager()
    gate = asyncio.Event()
    callback_results: list[dict[str, Any]] = []

    async def runner(update: Any, budget: Any) -> dict[str, Any]:
        assert budget.chunk_deadline_monotonic < budget.merge_deadline_monotonic
        await update("merging", 9, 3)
        await gate.wait()
        return {"reply": "done", "summary": "ok"}

    def callback(result: dict[str, Any]) -> None:
        callback_results.append(dict(result))
        raise RuntimeError("optional callback failed")

    started = await manager.start(
        owner_id="owner",
        start_token="token",
        analysis_mode="chunked",
        document={"name": "doc.md"},
        total_chunks=3,
        runner=runner,
        on_completed=callback,
    )
    active = await manager.active(owner_id="owner", start_token="token")
    assert active["job_id"] == started["job_id"]
    with pytest.raises(jobs.DocumentAnalysisJobError) as raised:
        await manager.start(
            owner_id="owner",
            analysis_mode="direct",
            document={},
            total_chunks=1,
            runner=runner,
        )
    assert raised.value.diagnostic == "document_job_busy"

    gate.set()
    completed = await _wait_for_terminal(manager, started["job_id"])
    assert completed["status"] == "completed"
    assert completed["completed_chunks"] == 3
    assert callback_results == [{"reply": "done", "summary": "ok"}]
    await manager.status(started["job_id"], owner_id="owner", acknowledge=True)
    assert (await manager.active(owner_id="owner"))["status"] == "idle"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_document_job_cancel_and_committed_result_race(document_modules: Any) -> None:
    jobs = document_modules.jobs
    manager = jobs.DocumentAnalysisJobManager()
    entered = asyncio.Event()

    async def runner(_update: Any) -> dict[str, Any]:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {
                jobs.DOCUMENT_JOB_COMMITTED_RESULT_KEY: True,
                "reply": "persisted before cancel",
            }

    started = await manager.start(
        owner_id="owner",
        analysis_mode="direct",
        document={},
        total_chunks=1,
        runner=runner,
    )
    await entered.wait()
    result = await manager.cancel(started["job_id"], owner_id="owner", source="user")
    assert result["status"] == "completed"
    assert result["reply"] == "persisted before cancel"

    unchanged = await manager.cancel(started["job_id"], owner_id="owner")
    assert unchanged["status"] == "completed"
    with pytest.raises(jobs.DocumentAnalysisJobError):
        await manager.cancel(started["job_id"], owner_id="someone-else")
    await manager.shutdown()


@pytest.mark.asyncio
async def test_document_job_timeout_failure_expiry_and_shutdown(
    document_modules: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = document_modules.jobs
    monkeypatch.setattr(jobs, "DOCUMENT_JOB_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(jobs, "DOCUMENT_JOB_RESULT_TTL_SECONDS", 0.01)
    manager = jobs.DocumentAnalysisJobManager()

    async def timeout_runner(_update: Any) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {}

    started = await manager.start(
        owner_id="owner",
        analysis_mode="direct",
        document={},
        total_chunks=1,
        runner=timeout_runner,
    )
    timed_out = await _wait_for_terminal(manager, started["job_id"])
    assert timed_out["status"] == "failed"
    assert timed_out["diagnostic"] == "timeout"
    assert timed_out["cancellation_source"] == "job_timeout"
    expiry_deadline = asyncio.get_running_loop().time() + 1.0
    while True:
        try:
            await manager.status(started["job_id"], owner_id="owner")
        except jobs.DocumentAnalysisJobError:
            break
        if asyncio.get_running_loop().time() >= expiry_deadline:
            raise AssertionError("document job result did not expire")
        await asyncio.sleep(0.001)

    monkeypatch.setattr(jobs, "DOCUMENT_JOB_TIMEOUT_SECONDS", 1.0)

    class DependencyUnavailable(RuntimeError):
        diagnostic = "provider_unavailable"

    async def failed_runner(_update: Any) -> dict[str, Any]:
        raise DependencyUnavailable

    failed = await manager.start(
        owner_id="owner",
        analysis_mode="direct",
        document={},
        total_chunks=1,
        runner=failed_runner,
    )
    failure = await _wait_for_terminal(manager, failed["job_id"])
    assert failure["degraded"] is True
    assert failure["diagnostic"] == "provider_unavailable"

    blocker = asyncio.Event()

    async def pending_runner(_update: Any) -> dict[str, Any]:
        await blocker.wait()
        return {}

    pending = await manager.start(
        owner_id="owner",
        analysis_mode="direct",
        document={},
        total_chunks=1,
        runner=pending_runner,
    )
    await asyncio.sleep(0)
    await manager.shutdown()
    assert pending["job_id"] not in manager._jobs
    with pytest.raises(jobs.DocumentAnalysisJobError) as raised:
        await manager.start(
            analysis_mode="direct",
            document={},
            total_chunks=1,
            runner=pending_runner,
        )
    assert raised.value.diagnostic == "document_job_busy"
