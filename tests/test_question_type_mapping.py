import importlib
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _mapping_module(monkeypatch: pytest.MonkeyPatch, name: str):
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    return importlib.import_module(f"{name}.question_type_mapping")


def _models_module(monkeypatch: pytest.MonkeyPatch, name: str):
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    constants = ModuleType(f"{name}.constants")
    constants.MODE_COMPANION = "companion"
    monkeypatch.setitem(sys.modules, constants.__name__, constants)
    json_utils = ModuleType(f"{name}.json_utils")
    json_utils.json_copy = deepcopy
    monkeypatch.setitem(sys.modules, json_utils.__name__, json_utils)
    mode_manager = ModuleType(f"{name}.mode_manager")
    mode_manager.normalize_mode = lambda value: value
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{name}.models")


def test_high_frequency_styles_map_to_the_server_owned_machine_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = _mapping_module(monkeypatch, "_question_type_mapping_common_test")

    assert mapping.resolve_target_question_type(
        {"subject": "math", "question_types": ["几何证明"]}
    ).machine_question_type == "math_reasoning"
    assert mapping.resolve_target_question_type(
        {"subject": "math", "question_types": ["基础计算"]}
    ).machine_question_type == "math_exact"
    assert mapping.resolve_target_question_type(
        {"subject": "math", "question_types": ["概念辨析"]}
    ).machine_question_type == "short_answer"
    assert mapping.resolve_target_question_type(
        {"subject": "computer_science", "question_types": ["程序阅读"]}
    ).machine_question_type == "short_answer"


def test_seed_style_order_is_deterministic_and_not_an_llm_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = _mapping_module(monkeypatch, "_question_type_mapping_order_test")
    resolved = mapping.resolve_target_question_type(
        {
            "subject": "math",
            "question_types": ["概念辨析", "基础计算", "几何证明"],
        }
    )

    assert resolved.question_style == "概念辨析"
    assert resolved.machine_question_type == "short_answer"
    assert resolved.allowed_machine_question_types == ("short_answer",)
    assert resolved.to_context()["required_question_type"] == "short_answer"


def test_unknown_style_uses_subject_default_and_records_a_fallback_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = _mapping_module(monkeypatch, "_question_type_mapping_fallback_test")
    before = mapping.unmapped_question_style_metrics().get("newstyle", 0)

    resolved = mapping.resolve_target_question_type(
        {"subject": "math", "question_types": ["New Style"]}
    )

    assert resolved.question_style == "New Style"
    assert resolved.machine_question_type == "math_reasoning"
    assert resolved.unmapped_question_style == "New Style"
    assert mapping.unmapped_question_style_metrics()["newstyle"] == before + 1


def test_server_enforcement_overrides_a_model_selected_machine_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = _mapping_module(monkeypatch, "_question_type_mapping_enforcement_test")
    resolved = mapping.resolve_target_question_type(
        {"subject": "math", "question_types": ["基础计算"]}
    )

    payload = mapping.enforce_mapped_question_type(
        {"question": "2 + 2 = ?", "question_type": "math_reasoning"}, resolved
    )

    assert payload["question_type"] == "math_exact"
    assert payload["question_style"] == "基础计算"


def test_question_style_is_private_current_question_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _models_module(monkeypatch, "_question_style_private_payload_test")

    public = models.public_current_question_payload(
        {
            "question": "2 + 2 = ?",
            "question_type": "math_exact",
            "question_style": "基础计算",
        }
    )

    assert public == {"question": "2 + 2 = ?", "question_type": "math_exact"}
