from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest
from adaptive_learning.mastery_v2 import (
    DEFAULT_MASTERY_V2_POLICY,
    MASTERY_V2_MODEL_VERSION,
    MasteryEvidence,
    MasteryV2Accumulator,
    calculate_mastery_v2,
)

NOW = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)


def _evidence(
    attempt_id: str,
    *,
    verdict: str = "correct",
    score: float | int | None = 100,
    difficulty: float | int | None = 3,
    used_hint: bool | None = False,
    response_time_ms: int | None = 8_000,
    evaluator_confidence: float | None = 1.0,
    submitted_at: datetime | str = NOW,
) -> MasteryEvidence:
    return MasteryEvidence(
        attempt_id=attempt_id,
        verdict=verdict,
        score=score,
        difficulty=difficulty,
        used_hint=used_hint,
        response_time_ms=response_time_ms,
        evaluator_confidence=evaluator_confidence,
        submitted_at=submitted_at,
    )


def _snapshot(
    evidence: list[MasteryEvidence],
    *,
    unresolved_wrong_count: int = 0,
):
    return calculate_mastery_v2(
        "math.linear-equation",
        evidence,
        unresolved_wrong_count=unresolved_wrong_count,
        as_of=NOW,
    )


def test_policy_is_immutable_and_versioned() -> None:
    assert DEFAULT_MASTERY_V2_POLICY.model_version == MASTERY_V2_MODEL_VERSION
    assert MASTERY_V2_MODEL_VERSION == "mastery-v2-shadow-1"

    with pytest.raises(FrozenInstanceError):
        DEFAULT_MASTERY_V2_POLICY.mastered_threshold = 0.5  # type: ignore[misc]


def test_incremental_fold_equals_full_rebuild_regardless_of_fact_order() -> None:
    facts = [
        _evidence("attempt-2", verdict="partial", score=65, submitted_at=NOW - timedelta(days=2)),
        _evidence("attempt-1", verdict="wrong", score=0, submitted_at=NOW - timedelta(days=5)),
        _evidence("attempt-3", difficulty=5, submitted_at=NOW - timedelta(hours=1)),
    ]
    rebuilt = _snapshot(list(reversed(facts)))
    accumulated = MasteryV2Accumulator("math.linear-equation").extend(facts).snapshot(
        unresolved_wrong_count=0,
        as_of=NOW,
    )

    assert accumulated == rebuilt
    assert rebuilt.source_attempt_id == "attempt-3"
    assert rebuilt.evidence_count == 3


def test_duplicate_attempt_is_idempotent_and_conflicting_duplicate_is_rejected() -> None:
    fact = _evidence("attempt-1")

    assert _snapshot([fact, fact]) == _snapshot([fact])

    with pytest.raises(ValueError, match="conflicting facts"):
        _snapshot([fact, _evidence("attempt-1", score=0, verdict="wrong")])


def test_unknown_hint_is_neutral_not_a_penalty() -> None:
    unknown = _snapshot([_evidence("attempt-1", used_hint=None)])
    no_hint = _snapshot([_evidence("attempt-1", used_hint=False)])
    used_hint = _snapshot([_evidence("attempt-1", used_hint=True)])

    assert unknown.mastery == no_hint.mastery
    assert unknown.accuracy == no_hint.accuracy
    assert used_hint.mastery < no_hint.mastery


def test_difficulty_rewards_stronger_correct_evidence_without_changing_accuracy() -> None:
    easy = _snapshot([_evidence("attempt-1", difficulty=1)])
    hard = _snapshot([_evidence("attempt-1", difficulty=5)])

    assert easy.accuracy == hard.accuracy == 1.0
    assert hard.mastery > easy.mastery


def test_response_time_changes_evidence_reliability_not_answer_accuracy() -> None:
    very_fast = _snapshot([_evidence("attempt-1", response_time_ms=200)])
    normal = _snapshot([_evidence("attempt-1", response_time_ms=8_000)])
    very_slow = _snapshot([_evidence("attempt-1", response_time_ms=800_000)])

    assert very_fast.accuracy == normal.accuracy == very_slow.accuracy == 1.0
    assert very_fast.confidence < normal.confidence
    assert very_slow == normal


def test_old_evidence_decays_and_missing_confidence_uses_policy_default() -> None:
    recent = _snapshot([_evidence("attempt-1", evaluator_confidence=None)])
    old = _snapshot(
        [
            _evidence(
                "attempt-1",
                evaluator_confidence=None,
                submitted_at=NOW - timedelta(days=60),
            )
        ]
    )

    assert recent.confidence > old.confidence
    assert recent.recency == 1.0
    assert old.recency == 0.5


def test_unresolved_wrong_question_blocks_mastered_state() -> None:
    strong_facts = [
        _evidence(f"attempt-{index}", difficulty=5, submitted_at=NOW - timedelta(minutes=index))
        for index in range(20)
    ]
    clear = _snapshot(strong_facts)
    blocked = _snapshot(strong_facts, unresolved_wrong_count=1)

    assert clear.mastered is True
    assert blocked.mastered is False
    assert blocked.mastery <= DEFAULT_MASTERY_V2_POLICY.unresolved_wrong_mastery_cap
    assert "unresolved_wrong_cap" in blocked.flags


def test_every_ratio_is_finite_and_clamped_for_malformed_numeric_facts() -> None:
    snapshot = _snapshot(
        [
            _evidence(
                "attempt-1",
                score=float("nan"),
                difficulty=float("inf"),
                evaluator_confidence=float("nan"),
                response_time_ms=-1,
                submitted_at="not-a-date",
            ),
            _evidence("attempt-2", score=10_000, evaluator_confidence=-10.0),
        ]
    )

    for value in (
        snapshot.mastery,
        snapshot.accuracy,
        snapshot.recency,
        snapshot.consistency,
        snapshot.confidence,
    ):
        assert math.isfinite(value)
        assert 0.0 <= value <= 1.0

    contradictory = _snapshot([_evidence("attempt-3", verdict="wrong", score=100)])
    assert contradictory.accuracy == 0.0


def test_empty_projection_and_record_shape_are_stable() -> None:
    snapshot = _snapshot([])

    assert snapshot.mastery == 0.0
    assert snapshot.evidence_count == 0
    assert snapshot.source_attempt_id == ""
    assert snapshot.mastered is False
    assert snapshot.computed_at == "2026-08-26T03:00:00Z"
    assert set(snapshot.to_record()) == {
        "topic_id",
        "mastery",
        "accuracy",
        "recency",
        "consistency",
        "confidence",
        "evidence_count",
        "unresolved_wrong_count",
        "mastery_model_version",
        "source_attempt_id",
        "computed_at",
    }
