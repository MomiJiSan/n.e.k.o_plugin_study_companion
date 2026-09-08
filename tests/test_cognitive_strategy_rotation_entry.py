from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

# isort: split
from test_cognitive_active_question_entry import _active_subject, _generate


def test_active_entry_rotates_only_with_both_shadow_and_rotation_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, _ = _active_subject(
        monkeypatch, "_cognitive_strategy_rotation_entry"
    )
    tracker = subject._knowledge_tracker
    original_propose = tracker.propose_cognitive_intent

    def propose_with_alternate_bucket(original_plan):
        decision = original_propose(original_plan)
        hypothesis = replace(
            decision.selected_hypothesis,
            source_attempt_id="attempt-a",
        )
        proposed = replace(decision.proposed_plan, hypothesis_target=hypothesis)
        return replace(
            decision,
            proposed_plan=proposed,
            effective_plan=proposed,
            selected_hypothesis=hypothesis,
        )

    tracker.propose_cognitive_intent = propose_with_alternate_bucket
    tracker.cognitive_strategy_shadow_enabled = True
    tracker.cognitive_strategy_rotation_enabled = True
    tracker._cognitive_version_set_id = "cognitive-v2.1-1"
    subject._store.list_cognitive_intervention_events = lambda **_kwargs: []

    payload = asyncio.run(_generate(subject))

    assert payload["question"] == (
        "The derivative of sin(x^2) is 2*x*cos(x^2). Change only the "
        "inner power to x^3 and give the new derivative."
    )
    proposal, committed = subject._store.events
    for event in (proposal, committed):
        assignment = event["metadata"]["strategy_rotation_assignment"]
        assert assignment["experiment_version"] == (
            "chain-omit-inner-repair-rotation-v1"
        )
        assert assignment["bucket"] == 1
        assert assignment["variant"] == "alternate"
        assert assignment["repair_strategy"] == "minimal_change"
        assert "attempt-a" not in str(assignment)
    exposure = committed["metadata"]["strategy_exposure"]
    assert exposure["strategy_id"] == "chain.omit-inner.minimal-change"
    assert exposure["baseline"] is False


def test_rotation_gate_without_shadow_collection_keeps_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, _ = _active_subject(
        monkeypatch, "_cognitive_strategy_rotation_no_shadow"
    )
    subject._knowledge_tracker.cognitive_strategy_rotation_enabled = False

    payload = asyncio.run(_generate(subject))

    assert payload["question"] == (
        "Complete the missing factor: d/dx cos(x^3) = -sin(x^3) * ____."
    )
    assert all(
        "strategy_rotation_assignment" not in event.get("metadata", {})
        for event in subject._store.events
    )


def test_rotation_failure_keeps_the_reviewed_baseline_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, _ = _active_subject(
        monkeypatch, "_cognitive_strategy_rotation_failure"
    )
    subject._knowledge_tracker.cognitive_strategy_rotation_enabled = True
    subject._store.list_cognitive_intervention_events = lambda **_kwargs: []

    def fail_rotation(*_args, **_kwargs):
        raise RuntimeError("injected rotation failure")

    monkeypatch.setitem(
        subject._generate_question_payload_impl.__func__.__globals__,
        "apply_guarded_strategy_rotation",
        fail_rotation,
    )

    payload = asyncio.run(_generate(subject))

    assert payload["question"] == (
        "Complete the missing factor: d/dx cos(x^3) = -sin(x^3) * ____."
    )
    assert [event["event_type"] for event in subject._store.events] == [
        "intent_proposed",
        "question_committed",
    ]


def test_failed_repair_retry_reuses_the_delivered_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, _ = _active_subject(
        monkeypatch, "_cognitive_strategy_rotation_retry"
    )
    tracker = subject._knowledge_tracker
    tracker.cognitive_strategy_shadow_enabled = True
    tracker.cognitive_strategy_rotation_enabled = True
    tracker._cognitive_version_set_id = "cognitive-v2.1-1"
    calls: list[dict[str, object]] = []

    def list_prior_events(**kwargs):
        calls.append(dict(kwargs))
        return [
            {
                "event_type": "attempt_committed",
                "hypothesis_target": {
                    "hypothesis_id": "hypothesis-active",
                    "topic_id": "college_chain_rule",
                    "code": "omit_inner_derivative",
                },
                "learning_intent": "misconception_repair",
                "repair_strategy": "minimal_change",
                "evaluation_verdict": "wrong",
                "metadata": {
                    "strategy_rotation_assignment": {
                        "eligible": True,
                        "applied": True,
                        "reason": "assigned",
                        "experiment_version": (
                            "chain-omit-inner-repair-rotation-v1"
                        ),
                        "comparison_scope_id": "chain.omit-inner.repair",
                        "bucket": 1,
                        "variant": "alternate",
                        "repair_strategy": "minimal_change",
                        "assignment_key_sha256": "a" * 64,
                    }
                },
            }
        ]

    subject._store.list_cognitive_intervention_events = list_prior_events

    payload = asyncio.run(_generate(subject))

    assert payload["question"] == (
        "The derivative of sin(x^2) is 2*x*cos(x^2). Change only the "
        "inner power to x^3 and give the new derivative."
    )
    assert calls == [
        {
            "topic_id": "college_chain_rule",
            "hypothesis_code": "omit_inner_derivative",
            "model_version": "cognitive-v1",
            "event_types": ("attempt_committed",),
            "newest_first": True,
            "limit": 200,
        }
    ]
    assignment = subject._store.events[-1]["metadata"][
        "strategy_rotation_assignment"
    ]
    assert assignment["reason"] == "reused_retry_assignment"
    assert assignment["assignment_key_sha256"] == "a" * 64
