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
    assert {"hint_leaks_answer", "retry_copies_original_question"} <= set(
        invalid.errors
    )


def test_targeted_question_contract_handles_single_letter_answer_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(monkeypatch, "_targeted_short_answer_hint_test")
    contract = importlib.import_module(f"{package}.targeted_question_contract")
    base = {
        "question": "Choose the best option.",
        "answer": "A",
        "reference_answer": "A",
        "question_type": "multiple_choice",
        "difficulty": 2,
        "topic": "Target topic",
        "target_topic_id": "target",
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
    with pytest.raises(
        ValueError, match="required context exceeds prompt token budget"
    ):
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
        def list_wrong_questions(self, **_kwargs):
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
                "retry_wrong_question": {"id": "wrong-1", "topic_id": topic_id},
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
        {"question": "Question", "reference_answer": "Answer"},
        {
            "question_params": {
                "target_topic": {"id": "target", "name": "Target"},
                "blockers": [{"id": "client-blocker"}],
            },
            "knowledge_guidance": {
                "prerequisites": ["forged prerequisite"],
                "applications": ["forged application"],
            },
        },
        canonical_relations={"prerequisites": ["Canonical prerequisite"]},
    )
    assert context["necessary_relations"] == {
        "prerequisites": ["Canonical prerequisite"]
    }
    assert "forged prerequisite" not in str(context)
    assert "client-blocker" not in str(context)


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
    relations = asyncio.run(
        entries._canonical_validation_relations_for_target(
            owner, selected_topic_id="target"
        )
    )
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

    class Subject(entries._TutorQuestionEntriesMixin):
        _lock = asyncio.Lock()
        _state = SimpleNamespace(active_mode="companion", practice_scope_revision=0)
        _agent = Agent()
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

    class Subject(entries._TutorQuestionEntriesMixin):
        _lock = asyncio.Lock()
        _state = SimpleNamespace(active_mode="companion", practice_scope_revision=0)
        _agent = Agent()
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


def test_second_semantic_failure_never_finalizes_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_second_semantic_failure_never_finalizes_question(monkeypatch))
