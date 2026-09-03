from __future__ import annotations

import asyncio
import importlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from test_cognitive_active_question_entry import _targeted_context
from test_targeted_question_contract import _load_entries, _Reply


def _subject(
    monkeypatch: pytest.MonkeyPatch,
    package: str,
    *,
    proposal_enabled: bool = True,
    expected_obligation: bool = True,
):
    entries, _ = _load_entries(monkeypatch, package)
    retention = importlib.import_module(
        f"{package}.adaptive_learning.cognitive_retention"
    )
    proposal = retention.build_retention_action_proposal(
        {
            "obligation_id": "obligation-1",
            "episode_id": "episode-1",
            "hypothesis_id": "hypothesis-1",
            "topic_id": "college_chain_rule",
            "hypothesis_code": "omit_inner_derivative",
            "obligation_type": "retention",
            "status": "pending",
            "not_before": "2026-09-03T00:00:00Z",
            "due_by": "2026-09-04T00:00:00Z",
            "eligibility_until": "2026-09-10T00:00:00Z",
        },
        {
            "episode_id": "episode-1",
            "hypothesis_id": "hypothesis-1",
            "topic_id": "college_chain_rule",
            "hypothesis_code": "omit_inner_derivative",
            "model_version": "cognitive-v2.1-1",
            "source_attempt_id": "attempt-transfer",
            "source_event_id": "event-transfer",
            "transfer_question_family_id": "chain.transfer.family",
            "status": "open",
        },
        version_set="cognitive-v2.1-1",
        projection_current=True,
        as_of=datetime(2026, 9, 4, 12, tzinfo=timezone.utc),
    )
    assert proposal is not None

    class Agent:
        generated = 0

        async def question_generate(self, *_args, **_kwargs):
            self.generated += 1
            payload = {
                "question": "Ordinary question",
                "answer": "ordinary-answer",
                "reference_answer": "ordinary-answer",
                "accepted_answers": ["ordinary-answer"],
                "key_points": ["ordinary"],
                "rubric": {"ordinary": 1},
                "solution_steps": ["ordinary"],
                "question_type": "math_reasoning",
                "difficulty": 3,
                "hint": "",
            }
            return _Reply("question_generate", "", payload["question"], payload)

        async def question_validate(self, **_kwargs):
            return _Reply(
                "question_validate",
                "",
                "ok",
                {"relevant": True, "answer_supported": True, "retry": False},
            )

    class Store:
        def __init__(self) -> None:
            self.claims: list[dict[str, Any]] = []
            self.releases: list[dict[str, Any]] = []

        def claim_cognitive_obligations(self, **kwargs):
            self.claims.append(dict(kwargs))
            return [
                {
                    "obligation_id": "obligation-1",
                    "episode_id": "episode-1",
                    "hypothesis_code": "omit_inner_derivative",
                    "status": "claimed",
                    "claim_id": "claim-1",
                    "claim_token": "secret-token",
                    "worker_id": kwargs["worker_id"],
                    "lease_expires_at": "2026-09-04T12:05:00Z",
                }
            ]

        def release_cognitive_obligation_claim(self, **kwargs):
            self.releases.append(dict(kwargs))
            return {"status": "pending"}

    class Tracker:
        proposed = 0

        def propose_cognitive_retention_actions(self):
            self.proposed += 1
            return (proposal,) if proposal_enabled else ()

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
        private_payload: dict[str, Any] | None = None

        async def _build_learning_context(self, _operation, *, input_text, extra):
            return {**extra, "input_text": input_text, "language": "en"}

        def _resolve_active_practice_scope(self):
            return SimpleNamespace(scope_key="scope-a")

        def _validate_learning_plan_selection_context(self, _context):
            return None

        @asynccontextmanager
        async def _practice_scope_write_lock(self):
            yield

        async def _finalize_tutor_call(self, _operation, reply, **kwargs):
            self.private_payload = dict(reply.payload)
            return dict(kwargs.get("public_payload") or {})

    context = _targeted_context()
    context["selection_reason"] = "recommended"
    context["question_params"]["retry_wrong_question"] = {}
    if expected_obligation:
        context["obligation_refs"] = ["obligation-1"]
        context["cognitive_strategy"] = "independent_delayed_retention"
    return Subject(), entries, context


