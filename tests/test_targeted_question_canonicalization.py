from __future__ import annotations

from typing import Any

import pytest
from targeted_question_contract import (
    canonicalize_targeted_question,
    validate_targeted_question,
)


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "question": "What is 2 + 2?",
        "answer": "4",
        "reference_answer": "4",
        "accepted_answers": ["4"],
        "key_points": ["Add the two terms."],
        "rubric": {"addition": 1},
        "solution_steps": ["Compute 2 + 2."],
        "question_type": "math_exact",
        "difficulty": 2,
        "target_topic_id": "target",
    }
    payload.update(overrides)
    return payload


def test_canonicalization_repairs_the_observed_failure_triple() -> None:
    payload = _valid_payload(
        reference_answer="four",
        accepted_answers=["4", " 4 ", "four"],
        difficulty=3,
        target_topic_id="outside",
        _answer_reference_answer_consistent=False,
        _targeted_difficulty_valid=True,
    )

    canonical, repairs = canonicalize_targeted_question(
        payload,
        target_topic_id="target",
        planned_difficulty=2,
    )

    assert canonical is not payload
    assert canonical["answer"] == canonical["reference_answer"] == "4"
    assert canonical["accepted_answers"] == ["4", "four"]
    assert canonical["difficulty"] == 2
    assert canonical["target_topic_id"] == "target"
    assert "_answer_reference_answer_consistent" not in canonical
    assert "_targeted_difficulty_valid" not in canonical
    assert {
        "difficulty_overridden",
        "reference_answer_canonicalized",
        "accepted_answers_deduplicated",
        "target_topic_id_overridden",
    } <= set(repairs)
    validation = validate_targeted_question(
        canonical,
        target_topic_id="target",
        target_topic_name="Target",
        expected_difficulty=2,
    )
    assert validation.valid, validation.errors


def test_canonicalization_is_idempotent_and_shallow_copies() -> None:
    rubric = {"addition": 1}
    payload = _valid_payload(rubric=rubric)

    first, first_repairs = canonicalize_targeted_question(
        payload,
        target_topic_id="target",
        planned_difficulty=2,
    )
    second, second_repairs = canonicalize_targeted_question(
        first,
        target_topic_id="target",
        planned_difficulty=2,
    )

    assert first == second == payload
    assert first is not payload
    assert first["rubric"] is rubric
    assert first_repairs == ()
    assert second_repairs == ()


def test_canonicalization_keeps_answer_first_and_limits_answers_to_twelve() -> None:
    payload = _valid_payload(
        answer="canonical",
        reference_answer="canonical",
        accepted_answers=[f"variant-{index}" for index in range(20)],
    )

    canonical, repairs = canonicalize_targeted_question(
        payload,
        target_topic_id="target",
        planned_difficulty=2,
    )

    assert len(canonical["accepted_answers"]) == 12
    assert canonical["accepted_answers"][0] == "canonical"
    assert canonical["accepted_answers"][1:] == [
        f"variant-{index}" for index in range(11)
    ]
    assert "accepted_answers_truncated" in repairs


def test_canonicalization_does_not_hide_overlong_material() -> None:
    overlong = "x" * 501
    payload = _valid_payload(accepted_answers=["4", overlong])

    canonical, _ = canonicalize_targeted_question(
        payload,
        target_topic_id="target",
        planned_difficulty=2,
    )

    assert canonical["accepted_answers"] == ["4", overlong]
    validation = validate_targeted_question(
        canonical,
        target_topic_id="target",
        target_topic_name="Target",
        expected_difficulty=2,
    )
    assert "invalid_accepted_answers" in validation.errors


@pytest.mark.parametrize("planned_difficulty", [None, True, 0, 6, 2.5])
def test_canonicalization_rejects_an_invalid_server_plan(
    planned_difficulty: object,
) -> None:
    with pytest.raises(ValueError, match="planned_difficulty"):
        canonicalize_targeted_question(
            _valid_payload(),
            target_topic_id="target",
            planned_difficulty=planned_difficulty,  # type: ignore[arg-type]
        )
