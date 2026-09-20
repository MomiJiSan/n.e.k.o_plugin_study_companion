"""Pure release-profile and rollback gates for cognitive capabilities.

This module deliberately has no store, network, or plugin dependencies.  A
caller can persist the returned snapshot alongside a release report and use
the same decision function to move back to profile A (ordinary learning).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

RELEASE_GATE_SCHEMA_VERSION = 1
SUPPORTED_PROFILES = ("A", "B", "C", "D")


@dataclass(frozen=True)
class ReleaseGateDecision:
    profile: str
    allowed: bool
    rollback_to: str | None
    reasons: tuple[str, ...]
    snapshot: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RELEASE_GATE_SCHEMA_VERSION,
            "profile": self.profile,
            "allowed": self.allowed,
            "rollback_to": self.rollback_to,
            "reasons": list(self.reasons),
            "snapshot": dict(self.snapshot),
        }


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_release_snapshot(
    *,
    profile: str,
    config: Mapping[str, Any],
    report_hash: str,
    plugin_version: str,
    model_version: str,
    database_schema_version: int | str,
) -> dict[str, Any]:
    """Build a stable, auditable release snapshot without persisting it."""
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(f"unsupported release profile: {profile}")
    # Materialize a JSON-compatible deep copy so callers cannot mutate an
    # already-created release artifact through nested mappings or lists.
    config_copy = json.loads(
        json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    )
    snapshot = {
        "schema_version": RELEASE_GATE_SCHEMA_VERSION,
        "profile": profile,
        "plugin_version": plugin_version,
        "model_version": model_version,
        "database_schema_version": database_schema_version,
        "report_hash": report_hash,
        "config": config_copy,
    }
    snapshot["config_hash"] = _fingerprint(config_copy)
    snapshot["snapshot_hash"] = _fingerprint(snapshot)
    return snapshot


def evaluate_release_gates(
    profile: str,
    *,
    config: Mapping[str, Any],
    evidence: Mapping[str, Any] | None = None,
) -> ReleaseGateDecision:
    """Evaluate a profile; failures always fail closed to ordinary learning.

    ``evidence`` is intentionally generic so CI and a host can supply the
    same metrics (error rate, backlog, duplicate delivery, ordinary answer
    failures) without coupling this policy to a database or telemetry SDK.
    """
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(f"unsupported release profile: {profile}")
    evidence = evidence or {}
    reasons: list[str] = []
    def enabled(name: str) -> bool:
        return config.get(name) is True
    if profile == "A":
        required_off = (
            "projection_enabled", "ui_enabled", "retention_enabled",
            "strategy_shadow_enabled", "strategy_rotation_enabled",
            "strategy_personalization_enabled",
            "strategy_personalization_exploration_enabled",
        )
        if any(enabled(name) for name in required_off):
            reasons.append("profile_a_requires_all_cognitive_surfaces_off")
        if config.get("read_mode") not in (None, "off") or config.get("intent_policy") not in (None, "off"):
            reasons.append("profile_a_requires_read_and_intent_off")
    elif profile == "B":
        if not enabled("projection_enabled") or not enabled("strategy_shadow_enabled"):
            reasons.append("profile_b_requires_projection_and_shadow")
        if config.get("read_mode") == "active" or config.get("intent_policy") == "on":
            reasons.append("profile_b_must_not_deliver_active_personalization")
    else:
        for name in ("projection_enabled", "read_mode", "intent_policy", "ui_enabled", "retention_enabled"):
            if name in ("read_mode", "intent_policy"):
                expected = "active" if name == "read_mode" else "on"
                if config.get(name) != expected:
                    reasons.append(f"profile_{profile.lower()}_requires_{name}_{expected}")
            elif not enabled(name):
                reasons.append(f"profile_{profile.lower()}_requires_{name}")
        if profile == "C" and enabled("strategy_personalization_exploration_enabled"):
            reasons.append("profile_c_exploration_must_remain_off")
        if profile == "D":
            for name in (
                "strategy_personalization_enabled",
                "strategy_personalization_exploration_enabled",
            ):
                if not enabled(name):
                    reasons.append(f"profile_d_requires_{name}")
    thresholds = (("ordinary_answer_failures", 0), ("duplicate_delivery", 0), ("error_rate_bps", 500))
    for metric, limit in thresholds:
        value = evidence.get(metric, 0)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            reasons.append(f"invalid_evidence_{metric}")
        elif value > limit:
            reasons.append(f"rollback_metric_{metric}")
    allowed = not reasons
    return ReleaseGateDecision(
        profile=profile,
        allowed=allowed,
        rollback_to=None if allowed or profile == "A" else "A",
        reasons=tuple(reasons),
        snapshot={"schema_version": RELEASE_GATE_SCHEMA_VERSION, "profile": profile},
    )
