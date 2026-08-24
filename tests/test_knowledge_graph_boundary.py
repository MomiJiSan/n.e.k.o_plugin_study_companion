from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module(monkeypatch: pytest.MonkeyPatch, module: str):
    package_name = f"_study_companion_{module}_boundary_test"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    return importlib.import_module(f"{package_name}.{module}")


def _seed_topics() -> list[dict[str, object]]:
    manifest = json.loads(
        (ROOT / "static" / "knowledge_graph_seed.json").read_text(encoding="utf-8")
    )
    topics: list[dict[str, object]] = []
    for item in manifest["files"]:
        payload = json.loads((ROOT / "static" / item["path"]).read_text(encoding="utf-8"))
        topics.extend(payload["topics"])
    return topics


def test_map_scope_closure_never_returns_dangling_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui_api = _load_module(monkeypatch, "ui_api")
    topics = _seed_topics()
    scope_ids = {str(topic["id"]) for topic in topics if topic.get("stage") == "junior_high"}

    payload = ui_api.build_knowledge_map_payload(
        topics=topics,
        scope_topic_ids=scope_ids,
        max_boundary_nodes=200,
    )

    node_ids = {node["id"] for node in payload["nodes"]}
    assert payload["summary"]["scope_topic_count"] == len(scope_ids)
    assert payload["summary"]["boundary_node_count"] > 0
    assert all(
        edge["from"] in node_ids and edge["to"] in node_ids
        for edge in payload["edges"]
    )
    assert any(node["boundary"] and not node["in_scope"] for node in payload["nodes"])


def test_canonical_edges_preserve_supports_and_single_prerequisite_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edges_module = _load_module(monkeypatch, "knowledge_graph_edges")
    topics = _seed_topics()
    edges = edges_module.build_topic_edges(topics)

    assert any(edge["relation"] == "supports" for edge in edges)
    grouping_edges = [
        edge
        for edge in edges
        if {edge["from"], edge["to"]}
        == {"factorization_common", "factorization_grouping"}
        and edge["relation"] == "prerequisite"
    ]
    assert grouping_edges == [
        edge
        for edge in grouping_edges
        if edge["from"] == "factorization_common"
        and edge["to"] == "factorization_grouping"
    ]
    assert len(grouping_edges) == 1
