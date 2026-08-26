from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _package(monkeypatch: pytest.MonkeyPatch, name: str) -> str:
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    return name


def _modules(monkeypatch: pytest.MonkeyPatch, name: str):
    package = _package(monkeypatch, name)
    guidance = importlib.import_module(f"{package}.knowledge_graph_guidance")
    index = importlib.import_module(f"{package}.knowledge_graph_index")
    return guidance, index


def _seed_topics() -> list[dict[str, object]]:
    manifest = json.loads(
        (ROOT / "static" / "knowledge_graph_seed.json").read_text(encoding="utf-8")
    )
    topics: list[dict[str, object]] = []
    for item in manifest["files"]:
        payload = json.loads((ROOT / "static" / item["path"]).read_text(encoding="utf-8"))
        topics.extend(payload["topics"])
    return topics


def _labels(
    graph, edges: list[dict[str, object]], *, incoming: bool
) -> list[str]:
    values = []
    for edge in edges:
        other_id = str(edge["from"] if incoming else edge["to"])
        topic = graph.by_id[other_id]
        values.append((other_id, str(topic.get("name") or topic.get("title") or other_id)))
    seen: set[str] = set()
    labels: list[str] = []
    for _topic_id, label in sorted(values, key=lambda item: (item[1].casefold(), item[0])):
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def test_focused_model_context_is_exactly_one_hop_for_every_bundled_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance, index_module = _modules(monkeypatch, "_focused_context_full_graph_test")
    graph = index_module.KnowledgeGraphIndex(_seed_topics())
    assert len(graph.by_id) == 892
    assert len(graph.edges) == 4872

    for selected_id, selected_topic in graph.by_id.items():
        context = guidance._build_focused_model_context(
            selected_id=selected_id,
            by_id=graph.by_id,
            incoming_edges=graph.incoming_edges,
            outgoing_edges=graph.outgoing_edges,
            relevant_subgraph={},
        )
        incoming = list(graph.incoming_edges.get(selected_id) or [])
        outgoing = list(graph.outgoing_edges.get(selected_id) or [])
        expected_prerequisites = _labels(
            graph,
            [edge for edge in incoming if edge.get("relation") == "prerequisite"],
            incoming=True,
        )
        expected_procedure = _labels(
            graph,
            [edge for edge in incoming if edge.get("relation") == "procedure_step"],
            incoming=True,
        )
        expected_applications = _labels(
            graph,
            [edge for edge in outgoing if edge.get("relation") == "application"],
            incoming=False,
        )
        expected_extensions = _labels(
            graph,
            [edge for edge in outgoing if edge.get("relation") == "extends"],
            incoming=False,
        )
        # Symmetric edges can be incident from either side, so choose the
        # opposite endpoint independently for each edge.
        expected_confusions = sorted(
            {
                str(
                    graph.by_id[
                        str(edge["to"] if edge["from"] == selected_id else edge["from"])
                    ].get("name")
                    or graph.by_id[
                        str(edge["to"] if edge["from"] == selected_id else edge["from"])
                    ].get("title")
                    or (edge["to"] if edge["from"] == selected_id else edge["from"])
                )
                for edge in [*incoming, *outgoing]
                if edge.get("relation") == "confusable"
            },
            key=str.casefold,
        )
        expected_review_with = sorted(
            {
                str(
                    graph.by_id[
                        str(edge["to"] if edge["from"] == selected_id else edge["from"])
                    ].get("name")
                    or graph.by_id[
                        str(edge["to"] if edge["from"] == selected_id else edge["from"])
                    ].get("title")
                    or (edge["to"] if edge["from"] == selected_id else edge["from"])
                )
                for edge in [*incoming, *outgoing]
                if edge.get("relation") == "co_occurs"
            },
            key=str.casefold,
        )

        assert context["prerequisites"] == expected_prerequisites
        assert context["procedure"] == expected_procedure
        assert context["applications"] == expected_applications
        assert context["extensions"] == expected_extensions
        assert context["confusions"] == expected_confusions
        assert context["review_with"] == expected_review_with
        assert set(context["summary"]["diagnostics"]) == {
            "guidance_self_relation_dropped",
            "guidance_nonincident_edge_dropped",
            "guidance_direction_mismatch_dropped",
        }


