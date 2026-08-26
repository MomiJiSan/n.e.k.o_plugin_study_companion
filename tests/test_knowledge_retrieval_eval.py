from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import knowledge_retrieval_eval as retrieval_eval


def _payload(*, edges: list[dict[str, str]], node_ids: list[str]) -> dict[str, object]:
    return {
        "topic": {"id": "topic_a"},
        "matches": [],
        "relevant_subgraph": {
            "nodes": [
                {
                    "id": node_id,
                    "label": {
                        "topic_a": "主题 A",
                        "topic_b": "主题 B",
                        "other_a": "其他 A",
                        "other_b": "其他 B",
                    }.get(node_id, node_id),
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
        "model_context": {"prerequisites": ["主题 A 是主题 B 的基础"]},
        "relation_groups": {"prerequisite": {"items": ["主题 A"]}},
    }


def test_shadow_metrics_require_the_explicit_expected_link(monkeypatch) -> None:
    monkeypatch.setattr(
        retrieval_eval,
        "build_knowledge_guidance_payload",
        lambda **_kwargs: _payload(
            edges=[{"from": "topic_a", "to": "topic_b", "relation": "application"}],
            node_ids=["topic_a", "topic_b"],
        ),
    )

    report = retrieval_eval.evaluate_knowledge_retrieval_queries(
        topics=[
            {"id": "topic_a", "name": "主题 A", "subject": "数学"},
            {"id": "topic_b", "name": "主题 B", "subject": "物理"},
        ],
        cases=[
            {
                "query": "测试",
                "expected_topic_ids": ["topic_a", "topic_b"],
                "expected_links": [
                    {
                        "from": "topic_a",
                        "to": "topic_b",
                        "relations": ["application", "supports"],
                    }
                ],
                "expect_cross_subject": True,
                "require_model_context_cue": True,
            }
        ],
    )

    result = report["results"][0]
    assert result["passed"] is True
    assert result["focus_hit"] is True
    assert result["expected_link_hit"] is True
    assert result["expected_endpoint_returned"] is True
    assert result["model_context_cue_hit"] is True
    assert result["unrelated_cross_edge_only"] is False


def test_shadow_metrics_identify_an_unrelated_cross_subject_edge(monkeypatch) -> None:
    monkeypatch.setattr(
        retrieval_eval,
        "build_knowledge_guidance_payload",
        lambda **_kwargs: _payload(
            edges=[{"from": "other_a", "to": "other_b", "relation": "application"}],
            node_ids=["topic_a", "topic_b", "other_a", "other_b"],
        ),
    )

    report = retrieval_eval.evaluate_knowledge_retrieval_queries(
        topics=[
            {"id": "topic_a", "name": "主题 A", "subject": "数学"},
            {"id": "topic_b", "name": "主题 B", "subject": "物理"},
        ],
        cases=[
            {
                "query": "测试",
                "expected_topic_ids": ["topic_a", "topic_b"],
                "expected_links": [
                    {
                        "from": "topic_a",
                        "to": "topic_b",
                        "relations": ["application"],
                    }
                ],
                "expect_cross_subject": True,
                "require_model_context_cue": True,
            }
        ],
    )

    result = report["results"][0]
    # The legacy gate stays intentionally unchanged for this PR.
    assert result["passed"] is True
    assert result["expected_link_hit"] is False
    assert result["legacy_unrelated_cross_edge_only"] is True
    assert result["unrelated_cross_edge_only"] is False


def test_direct_script_cli_imports_from_the_repository_root(tmp_path: Path) -> None:
    seed = tmp_path / "seed.json"
    cases = tmp_path / "cases.json"
    seed.write_text(
        json.dumps(
            {"topics": [{"id": "topic_a", "name": "主题 A", "subject": "数学"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cases.write_text(
        json.dumps(
            {"cases": [{"query": "主题 A", "expected_topic_ids": ["topic_a"]}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[1] / "knowledge_retrieval_eval.py"

    completed = subprocess.run(
        [sys.executable, str(script), str(cases), "--seed", str(seed)],
        check=False,
        cwd=script.parent,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["summary"]["case_count"] == 1
