"""Guarded, non-personalized assignment for V3 repair strategy collection.

The assignment changes only the reviewed repair blueprint inside one already
accepted cognitive action.  It never selects a topic, infers a hypothesis,
looks at an answer/outcome, or consumes an offline attribution report.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

from .cognitive_catalog import COGNITIVE_CATALOG_V1
from .cognitive_policy import CognitivePolicyDecision
from .cognitive_strategy_catalog import COGNITIVE_STRATEGY_CATALOG_V1
from .contracts import RepairStrategy

STRATEGY_ROTATION_EXPERIMENT_VERSION = "chain-omit-inner-repair-rotation-v1"
STRATEGY_ROTATION_DEFAULT_ENABLED = False

_TOPIC_ID = "calculus.chain_rule"
_HYPOTHESIS_CODE = "omit_inner_derivative"
_INTENT = "misconception_repair"
_COMPARISON_SCOPE_ID = "chain.omit-inner.repair"
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class StrategyRotationAssignment:
    """One reproducible assignment decision with no raw learner identity."""

    eligible: bool
    applied: bool
    reason: str
    experiment_version: str = STRATEGY_ROTATION_EXPERIMENT_VERSION
    comparison_scope_id: str = _COMPARISON_SCOPE_ID
    bucket: int | None = None
    variant: str = ""
    repair_strategy: RepairStrategy = ""
    assignment_key_sha256: str = ""

    def to_metadata(self) -> dict[str, object]:
        """Return the bounded audit payload stored beside intervention facts."""

        return {
            "eligible": self.eligible,
            "applied": self.applied,
            "reason": self.reason,
            "experiment_version": self.experiment_version,
            "comparison_scope_id": self.comparison_scope_id,
            "bucket": self.bucket,
            "variant": self.variant,
            "repair_strategy": self.repair_strategy,
            "assignment_key_sha256": self.assignment_key_sha256,
        }


def _assignment_key(decision: CognitivePolicyDecision) -> str:
    hypothesis = decision.selected_hypothesis
    if hypothesis is None:
        return ""
    hypothesis_id = str(hypothesis.hypothesis_id or "").strip()
    source_attempt_id = str(hypothesis.source_attempt_id or "").strip()
    if not hypothesis_id or not source_attempt_id:
        return ""
    canonical_topic = COGNITIVE_CATALOG_V1.canonical_topic_id(hypothesis.topic_id)
    if canonical_topic is None:
        return ""
    return json.dumps(
        [
            STRATEGY_ROTATION_EXPERIMENT_VERSION,
            canonical_topic,
            hypothesis.code,
            hypothesis_id,
            source_attempt_id,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _reviewed_variants() -> tuple[RepairStrategy, RepairStrategy] | None:
    entries = tuple(
        entry
        for entry in COGNITIVE_STRATEGY_CATALOG_V1.entries(
            topic_id=_TOPIC_ID,
            hypothesis_code=_HYPOTHESIS_CODE,
            measurement_purpose="repair",
            shadow_collection_eligible=True,
        )
        if entry.comparison_scope_id == _COMPARISON_SCOPE_ID
    )
    baseline = tuple(entry for entry in entries if entry.baseline)
    alternatives = tuple(entry for entry in entries if not entry.baseline)
    if len(baseline) != 1 or len(alternatives) != 1:
        return None
    return baseline[0].repair_strategy, alternatives[0].repair_strategy


def apply_guarded_strategy_rotation(
    decision: CognitivePolicyDecision,
    *,
    enabled: bool = STRATEGY_ROTATION_DEFAULT_ENABLED,
    retry_assignment: Mapping[str, object] | None = None,
) -> tuple[CognitivePolicyDecision, StrategyRotationAssignment]:
    """Assign one reviewed repair variant without changing Coach-owned fields."""

    if enabled is not True:
        return decision, StrategyRotationAssignment(False, False, "disabled")
    proposed = decision.proposed_plan
    hypothesis = decision.selected_hypothesis
    if (
        decision.mode != "on"
        or not decision.applied
        or proposed is None
        or hypothesis is None
    ):
        return decision, StrategyRotationAssignment(False, False, "inactive_decision")
    if (
        COGNITIVE_CATALOG_V1.canonical_topic_id(proposed.target_topic.id) != _TOPIC_ID
        or COGNITIVE_CATALOG_V1.canonical_topic_id(hypothesis.topic_id) != _TOPIC_ID
        or hypothesis.code != _HYPOTHESIS_CODE
        or decision.proposed_intent != _INTENT
        or proposed.learning_intent != _INTENT
    ):
        return decision, StrategyRotationAssignment(False, False, "outside_frozen_scope")

    variants = _reviewed_variants()
    if variants is None:
        return decision, StrategyRotationAssignment(False, False, "catalog_not_ready")
    baseline_strategy, alternate_strategy = variants
    if (
        decision.repair_strategy != baseline_strategy
        or proposed.repair_strategy != baseline_strategy
    ):
        return decision, StrategyRotationAssignment(False, False, "unexpected_source_strategy")

    prior = retry_assignment if isinstance(retry_assignment, Mapping) else {}
    prior_digest = str(prior.get("assignment_key_sha256") or "").strip()
    prior_bucket = prior.get("bucket")
    prior_variant = str(prior.get("variant") or "").strip()
    prior_strategy = str(prior.get("repair_strategy") or "").strip()
    reusable = (
        prior.get("experiment_version") == STRATEGY_ROTATION_EXPERIMENT_VERSION
        and prior.get("comparison_scope_id") == _COMPARISON_SCOPE_ID
        and prior.get("applied") is True
        and isinstance(prior_bucket, int)
        and not isinstance(prior_bucket, bool)
        and prior_bucket in {0, 1}
        and _SHA256.fullmatch(prior_digest) is not None
        and (prior_bucket, prior_variant, prior_strategy)
        in {
            (0, "baseline", baseline_strategy),
            (1, "alternate", alternate_strategy),
        }
    )
    if reusable:
        bucket = int(prior_bucket)
        variant = prior_variant
        selected = baseline_strategy if bucket == 0 else alternate_strategy
        digest = prior_digest
        reason = "reused_retry_assignment"
    else:
        raw_key = _assignment_key(decision)
        if not raw_key:
            return decision, StrategyRotationAssignment(False, False, "missing_assignment_identity")
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        bucket = int(digest[:16], 16) % 2
        selected = baseline_strategy if bucket == 0 else alternate_strategy
        variant = "baseline" if bucket == 0 else "alternate"
        reason = "assigned"
    rotated_plan = replace(proposed, repair_strategy=selected)
    rotated = replace(
        decision,
        proposed_plan=rotated_plan,
        effective_plan=rotated_plan,
        repair_strategy=selected,
        decision_trace=(
            *decision.decision_trace,
            f"strategy_rotation:{STRATEGY_ROTATION_EXPERIMENT_VERSION}",
            f"strategy_rotation:bucket:{bucket}",
            f"strategy_rotation:variant:{variant}",
        ),
    )
    return rotated, StrategyRotationAssignment(
        eligible=True,
        applied=True,
        reason=reason,
        bucket=bucket,
        variant=variant,
        repair_strategy=selected,
        assignment_key_sha256=digest,
    )


__all__ = [
    "STRATEGY_ROTATION_DEFAULT_ENABLED",
    "STRATEGY_ROTATION_EXPERIMENT_VERSION",
    "StrategyRotationAssignment",
    "apply_guarded_strategy_rotation",
]
