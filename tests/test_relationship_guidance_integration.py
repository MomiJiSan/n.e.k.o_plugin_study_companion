from __future__ import annotations

import asyncio
import importlib
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _support_module(monkeypatch: pytest.MonkeyPatch):
    package_name = "_relationship_guidance_integration_test"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    common = ModuleType(f"{package_name}.entry_common")
    for name, value in {
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
    }.items():
        setattr(common, name, value)
    monkeypatch.setitem(sys.modules, common.__name__, common)

    models = ModuleType(f"{package_name}.models")
    models.public_current_question_payload = lambda *_args, **_kwargs: {}
    monkeypatch.setitem(sys.modules, models.__name__, models)
    target_binding = ModuleType(f"{package_name}.target_binding")
    target_binding.resolve_existing_target_topic_id = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, target_binding.__name__, target_binding)
    guidance = importlib.import_module(f"{package_name}.knowledge_graph_guidance")
    support = importlib.import_module(f"{package_name}.entry_tutor_context_support")
    return guidance, support


def _topics() -> list[dict[str, object]]:
    return [
        {
            "id": "math_left",
            "name": "Left concept",
            "subject": "math",
            "stage": "college",
            "chapter": "relationships",
            "unit": "relationships",
            "depth": 2,
            "difficulty": 0.5,
            "prerequisites": [],
            "related": [{"id": "physics_right", "relation": "application"}],
        },
        {
            "id": "physics_right",
            "name": "Right concept",
            "subject": "physics",
            "stage": "college",
            "chapter": "relationships",
            "unit": "relationships",
            "depth": 2,
            "difficulty": 0.5,
            "prerequisites": [],
            "related": [],
        },
    ]


def _evidence(*, resolved: bool = True) -> dict[str, object]:
    return {
        "is_relationship_query": True,
        "resolved": resolved,
        "relationship_unresolved": not resolved,
        "endpoints": [
            {"id": "math_left", "label": "Left concept", "subject": "math"},
            {
                "id": "physics_right",
                "label": "Right concept",
                "subject": "physics",
            },
        ],
        "path": (
            [
                {
                    "from_id": "math_left",
                    "to_id": "physics_right",
                    "relation": "application",
                    "reason": "canonical relation",
                    "confidence": 1.0,
                }
            ]
            if resolved
            else []
        ),
        "hop_count": 1 if resolved else 0,
    }


def test_relationship_model_context_is_bounded_and_path_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance, _support = _support_module(monkeypatch)
    evidence = _evidence()
    evidence["endpoints"] = [
        *evidence["endpoints"],  # type: ignore[list-item]
        {"id": "ignored", "label": "Ignored", "subject": "history"},
    ]
    evidence["path"] = [
        *evidence["path"],  # type: ignore[list-item]
        {
            "from_id": "unrelated_a",
            "to_id": "unrelated_b",
            "relation": "not_a_relation",
            "reason": "must not be forwarded",
        },
    ]

    context = guidance._build_relationship_model_context(evidence)

    assert context == {
        "relationship": {
            "endpoints": [
                {"id": "math_left", "label": "Left concept", "subject": "math"},
                {
                    "id": "physics_right",
                    "label": "Right concept",
                    "subject": "physics",
                },
            ],
            "path": [
                {
                    "from_id": "math_left",
                    "to_id": "physics_right",
                    "relation": "application",
                    "reason": "canonical relation",
                }
            ],
            "hop_count": 1,
        }
    }
    assert "nodes" not in context
    assert "edges" not in context


def test_relationship_model_context_fails_closed_without_a_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance, _support = _support_module(monkeypatch)
    assert guidance._build_relationship_model_context(_evidence(resolved=False)) == {
        "relationship_unresolved": True
    }


