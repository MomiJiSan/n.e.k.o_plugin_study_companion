from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _guidance_module(monkeypatch: pytest.MonkeyPatch):
    package_name = "_public_guidance_payload_budget_test"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    return importlib.import_module(f"{package_name}.knowledge_graph_guidance")


def _topic(topic_id: str, *, prerequisites: list[dict[str, str]] | None = None) -> dict[str, object]:
    return {
        "id": topic_id,
        "name": topic_id,
        "subject": "math",
        "stage": "college",
        "chapter": "budget",
        "unit": "budget",
        "depth": 2,
        "difficulty": 0.5,
        "prerequisites": prerequisites or [],
        "related": [],
    }


def _wide_learning_path_topics() -> tuple[list[dict[str, object]], set[str]]:
    topics = [_topic("target")]
    direct_ids: set[str] = set()
    for parent_index in range(3):
        parent_id = f"parent-{parent_index}"
        direct_ids.add(parent_id)
        topics[0]["prerequisites"].append({"id": parent_id})  # type: ignore[index]
        prerequisites = []
        for ancestor_index in range(24):
            ancestor_id = f"ancestor-{parent_index}-{ancestor_index}"
            prerequisites.append({"id": ancestor_id})
            topics.append(_topic(ancestor_id))
        topics.append(_topic(parent_id, prerequisites=prerequisites))
    return topics, direct_ids


def test_public_learning_path_is_bounded_and_keeps_all_direct_neighbours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance = _guidance_module(monkeypatch)
    topics, direct_ids = _wide_learning_path_topics()

    payload = guidance.build_knowledge_guidance_payload(
        topics=topics,
        topic_id="target",
        max_depth=2,
    )

    learning_path = payload["learning_path"]
    returned_parent_ids = {edge["from"] for edge in learning_path if edge["depth"] == 1}
    assert len(learning_path) == 64
    assert direct_ids <= returned_parent_ids
    assert payload["summary"]["learning_path_count"] == 75
    assert payload["summary"]["learning_path_total_count"] == 75
    assert payload["summary"]["learning_path_returned_count"] == 64
    assert payload["summary"]["learning_path_truncated"] is True

    prerequisite_items = payload["relation_groups"]["prerequisite"]["items"]
    assert {edge["from"] for edge in prerequisite_items} == {
        edge["from"] for edge in learning_path
    }
    prerequisite_section = next(
        section
        for section in payload["guidance_sections"]
        if section["relation"] == "prerequisite"
    )
    assert prerequisite_section["items"] == prerequisite_items


def test_public_learning_path_prioritizes_direct_edges_and_preserves_remaining_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance = _guidance_module(monkeypatch)
    path = [
        {"from": "ancestor-a", "to": "parent-a", "depth": 2},
        {"from": "parent-a", "to": "target", "depth": 1},
        {"from": "ancestor-b", "to": "parent-b", "depth": 2},
        {"from": "parent-b", "to": "target", "depth": 1},
        {"from": "ancestor-c", "to": "parent-c", "depth": 2},
    ]

    limited = guidance._limit_public_learning_path(path, max_items=4)

    assert [edge["from"] for edge in limited] == [
        "parent-a",
        "parent-b",
        "ancestor-a",
        "ancestor-b",
    ]


def test_relation_groups_do_not_add_edges_outside_the_bounded_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance = _guidance_module(monkeypatch)
    topics = [
        _topic("prerequisite"),
        _topic("application"),
        {
            **_topic("target", prerequisites=[{"id": "prerequisite"}]),
            "related": [{"id": "application", "relation": "application"}],
        },
    ]

    payload = guidance.build_knowledge_guidance_payload(topics=topics, topic_id="target")
    path_edges = {
        (edge["from"], edge["to"], edge["relation"])
        for edge in payload["learning_path"]
    }
    grouped_edges = {
        (edge["from"], edge["to"], edge["relation"])
        for group in payload["relation_groups"].values()
        for edge in group["items"]
    }

    assert grouped_edges <= path_edges
