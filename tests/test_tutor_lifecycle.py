from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_lifecycle_module():
    spec = importlib.util.spec_from_file_location(
        "_test_tutor_lifecycle", ROOT / "tutor_lifecycle.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lifecycle_rejects_concurrent_generation_and_evaluation() -> None:
    lifecycle = _load_lifecycle_module()

    class Owner:
        pass

    async def scenario() -> None:
        owner = Owner()
        assert await lifecycle.reserve_question_lifecycle(owner, "question_generation") == ""
        assert (
            await lifecycle.reserve_question_lifecycle(owner, "question_generation")
            == "question_generation"
        )
        assert (
            await lifecycle.reserve_question_lifecycle(owner, "answer_evaluation")
            == "question_generation"
        )
        await lifecycle.release_question_lifecycle(owner, "question_generation")
        assert await lifecycle.reserve_question_lifecycle(owner, "answer_evaluation") == ""
        assert (
            await lifecycle.reserve_question_lifecycle(owner, "question_generation")
            == "answer_evaluation"
        )

    asyncio.run(scenario())


def test_lifecycle_releases_after_cancelled_generation() -> None:
    lifecycle = _load_lifecycle_module()

    class Owner:
        pass

    async def scenario() -> None:
        owner = Owner()

        async def generation() -> None:
            assert await lifecycle.reserve_question_lifecycle(owner, "question_generation") == ""
            try:
                await asyncio.Event().wait()
            finally:
                await lifecycle.release_question_lifecycle(owner, "question_generation")

        task = asyncio.create_task(generation())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert await lifecycle.reserve_question_lifecycle(owner, "answer_evaluation") == ""

    asyncio.run(scenario())


def test_targeted_generation_timeouts_match_in_both_frontends() -> None:
    panel = (ROOT / "surfaces" / "study_panel.tsx").read_text(encoding="utf-8")
    legacy = (ROOT / "static" / "main.js").read_text(encoding="utf-8")
    expected = "study_generate_targeted_question: 130000"
    assert expected in panel
    assert expected in legacy
