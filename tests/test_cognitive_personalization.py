from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

# isort: off
import pytest
from adaptive_learning.cognitive_personalization import (
    ALTERNATE,
    BASELINE,
    FAMILY,
    VERSION_SET,
    select_personalized_strategy,
)
from adaptive_learning.cognitive_policy import question_plan_ownership_fingerprint
# isort: on

# isort: split
from test_cognitive_strategy_report import _exposure
from test_cognitive_strategy_rotation import _decision

NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


def evidence(*, baseline_success: int = 2, alternate_success: int = 20):
    records = []
    for i in range(40):
        alternate = i >= 20
        row = _exposure(
            i,
            strategy_id=ALTERNATE if alternate else BASELINE,
            success=i % 20 < (alternate_success if alternate else baseline_success),
        )
        row.update(
            learner_id="local",
            hypothesis_id="hypothesis-active",
            hypothesis_code="omit_inner_derivative",
            difficulty_bucket="3",
            comparison_family_id=FAMILY,
            strategy_version="v1",
            question_purpose="repair",
            catalog_version="cognitive-strategy-catalog-v1",
            exposed_at=(NOW - timedelta(days=3, minutes=i)).isoformat(),
        )
        row["outcomes"]["retention"].update(independence_known=True)
        records.append(row)
    return {
        "catalog_version": "cognitive-strategy-catalog-v1",
        "version_set_id": VERSION_SET,
        "as_of_root_fact_seq": 1000,
        "exposures": records,
    }


def choose(snapshot=None, **kwargs):
    return select_personalized_strategy(_decision(), snapshot or evidence(), now=NOW, enabled=True, **kwargs)


def test_default_off_and_strong_evidence_preserve_coach_ownership():
    original = _decision()
    assert select_personalized_strategy(original, evidence(), now=NOW)[0] is original
    selected, audit = choose()
    assert selected.repair_strategy == "minimal_change", audit
    assert audit["reason"] == "supported_alternate"
    assert question_plan_ownership_fingerprint(selected.proposed_plan) == question_plan_ownership_fingerprint(
        original.original_plan
    )
    assert choose() == (selected, audit)
    reversed_input = evidence()
    reversed_input["exposures"].reverse()
    assert choose(reversed_input) == (selected, audit)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("insufficient", "insufficient_data"),
        ("stale", "stale_evidence"),
        ("future", "future_evidence"),
        ("duplicate", "conflicting_evidence"),
        ("version", "incompatible_version"),
        ("catalog", "incompatible_version"),
        ("uncertified", "invalid_evidence"),
        ("disclosed", "invalid_evidence"),
        ("time_override", "invalid_evidence"),
        ("hint", "insufficient_data"),
        ("wrong_learner", "insufficient_data"),
        ("wrong_hypothesis", "insufficient_data"),
        ("wrong_difficulty", "insufficient_data"),
        ("delayed_conflict", "conflicting_endpoints"),
        ("immediate_regression", "conflicting_endpoints"),
    ],
)
def test_bad_evidence_returns_baseline(case, reason):
    snapshot = evidence()
    rows = snapshot["exposures"]
    if case == "insufficient":
        rows.pop()
    elif case == "stale":
        for row in rows:
            row["exposed_at"] = (NOW - timedelta(days=9)).isoformat()
    elif case == "future":
        rows[0]["exposed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif case == "duplicate":
        duplicate = deepcopy(rows[0])
        duplicate["outcomes"]["immediate"]["success"] = False
        rows.append(duplicate)
    elif case == "version":
        snapshot["version_set_id"] = "cognitive-v1"
    elif case == "catalog":
        snapshot["catalog_version"] = "unreviewed"
    elif case in {"uncertified", "disclosed", "time_override"}:
        key, value = {
            "uncertified": ("certified", False),
            "disclosed": ("answer_disclosed", True),
            "time_override": ("development_time_override", True),
        }[case]
        rows[0]["outcomes"]["retention"][key] = value
    elif case in {"hint", "wrong_learner", "wrong_hypothesis", "wrong_difficulty"}:
        key = {
            "hint": "hint_state",
            "wrong_learner": "learner_id",
            "wrong_hypothesis": "hypothesis_id",
            "wrong_difficulty": "difficulty_bucket",
        }[case]
        for row in rows:
            row[key] = "other"
    elif case in {"delayed_conflict", "immediate_regression"}:
        endpoint = "retention" if case == "delayed_conflict" else "immediate"
        for row in rows[20:]:
            row["outcomes"][endpoint]["success"] = False
    selected, audit = choose(snapshot)
    assert selected.repair_strategy == "complete_inner_derivative"
    assert audit["reason"] == reason


def test_exploration_requires_explicit_opt_in_and_the_same_safety_budget():
    uncertain = evidence(baseline_success=10, alternate_success=13)
    assert choose(uncertain)[1]["reason"] == "uncertain_evidence"
    assert choose(uncertain, explore=True)[1]["reason"] == "bounded_exploration"
    for flags, reason in [
        ({"alternate_n": 3}, "exposure_limit"),
        ({"consecutive_failures": 2}, "failure_stop"),
        ({"stopped": True}, "user_stopped"),
    ]:
        selected, audit = choose(uncertain, explore=True, **flags)
        assert selected.repair_strategy == "complete_inner_derivative"
        assert audit["reason"] == reason


def test_scope_and_unknown_projection_cannot_be_promoted():
    decision = _decision()
    hypothesis = replace(decision.selected_hypothesis, model_version="cognitive-v1")
    outside = replace(decision, selected_hypothesis=hypothesis)
    assert select_personalized_strategy(outside, evidence(), now=NOW, enabled=True)[1]["reason"] == "outside_scope"
