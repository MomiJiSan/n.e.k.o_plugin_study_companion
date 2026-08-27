import asyncio
import importlib
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _package(monkeypatch: pytest.MonkeyPatch, name: str) -> str:
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    return name


def test_targeted_question_contract_rejects_failure_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(monkeypatch, "_targeted_contract_test")
    contract = importlib.import_module(f"{package}.targeted_question_contract")
    invalid = contract.validate_targeted_question(
        {
            "question": "What is X?",
            "answer": "",
            "reference_answer": "",
            "question_type": "unsupported",
            "difficulty": 7,
            "target_topic_id": "outside",
        },
        target_topic_id="target",
        target_topic_name="Target topic",
    )
    assert not invalid.valid
    assert {
        "missing_reference_answer",
        "unsupported_question_type",
        "invalid_difficulty",
        "target_topic_mismatch",
    } <= set(invalid.errors)


def test_target_topic_evidence_projection_is_the_single_seed_field_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(monkeypatch, "_target_topic_evidence_contract_test")
    contract = importlib.import_module(f"{package}.targeted_question_contract")
    projected = contract.project_target_topic_evidence(
        {
            "id": "target",
            "name": "Target",
            "subject": "math",
            "skills": ["SKILL_SENTINEL"],
            "typical_misconceptions": ["MISCONCEPTION_SENTINEL"],
            "question_types": ["short_answer"],
            "examples": [{"prompt": "EXAMPLE_SENTINEL"}],
            "description": "LEGACY_DESCRIPTION",
            "definition": "LEGACY_DEFINITION",
            "common_mistakes": ["LEGACY_MISTAKE"],
            "internal_private_payload": {"answer": "PRIVATE_ANSWER"},
            "empty": "",
        }
    )
    assert tuple(projected) == (
        "id",
        "name",
        "subject",
        "skills",
        "typical_misconceptions",
        "question_types",
        "examples",
    )
    assert (
        not {
            "description",
            "definition",
            "common_mistakes",
            "internal_private_payload",
            "empty",
        }
        & projected.keys()
    )


def test_targeted_question_contract_rejects_answer_hint_and_copied_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(monkeypatch, "_targeted_retry_contract_test")
    contract = importlib.import_module(f"{package}.targeted_question_contract")
    invalid = contract.validate_targeted_question(
        {
            "question": "Original question",
            "answer": "42",
            "reference_answer": "42",
            "question_type": "math_exact",
            "difficulty": 2,
            "hint": "The final answer is 42.",
            "topic": "Target topic",
            "target_topic_id": "target",
        },
        target_topic_id="target",
        target_topic_name="Target topic",
        origin_wrong_question={"question": {"question": "Original question"}},
    )
    assert {"hint_leaks_answer", "retry_copies_original_question"} <= set(invalid.errors)


def test_targeted_question_contract_rejects_conflicting_answer_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(monkeypatch, "_targeted_answer_contract_test")
    contract = importlib.import_module(f"{package}.targeted_question_contract")
    invalid = contract.validate_targeted_question(
        {
            "question": "What is 2 + 2?",
            "answer": "4",
            "reference_answer": "5",
            "question_type": "math_exact",
            "difficulty": 2,
            "target_topic_id": "target",
        },
        target_topic_id="target",
        target_topic_name="Target topic",
    )
    assert not invalid.valid
    assert "answer_reference_answer_mismatch" in invalid.errors


def test_targeted_question_contract_requires_bounded_scoring_materials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(monkeypatch, "_targeted_material_contract_test")
    contract = importlib.import_module(f"{package}.targeted_question_contract")
    invalid = contract.validate_targeted_question(
        {
            "question": "What is 2 + 2?",
            "answer": "4",
            "reference_answer": "4",
            "accepted_answers": ["4", "4"],
            "key_points": [],
            "rubric": {"calculation": -1},
            "solution_steps": [""],
            "question_type": "math_exact",
            "difficulty": 2,
            "target_topic_id": "target",
        },
        target_topic_id="target",
        target_topic_name="Target topic",
    )
    assert {
        "invalid_accepted_answers",
        "invalid_key_points",
        "invalid_rubric",
        "invalid_solution_steps",
    } <= set(invalid.errors)


def test_targeted_question_contract_handles_single_letter_answer_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(monkeypatch, "_targeted_short_answer_hint_test")
    contract = importlib.import_module(f"{package}.targeted_question_contract")
    base = {
        "question": "Name the applicable category.",
        "answer": "A",
        "reference_answer": "A",
        "question_type": "short_answer",
        "difficulty": 2,
        "topic": "Target topic",
        "target_topic_id": "target",
        "accepted_answers": ["A"],
        "key_points": ["Classifies the item correctly."],
        "rubric": {"classification": 1},
        "solution_steps": ["Apply the target definition."],
    }
    safe = contract.validate_targeted_question(
        {**base, "hint": "Recall the applicable definition."},
        target_topic_id="target",
        target_topic_name="Target topic",
    )
    leaked = contract.validate_targeted_question(
        {**base, "hint": "The answer is A."},
        target_topic_id="target",
        target_topic_name="Target topic",
    )
    assert safe.valid
    assert "hint_leaks_answer" in leaked.errors