@pytest.mark.parametrize(
    ("enabled", "resolved", "relationship_query"),
    ((False, True, True), (True, True, True), (True, False, True), (True, True, False)),
)
def test_relationship_context_is_opt_in_and_only_replaces_explain_model_context(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    resolved: bool,
    relationship_query: bool,
) -> None:
    _guidance, support = _support_module(monkeypatch)
    evidence = _evidence(resolved=resolved)
    evidence["is_relationship_query"] = relationship_query
    monkeypatch.setattr(
        support,
        "resolve_relationship_evidence",
        lambda **_kwargs: evidence,
    )

    class Store:
        def list_topics(self, *_args: object) -> list[dict[str, object]]:
            return _topics()

        def list_interactions(self, *_args: object) -> list[object]:
            return []

    class Tracker:
        def get_status_summary(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {}

    class Harness(support._TutorContextSupportMixin):
        def __init__(self) -> None:
            self._store = Store()
            self._knowledge_tracker = Tracker()
            self._knowledge_guidance_topics_cache: dict[str, object] = {}
            self._cfg = SimpleNamespace(
                language="zh-CN",
                history_limit=10,
                mode="study",
                llm_vision_enabled=False,
                knowledge_relation_retrieval_v2_enabled=enabled,
            )
            self.logger = None

        def _state_snapshot(self) -> dict[str, object]:
            return {}

        async def _route_study_input_semantics(self, *_args: object, **_kwargs: object):
            return (
                support.StudyInputSemantics(
                    subject="math",
                    content_type="concept",
                    intent="explain",
                    response_mode="general_explanation",
                    entity="Left concept",
                    retrieval_concepts=("Left concept", "Right concept"),
                    confidence=1.0,
                ),
                "available",
                "",
            )

    context = asyncio.run(
        Harness()._build_learning_context(
            "concept_explain",
            input_text="Left concept 和 Right concept 有什么关系？",
        )
    )

    supplied = context["knowledge_guidance"]
    assert isinstance(supplied, dict)
    if enabled and relationship_query and resolved:
        assert supplied == {
            "relationship": {
                "endpoints": [
                    {"id": "math_left", "label": "Left concept", "subject": "math"},
                    {
                        "id": "physics_right",
                        "label": "Right concept",
                        "subject": "physics",
                    },
                ],
                "path": [
                    {
                        "from_id": "math_left",
                        "to_id": "physics_right",
                        "relation": "application",
                        "reason": "canonical relation",
                    }
                ],
                "hop_count": 1,
            }
        }
    elif enabled and relationship_query:
        assert supplied == {"relationship_unresolved": True}
    else:
        assert "topic" in supplied
        assert "relationship" not in supplied
        assert "_relationship_model_context" not in supplied


def test_explicit_topic_bypasses_relationship_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _guidance, support = _support_module(monkeypatch)
    calls = 0

    def resolver(**_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return _evidence()

    monkeypatch.setattr(support, "resolve_relationship_evidence", resolver)

    class Store:
        def list_topics(self, *_args: object) -> list[dict[str, object]]:
            return _topics()

    class Harness(support._TutorContextSupportMixin):
        _store = Store()
        _cfg = SimpleNamespace(knowledge_relation_retrieval_v2_enabled=True)
        _knowledge_guidance_topics_cache: dict[str, object] = {}
        logger = None

    guidance, outcome = asyncio.run(
        Harness()._build_knowledge_guidance_context(
            "concept_explain",
            input_text="Left concept 和 Right concept 有什么关系？",
            context={"selected_topic_id": "math_left"},
        )
    )

    assert calls == 0
    assert "_relationship_model_context" not in guidance
    assert outcome["knowledge_guidance_source"] == "selected_topic"


def test_enabled_relationship_v2_does_not_depend_on_legacy_focus_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _guidance, support = _support_module(monkeypatch)
    monkeypatch.setattr(support, "resolve_relationship_evidence", lambda **_kwargs: _evidence())
    monkeypatch.setattr(support, "match_topics", lambda *_args, **_kwargs: [])

    class Store:
        def list_topics(self, *_args: object) -> list[dict[str, object]]:
            return _topics()

        def list_interactions(self, *_args: object) -> list[object]:
            return []

    class Tracker:
        def get_status_summary(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {}

    class Harness(support._TutorContextSupportMixin):
        _store = Store()
        _knowledge_tracker = Tracker()
        _knowledge_guidance_topics_cache: dict[str, object] = {}
        _cfg = SimpleNamespace(
            language="zh-CN",
            history_limit=10,
            mode="study",
            llm_vision_enabled=False,
            knowledge_relation_retrieval_v2_enabled=True,
        )
        logger = None

        def _state_snapshot(self) -> dict[str, object]:
            return {}

        async def _route_study_input_semantics(self, *_args: object, **_kwargs: object):
            return (
                support.StudyInputSemantics(
                    subject="math",
                    content_type="concept",
                    intent="explain",
                    response_mode="general_explanation",
                    entity="Left concept",
                    retrieval_concepts=("Left concept", "Right concept"),
                    confidence=1.0,
                ),
                "available",
                "",
            )

    context = asyncio.run(
        Harness()._build_learning_context(
            "concept_explain",
            input_text="Left concept 和 Right concept 有什么关系？",
        )
    )

    assert context["knowledge_guidance"] == {
        "relationship": {
            "endpoints": [
                {"id": "math_left", "label": "Left concept", "subject": "math"},
                {"id": "physics_right", "label": "Right concept", "subject": "physics"},
            ],
            "path": [
                {
                    "from_id": "math_left",
                    "to_id": "physics_right",
                    "relation": "application",
                    "reason": "canonical relation",
                }
            ],
            "hop_count": 1,
        }
    }
