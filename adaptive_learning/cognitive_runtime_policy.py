"""Pure runtime gates for the cognitive engine.

The policy keeps configuration parsing, tracker compatibility and user stop
state in one fail-closed value object.  Callers can use the same snapshot for
personalization and status payloads without rebuilding subtly different
conditions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .cognitive_personalization import VERSION_SET


def _value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


@dataclass(frozen=True, slots=True)
class CognitiveRuntimeGates:
    """An immutable, diagnostic-friendly snapshot of effective gates."""

    effective_enabled: bool
    effective_gates: Mapping[str, bool]
    disabled_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "effective_enabled": self.effective_enabled,
            "effective_gates": dict(self.effective_gates),
            "disabled_reasons": list(self.disabled_reasons),
        }


def evaluate_cognitive_runtime_gates(
    config: Any,
    tracker: Any = None,
    *,
    ledger_readable: bool = True,
    tracker_state_readable: bool = True,
) -> CognitiveRuntimeGates:
    """Compute all cognitive gates without side effects.

    ``ledger_readable`` is supplied by the store adapter because a failed
    ledger query must close personalization for this request.  Unknown
    versions and missing tracker compatibility always fail closed.
    """

    requested_version = str(_value(config, "version_set", "") or "").strip()
    version_supported = requested_version == VERSION_SET
    stopped = _value(config, "strategy_personalization_stopped", False) is not False
    gates = {
        "projection_enabled": _value(config, "projection_enabled", False) is True,
        "read_active": str(_value(config, "read_mode", "off") or "").strip().lower()
        == "active",
        "intent_on": str(_value(config, "intent_policy", "off") or "").strip().lower()
        == "on",
        "ui_enabled": _value(config, "ui_enabled", False) is True,
        "retention_enabled": _value(config, "retention_enabled", False) is True,
        "strategy_shadow_enabled": _value(config, "strategy_shadow_enabled", False)
        is True,
        "strategy_rotation_enabled": _value(config, "strategy_rotation_enabled", False)
        is True,
        "strategy_personalization_enabled": _value(
            config, "strategy_personalization_enabled", False
        )
        is True,
        "strategy_personalization_exploration_enabled": _value(
            config, "strategy_personalization_exploration_enabled", False
        )
        is True,
        "personalization_not_stopped": not stopped,
        "version_supported": version_supported,
        "tracker_shadow_enabled": getattr(
            tracker, "cognitive_strategy_shadow_enabled", False
        )
        is True,
        "tracker_version_compatible": getattr(
            tracker, "_cognitive_version_set_id", ""
        )
        == VERSION_SET,
        "tracker_state_readable": bool(tracker_state_readable),
        "strategy_ledger_readable": bool(ledger_readable),
    }

    reasons: list[str] = []
    if not gates["strategy_personalization_enabled"]:
        reasons.append("personalization_disabled")
    if stopped:
        reasons.append("user_stopped")
    if not gates["projection_enabled"]:
        reasons.append("projection_disabled")
    if not gates["read_active"]:
        reasons.append("read_mode_not_active")
    if not gates["intent_on"]:
        reasons.append("intent_policy_not_on")
    if not gates["strategy_shadow_enabled"]:
        reasons.append("strategy_shadow_disabled")
    if not version_supported:
        reasons.append("unknown_version_set")
    if not gates["tracker_state_readable"]:
        reasons.append("tracker_state_unavailable")
    if not gates["tracker_shadow_enabled"]:
        reasons.append("tracker_shadow_disabled")
    if not gates["tracker_version_compatible"]:
        reasons.append("tracker_version_mismatch")
    if not gates["strategy_ledger_readable"]:
        reasons.append("strategy_ledger_unavailable")

    effective = not reasons
    return CognitiveRuntimeGates(
        effective_enabled=effective,
        effective_gates=MappingProxyType(gates),
        disabled_reasons=tuple(dict.fromkeys(reasons)),
    )


__all__ = ["CognitiveRuntimeGates", "evaluate_cognitive_runtime_gates"]
