import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_prompts(monkeypatch: pytest.MonkeyPatch, package_name: str):
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    utils = ModuleType("utils")
    tokenize = ModuleType("utils.tokenize")
    tokenize.count_tokens = lambda text: max(1, len(str(text)) // 4)  # type: ignore[attr-defined]
    tokenize.truncate_to_tokens = lambda text, limit: str(text)[: limit * 4]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "utils", utils)
    monkeypatch.setitem(sys.modules, "utils.tokenize", tokenize)

    mode_manager = ModuleType(f"{package_name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"{package_name}.mode_manager", mode_manager)
    return importlib.import_module(f"{package_name}.llm_prompts")


def test_targeted_question_prompt_includes_strict_generation_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _load_prompts(monkeypatch, "_targeted_prompt_requirements_test")

    messages = prompts.build_question_generate_messages(
        text="Generate one adaptive practice question.",
        language="en",
        context={
            "targeted_question": True,
            "knowledge_question_params": {"suggested_difficulty": 2},
        },
    )

    user_prompt = messages[1]["content"]
    assert "Additional requirements for targeted questions:" in user_prompt
    assert "reference_answer must exactly copy answer" in user_prompt
    assert "accepted_answers must include answer" in user_prompt
    assert "at most 12 unique items" in user_prompt
    assert "each item must be at most 500 characters" in user_prompt
    assert (
        "difficulty must exactly copy "
        "context.knowledge_question_params.suggested_difficulty"
    ) in user_prompt
    assert "question, hint, topic, key_points" in user_prompt
    assert "must use context.language" in user_prompt


def test_ordinary_question_prompt_keeps_base_requirements_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _load_prompts(monkeypatch, "_ordinary_prompt_requirements_test")
    context = {
        "targeted_question": False,
        "text": "Generate one study question.",
        "language": "en",
        "mode": "companion",
    }

    messages = prompts.build_question_generate_messages(
        text=context["text"],
        language=context["language"],
        context={"targeted_question": False},
    )
    expected = prompts._build_structured_messages(
        operation=prompts.LLM_OPERATION_QUESTION_GENERATE,
        system_prompt=prompts.STUDY_QUESTION_GENERATE_SYSTEM_PROMPT,
        requirements=prompts.STUDY_QUESTION_GENERATE_REQUIREMENTS,
        context=context,
        example=prompts.STUDY_QUESTION_GENERATE_EXAMPLE,
        mode="companion",
    )

    assert messages == expected
    assert "Additional requirements for targeted questions:" not in messages[1]["content"]
