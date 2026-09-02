from __future__ import annotations

import asyncio
import importlib
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from test_targeted_question_contract import _load_entries, _Reply


def _targeted_context() -> dict[str, Any]:
    return {
        "selected_topic_id": "college_chain_rule",
        "selected_topic_name": "Chain rule",
        "selection_context_id": "selection-active",
        "selection_reason": "wrong_retry",
        "eligible_topic_ids": ["college_chain_rule"],
        "scope_key": "scope-a",
        "scope_revision": 7,
        "learning_plan_id": "plan-a",
        "learning_plan_revision": 4,
        "question_params": {
            "target_topic_id": "college_chain_rule",
            "target_topic": {
                "id": "college_chain_rule",
                "name": "Chain rule",
            },
            "planned_difficulty": 3,
            "retry_wrong_question": {
                "id": "wrong-a",
                "topic_id": "college_chain_rule",
                "question": {"question": "Differentiate sin(x^2)."},
            },
        },
    }


def _active_subject(
    monkeypatch: pytest.MonkeyPatch,
    package: str,
    *,
    fail_second_selection_check: bool = False,
    finalize_failure: BaseException | None = None,
    fail_question_ledger: bool = False,
):
    entries, sdk_error = _load_entries(monkeypatch, package)
    contracts = importlib.import_module(f"{package}.adaptive_learning.contracts")
    policy = importlib.import_module(f"{package}.adaptive_learning.cognitive_policy")

    class Agent:
        generated = 0
        validated = 0

        async def question_generate(self, *_args, **_kwargs):
            self.generated += 1
            payload = {
                "question": "Fallback ordinary chain-rule question",
                "answer": "2*x",
                "reference_answer": "2*x",
                "accepted_answers": ["2*x"],
                "key_points": ["Differentiate the expression."],
                "rubric": {"derivative": 1},
                "solution_steps": ["Apply the derivative rule."],
                "question_type": "math_reasoning",
                "difficulty": 3,
                "hint": "",
            }
            return _Reply("question_generate", "", payload["question"], payload)

        async def question_validate(self, **_kwargs):
            self.validated += 1
            return _Reply(
                "question_validate",
                "",
                "ok",
                {"relevant": True, "answer_supported": True, "retry": False},
            )

    class Store:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def record_cognitive_intervention_event(self, event):
            payload = dict(event)
            if fail_question_ledger and payload.get("event_type") == "question_committed":
                raise RuntimeError("injected question ledger failure")
            self.events.append(payload)
            return payload

    class Tracker:
        proposed = 0

        def propose_cognitive_intent(self, original_plan):
            self.proposed += 1
            hypothesis = contracts.HypothesisRef(
                hypothesis_id="hypothesis-active",
                topic_id="college_chain_rule",
                code="omit_inner_derivative",
                status="supported",
                probability=0.91,
                model_version="cognitive-v1",
                source_snapshot_id="snapshot-active",
                source_attempt_id="attempt-source",
                projection_generation=9,
            )
            proposed = replace(
                original_plan,
                learning_intent="misconception_repair",
                hypothesis_target=hypothesis,
                repair_strategy="complete_inner_derivative",
            )
            return policy.CognitivePolicyDecision(
                mode="on",
                original_plan=original_plan,
                proposed_plan=proposed,
                effective_plan=proposed,
                proposed_intent="misconception_repair",
                selected_hypothesis=hypothesis,
                repair_strategy="complete_inner_derivative",
                applied=True,
            )

        def record_prompt_usage_for_question_params(self, _params):
            return None

    class Subject(entries._TutorQuestionEntriesMixin):
        _lock = asyncio.Lock()
        _state = SimpleNamespace(
            active_mode="companion",
            practice_scope_revision=7,
            current_question={},
        )
        _agent = Agent()
        _store = Store()
        _knowledge_tracker = Tracker()
        selection_checks = 0
        private_payload: dict[str, Any] | None = None
        public_payload: dict[str, Any] | None = None

        async def _build_learning_context(self, _operation, *, input_text, extra):
            return {**extra, "input_text": input_text, "language": "en"}

        def _resolve_active_practice_scope(self):
            return SimpleNamespace(scope_key="scope-a")

        def _validate_learning_plan_selection_context(self, _context):
            self.selection_checks += 1
            if fail_second_selection_check and self.selection_checks == 2:
                raise sdk_error("learning plan changed", code="LEARNING_PLAN_CHANGED")

        @asynccontextmanager
        async def _practice_scope_write_lock(self):
            yield

        async def _finalize_tutor_call(self, _operation, reply, **kwargs):
            if finalize_failure is not None:
                raise finalize_failure
            self.private_payload = dict(reply.payload)
            self.public_payload = dict(kwargs.get("public_payload") or {})
            return dict(self.public_payload)

    return Subject(), sdk_error


