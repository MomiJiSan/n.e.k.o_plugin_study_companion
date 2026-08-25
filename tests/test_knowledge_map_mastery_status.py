from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_ui_api(monkeypatch: pytest.MonkeyPatch):
    package_name = "_study_companion_mastery_status_test"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    return importlib.import_module(f"{package_name}.ui_api")


def test_knowledge_map_keeps_unassessed_topics_distinct_from_zero_mastery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui_api = _load_ui_api(monkeypatch)
    payload = ui_api.build_knowledge_map_payload(
        topics=[
            {"id": "unassessed", "name": "未评估"},
            {"id": "zero", "name": "零分"},
            {"id": "progress", "name": "进行中"},
            {"id": "good", "name": "熟练"},
            {"id": "mastered", "name": "掌握"},
        ],
        mastery_overview=[
            {"topic_id": "zero", "mastery": 0.0, "flags": []},
            {"topic_id": "progress", "mastery": 0.5, "flags": []},
            {"topic_id": "good", "mastery": 0.7, "flags": []},
            {"topic_id": "mastered", "mastery": 0.9, "flags": []},
        ],
    )
    nodes = {node["id"]: node for node in payload["nodes"]}

    assert nodes["unassessed"]["assessed"] is False
    assert nodes["unassessed"]["mastery"] is None
    assert nodes["unassessed"]["mastery_status"] == "unassessed"
    assert nodes["unassessed"]["weak"] is False
    assert nodes["zero"]["assessed"] is True
    assert nodes["zero"]["mastery"] == 0.0
    assert nodes["zero"]["mastery_status"] == "weak"
    assert nodes["zero"]["weak"] is True
    assert nodes["progress"]["mastery_status"] == "progress"
    assert nodes["progress"]["weak"] is True
    assert nodes["good"]["mastery_status"] == "good"
    assert nodes["good"]["weak"] is False
    assert nodes["mastered"]["mastery_status"] == "mastered"
    assert payload["summary"]["weak_topic_count"] == 2


def test_knowledge_map_false_mastery_is_weak_even_at_high_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui_api = _load_ui_api(monkeypatch)
    payload = ui_api.build_knowledge_map_payload(
        topics=[{"id": "topic", "name": "主题"}],
        mastery_overview=[
            {"topic_id": "topic", "mastery": 0.95, "flags": ["false_mastery"]}
        ],
    )

    node = payload["nodes"][0]
    assert node["assessed"] is True
    assert node["mastery_status"] == "weak"
    assert node["weak"] is True
    assert payload["summary"]["weak_topic_count"] == 1


def test_knowledge_map_uses_weak_results_when_overview_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui_api = _load_ui_api(monkeypatch)
    payload = ui_api.build_knowledge_map_payload(
        topics=[{"id": "omitted", "name": "仅在薄弱结果中"}],
        mastery_overview=[],
        weak_topics=[{"topic_id": "omitted", "mastery": 0.2, "flags": []}],
    )

    node = payload["nodes"][0]
    assert node["assessed"] is True
    assert node["mastery"] == 0.2
    assert node["mastery_status"] == "weak"
    assert node["weak"] is True
    assert payload["summary"]["weak_topic_count"] == 1


def test_knowledge_map_active_wrong_question_blocks_mastered_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui_api = _load_ui_api(monkeypatch)
    payload = ui_api.build_knowledge_map_payload(
        topics=[{"id": "topic", "name": "仍需订正"}],
        mastery_overview=[
            {"topic_id": "topic", "mastery": 0.9954, "flags": [], "attempts": 8}
        ],
        wrong_questions=[{"id": "wrong-1", "topic_id": "topic", "status": "retrying"}],
    )

    node = payload["nodes"][0]
    assert node["assessed"] is True
    assert node["mastery"] == 0.9954
    assert node["mastery_status"] == "progress"
    assert node["weak"] is True
    assert payload["summary"]["wrong_question_count"] == 1
