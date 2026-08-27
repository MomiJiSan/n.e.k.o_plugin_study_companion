from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _guidance_module(monkeypatch: pytest.MonkeyPatch):
    package_name = "_knowledge_retrieval_ranking_test"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    return importlib.import_module(f"{package_name}.knowledge_graph_guidance")


def _bundled_topics_and_cases() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    manifest = json.loads(
        (ROOT / "static" / "knowledge_graph_seed.json").read_text(encoding="utf-8")
    )
    topics: list[dict[str, object]] = []
    for item in manifest["files"]:
        payload = json.loads((ROOT / "static" / item["path"]).read_text(encoding="utf-8"))
        topics.extend(payload["topics"])
    cases = json.loads(
        (ROOT / "static" / "knowledge_eval_cases.json").read_text(encoding="utf-8")
    )["cases"]
    return topics, cases


def test_query_only_retrieval_eval_cases_all_select_expected_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock the 130-case query-only ranking suite independently of routing."""
    guidance = _guidance_module(monkeypatch)
    topics, cases = _bundled_topics_and_cases()

    failures = []
    for case in cases:
        matches = guidance.match_topics(topics, query=str(case["query"]), limit=5)
        selected_id = str(matches[0]["id"]) if matches else ""
        expected_ids = {str(topic_id) for topic_id in case["expected_topic_ids"]}
        if selected_id not in expected_ids:
            failures.append((case["query"], selected_id, sorted(expected_ids)))

    assert failures == []


def test_complete_compound_concepts_outrank_short_cjk_substrings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance = _guidance_module(monkeypatch)
    topics, _cases = _bundled_topics_and_cases()
    query = "协方差和相关系数有什么区别？"

    terms = guidance._query_terms(query)
    matches = guidance.match_topics(topics, query=query, limit=3)

    assert "数" not in terms
    assert "相" not in terms
    assert matches[0]["id"] == "college_covariance_correlation"


@pytest.mark.parametrize("query", ("数", "相", "帮我出一道题", "create a question"))
def test_single_cjk_and_generic_generation_intent_do_not_match_topics(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    guidance = _guidance_module(monkeypatch)
    topics, _cases = _bundled_topics_and_cases()

    assert guidance.match_topics(topics, query=query, limit=3) == []
