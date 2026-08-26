"""Regression baselines for the first adaptive-learning architecture phase.

These tests deliberately exercise the public-entry seams that later extraction
work must preserve.  The map assertion is an expected failure until Map V2
introduces explicit pagination and totals.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


def _package(monkeypatch: pytest.MonkeyPatch, prefix: str) -> str:
    name = f"{prefix}_{id(monkeypatch)}"
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    return name


def _load_knowledge_entries(monkeypatch: pytest.MonkeyPatch):
    package = _package(monkeypatch, "_pr0_knowledge_entries")
    ui_api = importlib.import_module(f"{package}.ui_api")
    common = ModuleType(f"{package}.entry_common")
    common.Ok = lambda payload: payload
    common.PublicGraphContributionBuilder = object
    common.StudyConfig = object
    common._entry_exception_error = lambda _owner, exc, **_kwargs: (_ for _ in ()).throw(exc)
    common.asyncio = asyncio
    common.build_contribution_settings_payload = lambda **_kwargs: {}
    common.build_knowledge_map_payload = ui_api.build_knowledge_map_payload
    common.plugin_entry = lambda **_kwargs: lambda function: function
    common.tr = lambda _key, *, default: default

    class Ui:
        @staticmethod
        def action():
            return lambda function: function

    common.ui = Ui()
    monkeypatch.setitem(sys.modules, common.__name__, common)

    guidance = ModuleType(f"{package}.knowledge_graph_guidance")
    guidance.build_knowledge_guidance_payload = lambda **_kwargs: {}
    monkeypatch.setitem(sys.modules, guidance.__name__, guidance)

    quality = ModuleType(f"{package}.knowledge_quality")
    quality.KnowledgeCandidateStatus = type(
        "Status", (), {"TRUSTED": type("Trusted", (), {"value": "trusted"})}
    )
    quality.KnowledgeCandidateType = type("Type", (), {})
    quality.KnowledgeEvidenceType = type("Evidence", (), {})
    monkeypatch.setitem(sys.modules, quality.__name__, quality)
    return importlib.import_module(f"{package}.entry_knowledge_entries")


def _load_question_entries(monkeypatch: pytest.MonkeyPatch):
    package = _package(monkeypatch, "_pr0_question_entries")
    common = ModuleType(f"{package}.entry_common")

    class SdkError(Exception):
        pass

    class Ui:
        @staticmethod
        def action():
            return lambda function: function

    common.LLM_OPERATION_QUESTION_GENERATE = "question_generate"
    common.Any = Any
    common.Err = lambda value: value
    common.Ok = lambda value: value
    common.SdkError = SdkError
    common.TutorReply = object
    common._entry_exception_error = lambda *_args, **_kwargs: None
    common._validate_optional_vision_image_payload = lambda *_args, **_kwargs: ""
    common.asyncio = asyncio
    common.plugin_entry = lambda **_kwargs: lambda function: function
    common.time = __import__("time")
    common.tr = lambda *_args, **kwargs: kwargs.get("default", "")
    common.ui = Ui()
    monkeypatch.setitem(sys.modules, common.__name__, common)

    guidance = ModuleType(f"{package}.knowledge_graph_guidance")
    guidance._canonical_necessary_relations = lambda *_args, **_kwargs: []
    monkeypatch.setitem(sys.modules, guidance.__name__, guidance)
    prompts = ModuleType(f"{package}.llm_prompts")
    prompts.ensure_targeted_prompt_context_fits = lambda _context: None
    monkeypatch.setitem(sys.modules, prompts.__name__, prompts)
    models = ModuleType(f"{package}.models")
    models.public_current_question_payload = lambda value: dict(value or {})
    monkeypatch.setitem(sys.modules, models.__name__, models)
    scope = ModuleType(f"{package}.practice_scope")
    scope.filter_question_params_to_scope = lambda params, _eligible: dict(params)
    scope.ordered_scope_topics = lambda topics, **_kwargs: list(topics)
    scope.practice_scope_matches_topic = lambda *_args: True
    monkeypatch.setitem(sys.modules, scope.__name__, scope)
    contract = ModuleType(f"{package}.targeted_question_contract")
    contract.project_target_topic_evidence = lambda value: dict(value or {})
    contract.semantic_validation_passed = lambda *_args, **_kwargs: True
    contract.validate_targeted_question = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, contract.__name__, contract)
    lifecycle = ModuleType(f"{package}.tutor_lifecycle")
    lifecycle.release_question_lifecycle = lambda *_args, **_kwargs: None
    lifecycle.reserve_question_lifecycle = lambda *_args, **_kwargs: ""
    monkeypatch.setitem(sys.modules, lifecycle.__name__, lifecycle)
    return importlib.import_module(f"{package}.entry_tutor_question_entries")


def _load_answer_entries(monkeypatch: pytest.MonkeyPatch):
    package = _package(monkeypatch, "_pr0_answer_entries")
    common = ModuleType(f"{package}.entry_common")

    class SdkError(Exception):
        pass

    class Ui:
        @staticmethod
        def action():
            return lambda function: function

    common.LLM_OPERATION_ANSWER_EVALUATE = "answer_evaluate"
    common.Err = lambda value: value
    common.Ok = lambda value: value
    common.SdkError = SdkError
    common._entry_exception_error = lambda *_args, **_kwargs: None
    common._validate_optional_vision_image_payload = lambda *_args, **_kwargs: ""
    common.asyncio = asyncio
    common.plugin_entry = lambda **_kwargs: lambda function: function
    common.tr = lambda *_args, **kwargs: kwargs.get("default", "")
    common.ui = Ui()
    monkeypatch.setitem(sys.modules, common.__name__, common)
    evaluation = ModuleType(f"{package}.evaluation_contract")
    evaluation.canonicalize_evaluation = lambda value, **_kwargs: dict(value or {})
    evaluation.validate_evaluation = lambda *_args, **_kwargs: SimpleNamespace(valid=True)
    monkeypatch.setitem(sys.modules, evaluation.__name__, evaluation)
    models = ModuleType(f"{package}.models")
    models.public_current_question_payload = lambda value: dict(value or {})
    monkeypatch.setitem(sys.modules, models.__name__, models)
    outcome = ModuleType(f"{package}.practice_outcome")
    outcome.build_practice_outcome = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, outcome.__name__, outcome)
    binding = ModuleType(f"{package}.target_binding")
    binding.validated_target_topic_id = lambda *_args, **_kwargs: ""
    monkeypatch.setitem(sys.modules, binding.__name__, binding)
    lifecycle = ModuleType(f"{package}.tutor_lifecycle")
    lifecycle.release_question_lifecycle = lambda *_args, **_kwargs: None
    lifecycle.reserve_question_lifecycle = lambda *_args, **_kwargs: ""
    monkeypatch.setitem(sys.modules, lifecycle.__name__, lifecycle)
    return importlib.import_module(f"{package}.entry_tutor_answer_entries")


def _topics(count: int) -> list[dict[str, object]]:
    return [
        {
            "id": f"topic-{index:05d}",
            "name": f"Topic {index}",
            "stage": "senior_high",
            "subject": "math",
            "prerequisites": [],
            "related": [],
        }
        for index in range(count)
    ]


@pytest.fixture
def ten_thousand_topic_catalog() -> list[dict[str, object]]:
    """Reusable large-catalog fixture for the Map V2 performance contract."""
    return _topics(10_000)


@pytest.mark.xfail(
    strict=True,
    reason="Map V1 clamps its public scope to 1000 topics without totals or pagination.",
)
def test_map_v1_must_not_silently_omit_the_1001st_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_knowledge_entries(monkeypatch)

    class Store:
        topics = _topics(1_001)

        def list_topics(self, limit, subject=None, stage=None):
            assert not subject and not stage
            return list(self.topics if limit is None else self.topics[:limit])

        def list_latest_mastery_for_topics(self, _topic_ids):
            return []

        def list_wrong_questions(self, **_kwargs):
            return []

    class Tracker:
        def get_weak_topics(self, **_kwargs):
            return []

    class Subject(entries._KnowledgeEntriesMixin):
        _store = Store()
        _knowledge_tracker = Tracker()

    payload = asyncio.run(Subject().study_knowledge_map(limit=1_001))
    assert payload["summary"]["topic_count"] == 1_001
    assert payload["summary"]["scope_total_count"] == 1_001
    assert payload["summary"]["has_more"] is False


def test_selection_priority_is_retry_then_due_then_weak_then_recommended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_question_entries(monkeypatch)

    class Store:
        def get_topic(self, topic_id: str):
            return {"id": topic_id, "name": f"Name {topic_id}"}

    class Subject(entries._TutorQuestionEntriesMixin):
        _knowledge_tracker = SimpleNamespace(store=Store())

    subject = Subject()
    common = {
        "target_topic_id": "recommended",
        "target_topic": {"id": "recommended", "name": "Recommended"},
        "retry_wrong_question": {"id": "wrong-1", "topic_id": "retry"},
        "due_reviews": [{"topic_id": "due", "topic": {"name": "Due"}}],
        "weak_topics": [{"topic_id": "weak", "name": "Weak"}],
    }
    assert subject._selection_from_question_params(common)["selection_reason"] == "retry"
    assert subject._selection_from_question_params(common)["selected_topic_id"] == "retry"
    without_retry = {**common, "retry_wrong_question": {}}
    assert subject._selection_from_question_params(without_retry)["selection_reason"] == "due_review"
    without_due = {**without_retry, "due_reviews": []}
    assert subject._selection_from_question_params(without_due)["selection_reason"] == "weak_topic"
    without_weak = {**without_due, "weak_topics": []}
    assert subject._selection_from_question_params(without_weak)["selection_reason"] == "recommended"


def test_selection_adapter_falls_back_to_the_legacy_public_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_question_entries(monkeypatch)
    calls: list[dict[str, object]] = []

    def no_plan(params: dict[str, object], **_kwargs: object) -> None:
        calls.append(dict(params))
        return None

    monkeypatch.setattr(entries, "build_question_plan", no_plan)

    class Store:
        def get_topic(self, topic_id: str):
            return {"id": topic_id, "name": f"Name {topic_id}"}

    class Subject(entries._TutorQuestionEntriesMixin):
        _knowledge_tracker = SimpleNamespace(store=Store())

    params = {
        "target_topic_id": "recommended",
        "target_topic": {"id": "recommended", "name": "Recommended"},
        "retry_wrong_question": {"id": "wrong-1", "topic_id": "retry"},
        "due_reviews": [{"topic_id": "due", "topic": {"name": "Due"}}],
        "weak_topics": [{"topic_id": "weak", "name": "Weak"}],
        "suggested_difficulty": 4,
    }
    selection = Subject()._selection_from_question_params(params)

    assert calls == [params]
    assert selection == {
        "selected_topic_id": "retry",
        "selected_topic_name": "Name retry",
        "selection_reason": "retry",
        "selection_reason_payload": {"wrong_question": {"id": "wrong-1", "topic_id": "retry"}},
        "difficulty": 4,
        "weak_topics": params["weak_topics"],
        "due_reviews": params["due_reviews"],
        "mastery_overview": [],
        "question_params": params,
    }


def test_scope_revision_rejects_a_question_from_the_previous_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_answer_entries(monkeypatch)

    class Subject(entries._TutorAnswerEntriesMixin):
        def __init__(self) -> None:
            self._lock = asyncio.Lock()
            self._scope_lock = asyncio.Lock()
            self._state = SimpleNamespace(
                active_practice_scope={"scope_key": "scope-a"},
                practice_scope_revision=7,
            )

        def _practice_scope_write_lock(self):
            return self._scope_lock

    subject = Subject()
    question = {"scope_key": "scope-a", "scope_revision": 7}
    assert asyncio.run(subject._question_matches_active_practice_scope(question))
    subject._state.practice_scope_revision = 8
    assert not asyncio.run(subject._question_matches_active_practice_scope(question))


def test_store_attempt_id_is_idempotent_at_the_transaction_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = _package(monkeypatch, "_pr0_store")
    mode_manager = ModuleType(f"{package}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    store_module = importlib.import_module(f"{package}.store")
    store = store_module.StudyStore(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    try:
        store.ensure_topic(topic_id="topic-a", name="Topic A")
        kwargs = {
            "session_id": "baseline",
            "mode": "companion",
            "topic_id": "topic-a",
            "question": {"question": "Question", "answer": "Answer"},
            "user_answer": "Learner answer",
            "eval_result": {"verdict": "wrong", "score": 10},
            "response_time_ms": None,
            "attempt_id": "attempt-1",
        }
        first = store.batch_write_answer_data(**kwargs)
        second = store.batch_write_answer_data(**kwargs)
        assert first["ok"] is True and not first.get("duplicate_attempt")
        assert second["duplicate_attempt"] is True
        assert len(store.list_qa_records(limit=10)) == 1
    finally:
        store.close()
