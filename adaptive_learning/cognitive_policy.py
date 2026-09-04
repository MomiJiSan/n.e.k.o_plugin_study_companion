"""Deterministic, topic-preserving cognitive intent decoration.

This module never selects a topic. It produces an auditable proposal after the
Coach has already chosen one; in ``shadow`` mode the effective plan is always
the exact original plan.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from typing import Literal, Mapping

from .cognitive_state import LearnerCognitiveHypothesis, LearnerCognitiveStateView
from .contracts import (
    HypothesisRef,
    LearningActionCandidate,
    LearningIntent,
    QuestionPlan,
    RepairStrategy,
)

CognitiveIntentMode = Literal["off", "shadow", "on"]

_ACTIVE_V2_HYPOTHESES = frozenset({"omit_inner_derivative"})
_KNOWN_V2_HYPOTHESES = frozenset(
    {
        "omit_inner_derivative",
        "differentiate_inner_incorrectly",
        "confuse_product_and_chain",
    }
)
_POSITIVE_OUTCOMES = frozenset({"confirmed", "correct", "counter", "passed", "success"})
_STRATEGIES: Mapping[tuple[str, LearningIntent], RepairStrategy] = {
    ("omit_inner_derivative", "misconception_probe"): "structure_classification",
    ("omit_inner_derivative", "misconception_repair"): "complete_inner_derivative",
    ("omit_inner_derivative", "transfer_check"): "cross_form_transfer",
    ("differentiate_inner_incorrectly", "misconception_probe"): "compare_steps",
    ("differentiate_inner_incorrectly", "misconception_repair"): "complete_inner_derivative",
    ("differentiate_inner_incorrectly", "transfer_check"): "cross_form_transfer",
    ("confuse_product_and_chain", "misconception_probe"): "structure_classification",
    ("confuse_product_and_chain", "misconception_repair"): "compare_steps",
    ("confuse_product_and_chain", "transfer_check"): "cross_form_transfer",
}


@dataclass(frozen=True, slots=True)
class CognitivePolicyDecision:
    """A proposal, its effective result, and a compact reproducible audit trace."""

    mode: CognitiveIntentMode
    original_plan: QuestionPlan
    proposed_plan: QuestionPlan | None
    effective_plan: QuestionPlan
    proposed_intent: LearningIntent = "practice"
    selected_hypothesis: HypothesisRef | None = None
    action_candidate: LearningActionCandidate | None = None
    repair_strategy: RepairStrategy = ""
    applied: bool = False
    fallback_reason: str = ""
    original_fingerprint: str = ""
    proposal_fingerprint: str = ""
    decision_trace: tuple[str, ...] = ()


class CognitiveIntentPolicy:
    """Submit one hypothesis-scoped intent without changing Coach ownership."""

    def __init__(
        self,
        *,
        mode: CognitiveIntentMode = "off",
        active_hypothesis_codes: frozenset[str] = _ACTIVE_V2_HYPOTHESES,
    ) -> None:
        if mode not in {"off", "shadow", "on"}:
            raise ValueError("unsupported cognitive intent policy mode")
        if not active_hypothesis_codes.issubset(_KNOWN_V2_HYPOTHESES):
            raise ValueError("active cognitive hypothesis is outside the V2 catalog")
        self._mode = mode
        self._active_hypothesis_codes = active_hypothesis_codes

    def decorate(
        self,
        original_plan: QuestionPlan,
        state: LearnerCognitiveStateView,
    ) -> CognitivePolicyDecision:
        original_fingerprint = question_plan_ownership_fingerprint(original_plan)
        trace = [f"mode:{self._mode}"]
        if self._mode == "off":
            return self._fallback(
                original_plan, original_fingerprint, "policy_off", trace
            )
        if original_plan.learning_intent != "practice":
            trace.append(f"plan_intent:{original_plan.learning_intent}")
            return self._fallback(
                original_plan,
                original_fingerprint,
                "planner_intent_already_set",
                trace,
            )
        if not state.usable:
            trace.append(f"state:{state.reason}")
            return self._fallback(
                original_plan, original_fingerprint, "state_unavailable", trace
            )
        if state.topic_id != original_plan.target_topic.id:
            trace.append("topic:mismatch")
            return self._fallback(
                original_plan, original_fingerprint, "topic_mismatch", trace
            )
        eligible = original_plan.selection.eligible_topic_ids
        if eligible and original_plan.target_topic.id not in eligible:
            trace.append("scope:target_not_eligible")
            return self._fallback(
                original_plan, original_fingerprint, "target_not_eligible", trace
            )

        hypothesis = self._select_hypothesis(state)
        if hypothesis is None:
            trace.append("hypothesis:none_eligible")
            return self._fallback(
                original_plan, original_fingerprint, "no_eligible_hypothesis", trace
            )
        intent = _next_intent(hypothesis)
        if intent is None:
            trace.append(f"stage:{hypothesis.intervention_stage}:no_action")
            return self._fallback(
                original_plan, original_fingerprint, "no_cognitive_action", trace
            )
        if self._mode == "on" and (
            hypothesis.evidence_status != "supported"
            or hypothesis.ref.code not in self._active_hypothesis_codes
            or not hypothesis.ref.source_snapshot_id
            or hypothesis.ref.projection_generation <= 0
            or hypothesis.ref.projection_generation != state.projected_generation
        ):
            trace.append("active:not_supported_or_not_enabled")
            return self._fallback(
                original_plan, original_fingerprint, "active_hypothesis_not_eligible", trace
            )

        strategy = _STRATEGIES.get((hypothesis.ref.code, intent), "")
        if not strategy:
            trace.append("strategy:missing")
            return self._fallback(
                original_plan, original_fingerprint, "strategy_missing", trace
            )
        try:
            candidate = LearningActionCandidate(
                source="cognitive_engine",
                topic_id=hypothesis.ref.topic_id,
                intent=intent,
                urgency=max(0.0, min(1.0, hypothesis.ref.probability)),
                expected_learning_gain=0.5,
                information_gain=(0.8 if intent == "misconception_probe" else 0.5),
                evidence_refs=tuple(
                    value
                    for value in (
                        hypothesis.ref.source_snapshot_id,
                        hypothesis.ref.source_attempt_id,
                    )
                    if value
                ),
                satisfies=(f"cognitive_hypothesis:{hypothesis.ref.hypothesis_id}",),
            )
            proposed = replace(
                original_plan,
                learning_intent=intent,
                hypothesis_target=hypothesis.ref,
                repair_strategy=strategy,
            )
        except (TypeError, ValueError):
            trace.append("contract:decoration_failed")
            return self._fallback(
                original_plan, original_fingerprint, "decoration_failed", trace
            )

        proposal_fingerprint = question_plan_ownership_fingerprint(proposed)
        if proposal_fingerprint != original_fingerprint:
            trace.append("ownership:fingerprint_changed")
            return self._fallback(
                original_plan, original_fingerprint, "ownership_invariant_changed", trace
            )
        trace.extend(
            (
                f"hypothesis:{hypothesis.ref.code}",
                f"evidence:{hypothesis.evidence_status}",
                f"stage:{hypothesis.intervention_stage}",
                f"intent:{intent}",
                f"strategy:{strategy}",
                "ownership:preserved",
            )
        )
        applied = self._mode == "on"
        return CognitivePolicyDecision(
            mode=self._mode,
            original_plan=original_plan,
            proposed_plan=proposed,
            effective_plan=proposed if applied else original_plan,
            proposed_intent=intent,
            selected_hypothesis=hypothesis.ref,
            action_candidate=candidate,
            repair_strategy=strategy,
            applied=applied,
            original_fingerprint=original_fingerprint,
            proposal_fingerprint=proposal_fingerprint,
            decision_trace=tuple(trace),
        )

    def _select_hypothesis(
        self, state: LearnerCognitiveStateView
    ) -> LearnerCognitiveHypothesis | None:
        candidates = [
            item
            for item in state.hypotheses
            if item.ref.topic_id == state.topic_id
            and item.ref.model_version == state.model_version
            and item.ref.code in _KNOWN_V2_HYPOTHESES
            and item.evidence_status in {"hypothesized", "supported"}
        ]
        if self._mode == "on":
            candidates = [
                item
                for item in candidates
                if item.evidence_status == "supported"
                and item.ref.code in self._active_hypothesis_codes
            ]
        if not candidates:
            return None
        candidates.sort(key=_policy_priority)
        return candidates[0]

    def _fallback(
        self,
        plan: QuestionPlan,
        fingerprint: str,
        reason: str,
        trace: list[str],
    ) -> CognitivePolicyDecision:
        return CognitivePolicyDecision(
            mode=self._mode,
            original_plan=plan,
            proposed_plan=None,
            effective_plan=plan,
            fallback_reason=reason,
            original_fingerprint=fingerprint,
            decision_trace=tuple(trace),
        )


def question_plan_ownership_fingerprint(plan: QuestionPlan) -> str:
    """Fingerprint every plan field except cognitive candidate decorations."""

    cognitive_fields = {
        "learning_intent",
        "hypothesis_target",
        "repair_strategy",
        "obligation_refs",
        "cognitive_strategy",
    }
    payload = {
        item.name: _json_value(getattr(plan, item.name))
        for item in fields(plan)
        if item.name not in cognitive_fields
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _next_intent(item: LearnerCognitiveHypothesis) -> LearningIntent | None:
    if item.consecutive_repair_failures >= 2:
        return None
    if item.evidence_status == "hypothesized":
        return "misconception_probe"
    if item.evidence_status != "supported":
        return None
    stage = item.intervention_stage
    last_intent = item.last_intent
    last_outcome = item.last_outcome.lower()
    if stage == "idle":
        return "misconception_probe"
    if stage == "probing":
        if last_intent == "misconception_probe" and last_outcome in _POSITIVE_OUTCOMES:
            return "misconception_repair"
        return None
    if stage == "remediating":
        if last_intent == "misconception_repair" and last_outcome in _POSITIVE_OUTCOMES:
            return "transfer_check"
        if last_intent == "misconception_repair" and last_outcome:
            # Permit one same-strategy retry. The failure-count guard at the
            # top stops cognitive intervention after the second consecutive
            # failure in this session.
            return "misconception_repair"
        return None
    if stage == "provisionally_resolved":
        return "transfer_check"
    return None


def _policy_priority(item: LearnerCognitiveHypothesis) -> tuple[object, ...]:
    ongoing_rank = 0 if item.intervention_stage != "idle" else 1
    support_rank = 0 if item.evidence_status == "supported" else 1
    return (ongoing_rank, support_rank, -item.ref.probability, item.ref.code)


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = [
    "CognitiveIntentMode",
    "CognitiveIntentPolicy",
    "CognitivePolicyDecision",
    "question_plan_ownership_fingerprint",
]
