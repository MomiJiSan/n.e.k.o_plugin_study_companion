from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def outcome(monkeypatch: pytest.MonkeyPatch):
    package_name = "_practice_outcome_contract"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    return importlib.import_module(f"{package_name}.practice_outcome")


@pytest.mark.parametrize(
    ("snapshot", "has_wrong", "expected"),
    [
        (None, False, "insufficient_evidence"),
        ({"mastery": 1.0, "attempts": 2, "flags": []}, False, "insufficient_evidence"),
        (
            {"mastery": 1.0, "attempts": 3, "flags": ["low_confidence"]},
            False,
            "insufficient_evidence",
        ),
        ({"mastery": 0.79, "attempts": 5, "flags": []}, False, "progressing"),
        ({"mastery": 0.80, "attempts": 3, "flags": []}, True, "progressing"),
        ({"mastery": 0.80, "attempts": 3, "flags": []}, False, "mastered"),
    ],
)
def test_mastery_status_requires_all_four_conditions(
    outcome, snapshot: dict | None, has_wrong: bool, expected: str
) -> None:
    payload = outcome.build_practice_outcome(
        verdict="correct",
        practice_scope={"mode": "explicit_topic"},
        active_scope_matches=True,
        validated_target=True,
        mastery_snapshot=snapshot,
        has_active_wrong_question=has_wrong,
    )
    assert payload["mastery_status"] == expected
    assert payload["scope_status"] == (
        "reviewing" if expected == "mastered" else "active"
    )
    assert payload["practice_scope_status"] == (
        "completed" if expected == "mastered" else "active"
    )
    assert payload["can_continue_review"] is (expected == "mastered")


def test_one_topic_explicit_scope_never_becomes_reviewing(outcome) -> None:
    payload = outcome.build_practice_outcome(
        verdict="correct",
        practice_scope={"mode": "explicit_scope", "scope_topic_count": 1},
        active_scope_matches=True,
        validated_target=True,
        mastery_snapshot={"mastery": 1.0, "attempts": 8, "flags": []},
    )
    assert payload["mastery_status"] == "mastered"
    assert payload["scope_status"] == "active"
    assert payload["practice_scope_status"] == "active"


def test_unvalidated_question_cannot_claim_mastery(outcome) -> None:
    payload = outcome.build_practice_outcome(
        verdict="correct",
        practice_scope={"mode": "explicit_topic"},
        active_scope_matches=True,
        validated_target=False,
        mastery_snapshot={"mastery": 1.0, "attempts": 8, "flags": []},
    )
    assert payload == {
        "attempt_status": "correct",
        "scope_status": "active",
        "mastery_status": "insufficient_evidence",
        "practice_scope_status": "active",
        "can_continue_review": False,
    }


@pytest.mark.parametrize("verdict", ["correct", "partial", "wrong", "dont_know"])
def test_attempt_status_preserves_canonical_verdict(outcome, verdict: str) -> None:
    payload = outcome.build_practice_outcome(
        verdict=verdict,
        practice_scope={},
        active_scope_matches=False,
        validated_target=False,
    )
    assert payload["attempt_status"] == verdict


@pytest.mark.parametrize("verdict", ["wrong", "partial", "dont_know"])
def test_non_correct_attempt_cannot_claim_mastery_or_reviewing(
    outcome, verdict: str
) -> None:
    payload = outcome.build_practice_outcome(
        verdict=verdict,
        practice_scope={"mode": "explicit_topic"},
        active_scope_matches=True,
        validated_target=True,
        mastery_snapshot={"mastery": 0.95, "attempts": 8, "flags": []},
        has_active_wrong_question=False,
    )
    assert payload == {
        "attempt_status": verdict,
        "scope_status": "active",
        "mastery_status": "progressing",
        "practice_scope_status": "active",
        "can_continue_review": False,
    }


def test_stale_scope_preserves_mastery_but_cannot_review(outcome) -> None:
    payload = outcome.build_practice_outcome(
        verdict="correct",
        practice_scope={"mode": "explicit_topic"},
        active_scope_matches=False,
        validated_target=True,
        mastery_snapshot={"mastery": 0.95, "attempts": 8, "flags": []},
        has_active_wrong_question=False,
    )
    assert payload["mastery_status"] == "mastered"
    assert payload["scope_status"] == "active"
    assert payload["practice_scope_status"] == "active"
    assert payload["can_continue_review"] is False
