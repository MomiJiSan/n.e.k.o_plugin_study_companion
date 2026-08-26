from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _resolver(monkeypatch: pytest.MonkeyPatch, name: str = "relation_retrieval_test"):
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    return importlib.import_module(f"{name}.knowledge_relation_retrieval").resolve_relationship_evidence


def _topic(
    topic_id: str,
    name: str,
    subject: str,
    *,
    aliases: list[str] | None = None,
    related: list[dict[str, object]] | None = None,
    prerequisites: list[object] | None = None,
) -> dict[str, object]:
    return {
        "id": topic_id,
        "name": name,
        "subject": subject,
        "aliases": aliases or [],
        "related": related or [],
        "prerequisites": prerequisites or [],
    }


def test_pair_connectors_resolve_the_same_direct_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve = _resolver(monkeypatch)
    topics = [
        _topic("a", "概念 A", "math", related=[{"id": "b", "relation": "application"}]),
        _topic("b", "概念 B", "physics"),
    ]

    results = [
        resolve(topics=topics, query=f"概念 A{connector}概念 B有什么区别", primary_subject="math")
        for connector in ("和", "与", "跟", "及")
    ]

    assert all(result["resolved"] for result in results)
    assert {tuple(edge["relation"] for edge in result["path"]) for result in results} == {
        ("application",)
    }


def test_compound_correlation_term_is_not_split_by_relationship_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve = _resolver(monkeypatch)
    topics = [
        _topic("covariance", "协方差", "math", related=[{"id": "correlation", "relation": "confusable"}]),
        _topic("correlation", "相关系数", "math"),
        _topic("generic", "系数", "math"),
    ]

    result = resolve(
        topics=topics,
        query="协方差和相关系数有什么区别？",
        primary_subject="math",
    )

    assert result["resolved"] is True
    assert [endpoint["id"] for endpoint in result["endpoints"]] == ["covariance", "correlation"]


def test_relation_intent_can_use_conservative_metadata_mentions_without_router_concepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve = _resolver(monkeypatch)
    topics = [
        _topic(
            "cost",
            "短期成本曲线",
            "economics",
            related=[{"id": "derivative", "relation": "application"}],
        ),
        _topic("derivative", "导数定义", "math"),
    ]
    topics[0]["examples"] = [{"prompt": "边际成本可以用导数理解"}]

    result = resolve(topics=topics, query="边际成本为什么用导数？")

    assert result["is_relationship_query"] is True
    assert result["resolved"] is True
    assert {endpoint["id"] for endpoint in result["endpoints"]} == {"cost", "derivative"}


def test_direct_edge_beats_high_confidence_unrelated_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve = _resolver(monkeypatch)
    topics = [
        _topic("a", "概念 A", "math", related=[{"id": "b", "relation": "application", "confidence": 0.7}]),
        _topic("b", "概念 B", "physics"),
        _topic("other_a", "无关 A", "chemistry", related=[{"id": "other_b", "relation": "prerequisite", "confidence": 1.0}]),
        _topic("other_b", "无关 B", "geography"),
    ]

    result = resolve(
        topics=topics,
        query="概念 A和概念 B的关系",
        primary_subject="math",
    )

    assert result["resolved"] is True
    assert result["hop_count"] == 1
    assert result["path"] == [
        {"from_id": "a", "to_id": "b", "relation": "application", "reason": "", "confidence": 0.7}
    ]


def test_difference_intent_prefers_confusable_direct_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve = _resolver(monkeypatch)
    topics = [
        _topic(
            "a",
            "概念 A",
            "math",
            related=[
                {"id": "b", "relation": "application", "confidence": 1.0},
                {"id": "b", "relation": "confusable", "confidence": 0.7},
            ],
        ),
        _topic("b", "概念 B", "physics"),
    ]

    result = resolve(
        topics=topics,
        query="概念 A和概念 B有什么区别",
        primary_subject="math",
    )

    assert result["resolved"] is True
    assert result["path"][0]["relation"] == "confusable"


@pytest.mark.parametrize("hop_count", [2, 3])
def test_bounded_two_and_three_hop_paths(monkeypatch: pytest.MonkeyPatch, hop_count: int) -> None:
    resolve = _resolver(monkeypatch, f"relation_retrieval_hops_{hop_count}")
    topics = [
        _topic("a", "概念 A", "math", related=[{"id": "middle_1", "relation": "supports"}]),
        _topic("middle_1", "中间一", "chemistry", related=[{"id": "b" if hop_count == 2 else "middle_2", "relation": "application"}]),
        _topic("b", "概念 B", "physics"),
    ]
    if hop_count == 3:
        topics.append(_topic("middle_2", "中间二", "biology", related=[{"id": "b", "relation": "procedure_step"}]))

    result = resolve(
        topics=topics,
        query="概念 A与概念 B的联系",
        primary_subject="math",
    )

    assert result["resolved"] is True
    assert result["hop_count"] == hop_count
    assert len(result["path"]) == hop_count


def test_no_path_fails_closed_without_unrelated_cross_subject_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve = _resolver(monkeypatch)
    topics = [
        _topic("a", "概念 A", "math"),
        _topic("b", "概念 B", "physics"),
        _topic("other_a", "无关 A", "chemistry", related=[{"id": "other_b", "relation": "application"}]),
        _topic("other_b", "无关 B", "geography"),
    ]

    result = resolve(
        topics=topics,
        query="概念 A与概念 B有什么联系",
        primary_subject="math",
    )

    assert result["detected"] is True
    assert result["resolved"] is False
    assert result["unresolved"] is True
    assert result["endpoints"] == []
    assert result["path"] == []


def test_path_preserves_canonical_direction_instead_of_reversing_for_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve = _resolver(monkeypatch)
    topics = [
        _topic("prerequisite", "前置概念", "math"),
        _topic("dependent", "后续概念", "physics", prerequisites=["prerequisite"]),
    ]

    result = resolve(
        topics=topics,
        query="后续概念和前置概念有什么关系",
        primary_subject="physics",
    )

    assert result["resolved"] is True
    assert result["path"] == [
        {"from_id": "prerequisite", "to_id": "dependent", "relation": "prerequisite", "reason": "", "confidence": 0.7}
    ]