def test_targeted_question_contract_rejects_multiple_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(monkeypatch, "_targeted_no_multiple_choice_test")
    contract = importlib.import_module(f"{package}.targeted_question_contract")
    invalid = contract.validate_targeted_question(
        {
            "question": "Choose the best option.",
            "answer": "A",
            "reference_answer": "A",
            "question_type": "multiple_choice",
            "difficulty": 2,
            "target_topic_id": "target",
        },
        target_topic_id="target",
        target_topic_name="Target topic",
    )
    assert not invalid.valid
    assert "unsupported_question_type" in invalid.errors


def _load_question_and_evaluation_normalizers(monkeypatch: pytest.MonkeyPatch, package: str):
    _package(monkeypatch, package)
    common = ModuleType(f"{package}.tutor_llm_agent_common")

    class SdkError(Exception):
        pass

    common.LLM_OPERATION_QUESTION_GENERATE = "question_generate"
    common.LLM_OPERATION_ANSWER_EVALUATE = "answer_evaluate"
    common.MODE_COMPANION = "companion"
    common.STUDY_FALLBACK_QUESTION_EMPTY = {}
    common.STUDY_FALLBACK_QUESTION_TEMPLATE = {}
    common.Any = Any
    common.SdkError = SdkError
    common.TutorReply = Any
    common._ANSWER_VERDICTS = frozenset({"correct", "partial", "wrong", "dont_know"})
    common._as_dict = lambda value: dict(value or {}) if isinstance(value, dict) else {}
    common._as_list = lambda value: list(value or []) if isinstance(value, list) else []
    common._as_str = lambda value, default="": str(default if value is None else value)
    common._clamp_int = lambda value, minimum, maximum, default: (
        value if isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum else default
    )
    common.normalize_mode = lambda value: str(value or "companion")
    monkeypatch.setitem(sys.modules, f"{package}.tutor_llm_agent_common", common)
    return (
        importlib.import_module(f"{package}.tutor_llm_agent_question_generate"),
        importlib.import_module(f"{package}.tutor_llm_agent_answer_evaluate"),
    )


def test_question_normalizer_syncs_canonical_answer_and_preserves_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question_generate, _ = _load_question_and_evaluation_normalizers(monkeypatch, "_canonical_question_normalizer_test")

    class Owner:
        _guess_topic = staticmethod(lambda _context: "Fallback")
        _screen_type_from_context = staticmethod(lambda _context: "")

    normalized = question_generate._normalize_question(
        Owner(),
        {
            "question": "What is 2 + 2?",
            "answer": "4",
            "reference_answer": "5",
            "question_type": "math_exact",
            "difficulty": 2,
            "target_topic_id": "target",
        },
        {"targeted_question": True},
    )
    assert normalized["answer"] == "4"
    assert normalized["reference_answer"] == "4"
    assert normalized["_answer_reference_answer_consistent"] is False

    reference_only = question_generate._normalize_question(
        Owner(),
        {
            "question": "What is 2 + 2?",
            "reference_answer": "4",
            "question_type": "math_exact",
            "difficulty": 2,
            "target_topic_id": "target",
        },
        {"targeted_question": True},
    )
    assert reference_only["answer"] == "4"
    assert reference_only["reference_answer"] == "4"
    assert reference_only["_answer_reference_answer_consistent"] is True


def test_evaluation_normalizer_keeps_server_expected_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, answer_evaluate = _load_question_and_evaluation_normalizers(monkeypatch, "_canonical_evaluation_normalizer_test")

    class Owner:
        _screen_type_from_context = staticmethod(lambda _context: "")
        _verdict_from_score = staticmethod(lambda _score, *, answer: "wrong")
        _fallback_feedback = staticmethod(lambda _verdict, _context: "")
        _fallback_next_action = staticmethod(lambda _verdict: "")

    normalized = answer_evaluate._normalize_evaluation(
        Owner(),
        {
            "verdict": "correct",
            "score": 100,
            "feedback": "Correct.",
            "next_action": "Continue.",
            "reference_answer": "MODEL_REPLACEMENT",
        },
        {"answer": "4", "expected_answer": "SERVER_CANONICAL"},
    )
    assert normalized["reference_answer"] == "SERVER_CANONICAL"


def test_question_generation_prompt_excludes_multiple_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _load_prompts(monkeypatch, "_targeted_question_types_prompt_test")
    assert "multiple_choice" not in prompts.STUDY_QUESTION_GENERATE_REQUIREMENTS


