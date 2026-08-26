from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _guidance(monkeypatch: pytest.MonkeyPatch, name: str):
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    return importlib.import_module(f"{name}.knowledge_graph_guidance")


def _seed_topics() -> list[dict[str, object]]:
    manifest = json.loads(
        (ROOT / "static" / "knowledge_graph_seed.json").read_text(encoding="utf-8")
    )
    topics: list[dict[str, object]] = []
    for item in manifest["files"]:
        payload = json.loads((ROOT / "static" / item["path"]).read_text(encoding="utf-8"))
        topics.extend(payload["topics"])
    return topics


def _topics() -> list[dict[str, object]]:
    return [
        {"id": "ancestor", "name": "Ancestor"},
        {"id": "base", "name": "Base", "prerequisites": ["ancestor"]},
        {"id": "pre-a", "name": "A prerequisite"},
        {"id": "pre-b", "name": "B prerequisite"},
        {"id": "pre-c", "name": "C prerequisite"},
        {"id": "pre-d", "name": "D prerequisite"},
        {"id": "procedure", "name": "Procedure", "related": [{"id": "target", "relation": "procedure_step"}]},
        {"id": "application", "name": "Application"},
        {"id": "extension", "name": "Extension"},
        {"id": "confusion", "name": "Confusion"},
        {"id": "review", "name": "Review"},
        {"id": "next", "name": "Next"},
        {"id": "wrong-way", "name": "Wrong direction", "related": [{"id": "target", "relation": "application"}]},
        {
            "id": "target",
            "name": "Target",
            "prerequisites": ["base", "pre-d", "pre-b", "pre-a", "pre-c"],
            "related": [
                {"id": "application", "relation": "application"},
                {"id": "extension", "relation": "extends"},
                {"id": "confusion", "relation": "confusable"},
                {"id": "review", "relation": "co_occurs"},
                {"id": "next", "relation": "next"},
                {"id": "target", "relation": "confusable"},
            ],
        },
    ]


def _diagnosis(payload: dict[str, object]) -> list[dict[str, object]]:
    return list(payload["diagnosis_questions"])  # type: ignore[arg-type]


def test_public_diagnosis_uses_only_direct_canonical_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance = _guidance(monkeypatch, "_direct_diagnosis_test")
    payload = guidance.build_knowledge_guidance_payload(
        topics=_topics(), topic_id="target", query="target"
    )
    diagnosis = _diagnosis(payload)

    assert [item["topic_id"] for item in diagnosis if item["kind"] == "prerequisite_probe"] == [
        "pre-a",
        "pre-b",
    ]
    assert {item["topic_id"] for item in diagnosis} <= {
        "pre-a",
        "pre-b",
        "base",
        "procedure",
        "application",
        "extension",
        "confusion",
        "review",
        "next",
    }
    assert "ancestor" not in {item["topic_id"] for item in diagnosis}
    assert "wrong-way" not in {item["topic_id"] for item in diagnosis}
    assert "target" not in {item["topic_id"] for item in diagnosis}
    assert {(item["kind"], item["relation"]) for item in diagnosis} == {
        ("prerequisite_probe", "prerequisite"),
        ("procedure_probe", "procedure_step"),
        ("application_practice", "application"),
        ("extension_suggestion", "extends"),
        ("confusion_check", "confusable"),
        ("related_review", "co_occurs"),
        ("next_step", "next"),
    }


def test_public_diagnosis_order_is_stable_when_seed_order_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance = _guidance(monkeypatch, "_direct_diagnosis_order_test")
    first = guidance.build_knowledge_guidance_payload(
        topics=_topics(), topic_id="target", query="target"
    )
    second = guidance.build_knowledge_guidance_payload(
        topics=list(reversed(_topics())), topic_id="target", query="target"
    )
    assert _diagnosis(first) == _diagnosis(second)


def test_direct_diagnosis_evidence_respects_incidence_and_direction_globally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_name = "_direct_diagnosis_full_graph_test"
    guidance = _guidance(monkeypatch, package_name)
    index_module = importlib.import_module(f"{package_name}.knowledge_graph_index")
    graph = index_module.KnowledgeGraphIndex(_seed_topics())
    assert len(graph.by_id) == 892
    assert len(graph.edges) == 4872

    allowed = {
        "prerequisite",
        "procedure_step",
        "application",
        "extends",
        "next",
        "confusable",
        "co_occurs",
    }
    for selected_id in graph.by_id:
        evidence = guidance._direct_diagnosis_edges(
            selected_id=selected_id,
            by_id=graph.by_id,
            incoming=graph.incoming_edges,
            outgoing=graph.outgoing_edges,
        )
        actual = {
            (edge["from"], edge["to"], edge["relation"])
            for edge in evidence
        }
        expected = set()
        for edge in [
            *(graph.incoming_edges.get(selected_id) or []),
            *(graph.outgoing_edges.get(selected_id) or []),
        ]:
            source_id = str(edge["from"])
            target_id = str(edge["to"])
            relation = str(edge["relation"])
            if (
                relation not in allowed
                or source_id == target_id
                or selected_id not in {source_id, target_id}
            ):
                continue
            if relation in {"prerequisite", "procedure_step"} and target_id != selected_id:
                continue
            if relation in {"application", "extends", "next"} and source_id != selected_id:
                continue
            expected.add((source_id, target_id, relation))
        assert actual == expected
