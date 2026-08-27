import importlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SEED_MANIFEST = ROOT / "static" / "knowledge_graph_seed.json"


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


def _all_seed_topics() -> list[dict[str, object]]:
    manifest = json.loads(SEED_MANIFEST.read_text(encoding="utf-8"))
    topics: list[dict[str, object]] = []
    for entry in manifest["files"]:
        source = json.loads((SEED_MANIFEST.parent / entry["path"]).read_text(encoding="utf-8"))
        topics.extend(source["topics"])
    return topics


def test_first_practice_uses_the_first_declared_teaching_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = _mapping_module(monkeypatch, "_question_style_first_test")
    topic = {
        "subject": "math",
        "question_types": ["概念辨析", "基础计算", "几何证明"],
    }

    selected = mapping.select_question_style(
        topic,
        attempt_count=0,
        selection_reason="recommended",
    )

    assert selected.question_style == "概念辨析"
    assert selected.machine_question_type == "short_answer"


def test_practice_rotates_without_repeating_a_declared_style_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = _mapping_module(monkeypatch, "_question_style_rotation_test")
    topic = {
        "subject": "math",
        "question_types": ["概念辨析", "基础计算", "几何证明"],
    }

    styles = [
        mapping.select_question_style(
            topic,
            attempt_count=attempt_count,
            selection_reason="recommended",
        ).question_style
        for attempt_count in range(4)
    ]

    assert styles == ["概念辨析", "基础计算", "几何证明", "概念辨析"]
    assert len(set(styles[:3])) == 3


def test_selection_is_pure_even_when_the_style_needs_a_subject_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = _mapping_module(monkeypatch, "_question_style_pure_test")
    topic = {"subject": "math", "question_types": ["Unmapped teaching style"]}
    metrics_before = mapping.unmapped_question_style_metrics()

    first = mapping.select_question_style(
        topic,
        attempt_count=0,
        selection_reason="recommended",
    )
    second = mapping.select_question_style(
        topic,
        attempt_count=0,
        selection_reason="recommended",
    )

    assert first == second
    assert first.question_style == "Unmapped teaching style"
    assert first.machine_question_type == "math_reasoning"
    assert mapping.unmapped_question_style_metrics() == metrics_before


def test_retry_avoids_the_previous_style_and_question_copy_protection_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = _mapping_module(monkeypatch, "_question_style_retry_test")
    contract_package = ModuleType("_question_style_retry_contract_test")
    contract_package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, contract_package.__name__, contract_package)
    contract = importlib.import_module(f"{contract_package.__name__}.targeted_question_contract")
    topic = {
        "subject": "math",
        "question_types": ["概念辨析", "基础计算", "几何证明"],
    }

    selected = mapping.select_question_style(
        topic,
        attempt_count=0,
        selection_reason="retry",
        previous_question_style="概念辨析",
        error_type="conceptual_error",
    )

    assert selected.question_style != "概念辨析"
    assert selected.question_style in topic["question_types"]
    invalid = contract.validate_targeted_question(
        {
            "question": "Original question",
            "answer": "42",
            "reference_answer": "42",
            "question_type": selected.machine_question_type,
            "difficulty": 2,
            "target_topic_id": "target",
        },
        target_topic_id="target",
        target_topic_name="Target topic",
        origin_wrong_question={"question": {"question": "Original question"}},
    )
    assert "retry_copies_original_question" in invalid.errors


def test_due_review_prefers_a_fast_recall_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = _mapping_module(monkeypatch, "_question_style_review_test")
    topic = {
        "subject": "math",
        "question_types": ["几何证明", "概念辨析", "基础计算"],
    }

    selected = mapping.select_question_style(
        topic,
        attempt_count=1,
        selection_reason="due_review",
    )

    assert selected.question_style == "概念辨析"
    assert selected.machine_question_type == "short_answer"


def test_selected_teaching_style_stays_private_to_the_question_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = _mapping_module(monkeypatch, "_question_style_private_mapping_test")
    models = _models_module(monkeypatch, "_question_style_private_payload_test")
    selected = mapping.select_question_style(
        {"subject": "math", "question_types": ["基础计算"]},
        attempt_count=0,
        selection_reason="recommended",
    )

    public = models.public_current_question_payload(
        {
            "question": "2 + 2 = ?",
            "question_type": selected.machine_question_type,
            "question_style": selected.question_style,
        }
    )

    assert public == {"question": "2 + 2 = ?", "question_type": "math_exact"}


@pytest.mark.parametrize(
    ("topic", "expected_machine_type"),
    [
        ({"subject": "math", "question_types": ["几何证明"]}, "math_reasoning"),
        ({"subject": "physics", "question_types": ["实验分析"]}, "short_answer"),
        ({"subject": "geography", "question_types": ["图像分析"]}, "short_answer"),
        ({"subject": "politics", "question_types": ["措施题"]}, "short_answer"),
        ({"subject": "english", "question_types": ["concept check"]}, "short_answer"),
    ],
)
def test_subject_specialists_keep_the_three_existing_machine_types(
    monkeypatch: pytest.MonkeyPatch,
    topic: dict[str, object],
    expected_machine_type: str,
) -> None:
    mapping = _mapping_module(monkeypatch, "_question_style_subject_test")

    selected = mapping.select_question_style(
        topic,
        attempt_count=0,
        selection_reason="recommended",
    )

    assert selected.machine_question_type == expected_machine_type
    assert selected.machine_question_type in mapping.MACHINE_QUESTION_TYPES
    assert selected.allowed_machine_question_types == (expected_machine_type,)


def test_seed_preferred_styles_have_at_most_seventeen_percent_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = _mapping_module(monkeypatch, "_question_style_seed_coverage_test")
    selected = [
        mapping.select_question_style(
            topic,
            attempt_count=0,
            selection_reason="recommended",
        )
        for topic in _all_seed_topics()
    ]

    fallback_count = sum(bool(item.unmapped_question_style) for item in selected)
    assert fallback_count / len(selected) <= 0.17
