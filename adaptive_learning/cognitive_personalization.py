"""Deterministic, default-off repair candidates from server-owned evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .cognitive_catalog import COGNITIVE_CATALOG_V1
from .cognitive_policy import CognitivePolicyDecision
from .cognitive_strategy_catalog import COGNITIVE_STRATEGY_CATALOG_V1
from .cognitive_strategy_report import build_cognitive_strategy_report

POLICY_VERSION = "cognitive-personalization-v1"
VERSION_SET = "cognitive-v2.1-1"
TOPIC = "calculus.chain_rule"
HYPOTHESIS = "omit_inner_derivative"
FAMILY = "chain.omit-inner.repair.v1"
BASELINE = "chain.omit-inner.complete-steps"
ALTERNATE = "chain.omit-inner.minimal-change"
MAX_ALTERNATES = 3
MAX_FAILURES = 2


def utc(value: object) -> datetime:
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("an explicit UTC offset is required")
    return result.astimezone(timezone.utc)


def select_personalized_strategy(
    decision: CognitivePolicyDecision,
    snapshot: Mapping[str, Any],
    *,
    now: datetime,
    enabled: bool = False,
    explore: bool = False,
    stopped: bool = False,
    alternate_n: int = 0,
    consecutive_failures: int = 0,
) -> tuple[CognitivePolicyDecision, dict[str, Any]]:
    """Return a candidate only; the caller must still obtain Coach acceptance.

    ``snapshot`` is an internal server-built ledger snapshot, never client data.
    Safety counters are reconstructed from canonical delivery events.
    """
    audit: dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "reason": "disabled",
        "applied": False,
        "repair_strategy": "complete_inner_derivative",
    }

    def fallback(reason: str):
        return decision, {**audit, "reason": reason}

    if stopped is not False:
        return fallback("user_stopped")
    if enabled is not True:
        return fallback("disabled")
    plan, hypothesis = decision.proposed_plan, decision.selected_hypothesis
    if (
        not decision.applied
        or decision.mode != "on"
        or plan is None
        or hypothesis is None
        or hypothesis.status != "supported"
        or not hypothesis.hypothesis_id
        or not hypothesis.source_attempt_id
        or hypothesis.model_version != VERSION_SET
        or COGNITIVE_CATALOG_V1.canonical_topic_id(plan.target_topic.id) != TOPIC
        or COGNITIVE_CATALOG_V1.canonical_topic_id(hypothesis.topic_id) != TOPIC
        or hypothesis.code != HYPOTHESIS
        or plan.hypothesis_target != hypothesis
        or decision.proposed_intent != "misconception_repair"
        or plan.learning_intent != "misconception_repair"
        or decision.repair_strategy != "complete_inner_derivative"
        or plan.repair_strategy != "complete_inner_derivative"
    ):
        return fallback("outside_scope")
    if alternate_n >= MAX_ALTERNATES:
        return fallback("exposure_limit")
    if consecutive_failures >= MAX_FAILURES:
        return fallback("failure_stop")
    try:
        now = utc(now.isoformat())
        if (
            snapshot.get("version_set_id") != VERSION_SET
            or snapshot.get("catalog_version") != COGNITIVE_STRATEGY_CATALOG_V1.catalog_version
        ):
            return fallback("incompatible_version")
        records: dict[str, dict[str, Any]] = {}
        for raw in snapshot["exposures"]:
            if not isinstance(raw, Mapping):
                return fallback("invalid_evidence")
            if (
                raw.get("learner_id") != "local"
                or COGNITIVE_CATALOG_V1.canonical_topic_id(str(raw.get("topic_id") or "")) != TOPIC
                or raw.get("hypothesis_id") != hypothesis.hypothesis_id
                or raw.get("hypothesis_code") != HYPOTHESIS
                or raw.get("difficulty_bucket") != str(plan.difficulty)
            ):
                continue
            exposed = utc(raw["exposed_at"])
            if exposed > now:
                return fallback("future_evidence")
            if exposed < now - timedelta(days=30):
                continue
            if (
                raw.get("comparison_family_id") != FAMILY
                or raw.get("strategy_id") not in {BASELINE, ALTERNATE}
                or raw.get("strategy_version") != "v1"
            ):
                return fallback("invalid_evidence")
            identity = str(raw["exposure_id"])
            normalized = {**raw, "topic_id": TOPIC}
            if identity in records and normalized != records[identity]:
                return fallback("conflicting_evidence")
            records[identity] = normalized
        catalog = [
            {
                "strategy_id": entry.strategy_id,
                "strategy_version": entry.strategy_version,
                "catalog_version": entry.catalog_version,
                "version_set_id": VERSION_SET,
                "baseline": entry.baseline,
                "comparison_family_id": entry.comparison_family_id,
            }
            for entry in COGNITIVE_STRATEGY_CATALOG_V1.entries(measurement_purpose="repair")
        ]
        # Discard the caller's catalog: resolve only the closed reviewed registry.
        report = build_cognitive_strategy_report(
            {
                **snapshot,
                "catalog": catalog,
                "exposures": list(records.values()),
            }
        )
        audit.update(
            {
                "report_sha256": report["content_sha256"],
                "as_of_root_fact_seq": snapshot["as_of_root_fact_seq"],
                "decided_at": now.isoformat(),
            }
        )
        evidence = {}
        for endpoint in ("immediate", "transfer", "retention"):
            payload = report["endpoints"][endpoint]
            if payload["status_counts"]["excluded"]:
                return fallback("invalid_evidence")
            strata = [s for s in payload["strata"] if s["stratum"]["hint_state"] == "none"]
            if len(strata) != 1:
                return fallback("insufficient_data")
            rows = {row["strategy_id"]: row for row in strata[0]["strategies"]}
            if set(rows) != {BASELINE, ALTERNATE} or any(row["eligible_n"] < 20 for row in rows.values()):
                return fallback("insufficient_data")
            comparisons = strata[0]["comparisons"]
            if len(comparisons) != 1 or comparisons[0]["status"] != "comparable":
                return fallback("insufficient_data")
            evidence[endpoint] = (rows, comparisons[0])
        for strategy in (BASELINE, ALTERNATE):
            recent = [
                utc(row["exposed_at"])
                for row in records.values()
                if row["strategy_id"] == strategy
                and row.get("hint_state") == "none"
                and row.get("outcomes", {}).get("retention", {}).get("status") == "observed"
            ]
            if not recent or max(recent) < now - timedelta(days=7):
                return fallback("stale_evidence")
        if evidence["immediate"][1]["effect"]["value"] < 0:
            return fallback("conflicting_endpoints")
        strong = True
        for endpoint in ("transfer", "retention"):
            rows, comparison = evidence[endpoint]
            effect = comparison["effect"]["value"]
            lower = comparison["sensitivity_bounds"]["lower"]
            if effect <= 0 or lower < 0:
                return fallback("conflicting_endpoints")
            strong = strong and (
                effect >= 0.10
                and lower > 0
                and rows[ALTERNATE]["wilson_95"]["lower"] > rows[BASELINE]["wilson_95"]["upper"]
            )
        if not strong and explore is not True:
            return fallback("uncertain_evidence")
        selected = replace(plan, repair_strategy="minimal_change")
        return replace(
            decision,
            proposed_plan=selected,
            effective_plan=selected,
            repair_strategy="minimal_change",
            decision_trace=(*decision.decision_trace, f"{POLICY_VERSION}:alternate"),
        ), {
            **audit,
            "applied": True,
            "repair_strategy": "minimal_change",
            "reason": "supported_alternate" if strong else "bounded_exploration",
        }
    except (KeyError, ValueError, TypeError, OverflowError):
        return fallback("invalid_evidence")


def canonical_audit(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