def test_public_question_payload_hides_answer_consistency_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(monkeypatch, "_targeted_private_metadata_test")
    mode_manager = ModuleType(f"{package}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"{package}.mode_manager", mode_manager)
    models = importlib.import_module(f"{package}.models")
    public = models.public_current_question_payload(
        {
            "question": "Question",
            "_answer_reference_answer_consistent": False,
        }
    )
    assert "_answer_reference_answer_consistent" not in public


def _load_prompts(monkeypatch: pytest.MonkeyPatch, package: str):
    _package(monkeypatch, package)
    utils = ModuleType("utils")
    tokenize = ModuleType("utils.tokenize")
    tokenize.count_tokens = lambda text: max(1, len(str(text)) // 4)  # type: ignore[attr-defined]
    tokenize.truncate_to_tokens = lambda text, limit: str(text)[: limit * 4]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "utils", utils)
    monkeypatch.setitem(sys.modules, "utils.tokenize", tokenize)
    mode_manager = ModuleType(f"{package}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"{package}.mode_manager", mode_manager)
    return importlib.import_module(f"{package}.llm_prompts")


def test_targeted_prompt_excludes_ambient_and_learner_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _load_prompts(monkeypatch, "_targeted_prompt_privacy_test")
    safe = prompts._prompt_context(
        {
            "targeted_question": True,
            "selected_topic_id": "target",
            "knowledge_question_params": {
                "retry_wrong_question": {
                    "id": "wrong-1",
                    "question": {"question": "old"},
                    "expected_answer": "expected",
                    "user_answer": "PRIVATE_OLD_ANSWER",
                }
            },
            "last_ocr_text": "PRIVATE_OCR",
            "history": [{"input_text": "PRIVATE_HISTORY"}],
            "recent_learning_events": ["PRIVATE_EVENT"],
            "screen_classification": {"text": "PRIVATE_SCREEN"},
            "vision_image_base64": "PRIVATE_IMAGE",
            "target_binding": {
                "target_topic_id": "PRIVATE_BINDING",
                "origin_wrong_question_id": "PRIVATE_WRONG_ID",
            },
        }
    )
    rendered = json.dumps(safe)
    assert "wrong-1" in rendered and "expected" in rendered
    for sentinel in (
        "PRIVATE_OLD_ANSWER",
        "PRIVATE_OCR",
        "PRIVATE_HISTORY",
        "PRIVATE_EVENT",
        "PRIVATE_SCREEN",
        "PRIVATE_IMAGE",
        "PRIVATE_BINDING",
        "PRIVATE_WRONG_ID",
    ):
        assert sentinel not in rendered


def test_targeted_prompt_budget_keeps_required_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _load_prompts(monkeypatch, "_targeted_prompt_budget_test")
    context = {
        "targeted_question": True,
        "selected_topic_id": "target-id",
        "selected_topic_name": "Target",
        "practice_scope": {"mode": "explicit_topic", "topic_id": "target-id"},
        "knowledge_question_params": {
            "target_topic_id": "target-id",
            "target_topic": {
                "id": "target-id",
                "name": "Target",
                "examples": ["x" * 5000] * 20,
            },
            "mastery": {"mastery": 0.2},
            "blockers": [{"id": "blocker-id"}],
            "retry_wrong_question": {"id": "wrong-id", "expected_answer": "answer"},
        },
        "knowledge_guidance": {"extensions": ["y" * 5000] * 20},
    }
    rendered = prompts._context_json_for_prompt("question_generate", context)
    assert "target-id" in rendered
    assert "blocker-id" in rendered
    assert "wrong-id" in rendered
    assert prompts.count_tokens(rendered) <= 4500

    required, _optional = prompts._targeted_context_parts(context)
    compact_examples = required["knowledge_question_params"]["target_topic"]["examples"]
    assert len(compact_examples) == 3
    assert all("...[truncated " in example for example in compact_examples)


def test_targeted_prompt_keeps_seed_and_candidate_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _load_prompts(monkeypatch, "_targeted_prompt_evidence_test")
    rendered = prompts._context_json_for_prompt(
        "question_generate",
        {
            "targeted_question": True,
            "knowledge_question_params": {
                "target_topic_id": "target-id",
                "target_topic": {
                    "id": "target-id",
                    "name": "Target",
                    "skills": ["SKILL_SENTINEL"],
                    "typical_misconceptions": ["MISCONCEPTION_SENTINEL"],
                    "examples": ["EXAMPLE_SENTINEL"],
                    "description": "LEGACY_DESCRIPTION",
                    "definition": "LEGACY_DEFINITION",
                    "common_mistakes": ["LEGACY_MISTAKE"],
                },
                "candidate_evidence": [
                    {
                        "id": "candidate-id",
                        "item_type": "topic",
                        "status": "trusted",
                        "payload_summary": "CANDIDATE_SENTINEL",
                        "private_token": "MUST_NOT_APPEAR",
                    }
                ],
            },
        },
    )
    for sentinel in (
        "SKILL_SENTINEL",
        "MISCONCEPTION_SENTINEL",
        "EXAMPLE_SENTINEL",
        "CANDIDATE_SENTINEL",
    ):
        assert sentinel in rendered
    assert "MUST_NOT_APPEAR" not in rendered
    for legacy_sentinel in (
        "LEGACY_DESCRIPTION",
        "LEGACY_DEFINITION",
        "LEGACY_MISTAKE",
    ):
        assert legacy_sentinel not in rendered


def test_targeted_prompt_rejects_oversized_required_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _load_prompts(monkeypatch, "_targeted_required_budget_test")
    context = {
        "targeted_question": True,
        "selected_topic_id": "target-id",
        "knowledge_question_params": {
            "target_topic_id": "target-id",
            "target_topic": {"id": "target-id", "name": "x" * 20000},
        },
    }
    with pytest.raises(ValueError, match="required context exceeds prompt token budget"):
        prompts.ensure_targeted_prompt_context_fits(context)


@dataclass
class _Reply:
    operation: str
    input_text: str
    reply: str
    payload: dict[str, Any]
    degraded: bool = False
    diagnostic: str = ""
    created_at: str = "now"


def _load_entries(monkeypatch: pytest.MonkeyPatch, package: str):
    _package(monkeypatch, package)
    common = ModuleType(f"{package}.entry_common")

    class SdkError(Exception):
        def __init__(self, message: str, *, code: str = "") -> None:
            super().__init__(message)
            self.code = code

    class _Ui:
        @staticmethod
        def action():
            return lambda value: value

    common.LLM_OPERATION_QUESTION_GENERATE = "question_generate"
    common.Any = Any
    common.Err = lambda value: value
    common.Ok = lambda value: value
    common.SdkError = SdkError
    common.TutorReply = _Reply
    common._entry_exception_error = lambda *_args, **_kwargs: None
    common._validate_optional_vision_image_payload = lambda *_args, **_kwargs: ""
    common.asyncio = asyncio
    common.plugin_entry = lambda **_kwargs: lambda value: value
    common.time = __import__("time")
    common.tr = lambda *_args, **kwargs: kwargs.get("default", "")
    common.ui = _Ui()
    monkeypatch.setitem(sys.modules, f"{package}.entry_common", common)

    models = ModuleType(f"{package}.models")

    def public_current_question_payload(value):
        payload = dict(value or {})
        for key in (
            "answer",
            "reference_answer",
            "accepted_answers",
            "key_points",
            "rubric",
            "solution_steps",
            "internal_private_payload",
            "target_binding",
        ):
            payload.pop(key, None)
        return payload

    models.public_current_question_payload = public_current_question_payload
    monkeypatch.setitem(sys.modules, f"{package}.models", models)
    scope = ModuleType(f"{package}.practice_scope")
    scope.filter_question_params_to_scope = lambda params, _eligible: dict(params)
    scope.ordered_scope_topics = lambda topics, **_kwargs: list(topics)
    scope.practice_scope_matches_topic = lambda *_args: True
    monkeypatch.setitem(sys.modules, f"{package}.practice_scope", scope)
    prompt = ModuleType(f"{package}.llm_prompts")
    prompt.ensure_targeted_prompt_context_fits = lambda _context: None
    monkeypatch.setitem(sys.modules, f"{package}.llm_prompts", prompt)
    return importlib.import_module(f"{package}.entry_tutor_question_entries"), SdkError


def test_unscoped_selection_refocuses_on_retry_with_complete_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = _load_entries(monkeypatch, "_targeted_focus_test")

    class Store:
        def list_auto_retry_candidates(self, **_kwargs):
            return [{"id": "wrong-1", "topic_id": "target"}]

        def get_topic(self, topic_id):
            return {"id": topic_id, "name": "Target"}

    class Tracker:
        store = Store()

        def preview_next_question_params(self, topic_id="", **_kwargs):
            if not topic_id:
                return {"weak_topics": [{"topic_id": "weak"}], "due_reviews": []}
            return {
                "target_topic_id": topic_id,
                "target_topic": self.store.get_topic(topic_id),
                "mastery": {"mastery": 0.41},
                "blockers": [{"id": "pre"}],
                "retry_wrong_question": {
                    "id": "newer-cooling-wrong",
                    "topic_id": topic_id,
                },
                "suggested_difficulty": 3,
            }

    class Subject(entries._TutorQuestionEntriesMixin):
        _knowledge_tracker = Tracker()
        _state = SimpleNamespace(practice_scope_revision=0)
        _targeted_context_lock = None

        def _resolve_active_practice_scope(self):
            return None

    result = Subject()._build_targeted_question_context()
    assert result["selection_reason"] == "retry"
    assert result["selected_topic_id"] == "target"
    assert result["question_params"]["mastery"]["mastery"] == 0.41
    assert result["question_params"]["blockers"] == [{"id": "pre"}]
    assert result["question_params"]["retry_wrong_question"]["id"] == "wrong-1"


def test_unscoped_selection_skips_cooling_retry_and_falls_back_to_due_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = _load_entries(monkeypatch, "_targeted_retry_cooldown_test")

    class Store:
        def list_auto_retry_candidates(self, **_kwargs):
            return []

        def get_topic(self, topic_id):
            return {"id": topic_id, "name": "Due topic"}

    class Tracker:
        store = Store()

        def preview_next_question_params(self, topic_id="", **_kwargs):
            if not topic_id:
                return {
                    "retry_wrong_question": {
                        "id": "cooling-wrong",
                        "topic_id": "due-topic",
                    },
                    "due_reviews": [
                        {
                            "topic_id": "due-topic",
                            "topic": {"id": "due-topic", "name": "Due topic"},
                        }
                    ],
                    "weak_topics": [{"topic_id": "weak-topic"}],
                }
            return {
                "target_topic_id": topic_id,
                "target_topic": self.store.get_topic(topic_id),
                "mastery": {"mastery": 0.5},
                "blockers": [],
                "retry_wrong_question": {
                    "id": "cooling-wrong",
                    "topic_id": topic_id,
                },
                "prompt_guidance": "Use a variant of the active wrong question.",
                "suggested_difficulty": 3,
            }

        @staticmethod
        def _question_guidance(mastery, *, blockers, retry):
            assert mastery == 0.5
            assert blockers == []
            assert retry is None
            return "normal-due-review-guidance"

    class Subject(entries._TutorQuestionEntriesMixin):
        _knowledge_tracker = Tracker()
        _state = SimpleNamespace(practice_scope_revision=0)
        _targeted_context_lock = None

        def _resolve_active_practice_scope(self):
            return None

    result = Subject()._build_targeted_question_context()
    assert result["selection_reason"] == "due_review"
    assert result["selected_topic_id"] == "due-topic"
    assert result["question_params"]["retry_wrong_question"] == {}
    assert result["question_params"]["prompt_guidance"] == ("normal-due-review-guidance")


def test_scoped_retry_cooldown_applies_to_broad_scope_but_not_explicit_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = _load_entries(monkeypatch, "_targeted_scoped_retry_cooldown_test")
    calls: list[str] = []

    class Store:
        def get_topic(self, topic_id):
            return {"id": topic_id, "name": "Target"}

        def list_topics(self, *_args, **_kwargs):
            return [{"id": "target", "name": "Target"}]

        def list_latest_mastery_for_topics(self, _eligible):
            return []

        def list_auto_retry_candidates(self, **_kwargs):
            calls.append("auto")
            return []

        def list_wrong_questions(self, **_kwargs):
            calls.append("display")
            return [{"id": "cooling-wrong", "topic_id": "target"}]

    class Tracker:
        store = Store()

        def preview_next_question_params(self, topic_id="", **_kwargs):
            return {
                "target_topic_id": topic_id,
                "target_topic": self.store.get_topic(topic_id),
                "retry_wrong_question": {
                    "id": "cooling-wrong",
                    "topic_id": "target",
                },
            }

    class Subject(entries._TutorQuestionEntriesMixin):
        _knowledge_tracker = Tracker()

    broad_scope = SimpleNamespace(
        mode="explicit_scope",
        eligible_topic_ids=["target"],
        topic_id="",
        subject="math",
        stage="junior_high",
        chapter="",
        unit="",
        course_family="",
    )
    explicit_topic = SimpleNamespace(**{**vars(broad_scope), "mode": "explicit_topic", "topic_id": "target"})
    explicit_topic.to_public_dict = lambda: {
        "mode": "explicit_topic",
        "topic_id": "target",
    }

    broad = Subject()._scoped_question_params(broad_scope)
    explicit = Subject()._scoped_question_params(explicit_topic)

    assert broad["retry_wrong_question"] == {}
    assert explicit["retry_wrong_question"]["id"] == "cooling-wrong"
    assert calls == ["auto", "display"]


def test_scoped_readiness_only_filters_automatic_recommendations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = _load_entries(monkeypatch, "_targeted_scoped_readiness_test")

    class Store:
        topics = [
            {"id": "blocked", "name": "Blocked"},
            {"id": "ready", "name": "Ready"},
        ]

        def get_topic(self, topic_id):
            return next((topic for topic in self.topics if topic["id"] == topic_id), {})

        def list_topics(self, *_args, **_kwargs):
            return list(self.topics)

        def list_latest_mastery_for_topics(self, _eligible):
            return []

        def list_auto_retry_candidates(self, **_kwargs):
            return []

    class Graph:
        @staticmethod
        def readiness_in_scope(_eligible):
            return {"ready"}, {"blocked": [{"id": "pre", "name": "Prerequisite", "required_mastery": 0.8}]}

    class Tracker:
        store = Store()
        graph = Graph()

        def preview_next_question_params(self, topic_id="", **_kwargs):
            return {
                "target_topic_id": topic_id,
                "target_topic": self.store.get_topic(topic_id),
                "candidate_evidence": [
                    {"payload": {"topic_id": "blocked", "name": "Blocked"}},
                    {"payload": {"topic_id": "ready", "name": "Ready"}},
                ],
                "weak_topics": [],
                "due_reviews": [],
            }

    class Subject(entries._TutorQuestionEntriesMixin):
        _knowledge_tracker = Tracker()
        _cfg = SimpleNamespace(adaptive_practice_readiness_enabled=True)

    scope = SimpleNamespace(
        mode="explicit_scope",
        eligible_topic_ids=["blocked", "ready"],
        topic_id="",
        subject="math",
        stage="junior_high",
        chapter="",
        unit="",
        course_family="",
    )

    params = Subject()._scoped_question_params(scope)

    assert params["target_topic_id"] == "ready"
    assert [item["payload"]["topic_id"] for item in params["candidate_evidence"]] == ["ready"]
    assert "blocked_diagnostic" not in params


def test_scoped_readiness_returns_diagnostic_only_after_priority_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = _load_entries(monkeypatch, "_targeted_blocked_diagnostic_test")

    class Store:
        def get_topic(self, topic_id):
            return {"id": topic_id, "name": "Blocked"}

        def list_topics(self, *_args, **_kwargs):
            return [{"id": "blocked", "name": "Blocked"}]

        def list_latest_mastery_for_topics(self, _eligible):
            return []

        def list_auto_retry_candidates(self, **_kwargs):
            return []

    class Graph:
        @staticmethod
        def readiness_in_scope(_eligible):
            return set(), {"blocked": [{"id": "pre", "name": "Prerequisite", "required_mastery": 0.8}]}

    class Tracker:
        store = Store()
        graph = Graph()

        def preview_next_question_params(self, topic_id="", **_kwargs):
            return {
                "target_topic_id": topic_id,
                "target_topic": self.store.get_topic(topic_id),
                "candidate_evidence": [{"payload": {"topic_id": "blocked"}}],
                "weak_topics": [],
                "due_reviews": [],
            }

    class Subject(entries._TutorQuestionEntriesMixin):
        _knowledge_tracker = Tracker()
        _cfg = SimpleNamespace(adaptive_practice_readiness_enabled=True)

    scope = SimpleNamespace(
        mode="explicit_scope",
        eligible_topic_ids=["blocked"],
        topic_id="",
        subject="math",
        stage="junior_high",
        chapter="",
        unit="",
        course_family="",
    )
    params = Subject()._scoped_question_params(scope)
    selection = Subject()._selection_from_question_params(params)

    assert params["candidate_evidence"] == []
    assert params["blocked_diagnostic"]["blockers"][0]["id"] == "pre"
    assert selection["selection_reason"] == "blocked_diagnostic"
    assert selection["selected_topic_id"] == "blocked"


def test_blocked_diagnostic_never_overrides_retry_due_or_weak_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = _load_entries(monkeypatch, "_targeted_priority_before_readiness_test")

    class Store:
        @staticmethod
        def get_topic(topic_id):
            return {"id": topic_id, "name": topic_id}

    class Subject(entries._TutorQuestionEntriesMixin):
        _knowledge_tracker = SimpleNamespace(store=Store())

    base = {
        "target_topic_id": "blocked",
        "blocked_diagnostic": {"target_topic_id": "blocked", "blockers": []},
        "retry_wrong_question": {"id": "wrong-1", "topic_id": "retry"},
        "due_reviews": [{"topic_id": "due", "topic": {"id": "due", "name": "due"}}],
        "weak_topics": [{"topic_id": "weak", "name": "weak"}],
    }

    assert Subject()._selection_from_question_params(base)["selection_reason"] == "retry"
    without_retry = {**base, "retry_wrong_question": {}}
    assert Subject()._selection_from_question_params(without_retry)["selection_reason"] == "due_review"
    without_due = {**without_retry, "due_reviews": []}
    assert Subject()._selection_from_question_params(without_due)["selection_reason"] == "weak_topic"


def test_server_target_binding_uses_only_selected_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = _load_entries(monkeypatch, "_targeted_binding_test")
    binding = entries._server_target_binding(
        {
            "selected_topic_id": "target",
            "selection_reason": "retry",
            "question_params": {
                "retry_wrong_question": {
                    "id": "wrong-1",
                    "topic_id": "target",
                }
            },
        },
        generated_at="generated",
    )
    assert binding == {
        "target_topic_id": "target",
        "validation_status": "passed",
        "generated_at": "generated",
        "origin_wrong_question_id": "wrong-1",
    }

    mismatched = entries._server_target_binding(
        {
            "selected_topic_id": "target",
            "selection_reason": "retry",
            "question_params": {
                "retry_wrong_question": {
                    "id": "wrong-2",
                    "topic_id": "outside",
                }
            },
        },
        generated_at="generated",
    )
    assert mismatched["origin_wrong_question_id"] == ""


def test_semantic_validation_uses_only_rebuilt_canonical_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = _load_entries(monkeypatch, "_targeted_validation_evidence_test")
    context = entries._question_validation_context(
        {
            "question": "Question",
            "reference_answer": "Answer",
            "accepted_answers": ["Answer"],
            "key_points": ["Key point"],
            "rubric": {"key point": 1},
            "solution_steps": ["Step"],
            "hint": "Hint",
            "difficulty": 2,
            "question_type": "short_answer",
        },
        {
            "question_params": {
                "target_topic": {
                    "id": "target",
                    "name": "Target",
                    "skills": ["SKILL_SENTINEL"],
                    "typical_misconceptions": ["MISCONCEPTION_SENTINEL"],
                    "examples": [{"prompt": "EXAMPLE_SENTINEL"}],
                    "description": "LEGACY_DESCRIPTION",
                    "definition": "LEGACY_DEFINITION",
                    "common_mistakes": ["LEGACY_MISTAKE"],
                },
                "blockers": [{"id": "client-blocker"}],
            },
            "knowledge_guidance": {
                "prerequisites": ["forged prerequisite"],
                "applications": ["forged application"],
            },
        },
        canonical_relations={"prerequisites": ["Canonical prerequisite"]},
    )
    assert context["necessary_relations"] == {"prerequisites": ["Canonical prerequisite"]}
    assert context["accepted_answers"] == ["Answer"]
    assert context["key_points"] == ["Key point"]
    assert context["rubric"] == {"key point": 1}
    assert context["solution_steps"] == ["Step"]
    assert context["hint"] == "Hint"
    assert context["difficulty"] == 2
    assert context["question_type"] == "short_answer"
    assert "forged prerequisite" not in str(context)
    assert "client-blocker" not in str(context)
    for sentinel in (
        "SKILL_SENTINEL",
        "MISCONCEPTION_SENTINEL",
        "EXAMPLE_SENTINEL",
    ):
        assert sentinel in str(context["target_topic"])
    for legacy_sentinel in (
        "LEGACY_DESCRIPTION",
        "LEGACY_DEFINITION",
        "LEGACY_MISTAKE",
    ):
        assert legacy_sentinel not in str(context)


def test_relationless_seed_topics_keep_semantic_validation_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = _load_entries(monkeypatch, "_targeted_relationless_evidence_test")
    requested_ids = {
        "english_junior_tense_system",
        "history_senior_chronology_spatial",
        "geo_senior_rock_cycle",
        "geo_senior_population_change",
    }
    topics: dict[str, dict[str, Any]] = {}
    for filename in ("english.json", "history.json", "geography.json"):
        payload = json.loads((ROOT / "static" / "knowledge_seeds" / filename).read_text(encoding="utf-8"))
        topics.update(
            {
                str(topic.get("id")): topic
                for topic in payload.get("topics", [])
                if str(topic.get("id")) in requested_ids
            }
        )

    assert topics.keys() == requested_ids
    for topic_id, topic in topics.items():
        context = entries._question_validation_context(
            {"question": "Question", "reference_answer": "Answer"},
            {"question_params": {"target_topic": topic}},
            canonical_relations={},
        )
        assert context["target_topic"]["id"] == topic_id
        assert context["target_topic"]["skills"]
        assert context["target_topic"]["typical_misconceptions"]
        assert context["target_topic"]["examples"]
        assert context["necessary_relations"] == {}


def test_semantic_validation_rebuilds_relations_from_server_topics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = _load_entries(monkeypatch, "_targeted_validation_store_test")

    class Store:
        def list_topics(self, *_args):
            return [
                {"id": "pre", "name": "Prerequisite"},
                {"id": "app", "name": "Application"},
                {
                    "id": "target",
                    "name": "Target",
                    "prerequisites": [{"id": "pre"}],
                    "related": [{"id": "app", "relation": "application"}],
                },
            ]

    owner = SimpleNamespace(_store=Store())
    relations = asyncio.run(entries._canonical_validation_relations_for_target(owner, selected_topic_id="target"))
    assert relations == {
        "prerequisites": ["Prerequisite"],
        "applications": ["Application"],
    }


async def _server_binding_overrides_model_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = _load_entries(monkeypatch, "_targeted_binding_override_test")

    class Agent:
        async def question_generate(self, *_args, **_kwargs):
            return _Reply(
                "question_generate",
                "",
                "Question",
                {
                    "question": "Question",
                    "answer": "Answer",
                    "reference_answer": "Answer",
                    "accepted_answers": ["Answer"],
                    "key_points": ["Uses the target definition."],
                    "rubric": {"definition": 1},
                    "solution_steps": ["Apply the target definition."],
                    "question_type": "short_answer",
                    "difficulty": 2,
                    "hint": "Recall the definition.",
                    "topic": "Forged",
                    "target_binding": {
                        "target_topic_id": "forged",
                        "validation_status": "passed",
                        "origin_wrong_question_id": "forged-wrong",
                    },
                },
            )

        async def question_validate(self, **_kwargs):
            return _Reply(
                "question_validate",
                "",
                "ok",
                {"relevant": True, "answer_supported": True, "retry": False},
            )

    class Tracker:
        recorded: list[dict[str, Any]] = []

        def record_prompt_usage_for_question_params(self, params: dict[str, Any]) -> None:
            self.recorded.append(dict(params))

    class Subject(entries._TutorQuestionEntriesMixin):
        _lock = asyncio.Lock()
        _state = SimpleNamespace(active_mode="companion", practice_scope_revision=0)
        _agent = Agent()
        _knowledge_tracker = Tracker()
        private_payload = None
        public_payload = None

        async def _build_learning_context(self, _operation, *, input_text, extra):
            return {**extra, "input_text": input_text, "language": "zh-CN"}

        def _resolve_active_practice_scope(self):
            return None

        @asynccontextmanager
        async def _practice_scope_write_lock(self):
            yield

        async def _finalize_tutor_call(self, _operation, reply, **kwargs):
            self.private_payload = dict(reply.payload)
            self.public_payload = dict(kwargs.get("public_payload") or {})
            return dict(self.public_payload)

    targeted = {
        "selected_topic_id": "target",
        "selected_topic_name": "Target",
        "selection_context_id": "ctx",
        "selection_reason": "retry",
        "scope_revision": 0,
        "scope_key": "",
        "question_params": {
            "target_topic_id": "target",
            "target_topic": {"id": "target", "name": "Target"},
            "retry_wrong_question": {
                "id": "wrong-1",
                "topic_id": "target",
                "question": {"question": "Old question"},
            },
        },
    }
    subject = Subject()
    await subject._generate_question_payload(
        source_text="Generate",
        source="targeted_question",
        targeted_context=targeted,
    )
    assert subject.private_payload["target_binding"] == {
        "target_topic_id": "target",
        "validation_status": "passed",
        "generated_at": "now",
        "origin_wrong_question_id": "wrong-1",
    }
    assert "target_binding" not in subject.public_payload
    assert "answer" not in subject.public_payload
    for private_field in ("accepted_answers", "key_points", "rubric", "solution_steps"):
        assert private_field not in subject.public_payload
    assert subject._knowledge_tracker.recorded == [targeted["question_params"]]


def test_server_binding_overrides_model_claim_and_stays_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_server_binding_overrides_model_claim(monkeypatch))


async def _second_semantic_failure_never_finalizes_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, SdkError = _load_entries(monkeypatch, "_targeted_generation_test")

    class Agent:
        generated = 0
        validated = 0

        async def question_generate(self, *_args, **_kwargs):
            self.generated += 1
            payload = {
                "question": f"Question {self.generated}",
                "answer": "Answer",
                "reference_answer": "Answer",
                "accepted_answers": ["Answer"],
                "key_points": ["Uses the target definition."],
                "rubric": {"definition": 1},
                "solution_steps": ["Apply the target definition."],
                "question_type": "short_answer",
                "difficulty": 2,
                "hint": "Think about the definition.",
                "topic": "Target",
            }
            return _Reply("question_generate", "", payload["question"], payload)

        async def question_validate(self, **_kwargs):
            self.validated += 1
            return _Reply(
                "question_validate",
                "",
                "off target",
                {
                    "relevant": False,
                    "answer_supported": False,
                    "retry": True,
                    "reason": "off target",
                },
            )

    class Tracker:
        recorded: list[dict[str, Any]] = []

        def record_prompt_usage_for_question_params(self, params: dict[str, Any]) -> None:
            self.recorded.append(dict(params))

    class Subject(entries._TutorQuestionEntriesMixin):
        _lock = asyncio.Lock()
        _state = SimpleNamespace(active_mode="companion", practice_scope_revision=0)
        _agent = Agent()
        _knowledge_tracker = Tracker()
        finalized = 0

        async def _build_learning_context(self, _operation, *, input_text, extra):
            return {**extra, "input_text": input_text, "language": "zh-CN"}

        def _resolve_active_practice_scope(self):
            return None

        async def _finalize_tutor_call(self, *_args, **_kwargs):
            self.finalized += 1
            return {}

    targeted = {
        "selected_topic_id": "target",
        "selected_topic_name": "Target",
        "selection_context_id": "ctx",
        "scope_revision": 0,
        "scope_key": "",
        "question_params": {
            "target_topic_id": "target",
            "target_topic": {"id": "target", "name": "Target"},
        },
    }
    subject = Subject()
    with pytest.raises(SdkError) as error:
        await subject._generate_question_payload(
            source_text="Generate",
            source="targeted_question",
            targeted_context=targeted,
        )
    assert error.value.code == "QUESTION_VALIDATION_FAILED"
    assert subject._agent.generated == 2
    assert subject._agent.validated == 2
    assert subject.finalized == 0
    assert subject._knowledge_tracker.recorded == []


def test_second_semantic_failure_never_finalizes_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_second_semantic_failure_never_finalizes_question(monkeypatch))
