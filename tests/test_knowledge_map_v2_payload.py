from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_ui_api(monkeypatch: pytest.MonkeyPatch):
    package_name = f"_study_companion_map_v2_payload_test_{id(monkeypatch)}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    return importlib.import_module(f"{package_name}.ui_api")


def _topic(topic_id: str, *, unit: str = "one") -> dict[str, object]:
    return {
        "id": topic_id,
        "name": topic_id.title(),
        "stage": "senior_high",
        "subject": "math",
        "chapter": "algebra",
        "unit": unit,
        "skills": [],
        "question_types": [],
    }


def test_v2_payload_reports_total_cursor_and_explicit_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui_api = _load_ui_api(monkeypatch)
    payload = ui_api.build_knowledge_map_page_payload(
        page_topics=[_topic("one"), _topic("two")],
        boundary_topics=[_topic("outside", unit="two")],
        edges=[
            {"from": "one", "to": "two", "relation": "procedure_step"},
            {"from": "two", "to": "outside", "relation": "application"},
            {"from": "outside", "to": "missing", "relation": "application"},
        ],
        mastery_overview=[{"topic_id": "one", "mastery": 0.9, "flags": []}],
        wrong_questions=[{"topic_id": "two", "status": "active"}],
        scope={"stage": "senior_high", "subject": "math"},
        scope_total_count=1_001,
        has_more=True,
        next_cursor="cursor-2",
        boundary_truncated=True,
        catalog_revision="catalog-v1",
    )

    assert payload["schema_version"] == 2
    assert payload["scope_total_count"] == 1_001
    assert payload["scope_returned_count"] == 2
    assert payload["has_more"] is True
    assert payload["next_cursor"] == "cursor-2"
    assert payload["catalog_revision"] == "catalog-v1"
    assert payload["boundary"] == {"returned_count": 1, "truncated": True}
    assert {node["id"] for node in payload["nodes"]} == {"one", "two", "outside"}
    assert next(node for node in payload["nodes"] if node["id"] == "outside")["boundary"] is True
    assert next(node for node in payload["nodes"] if node["id"] == "two")["weak"] is True
    assert {(edge["from"], edge["to"]) for edge in payload["edges"]} == {
        ("one", "two"),
        ("two", "outside"),
    }
