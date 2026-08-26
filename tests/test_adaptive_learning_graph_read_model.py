from __future__ import annotations

import pytest
from adaptive_learning.graph_read_model import MapCursorError, query_map_page


def _topic(topic_id: str, *, stage: str = "senior_high", subject: str = "math"):
    return {
        "id": topic_id,
        "name": topic_id,
        "stage": stage,
        "subject": subject,
        "course_family": subject,
        "chapter": "chapter",
        "unit": "unit",
        "depth": 1,
    }


def test_query_map_page_has_scope_bound_cursor_and_true_total() -> None:
    topics = [_topic(f"topic-{index:04d}") for index in range(1001)]
    first = query_map_page(
        topics=topics,
        edges=[],
        scope={"stage": "senior-high", "subject": "math"},
        page_size=200,
    )

    assert first["scope_total_count"] == 1001
    assert first["scope_returned_count"] == 200
    assert first["has_more"] is True
    assert first["next_cursor"]

    second = query_map_page(
        topics=topics,
        edges=[],
        scope={"stage": "senior_high", "subject": "math"},
        page_size=200,
        cursor=first["next_cursor"],
    )
    assert second["nodes"][0]["id"] == "topic-0200"
    assert second["scope_total_count"] == 1001


def test_query_map_page_keeps_only_page_incident_boundary_edges() -> None:
    topics = [_topic("in-scope"), _topic("boundary", subject="physics"), _topic("other", subject="physics")]
    edges = [
        {"from": "in-scope", "to": "boundary", "relation": "prerequisite"},
        {"from": "boundary", "to": "other", "relation": "related"},
    ]

    page = query_map_page(
        topics=topics,
        edges=edges,
        scope={"stage": "senior_high", "subject": "math"},
        include_boundary=True,
    )

    assert {node["id"] for node in page["nodes"]} == {"in-scope", "boundary"}
    assert page["nodes"][0]["in_scope"] is True
    assert page["nodes"][1]["boundary"] is True
    assert page["edges"] == [edges[0]]


def test_query_map_page_rejects_cursor_for_another_scope() -> None:
    topics = [_topic("one"), _topic("two")]
    page = query_map_page(
        topics=topics,
        edges=[],
        scope={"stage": "senior_high", "subject": "math"},
        page_size=1,
    )
    cursor = page["next_cursor"]
    assert cursor

    with pytest.raises(MapCursorError):
        query_map_page(
            topics=topics,
            edges=[],
            scope={"stage": "senior_high", "subject": "physics"},
            cursor=cursor,
        )
