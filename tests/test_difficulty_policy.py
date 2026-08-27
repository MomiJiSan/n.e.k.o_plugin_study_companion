import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _policy_module(monkeypatch: pytest.MonkeyPatch, name: str):
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    return importlib.import_module(f"{name}.difficulty_policy")


def _select(policy, *, topic_difficulty=0.5, mastery=0.5, **kwargs) -> int:
    return policy.select_targeted_difficulty(
        {"difficulty": topic_difficulty},
        mastery={"mastery": mastery, "attempts": 3, "confidence": 0.8, "flags": []},
        **kwargs,
    )


def test_policy_is_pure_and_only_emits_the_staged_range(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy_module(monkeypatch, "_difficulty_policy_range_test")
    inputs = {
        "mastery": {"mastery": 0.76, "attempts": 5, "confidence": 0.8, "flags": []},
        "selection_reason": "recommended",
        "recent_results": [{"verdict": "correct"}, {"verdict": "correct"}],
    }

    first = policy.select_targeted_difficulty({"difficulty": 0.8}, **inputs)
    second = policy.select_targeted_difficulty({"difficulty": 0.8}, **inputs)

    assert first == second == 4
    assert first in {2, 3, 4}


def test_low_evidence_never_selects_four(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy_module(monkeypatch, "_difficulty_policy_evidence_test")

    selected = policy.select_targeted_difficulty(
        {"difficulty": 0.95},
        mastery={"mastery": 1.0, "attempts": 2, "confidence": 0.5, "flags": ["low_confidence"]},
        recent_results=[{"verdict": "correct"}, {"verdict": "correct"}],
    )

    assert selected == 3


def test_blockers_force_foundation_difficulty(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy_module(monkeypatch, "_difficulty_policy_blocker_test")

    assert _select(policy, topic_difficulty=0.95, mastery=1.0, blockers=[{"id": "pre"}]) == 2


def test_retry_decreases_by_at_most_one_level(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy_module(monkeypatch, "_difficulty_policy_retry_test")
    baseline = _select(policy, topic_difficulty=0.95, mastery=0.95)
    retry = _select(
        policy,
        topic_difficulty=0.95,
        mastery=0.95,
        selection_reason="retry",
        retry_wrong_question={"id": "wrong"},
        recent_results=[{"verdict": "wrong"}, {"verdict": "wrong"}],
    )

    assert baseline == 4
    assert retry == 3
    assert baseline - retry == 1


def test_two_recent_correct_or_wrong_results_adjust_one_level(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy_module(monkeypatch, "_difficulty_policy_streak_test")
    baseline = _select(policy)
    correct = _select(policy, recent_results=[{"verdict": "correct"}, {"verdict": "correct"}])
    wrong = _select(policy, recent_results=[{"verdict": "wrong"}, {"verdict": "wrong"}])

    assert baseline == 3
    assert correct == 4
    assert wrong == 2
