"""Optional personalization adapter before the existing Coach merge."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timezone
from typing import Any, cast

from .adaptive_learning.cognitive_personalization import POLICY_VERSION, VERSION_SET
from .store_cognitive_personalization import decide_from_store, personalization_runtime_ledger_status


def config_value(owner: Any, key: str, default: Any = False) -> Any:
    config = getattr(getattr(owner, "_cfg", None), "cognitive", None)
    return config.get(key, default) if isinstance(config, Mapping) else getattr(config, key, default)


def personalization_owns_selection(owner: Any) -> bool:
    return (
        config_value(owner, "strategy_personalization_enabled") is True
        or config_value(owner, "strategy_personalization_stopped") is not False
    )


def personalization_enabled(owner: Any) -> bool:
    tracker = getattr(owner, "_knowledge_tracker", None)
    return (
        config_value(owner, "strategy_personalization_enabled") is True
        and config_value(owner, "strategy_personalization_stopped") is False
        and config_value(owner, "projection_enabled") is True
        and config_value(owner, "read_mode", "off") == "active"
        and config_value(owner, "intent_policy", "off") == "on"
        and config_value(owner, "strategy_shadow_enabled") is True
        and config_value(owner, "version_set", "") == VERSION_SET
        and getattr(tracker, "cognitive_strategy_shadow_enabled", False) is True
        and getattr(tracker, "_cognitive_version_set_id", "") == VERSION_SET
    )


def personalization_status(owner: Any) -> dict[str, Any]:
    """Describe live switches, gates, last delivered strategy, and stop counters."""
    provider = getattr(getattr(owner, "_store", None), "answer_write_lock", None)
    fence = (
        cast(AbstractContextManager[Any], provider())
        if callable(provider)
        else nullcontext()
    )
    with fence:
        return _personalization_status_locked(owner)


def _personalization_status_locked(owner: Any) -> dict[str, Any]:
    switches = {
        "strategy_personalization_enabled": config_value(
            owner, "strategy_personalization_enabled"
        )
        is True,
        "strategy_personalization_exploration_enabled": config_value(
            owner, "strategy_personalization_exploration_enabled"
        )
        is True,
        "strategy_personalization_stopped": config_value(
            owner, "strategy_personalization_stopped"
        )
        is not False,
    }
    tracker = getattr(owner, "_knowledge_tracker", None)
    gates = {
        "projection_enabled": config_value(owner, "projection_enabled") is True,
        "active_read_mode": config_value(owner, "read_mode", "off") == "active",
        "intent_policy_on": config_value(owner, "intent_policy", "off") == "on",
        "shadow_enabled": config_value(owner, "strategy_shadow_enabled") is True,
        "compatible_version_set": config_value(owner, "version_set", "") == VERSION_SET,
        "tracker_shadow_enabled": getattr(tracker, "cognitive_strategy_shadow_enabled", False) is True,
        "tracker_version_compatible": getattr(tracker, "_cognitive_version_set_id", "") == VERSION_SET,
    }
    effective = personalization_enabled(owner)
    try:
        ledger = personalization_runtime_ledger_status(
            owner._store,
            now=datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger = getattr(owner, "logger", None)
        warning = getattr(logger, "warning", None)
        if callable(warning):
            warning("cognitive personalization status unavailable: {}", exc)
        return {
            "status": "degraded",
            "switches": switches,
            "gates": gates,
            "effective_enabled": effective,
            "current_strategy": "baseline",
            "current_repair_strategy": "complete_inner_derivative",
            "last_delivered_strategy": "baseline",
            "last_delivered_repair_strategy": "complete_inner_derivative",
            "decision_reason": "strategy_ledger_unavailable",
            "last_decision_reason": "strategy_ledger_unavailable",
            "alternate_deliveries_7d": 0,
            "consecutive_alternate_failures": 0,
            "stopped": switches["strategy_personalization_stopped"],
            "error": "strategy_ledger_unavailable",
        }
    last_reason = str(ledger["decision_reason"])
    reason = last_reason
    if switches["strategy_personalization_stopped"]:
        status = "stopped"
        reason = "user_stopped"
    elif not switches["strategy_personalization_enabled"]:
        status = "disabled"
        reason = "disabled"
    elif not effective:
        status = "gates_closed"
        reason = "gates_closed"
    else:
        status = "active"
    current_strategy = (
        str(ledger["last_delivered_strategy"])
        if effective and last_reason != "not_evaluated"
        else "baseline"
    )
    current_repair_strategy = (
        str(ledger["last_delivered_repair_strategy"])
        if current_strategy == "alternate"
        else "complete_inner_derivative"
    )
    if status == "active":
        status = (
            "active_alternate" if current_strategy == "alternate" else "active_baseline"
        )
    return {
        "status": status,
        "switches": switches,
        "gates": gates,
        "effective_enabled": effective,
        **ledger,
        "current_strategy": current_strategy,
        "current_repair_strategy": current_repair_strategy,
        "decision_reason": reason,
        "last_decision_reason": last_reason,
        "stopped": switches["strategy_personalization_stopped"],
    }


def personalize_candidate(owner: Any, decision: Any):
    audit = {"policy_version": POLICY_VERSION, "applied": False, "reason": "disabled"}
    if config_value(owner, "strategy_personalization_stopped") is not False:
        return decision, {**audit, "reason": "user_stopped"}
    if not personalization_enabled(owner):
        return decision, {**audit, "reason": "gates_closed"}
    try:
        return decide_from_store(
            owner._store,
            decision,
            now=datetime.now(timezone.utc),
            explore=config_value(owner, "strategy_personalization_exploration_enabled") is True,
        )
    except Exception:
        # Optional evidence must never prevent baseline question generation.
        return decision, {**audit, "reason": "evidence_unavailable"}


def commit_personalized_question(owner: Any, event: Any):
    """Recheck live settings after entering the worker and acquiring its lock."""
    with owner._store.answer_write_lock():
        if not personalization_enabled(owner):
            raise ValueError("personalization stopped before question commit")
        return owner._store.record_cognitive_intervention_event(event)
