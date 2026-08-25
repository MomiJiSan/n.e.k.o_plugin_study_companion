from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _context_support_module(monkeypatch: pytest.MonkeyPatch):
    """Import the route in isolation from the plugin host runtime."""
    package_name = "_production_knowledge_routing_test"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    common = ModuleType(f"{package_name}.entry_common")
    stubs: dict[str, Any] = {
        "LLM_OPERATION_KNOWLEDGE_TRACK": "knowledge_track",
        "LLM_OPERATION_SUMMARIZE_SESSION": "summary",
        "LLM_OPERATION_CONCEPT_EXPLAIN": "concept_explain",
        "LLM_OPERATION_ANSWER_EVALUATE": "answer_evaluate",
        "LLM_OPERATION_QUESTION_GENERATE": "question_generate",
        "Any": Any,
        "asyncio": asyncio,
        "SdkError": Exception,
        "StudyEvent": object,
        "TutorReply": object,
        "_detect_mastery_threshold_crossed": lambda *_args: None,
        "_plugin_lock": None,
        "build_tutor_payload": lambda *_args, **_kwargs: {},
        "time": time,
        "utc_now_iso": lambda: "",
    }
    for name, value in stubs.items():
        setattr(common, name, value)
    monkeypatch.setitem(sys.modules, common.__name__, common)

    models = ModuleType(f"{package_name}.models")
    models.public_current_question_payload = lambda *_args, **_kwargs: {}
    monkeypatch.setitem(sys.modules, models.__name__, models)
    target_binding = ModuleType(f"{package_name}.target_binding")
    target_binding.resolve_existing_target_topic_id = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, target_binding.__name__, target_binding)
    return importlib.import_module(f"{package_name}.entry_tutor_context_support")


class _Store:
    def __init__(self, topics: list[dict[str, object]]) -> None:
        self._topics = topics

    def list_topics(self, *_args: object) -> list[dict[str, object]]:
        return self._topics


def _topic(topic_id: str, subject: str, **values: object) -> dict[str, object]:
    return {
        "id": topic_id,
        "name": topic_id,
        "subject": subject,
        "stage": "college",
        "chapter": "routing",
        "unit": "routing",
        "depth": 2,
        "difficulty": 0.5,
        "prerequisites": [],
        "related": [],
        **values,
    }


def _bundled_topics() -> list[dict[str, object]]:
    manifest = json.loads(
        (ROOT / "static" / "knowledge_graph_seed.json").read_text(encoding="utf-8")
    )
    topics: list[dict[str, object]] = []
    for item in manifest["files"]:
        payload = json.loads((ROOT / "static" / item["path"]).read_text(encoding="utf-8"))
        topics.extend(payload["topics"])
    return topics


def _has_cross_subject_edge(
    payload: dict[str, object], topic_subjects: dict[str, str]
) -> bool:
    subgraph = payload.get("relevant_subgraph")
    if not isinstance(subgraph, dict):
        return False
    for edge in subgraph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        if topic_subjects.get(str(edge.get("from"))) not in {
            "",
            topic_subjects.get(str(edge.get("to"))),
        }:
            return True
    return False


