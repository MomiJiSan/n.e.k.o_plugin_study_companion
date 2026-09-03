from __future__ import annotations

import random
from typing import Any

import pytest

from adaptive_learning.cognitive_projection import (
    CognitiveProjector,
    project_cognitive_fact_timeline,
)

TOPIC = "calculus.chain_rule"
CODE = "omit_inner_derivative"
MODEL = "cognitive-v1"


def _evidence(attempt_id: str, *, direction: str = "support") -> dict[str, Any]:
    return {
        "evidence_id": f"evidence-{attempt_id}",
        "attempt_id": attempt_id,
        "topic_id": TOPIC,
        "hypothesis_code": CODE,
        "direction": direction,
        "strength": 1.0,
        "extractor_confidence": 1.0,
        "diagnosticity": 0.8,
        "source_kind": "practice",
        "extractor_version": "cognitive-extractor-v1",
        "evidence_span": "evidence",
        "evidence_family_id": f"family-{attempt_id}",
        "session_id": f"session-{attempt_id}",
    }


def _event(
    event_type: str,
    intent: str,
    decision_id: str,
    *,
    question_id: str,
    attempt_id: str = "",
    verdict: str = "",
) -> dict[str, Any]:
    return {
        "event_id": f"{decision_id}-{event_type}",
        "event_type": event_type,
        "decision_id": decision_id,
        "hypothesis_id": f"{TOPIC}:{CODE}",
        "topic_id": TOPIC,
        "hypothesis_code": CODE,
        "model_version": MODEL,
        "learning_intent": intent,
        "repair_strategy": "complete_inner_derivative",
        "question_id": question_id,
        "attempt_id": attempt_id,
        "session_id": f"session-{attempt_id}",
        "diagnostic_validation_id": "validation",
        "evaluation_verdict": verdict,
    }


def _fact(root: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "root_fact_seq": root,
        "fact_order": {
            "evidence": 0,
            "intervention": 1,
            "obligation_satisfaction": 2,
            "episode": 2,
        }[kind],
        "fact_kind": kind,
        "payload": payload,
    }


def _monitored_facts() -> list[dict[str, Any]]:
    return [
        _fact(1, "evidence", _evidence("support-1")),
        _fact(2, "evidence", _evidence("support-2")),
        _fact(
            3,
            "intervention",
            _event(
                "question_committed",
                "misconception_repair",
                "repair",
                question_id="repair-question",
            ),
        ),
        _fact(
            4,
            "intervention",
            _event(
                "attempt_committed",
                "misconception_repair",
                "repair",
                question_id="repair-question",
                attempt_id="repair-attempt",
                verdict="correct",
            ),
        ),
        _fact(
            5,
            "intervention",
            _event(
                "question_committed",
                "transfer_check",
                "transfer",
                question_id="transfer-question",
            ),
        ),
        _fact(
            6,
            "intervention",
            _event(
                "attempt_committed",
                "transfer_check",
                "transfer",
                question_id="transfer-question",
                attempt_id="transfer-attempt",
                verdict="correct",
            ),
        ),
    ]


def _satisfaction(attempt_id: str, disposition: str) -> dict[str, Any]:
    return {
        "satisfaction_id": f"satisfaction-{attempt_id}",
        "obligation_id": "obligation-1",
        "episode_id": "episode-1",
        "claim_id": "claim-1",
        "attempt_id": attempt_id,
        "hypothesis_id": f"{TOPIC}:{CODE}",
        "topic_id": TOPIC,
        "hypothesis_code": CODE,
        "model_version": MODEL,
        "disposition": disposition,
    }


def _expired_episode() -> dict[str, Any]:
    return {
        "fact_id": "episode-expired-1",
        "episode_id": "episode-1",
        "event_type": "expired",
        "hypothesis_id": f"{TOPIC}:{CODE}",
        "topic_id": TOPIC,
        "hypothesis_code": CODE,
        "model_version": MODEL,
        "source_attempt_id": "transfer-attempt",
    }


