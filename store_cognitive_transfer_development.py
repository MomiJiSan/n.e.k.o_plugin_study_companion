"""Audited re-delivery for the frozen, uncertified legacy transfer only.

No historical fact, projection, or clock is rewritten. The normal question
ledger is the durable request record, and its transaction fences delivery.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from .adaptive_learning.cognitive_catalog import COGNITIVE_CATALOG_V1
from .adaptive_learning.cognitive_policy import CognitivePolicyDecision
from .adaptive_learning.contracts import HypothesisRef, LearningActionCandidate
from .store_cognitive_development import (
    _current_hypothesis,
    _json_mapping,
    _PreparationBlocked,
    _reject_active_control,
    _require_active_retention_config,
    _utc_now,
)
from .store_cognitive_outbox import _private_question_value

DEV_ENV = "STUDY_COMPANION_COGNITIVE_DEV_TOOLS"
TOPIC = "college_chain_rule"
CODE = "omit_inner_derivative"
CONFIRMATION = "RETEST_TRANSFER_FOR_DEVELOPMENT"
AUDIT_KEY = "development_transfer_retest"
BLUEPRINT = "chain.omit-inner.cross-form-transfer.v2-retest"


def _inspect_transfer_retest(store: Any, conn: Any, source: str) -> dict[str, Any]:
    if os.environ.get(DEV_ENV) != "1":
        raise _PreparationBlocked("dev_tools_disabled")
    model = _require_active_retention_config(store, conn)
    _reject_active_control(conn, topic_id=TOPIC, hypothesis_code=CODE, now=_utc_now())
    current = _current_hypothesis(
        conn, topic_id=TOPIC, hypothesis_code=CODE,
        expected_source_attempt_id=source, model_version=model,
    )
    source = str(current["source_attempt_id"])
    if conn.execute(
        "SELECT 1 FROM cognitive_monitoring_episodes WHERE hypothesis_id = ? LIMIT 1",
        (current["hypothesis_id"],),
    ).fetchone():
        raise _PreparationBlocked("episode_conflict")
    facts = conn.execute(
        """SELECT e.*, a.used_hint, a.topic_id AS attempt_topic,
                  v.evaluation_json, v.evaluator_type, v.evaluator_version,
                  v.confidence, q.question_json
           FROM cognitive_intervention_events e
           JOIN attempts a ON a.attempt_id = e.attempt_id AND a.question_id = e.question_id
           JOIN evaluations v ON v.attempt_id = a.attempt_id
           JOIN question_instances q ON q.question_id = a.question_id
           WHERE e.attempt_id = ? AND e.event_type = 'attempt_committed'
             AND e.learning_intent = 'transfer_check' AND e.evaluation_verdict = 'correct'
             AND e.topic_id = ? AND e.hypothesis_code = ? AND e.hypothesis_model_version = ?""",
        (source, TOPIC, CODE, model),
    ).fetchall()
    if len(facts) != 1:
        raise _PreparationBlocked("legacy_transfer_required")
    fact = facts[0]
    evaluation = _json_mapping(store, fact["evaluation_json"])
    question = _json_mapping(store, fact["question_json"])
    if (
        fact["used_hint"] != 0 or fact["attempt_topic"] != TOPIC
        or evaluation.get("verdict") != "correct"
        or any(evaluation.get(key) is not None for key in ("evaluator_type", "evaluator_version", "confidence"))
        or fact["evaluator_type"] != "llm_rubric"
        or fact["evaluator_version"] != "legacy-v1" or fact["confidence"] is not None
        or fact["blueprint_id"] != "chain.omit-inner.cross-form-transfer.v1"
        or fact["question_family_id"] != "chain.polynomial-power.cross-form-transfer"
        or not fact["diagnostic_validation_id"] or not fact["validator_version"]
        or any(str(_private_question_value(question, key) or "") != str(fact[column])
               for key, column in (
                   ("cognitive_blueprint_id", "blueprint_id"),
                   ("cognitive_question_family_id", "question_family_id"),
                   ("cognitive_validator_version", "validator_version"),
                   ("diagnostic_validation_id", "diagnostic_validation_id"),
               ))
    ):
        raise _PreparationBlocked("legacy_transfer_required")
    outbox = conn.execute(
        """SELECT payload_json FROM cognitive_outbox WHERE attempt_id = ?
           AND event_id = ? AND operation = 'intervention_event' AND status = 'done'""",
        (source, fact["event_id"]),
    ).fetchall()
    if len(outbox) != 1:
        raise _PreparationBlocked("legacy_transfer_required")
    payload = _json_mapping(store, outbox[0]["payload_json"])
    if any(payload.get(key) != fact[key] for key in (
        "event_id", "attempt_id", "question_id", "learning_intent", "evaluation_verdict",
        "diagnostic_validation_id", "blueprint_id", "question_family_id", "validator_version",
    )):
        raise _PreparationBlocked("legacy_transfer_required")
    # Reject a second delivery until the earlier question has been explicitly
    # abandoned. Answered retests are terminal for this legacy source.
    prior = conn.execute(
        """SELECT q.question_id FROM cognitive_intervention_events q
           WHERE q.event_type = 'question_committed' AND q.topic_id = ?
             AND q.hypothesis_code = ? AND q.hypothesis_source_attempt_id = ?
             AND NOT EXISTS (SELECT 1 FROM cognitive_intervention_events a
                 WHERE a.event_type = 'intervention_abandoned'
                   AND a.decision_id = q.decision_id AND a.question_id = q.question_id)
           LIMIT 1""", (TOPIC, CODE, source),
    ).fetchone()
    if prior:
        raise _PreparationBlocked("retest_already_generated")
    return dict(current)


def preview_transfer_retest(store: Any, source: str = "") -> dict[str, Any]:
    try:
        with store._lock:
            current = _inspect_transfer_retest(store, store._require_conn(), source)
        return {"enabled": True, "status": "ready", "reason_code": "",
                "source_attempt_id": current["source_attempt_id"]}
    except _PreparationBlocked as exc:
        return {"enabled": exc.reason_code != "dev_tools_disabled", "status": "blocked",
                "reason_code": exc.reason_code}


def propose_transfer_retest(store: Any, plan: Any, source: str) -> CognitivePolicyDecision:
    with store._lock:
        current = _inspect_transfer_retest(store, store._require_conn(), source)
    if (
        not source or plan.target_topic.id != TOPIC or plan.learning_intent != "practice"
        or plan.selection.reason == "blocked_diagnostic"
        or TOPIC not in plan.selection.eligible_topic_ids
    ):
        raise _PreparationBlocked("selection_conflict")
    hypothesis = HypothesisRef(
        hypothesis_id=str(current["hypothesis_id"]), topic_id=TOPIC, code=CODE,
        status="supported", probability=float(current["probability"]),
        model_version=str(current["model_version"]),
        source_snapshot_id=str(current["source_snapshot_id"]), source_attempt_id=source,
        projection_generation=int(current["projected_generation"]),
    )
    proposed = replace(plan, learning_intent="transfer_check", hypothesis_target=hypothesis,
                       repair_strategy="cross_form_transfer")
    candidate = LearningActionCandidate(
        source="cognitive_engine", topic_id=TOPIC, intent="transfer_check",
        urgency=0.5, expected_learning_gain=0.5, information_gain=0.5,
        evidence_refs=(source, hypothesis.source_snapshot_id),
        satisfies=(f"cognitive_hypothesis:{hypothesis.hypothesis_id}",),
    )
    return CognitivePolicyDecision(
        mode="on", original_plan=plan, proposed_plan=proposed, effective_plan=proposed,
        proposed_intent="transfer_check", selected_hypothesis=hypothesis,
        action_candidate=candidate, repair_strategy="cross_form_transfer", applied=True,
        decision_trace=("development:legacy_transfer_retest", f"source:{source}"),
    )


def bind_transfer_retest(prepared: Any, source: str) -> Any:
    if prepared is None or not prepared.active or prepared.proposed_plan.learning_intent != "transfer_check":
        raise _PreparationBlocked("selection_conflict")
    if prepared.proposal_event.hypothesis_target.source_attempt_id != source:
        raise _PreparationBlocked("source_attempt_mismatch")
    return replace(prepared, blueprint=COGNITIVE_CATALOG_V1.get_blueprint(BLUEPRINT))


def validate_transfer_retest_commit(store: Any, conn: Any, event: Any) -> None:
    metadata = event.get("metadata") or {}
    source = str(metadata.get("legacy_source_attempt_id") or "")
    if not source or metadata.get(AUDIT_KEY) is not True:
        raise _PreparationBlocked("source_attempt_mismatch")
    current = _inspect_transfer_retest(store, conn, source)
    target = event.get("hypothesis_target") or {}
    if (target.get("source_attempt_id") != source
            or target.get("hypothesis_id") != current["hypothesis_id"]
            or event.get("learning_intent") != "transfer_check"
            or event.get("blueprint_id") != BLUEPRINT):
        raise _PreparationBlocked("source_attempt_mismatch")