def test_focused_model_context_drops_nonincident_self_and_wrong_direction_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance, _ = _modules(monkeypatch, "_focused_context_defensive_test")
    topics = {
        "target": {"id": "target", "name": "Target"},
        "pre": {"id": "pre", "name": "Prerequisite"},
        "app": {"id": "app", "name": "Application"},
    }
    context = guidance._build_focused_model_context(
        selected_id="target",
        by_id=topics,
        incoming_edges={
            "target": [
                {"from": "pre", "to": "target", "relation": "prerequisite"},
                {"from": "target", "to": "target", "relation": "confusable"},
                {"from": "pre", "to": "app", "relation": "application"},
            ]
        },
        outgoing_edges={
            "target": [
                {"from": "target", "to": "app", "relation": "application"},
                {"from": "target", "to": "pre", "relation": "prerequisite"},
            ]
        },
        relevant_subgraph={},
    )

    assert context["prerequisites"] == ["Prerequisite"]
    assert context["applications"] == ["Application"]
    assert context["confusions"] == []
    assert context["summary"]["diagnostics"] == {
        "guidance_self_relation_dropped": 1,
        "guidance_nonincident_edge_dropped": 1,
        "guidance_direction_mismatch_dropped": 1,
    }


def test_focused_context_and_semantic_evidence_share_all_relation_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance, index_module = _modules(monkeypatch, "_focused_relation_contract_test")
    topics = [
        {"id": "target", "name": "Target", "prerequisites": [{"id": "pre"}], "related": [
            {"id": "app", "relation": "application"},
            {"id": "extension", "relation": "extends"},
            {"id": "next", "relation": "next"},
            {"id": "confusion", "relation": "confusable"},
            {"id": "review", "relation": "co_occurs"},
            {"id": "analogy", "relation": "analogy"},
            {"id": "nearby", "relation": "nearby"},
        ]},
        {"id": "pre", "name": "Prerequisite"},
        {"id": "procedure", "name": "Procedure", "related": [{"id": "target", "relation": "procedure_step"}]},
        {"id": "app", "name": "Application"},
        {"id": "extension", "name": "Extension"},
        {"id": "next", "name": "Next"},
        {"id": "support", "name": "Support", "related": [{"id": "target", "relation": "supports"}]},
        {"id": "confusion", "name": "Confusion"},
        {"id": "review", "name": "Review"},
        {"id": "analogy", "name": "Analogy"},
        {"id": "nearby", "name": "Nearby"},
    ]
    graph = index_module.KnowledgeGraphIndex(topics)
    focused = guidance._build_focused_model_context(
        selected_id="target",
        by_id=graph.by_id,
        incoming_edges=graph.incoming_edges,
        outgoing_edges=graph.outgoing_edges,
        relevant_subgraph={},
    )
    assert focused["prerequisites"] == ["Prerequisite"]
    assert focused["procedure"] == ["Procedure"]
    assert focused["applications"] == ["Application"]
    assert focused["extensions"] == ["Extension"]
    assert focused["supporting_concepts"] == ["Support"]
    assert focused["next_topics"] == ["Next"]
    assert focused["confusions"] == ["Confusion"]
    assert focused["review_with"] == ["Nearby", "Review"]
    assert focused["analogies"] == ["Analogy"]

    canonical = guidance._canonical_necessary_relations(
        topics=topics, topic_id="target"
    )
    assert canonical == {
        key: focused[key]
        for key in (
            "prerequisites",
            "procedure",
            "confusions",
            "applications",
            "extensions",
            "supporting_concepts",
            "review_with",
            "analogies",
            "next_topics",
        )
    }