@pytest.mark.parametrize(
    "query",
    ("帮我出一道题", "请根据这段内容生题", "给我一个练习"),
)
def test_generic_generation_intent_does_not_match_a_topic(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    support = _context_support_module(monkeypatch)
    assert support.match_topics(_bundled_topics(), query=query, limit=3) == []


def test_generation_intent_keeps_meaningful_topic_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _context_support_module(monkeypatch)
    matches = support.match_topics(
        _bundled_topics(), query="请根据函数连续性内容出题", limit=3
    )
    assert matches
    assert matches[0]["id"] == "college_continuity"


def _context_has_cross_subject_cue(
    payload: dict[str, object],
    topic_labels: dict[str, str],
    topic_subjects: dict[str, str],
) -> bool:
    subgraph = payload.get("relevant_subgraph")
    model_context = payload.get("model_context")
    if not isinstance(subgraph, dict) or not isinstance(model_context, dict):
        return False
    compact = "\n".join(
        str(value)
        for key in (
            "prerequisites",
            "procedure",
            "confusions",
            "applications",
            "extensions",
            "review_with",
            "practice_suggestions",
        )
        for value in model_context.get(key, [])
        if isinstance(model_context.get(key), list)
    )
    for edge in subgraph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        from_id, to_id = str(edge.get("from")), str(edge.get("to"))
        if not topic_subjects.get(from_id) or topic_subjects.get(from_id) == topic_subjects.get(to_id):
            continue
        if topic_labels.get(from_id, "") in compact or topic_labels.get(to_id, "") in compact:
            return True
    return False


def test_semantic_focus_keeps_cross_subject_graph_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _context_support_module(monkeypatch)
    topics = [
        _topic(
            "mathfocus",
            "math",
            related=[{"id": "physicscontext", "relation": "application"}],
        ),
        _topic("physicscontext", "physics"),
    ]

    class Harness(support._TutorContextSupportMixin):
        def __init__(self) -> None:
            self._store = _Store(topics)
            self._knowledge_guidance_topics_cache: dict[str, object] = {}
            self.logger = None

        async def _route_study_input_semantics(self, *_args: object, **_kwargs: object):
            return (
                support.StudyInputSemantics(
                    subject="math",
                    content_type="concept",
                    intent="explain",
                    response_mode="general_explanation",
                    entity="mathfocus",
                    retrieval_concepts=("mathfocus",),
                    confidence=1.0,
                ),
                "available",
                "",
            )

    guidance, outcome = asyncio.run(
        Harness()._build_knowledge_guidance_context(
            "concept_explain", input_text="mathfocus", context={}
        )
    )

    assert outcome["knowledge_guidance_status"] == "applied"
    assert guidance["topic"]["id"] == "mathfocus"
    assert {
        (edge["from"], edge["to"], edge["relation"])
        for edge in guidance["relevant_subgraph"]["edges"]
    } == {("mathfocus", "physicscontext", "application")}
    assert "physicscontext" in guidance["model_context"]["applications"]


@pytest.mark.parametrize(
    ("subject", "confidence", "expected_status"),
    [("math", 0.5, "low_confidence"), ("unknown", 1.0, "not_matched")],
)
def test_semantic_route_low_confidence_and_unknown_remain_unapplied(
    monkeypatch: pytest.MonkeyPatch,
    subject: str,
    confidence: float,
    expected_status: str,
) -> None:
    support = _context_support_module(monkeypatch)

    class Harness(support._TutorContextSupportMixin):
        def __init__(self) -> None:
            self._store = _Store([_topic("mathfocus", "math")])
            self._knowledge_guidance_topics_cache: dict[str, object] = {}
            self.logger = None

        async def _route_study_input_semantics(self, *_args: object, **_kwargs: object):
            return (
                support.StudyInputSemantics(
                    subject=subject,
                    content_type="concept",
                    intent="explain",
                    response_mode="general_explanation",
                    entity="",
                    retrieval_concepts=(),
                    confidence=confidence,
                ),
                "available",
                "",
            )

    guidance, outcome = asyncio.run(
        Harness()._build_knowledge_guidance_context(
            "concept_explain", input_text="unmatched", context={}
        )
    )

    assert guidance == {}
    assert outcome["knowledge_guidance_status"] == expected_status


def test_bundled_production_equivalent_semantic_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the actual semantic-focus route with deterministic semantics."""
    support = _context_support_module(monkeypatch)
    topics = _bundled_topics()
    cases = json.loads(
        (ROOT / "static" / "knowledge_eval_cases.json").read_text(encoding="utf-8")
    )["cases"]
    topics_by_id = {str(topic["id"]): topic for topic in topics}
    topic_subjects = {topic_id: str(topic["subject"]) for topic_id, topic in topics_by_id.items()}
    topic_labels = {topic_id: str(topic.get("name") or topic_id) for topic_id, topic in topics_by_id.items()}
    route_subjects: dict[str, str] = {}
    for case in cases:
        query = str(case["query"])
        expected_ids = {str(topic_id) for topic_id in case["expected_topic_ids"]}
        for subject in sorted({topic_subjects[topic_id] for topic_id in expected_ids}):
            matches = support.match_topics(topics, query=query, subject=subject, limit=5)
            if not matches or int(matches[0].get("score") or 0) < 10:
                continue
            candidate = support.build_knowledge_guidance_payload(
                topics=topics,
                topic_id=str(matches[0]["id"]),
                query=query,
                response_mode="general_explanation",
                max_depth=3,
                match_limit=5,
            )
            focus = str(candidate.get("topic", {}).get("id") or "")
            if focus not in expected_ids:
                continue
            if case.get("expect_cross_subject") and not _has_cross_subject_edge(
                candidate, topic_subjects
            ):
                continue
            route_subjects[query] = subject
            break
        assert query in route_subjects

    class Harness(support._TutorContextSupportMixin):
        def __init__(self) -> None:
            self._store = _Store(topics)
            self._knowledge_guidance_topics_cache: dict[str, object] = {}
            self.logger = None

        async def _route_study_input_semantics(self, query: str, **_kwargs: object):
            return (
                support.StudyInputSemantics(
                    subject=route_subjects[query],
                    content_type="",
                    intent="",
                    response_mode="general_explanation",
                    entity=query,
                    retrieval_concepts=(),
                    confidence=1.0,
                ),
                "available",
                "",
            )

    async def build_all() -> list[tuple[dict[str, object], dict[str, object], dict[str, object]]]:
        harness = Harness()
        results = []
        for case in cases:
            guidance, outcome = await harness._build_knowledge_guidance_context(
                "concept_explain", input_text=str(case["query"]), context={}
            )
            results.append((case, guidance, outcome))
        return results

    results = asyncio.run(build_all())
    focus_hits = 0
    cross_edges = 0
    cross_cues = 0
    raw_seed = 0
    for case, guidance, outcome in results:
        assert outcome["knowledge_guidance_status"] == "applied"
        focus = str(guidance["topic"]["id"])
        focus_hits += int(focus in set(case["expected_topic_ids"]))
        if case.get("expect_cross_subject"):
            cross_edges += int(_has_cross_subject_edge(guidance, topic_subjects))
            cross_cues += int(
                _context_has_cross_subject_cue(guidance, topic_labels, topic_subjects)
            )
        raw_seed += int(
            any(
                key in guidance["model_context"]
                for key in ("topics", "nodes", "edges", "matches", "relation_groups")
            )
        )

    assert len(results) == 130
    assert focus_hits == 130
    assert cross_edges == 68
    assert cross_cues >= 56
    assert raw_seed == 0
