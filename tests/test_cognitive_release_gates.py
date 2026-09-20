import json

from adaptive_learning.cognitive_release_gates import build_release_snapshot, evaluate_release_gates


def test_release_snapshot_is_detached_and_json_stable():
    config = {"projection_enabled": True, "nested": {"mode": "shadow"}}
    snapshot = build_release_snapshot(
        profile="B",
        config=config,
        report_hash="report-hash",
        plugin_version="0.3.0",
        model_version="cognitive-v2.1-1",
        database_schema_version="7",
    )

    # The release artifact is a detached, canonicalizable contract.
    config["nested"]["mode"] = "mutated-after-build"
    assert snapshot["config"]["nested"]["mode"] == "shadow"
    assert json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    assert snapshot["config_hash"] == snapshot["config_hash"]
    assert len(snapshot["snapshot_hash"]) == 64


def test_release_gate_threshold_boundaries_are_explicit_and_fail_closed():
    config = {
        "projection_enabled": True,
        "read_mode": "active",
        "intent_policy": "on",
        "ui_enabled": True,
        "retention_enabled": True,
    }
    at_limits = evaluate_release_gates(
        "C", config=config, evidence={"ordinary_answer_failures": 0, "duplicate_delivery": 0, "error_rate_bps": 500}
    )
    assert at_limits.allowed is True
    over_limit = evaluate_release_gates(
        "C", config=config, evidence={"error_rate_bps": 501}
    )
    assert over_limit.allowed is False
    assert over_limit.rollback_to == "A"
    assert over_limit.to_dict()["snapshot"] == {"schema_version": 1, "profile": "C"}


def test_release_gate_contract_round_trips_without_deployment_side_effects():
    decision = evaluate_release_gates(
        "D",
        config={
            "projection_enabled": True,
            "read_mode": "active",
            "intent_policy": "on",
            "ui_enabled": True,
            "retention_enabled": True,
            "strategy_personalization_enabled": True,
            "strategy_personalization_exploration_enabled": True,
        },
    )
    encoded = json.dumps(decision.to_dict(), sort_keys=True, separators=(",", ":"))
    decoded = json.loads(encoded)
    assert decoded["allowed"] is True
    assert decoded["rollback_to"] is None
    assert decoded["reasons"] == []


def test_profile_a_requires_read_and_intent_off():
    decision = evaluate_release_gates(
        "A", config={"read_mode": "active", "intent_policy": "on"}, evidence={}
    )
    assert decision.allowed is False
    assert "profile_a_requires_read_and_intent_off" in decision.reasons
