from __future__ import annotations

from types import SimpleNamespace

from adaptive_learning.cognitive_personalization import VERSION_SET
from adaptive_learning.cognitive_runtime_policy import evaluate_cognitive_runtime_gates


def _config(**changes):
    values = {
        "projection_enabled": True,
        "read_mode": "active",
        "intent_policy": "on",
        "ui_enabled": True,
        "retention_enabled": True,
        "strategy_shadow_enabled": True,
        "strategy_rotation_enabled": True,
        "strategy_personalization_enabled": True,
        "strategy_personalization_exploration_enabled": True,
        "strategy_personalization_stopped": False,
        "version_set": VERSION_SET,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _tracker(**changes):
    values = {
        "cognitive_strategy_shadow_enabled": True,
        "_cognitive_version_set_id": VERSION_SET,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_all_runtime_gates_open():
    result = evaluate_cognitive_runtime_gates(_config(), _tracker())
    assert result.effective_enabled is True
    assert all(result.effective_gates.values())
    assert result.disabled_reasons == ()


def test_disabled_config_reports_specific_reasons():
    result = evaluate_cognitive_runtime_gates(
        _config(
            projection_enabled=False,
            read_mode="off",
            intent_policy="off",
            strategy_shadow_enabled=False,
            strategy_personalization_enabled=False,
        ),
        _tracker(),
    )
    assert result.effective_enabled is False
    assert {
        "personalization_disabled",
        "projection_disabled",
        "read_mode_not_active",
        "intent_policy_not_on",
        "strategy_shadow_disabled",
    }.issubset(result.disabled_reasons)


def test_unknown_version_fails_closed():
    result = evaluate_cognitive_runtime_gates(_config(version_set="future"), _tracker())
    assert result.effective_enabled is False
    assert result.effective_gates["version_supported"] is False
    assert "unknown_version_set" in result.disabled_reasons


def test_user_stop_overrides_open_configuration():
    result = evaluate_cognitive_runtime_gates(
        _config(strategy_personalization_stopped=True), _tracker()
    )
    assert result.effective_enabled is False
    assert result.effective_gates["personalization_not_stopped"] is False
    assert "user_stopped" in result.disabled_reasons


def test_unreadable_ledger_fails_closed_but_preserves_other_gates():
    result = evaluate_cognitive_runtime_gates(
        _config(), _tracker(), ledger_readable=False
    )
    assert result.effective_enabled is False
    assert result.effective_gates["projection_enabled"] is True
    assert result.effective_gates["strategy_ledger_readable"] is False
    assert "strategy_ledger_unavailable" in result.disabled_reasons


def test_unreadable_tracker_state_fails_closed():
    result = evaluate_cognitive_runtime_gates(
        _config(), _tracker(), tracker_state_readable=False
    )
    assert result.effective_enabled is False
    assert "tracker_state_unavailable" in result.disabled_reasons