async def _generate(subject):
    return await subject._generate_question_payload(
        source_text="Generate the selected question",
        source="targeted_question",
        targeted_context=_targeted_context(),
    )


def test_active_entry_delivers_reviewed_blueprint_without_model_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, _ = _active_subject(monkeypatch, "_cognitive_active_entry_success")

    payload = asyncio.run(_generate(subject))

    assert subject._agent.generated == 0
    assert payload["question"] == ("Complete the missing factor: d/dx cos(x^3) = -sin(x^3) * ____.")
    assert [event["event_type"] for event in subject._store.events] == [
        "intent_proposed",
        "question_committed",
    ]
    binding = subject.private_payload["target_binding"]
    assert binding["cognitive_decision_id"]
    assert binding["diagnostic_validation_id"].startswith("cognitive-validation:")
    assert binding["cognitive_hypothesis_target"]["code"] == "omit_inner_derivative"
    assert binding["learning_plan_id"] == "plan-a"
    assert binding["learning_plan_revision"] == 4
    assert binding["scope_key"] == "scope-a"
    assert binding["scope_revision"] == 7
    assert binding["origin_wrong_question_id"] == "wrong-a"
    assert (
        not {
            "hypothesis_target",
            "cognitive_hypothesis_target",
            "probability",
            "competing_hypothesis_codes",
            "cognitive_decision_id",
            "diagnostic_validation_id",
        }
        & payload.keys()
    )


def test_second_plan_check_failure_abandons_the_committed_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, sdk_error = _active_subject(
        monkeypatch,
        "_cognitive_active_entry_revision_failure",
        fail_second_selection_check=True,
    )

    with pytest.raises(sdk_error, match="learning plan changed"):
        asyncio.run(_generate(subject))

    assert [event["event_type"] for event in subject._store.events] == [
        "intent_proposed",
        "question_committed",
        "intervention_abandoned",
    ]
    assert subject._store.events[-1]["abandonment_reason"] == ("question_commit_not_published")


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("finalize failed"), asyncio.CancelledError()],
)
def test_finalize_failure_or_cancellation_abandons_the_committed_question(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    subject, _ = _active_subject(
        monkeypatch,
        f"_cognitive_active_entry_finalize_{failure.__class__.__name__}",
        finalize_failure=failure,
    )

    with pytest.raises(failure.__class__):
        asyncio.run(_generate(subject))

    assert [event["event_type"] for event in subject._store.events] == [
        "intent_proposed",
        "question_committed",
        "intervention_abandoned",
    ]
    assert subject._store.events[-1]["abandonment_reason"] == ("question_commit_not_published")


def test_question_ledger_failure_falls_back_once_without_policy_reentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, _ = _active_subject(
        monkeypatch,
        "_cognitive_active_entry_ledger_fallback",
        fail_question_ledger=True,
    )

    payload = asyncio.run(_generate(subject))

    assert payload["question"] == "Fallback ordinary chain-rule question"
    assert subject._knowledge_tracker.proposed == 1
    assert subject._agent.generated == 1
    assert [event["event_type"] for event in subject._store.events] == ["intent_proposed"]


def test_replacing_unanswered_active_question_fails_closed_when_abandonment_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, sdk_error = _active_subject(
        monkeypatch,
        "_cognitive_active_entry_replace_failure",
    )
    subject._state.current_question = {
        "question_id": "question-old",
        "attempt_id": "attempt-old",
        "attempt_evaluated": False,
        "selected_topic_id": "college_chain_rule",
        "target_binding": {
            "target_topic_id": "college_chain_rule",
            "cognitive_hypothesis_target": {
                "hypothesis_id": "hypothesis-active",
                "topic_id": "college_chain_rule",
                "code": "omit_inner_derivative",
                "status": "supported",
                "probability": 0.91,
                "model_version": "cognitive-v1",
                "source_snapshot_id": "snapshot-active",
                "source_attempt_id": "attempt-source",
                "projection_generation": 9,
            },
        },
    }
    subject._abandon_current_cognitive_intervention = AsyncMock(return_value=False)

    with pytest.raises(sdk_error) as caught:
        asyncio.run(_generate(subject))

    assert caught.value.code == "COGNITIVE_INTERVENTION_ABANDON_FAILED"
    subject._abandon_current_cognitive_intervention.assert_awaited_once()
    assert subject._knowledge_tracker.proposed == 0
    assert subject._agent.generated == 0
