from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from test_cognitive_active_question_entry import _active_subject
from test_cognitive_personalization_integration import _config


@pytest.mark.parametrize("ledger_failed", [False, True])
@pytest.mark.parametrize("stopped", [False, True])
def test_status_uses_one_snapshot_for_legacy_and_effective_gates(monkeypatch, ledger_failed, stopped):
    name = f"_status_gates_{ledger_failed}_{stopped}"
    subject, _ = _active_subject(monkeypatch, name)
    runtime = importlib.import_module(name + ".cognitive_personalization_runtime")
    # Normalization used to disagree between the handwritten legacy gates and policy.
    subject._cfg = SimpleNamespace(cognitive=_config(
        read_mode=" ACTIVE ", intent_policy=" ON ", strategy_personalization_stopped=stopped,
    ))
    subject._store = SimpleNamespace()
    calls = []
    original = runtime.runtime_gates

    def snapshot(owner, **kwargs):
        calls.append(kwargs)
        return original(owner, **kwargs)

    def ledger(*args, **kwargs):
        if ledger_failed:
            raise RuntimeError("unavailable")
        return {
            "decision_reason": "not_evaluated",
            "last_delivered_strategy": "baseline",
            "last_delivered_repair_strategy": "complete_inner_derivative",
        }

    monkeypatch.setattr(runtime, "runtime_gates", snapshot)
    monkeypatch.setattr(runtime, "personalization_runtime_ledger_status", ledger)
    result = runtime.personalization_status(subject)
    assert len(calls) == 1
    assert result["gate_schema_version"] == 1
    aliases = {
        "projection_enabled": "projection_enabled",
        "active_read_mode": "read_active",
        "intent_policy_on": "intent_on",
        "shadow_enabled": "strategy_shadow_enabled",
        "compatible_version_set": "version_supported",
        "tracker_shadow_enabled": "tracker_shadow_enabled",
        "tracker_version_compatible": "tracker_version_compatible",
    }
    assert result["gates"] == {
        old: result["effective_gates"][new] for old, new in aliases.items()
    }
    assert result["gates"]["active_read_mode"] is True
    assert result["gates"]["intent_policy_on"] is True
    assert result["effective_gates"]["strategy_ledger_readable"] is not ledger_failed
    if ledger_failed:
        assert result["status"] == "degraded"
        assert result["effective_enabled"] is False
        assert "strategy_ledger_unavailable" in result["disabled_reasons"]
    if stopped:
        assert result["effective_enabled"] is False
        assert "user_stopped" in result["disabled_reasons"]