def test_enabled_retention_claims_after_planner_and_delivers_reviewed_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, _, context = _subject(
        monkeypatch,
        "_retention_question_success",
    )

    payload = asyncio.run(
        subject._generate_question_payload(
            source_text="Generate",
            source="targeted_question",
            targeted_context=context,
        )
    )

    assert subject._store.claims, (
        payload,
        subject._store.releases,
        subject._knowledge_tracker.proposed,
        subject.private_payload,
    )
    assert subject._store.releases == []
    assert payload["question"] == "Differentiate exp(5x - 2)."
    assert subject._agent.generated == 0
    assert subject._store.claims[0]["obligation_ids"] == ("obligation-1",)
    assert subject.private_payload is not None
    assert subject.private_payload["cognitive_episode_id"] == "episode-1"
    assert subject.private_payload["cognitive_obligation_id"] == "obligation-1"
    assert subject.private_payload["cognitive_claim_token"] == "secret-token"
    assert subject.private_payload["cognitive_claim_worker_id"].startswith(
        "question-generation:"
    )
    assert subject.private_payload["retention_blueprint_version"]
    assert subject.private_payload["retention_validator_version"]
    assert subject.private_payload["cognitive_independence_group"]
    assert not {
        "cognitive_episode_id",
        "cognitive_obligation_id",
        "cognitive_claim_token",
        "cognitive_claim_worker_id",
        "retention_blueprint_version",
        "retention_validator_version",
        "cognitive_independence_group",
        "obligation_refs",
        "cognitive_strategy",
    }.intersection(payload)


def test_switch_off_or_stale_expected_obligation_falls_back_without_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, _, context = _subject(
        monkeypatch,
        "_retention_question_disabled",
        proposal_enabled=False,
    )

    payload = asyncio.run(
        subject._generate_question_payload(
            source_text="Generate",
            source="targeted_question",
            targeted_context=context,
        )
    )

    assert payload["question"] == "Ordinary question"
    assert subject._agent.generated == 1
    assert subject._store.claims == []


def test_retention_validation_failure_releases_claim_and_falls_back_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, entries, context = _subject(
        monkeypatch,
        "_retention_question_validation_failure",
    )
    original_payload = entries.retention_question_payload

    def tampered(*args, **kwargs):
        return {**original_payload(*args, **kwargs), "answer": "tampered"}

    monkeypatch.setattr(entries, "retention_question_payload", tampered)

    payload = asyncio.run(
        subject._generate_question_payload(
            source_text="Generate",
            source="targeted_question",
            targeted_context=context,
        )
    )

    assert payload["question"] == "Ordinary question"
    assert len(subject._store.claims) == 1
    assert len(subject._store.releases) == 1
    assert subject._store.releases[0]["claim_token"] == "secret-token"


def test_failed_replacement_release_cannot_acquire_a_second_cognitive_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, _, context = _subject(
        monkeypatch,
        "_retention_question_replace_release_failure",
        expected_obligation=False,
    )
    subject._state.current_question = {
        "question_id": "old-question",
        "attempt_evaluated": False,
        "selected_topic_id": "college_chain_rule",
        "cognitive_episode_id": "episode-old",
        "cognitive_obligation_id": "obligation-old",
        "cognitive_claim_token": "old-token",
        "cognitive_claim_worker_id": "old-worker",
    }

    def fail_release(**_kwargs):
        raise RuntimeError("injected release failure")

    subject._store.release_cognitive_obligation_claim = fail_release

    payload = asyncio.run(
        subject._generate_question_payload(
            source_text="Generate",
            source="targeted_question",
            targeted_context=context,
        )
    )

    assert payload["question"] == "Ordinary question"
    assert subject._knowledge_tracker.proposed == 0
    assert subject._store.claims == []
