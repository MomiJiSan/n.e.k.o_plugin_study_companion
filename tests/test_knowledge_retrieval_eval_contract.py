from __future__ import annotations

import knowledge_retrieval_eval as retrieval_eval


def _payload(
    *,
    edges: list[dict[str, str]],
    node_ids: list[str],
    relationship: dict[str, object] | None = None,
) -> dict[str, object]:
    model_context: dict[str, object] = {"prerequisites": ["主题 A"]}
    if relationship is not None:
        model_context["relationship"] = relationship
    return {
        "topic": {"id": "topic_a"},
        "matches": [],
        "relevant_subgraph": {
            "nodes": [
                {
                    "id": node_id,
                    "label": {"topic_a": "主题 A", "topic_b": "主题 B"}.get(
                        node_id, node_id
                    ),
                    "subject": {
                        "topic_a": "数学",
                        "topic_b": "物理",
                        "other_a": "化学",
                        "other_b": "地理",
                    }.get(node_id, "化学"),
                }
                for node_id in node_ids
            ],
            "edges": edges,
        },
        "model_context": model_context,
        "relation_groups": {},
    }


def test_strict_contract_accepts_bounded_path_and_context(monkeypatch) -> None:
    path = [
        {"from": "topic_a", "to": "middle", "relation": "application"},
        {"from": "middle", "to": "topic_b", "relation": "procedure_step"},
    ]
    monkeypatch.setattr(
        retrieval_eval,
        "build_knowledge_guidance_payload",
        lambda **_kwargs: _payload(
            edges=path,
            node_ids=["topic_a", "middle", "topic_b"],
            relationship={
                "endpoints": [{"id": "topic_a"}, {"id": "topic_b"}],
                "path": path,
                "hop_count": 2,
            },
        ),
    )
    monkeypatch.setattr(
        retrieval_eval,
        "resolve_relationship_evidence",
        lambda **_kwargs: {
            "resolved": True,
            "relationship_unresolved": False,
            "endpoints": [
                {"id": "topic_a", "label": "主题 A", "subject": "数学"},
                {"id": "topic_b", "label": "主题 B", "subject": "物理"},
            ],
            "path": [
                {
                    "from_id": edge["from"],
                    "to_id": edge["to"],
                    "relation": edge["relation"],
                }
                for edge in path
            ],
        },
    )

    report = retrieval_eval.evaluate_knowledge_retrieval_queries(
        topics=[
            {"id": "topic_a", "name": "主题 A", "subject": "数学"},
            {"id": "topic_b", "name": "主题 B", "subject": "物理"},
        ],
        cases=[
            {
                "query": "主题 A 和主题 B 有什么关系？",
                "query_mode": "relationship",
                "expected_topic_ids": ["topic_a", "topic_b"],
                "expected_paths": [
                    {
                        "from": "topic_a",
                        "to": "topic_b",
                        "max_hops": 2,
                        "allowed_relations": ["application", "procedure_step"],
                    }
                ],
                "expect_cross_subject": True,
            }
        ],
        strict=True,
    )

    result = report["results"][0]
    assert result["endpoint_pair_hit"] is True
    assert result["expected_path_hit"] is True
    assert result["context_path_hit"] is True
    assert result["relationship_unresolved"] is False
    assert result["strict_passed"] is True


def test_known_no_path_requires_fail_closed_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        retrieval_eval,
        "build_knowledge_guidance_payload",
        lambda **_kwargs: _payload(edges=[], node_ids=["topic_a"]),
    )
    monkeypatch.setattr(
        retrieval_eval,
        "resolve_relationship_evidence",
        lambda **_kwargs: {"resolved": False, "relationship_unresolved": True},
    )

    report = retrieval_eval.evaluate_knowledge_retrieval_queries(
        topics=[
            {"id": "topic_a", "name": "主题 A", "subject": "数学"},
            {"id": "topic_b", "name": "主题 B", "subject": "物理"},
        ],
        cases=[
            {
                "query": "主题 A 和主题 B 有什么关系？",
                "query_mode": "relationship",
                "expected_topic_ids": ["topic_a", "topic_b"],
                "expect_cross_subject": True,
                "known_no_path": {
                    "from": "topic_a",
                    "to": "topic_b",
                    "max_hops": 3,
                    "allowed_relations": ["application", "prerequisite"],
                },
            }
        ],
        strict=True,
    )

    result = report["results"][0]
    assert result["relationship_unresolved"] is True
    assert result["known_no_path_confirmed"] is True
    assert result["strict_passed"] is True


def test_known_no_path_rejects_unrelated_cross_subject_edge(monkeypatch) -> None:
    monkeypatch.setattr(
        retrieval_eval,
        "build_knowledge_guidance_payload",
        lambda **_kwargs: _payload(
            edges=[{"from": "other_a", "to": "other_b", "relation": "application"}],
            node_ids=["topic_a", "other_a", "other_b"],
        ),
    )
    monkeypatch.setattr(
        retrieval_eval,
        "resolve_relationship_evidence",
        lambda **_kwargs: {"resolved": False, "relationship_unresolved": True},
    )

    report = retrieval_eval.evaluate_knowledge_retrieval_queries(
        topics=[
            {"id": "topic_a", "name": "主题 A", "subject": "数学"},
            {"id": "topic_b", "name": "主题 B", "subject": "物理"},
        ],
        cases=[
            {
                "query": "主题 A 和主题 B 有什么关系？",
                "query_mode": "relationship",
                "expected_topic_ids": ["topic_a", "topic_b"],
                "expect_cross_subject": True,
                "known_no_path": {
                    "from": "topic_a",
                    "to": "topic_b",
                    "max_hops": 3,
                    "allowed_relations": ["application", "prerequisite"],
                },
            }
        ],
        strict=True,
    )

    result = report["results"][0]
    assert result["legacy_unrelated_cross_edge_only"] is True
    assert result["unrelated_cross_edge_only"] is False
    assert result["known_no_path_confirmed"] is True
    assert result["strict_passed"] is True


def test_relationship_eval_forwards_declared_semantic_route(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        retrieval_eval,
        "build_knowledge_guidance_payload",
        lambda **_kwargs: _payload(edges=[], node_ids=["topic_a"]),
    )

    def resolve(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"resolved": False, "relationship_unresolved": True}

    monkeypatch.setattr(retrieval_eval, "resolve_relationship_evidence", resolve)
    retrieval_eval.evaluate_knowledge_retrieval_queries(
        topics=[
            {"id": "topic_a", "name": "主题 A", "subject": "数学"},
            {"id": "topic_b", "name": "主题 B", "subject": "物理"},
        ],
        cases=[
            {
                "query": "主题 A 和主题 B 有什么关系？",
                "query_mode": "relationship",
                "retrieval_concepts": ["主题 A", "主题 B"],
                "primary_subject": "数学",
                "expected_topic_ids": ["topic_a", "topic_b"],
            }
        ],
    )

    assert captured["retrieval_concepts"] == ["主题 A", "主题 B"]
    assert captured["primary_subject"] == "数学"