def test_single_reducer_keeps_new_relapse_after_older_transfer() -> None:
    facts = [
        _fact(1, "evidence", _evidence("support-1")),
        _fact(2, "evidence", _evidence("support-2")),
        _fact(
            3,
            "intervention",
            _event(
                "question_committed",
                "misconception_repair",
                "repair",
                question_id="repair-question",
            ),
        ),
        _fact(
            4,
            "intervention",
            _event(
                "attempt_committed",
                "misconception_repair",
                "repair",
                question_id="repair-question",
                attempt_id="repair-attempt",
                verdict="correct",
            ),
        ),
        _fact(
            5,
            "intervention",
            _event(
                "question_committed",
                "transfer_check",
                "transfer",
                question_id="transfer-question",
            ),
        ),
        _fact(
            6,
            "intervention",
            _event(
                "attempt_committed",
                "transfer_check",
                "transfer",
                question_id="transfer-question",
                attempt_id="transfer-attempt",
                verdict="correct",
            ),
        ),
        _fact(7, "evidence", _evidence("relapse")),
    ]

    expected = project_cognitive_fact_timeline(
        facts,
        topic_id=TOPIC,
        model_version=MODEL,
        computed_at="2026-09-03T12:00:00Z",
    )
    shuffled = list(facts)
    random.Random(42).shuffle(shuffled)
    rebuilt = project_cognitive_fact_timeline(
        shuffled,
        topic_id=TOPIC,
        model_version=MODEL,
        computed_at="2026-09-03T12:00:00Z",
    )

    assert rebuilt == expected
    assert rebuilt[-1]["status"] == "supported"
    assert rebuilt[-1]["intervention_stage"] == "idle"
    assert rebuilt[-1]["relapse_count"] == 1
    assert rebuilt[-1]["source_attempt_id"] == "relapse"


@pytest.mark.parametrize(
    ("disposition", "expected_status", "expected_stage"),
    [
        ("resolved", "resolved", "resolved"),
        ("reschedule", "monitored", "monitored"),
        ("ordinary_evidence", "monitored", "monitored"),
    ],
)
def test_retention_satisfaction_is_folded_by_the_single_reducer(
    disposition: str,
    expected_status: str,
    expected_stage: str,
) -> None:
    facts = _monitored_facts()
    facts.append(
        _fact(
            7,
            "obligation_satisfaction",
            _satisfaction("retention-attempt", disposition),
        )
    )

    projected = project_cognitive_fact_timeline(
        list(reversed(facts)),
        topic_id=TOPIC,
        model_version=MODEL,
        computed_at="2026-09-03T12:00:00Z",
    )

    assert projected[-1]["status"] == expected_status
    assert projected[-1]["intervention_stage"] == expected_stage
    assert projected[-1]["last_intent"] == "retention_check"
    assert projected[-1]["last_outcome"] == disposition


def test_retention_relapse_does_not_double_count_matching_support_evidence() -> None:
    facts = _monitored_facts()
    relapse_evidence = _evidence("retention-relapse")
    relapse_evidence["source_kind"] = "retention_check"
    facts.extend(
        [
            _fact(7, "evidence", relapse_evidence),
            _fact(
                7,
                "obligation_satisfaction",
                _satisfaction("retention-relapse", "relapse"),
            ),
        ]
    )

    projected = project_cognitive_fact_timeline(
        facts,
        topic_id=TOPIC,
        model_version=MODEL,
        computed_at="2026-09-03T12:00:00Z",
    )

    assert projected[-1]["status"] == "supported"
    assert projected[-1]["intervention_stage"] == "idle"
    assert projected[-1]["relapse_count"] == 1
    assert projected[-1]["last_outcome"] == "relapse"


def test_immutable_episode_expiry_returns_to_provisional_without_relapse() -> None:
    facts = _monitored_facts()
    facts.append(_fact(7, "episode", _expired_episode()))

    projected = project_cognitive_fact_timeline(
        facts,
        topic_id=TOPIC,
        model_version=MODEL,
        computed_at="2026-09-03T12:00:00Z",
    )

    assert projected[-1]["status"] == "provisionally_resolved"
    assert projected[-1]["intervention_stage"] == "provisionally_resolved"
    assert projected[-1]["relapse_count"] == 0
    assert projected[-1]["last_outcome"] == "monitoring_window_expired"


class _UnusedExtractor:
    async def extract(self, _input: object) -> object:
        raise AssertionError("not called")


def test_projector_fails_closed_for_unknown_or_mixed_version_sets() -> None:
    with pytest.raises(ValueError, match="unsupported cognitive version set"):
        CognitiveProjector(
            object(),  # type: ignore[arg-type]
            _UnusedExtractor(),  # type: ignore[arg-type]
            model_version="unknown-projection",
        )
    with pytest.raises(ValueError, match="unsupported cognitive version set"):
        CognitiveProjector(
            object(),  # type: ignore[arg-type]
            _UnusedExtractor(),  # type: ignore[arg-type]
            version_set="cognitive-v2.1-1",
            model_version="cognitive-v1",
        )
