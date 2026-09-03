"""Cognitive-only transactional outbox.

Answer facts own the transaction.  Cognitive delivery may fail independently:
the durable outbox row records only server identities and can be retried without
copying the learner's raw answer.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from typing import Any

from .adaptive_learning.cognitive_catalog import COGNITIVE_CATALOG_V1
from .adaptive_learning.cognitive_retention import (
    RETENTION_COGNITIVE_STRATEGY,
    RetentionValidationInput,
    RetentionValidator,
)
from .adaptive_learning.cognitive_versions import (
    DEFAULT_COGNITIVE_VERSION_SET,
    get_cognitive_version_set,
)
from .store_cognitive_retention import (
    ACTIVE_RETENTION_HYPOTHESIS,
    apply_cognitive_retention_disposition,
    insert_certified_transfer_episode,
)
from .store_common import sqlite3

_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "answer",
        "learner_answer",
        "user_answer",
        "raw_answer",
        "expected_answer",
    }
)


def _identity_only(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _identity_only(child)
            for key, child in value.items()
            if str(key).strip().lower() not in _FORBIDDEN_PAYLOAD_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_identity_only(child) for child in value]
    return value


class _RetryableRetentionDelivery(RuntimeError):
    pass


class _DiscardRetentionDelivery(RuntimeError):
    pass


def _private_question_value(question: Mapping[str, Any], key: str) -> object:
    """Read server-only provenance and reject conflicting mirrored values."""

    binding = question.get("target_binding")
    nested = binding.get(key) if isinstance(binding, Mapping) else None
    direct = question.get(key)
    direct_present = direct not in (None, "", (), [])
    nested_present = nested not in (None, "", (), [])
    if direct_present and nested_present and direct != nested:
        raise ValueError(f"retention question has conflicting {key}")
    return direct if direct_present else nested


def _required_question_identity(question: Mapping[str, Any], key: str) -> str:
    value = str(_private_question_value(question, key) or "").strip()
    if not value:
        raise ValueError(f"retention question {key} is required")
    return value


def _retention_context(
    self: Any,
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve retention identities from canonical attempt/question facts."""

    attempt = conn.execute(
        """
        SELECT attempts.attempt_id, attempts.question_id, attempts.topic_id,
               attempts.used_hint, attempts.submitted_at,
               questions.question_json, evaluations.evaluation_json,
               evaluations.evaluator_type, evaluations.evaluator_version,
               evaluations.confidence
        FROM attempts
        JOIN question_instances questions
          ON questions.question_id = attempts.question_id
        LEFT JOIN evaluations ON evaluations.attempt_id = attempts.attempt_id
        WHERE attempts.attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    if attempt is None:
        raise ValueError("retention outbox attempt fact is missing")
    question = self._json_loads(attempt["question_json"], {})
    if not isinstance(question, dict):
        raise ValueError("retention question fact is invalid")
    if (
        str(_private_question_value(question, "cognitive_strategy") or "").strip()
        != RETENTION_COGNITIVE_STRATEGY
    ):
        raise ValueError("retention question strategy is detached")
    episode_id = _required_question_identity(question, "cognitive_episode_id")
    obligation_id = _required_question_identity(
        question, "cognitive_obligation_id"
    )
    claim_id = _required_question_identity(question, "cognitive_claim_id")
    claim_token = _required_question_identity(question, "cognitive_claim_token")
    worker_id = _required_question_identity(
        question, "cognitive_claim_worker_id"
    )
    lease_expires_at = _required_question_identity(
        question, "cognitive_claim_lease_expires_at"
    )
    obligation_refs = _private_question_value(question, "obligation_refs")
    if not isinstance(obligation_refs, (list, tuple)) or tuple(
        str(item or "").strip() for item in obligation_refs
    ) != (obligation_id,):
        raise ValueError("retention question obligation_refs is detached")
    stored = conn.execute(
        """
        SELECT obligations.*, episodes.status AS episode_status,
               episodes.model_version,
               episodes.transfer_question_family_id,
               claims.claim_id AS stored_claim_id,
               claims.claim_token AS stored_claim_token,
               claims.worker_id AS stored_worker_id,
               claims.status AS claim_status,
               claims.lease_expires_at AS stored_lease_expires_at
        FROM cognitive_learning_obligations obligations
        JOIN cognitive_monitoring_episodes episodes
          ON episodes.episode_id = obligations.episode_id
        JOIN cognitive_obligation_claims claims
          ON claims.claim_id = ? AND claims.obligation_id = obligations.obligation_id
        WHERE obligations.obligation_id = ?
        """,
        (claim_id, obligation_id),
    ).fetchone()
    if stored is None:
        raise ValueError("retention question claim is detached")
    expected = {
        "episode_id": episode_id,
        "stored_claim_id": claim_id,
        "stored_claim_token": claim_token,
        "stored_worker_id": worker_id,
        "stored_lease_expires_at": lease_expires_at,
    }
    if any(str(stored[name] or "").strip() != value for name, value in expected.items()):
        raise ValueError("retention question ownership is detached")
    if (
        str(stored["obligation_type"] or "") != "retention"
        or str(stored["hypothesis_code"] or "")
        != ACTIVE_RETENTION_HYPOTHESIS
        or str(stored["topic_id"] or "") != str(attempt["topic_id"] or "")
    ):
        raise ValueError("retention question obligation is detached")
    transfer_family = _required_question_identity(
        question, "cognitive_transfer_question_family_id"
    )
    if transfer_family != str(stored["transfer_question_family_id"] or ""):
        raise ValueError("retention question transfer family is detached")
    identities = {
        "attempt_id": str(attempt["attempt_id"]),
        "episode_id": episode_id,
        "obligation_id": obligation_id,
        "claim_id": claim_id,
        "claim_token": claim_token,
        "worker_id": worker_id,
        "hypothesis_code": str(stored["hypothesis_code"]),
    }
    if payload is not None and any(
        str(payload.get(key) or "").strip() != value
        for key, value in identities.items()
    ):
        raise ValueError("retention outbox identities are detached")
    return {
        **identities,
        "attempt": attempt,
        "question": question,
        "obligation": stored,
    }


def enqueue_cognitive_retention_outbox(
    self: Any,
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
) -> str:
    """Validate one server-authored retention question and enqueue identities."""

    context = _retention_context(self, conn, attempt_id=attempt_id)
    event = {
        "event_id": (
            f"cognitive-retention:{context['attempt_id']}:{context['obligation_id']}"
        ),
        **{
            key: context[key]
            for key in (
                "attempt_id",
                "episode_id",
                "obligation_id",
                "claim_id",
                "claim_token",
                "worker_id",
                "hypothesis_code",
            )
        },
    }
    return enqueue_cognitive_outbox(
        self,
        conn,
        attempt_id=str(context["attempt_id"]),
        event=event,
        operation="retention_disposition",
    )


def _retention_observed_hypothesis(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    target_hypothesis: str,
) -> str:
    queue = conn.execute(
        """SELECT status FROM cognitive_extraction_queue
        WHERE attempt_id = ? ORDER BY extractor_version""",
        (attempt_id,),
    ).fetchall()
    if not queue or any(str(row["status"] or "") != "done" for row in queue):
        raise _RetryableRetentionDelivery("retention evidence is not ready")
    evidence = conn.execute(
        """SELECT hypothesis_code FROM cognitive_evidence
        WHERE attempt_id = ? AND direction = 'support'
        ORDER BY CASE WHEN hypothesis_code = ? THEN 0 ELSE 1 END,
                 root_fact_seq, evidence_id""",
        (attempt_id, target_hypothesis),
    ).fetchall()
    return str(evidence[0]["hypothesis_code"] or "") if evidence else ""


def _previous_retention_provenance(
    self: Any, conn: sqlite3.Connection, episode_id: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rows = conn.execute(
        """SELECT metadata_json FROM cognitive_obligation_satisfactions
        WHERE episode_id = ? ORDER BY occurred_at, satisfaction_id""",
        (episode_id,),
    ).fetchall()
    groups: list[str] = []
    families: list[str] = []
    for row in rows:
        metadata = self._json_loads(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            continue
        group = str(metadata.get("independence_group") or "").strip()
        family = str(metadata.get("question_family_id") or "").strip()
        if group and group not in groups:
            groups.append(group)
        if family and family not in families:
            families.append(family)
    return tuple(families), tuple(groups)


def _active_retention_delivery(conn: sqlite3.Connection, context: Mapping[str, Any]) -> None:
    obligation = context["obligation"]
    if (
        str(obligation["episode_status"] or "") != "open"
        or str(obligation["status"] or "") != "claimed"
        or str(obligation["current_claim_id"] or "") != str(context["claim_id"])
        or str(obligation["claim_status"] or "") != "active"
    ):
        raise _DiscardRetentionDelivery("stale retention claim")
    lease_active = conn.execute(
        "SELECT julianday(?) > julianday('now')",
        (str(obligation["stored_lease_expires_at"] or ""),),
    ).fetchone()[0]
    if not lease_active:
        raise _DiscardRetentionDelivery("stale retention claim lease")
    control = conn.execute(
        """SELECT action, expires_at FROM cognitive_user_controls
        WHERE topic_id = ? AND hypothesis_code = ?
        ORDER BY root_fact_seq DESC, rowid DESC LIMIT 1""",
        (str(obligation["topic_id"]), str(obligation["hypothesis_code"])),
    ).fetchone()
    if control is None:
        return
    action = str(control["action"] or "")
    active = action in {"dismiss", "delete"} or (
        action == "suppress"
        and bool(str(control["expires_at"] or "").strip())
        and bool(
            conn.execute(
                "SELECT julianday(?) > julianday('now')",
                (str(control["expires_at"]),),
            ).fetchone()[0]
        )
    )
    if active:
        raise _DiscardRetentionDelivery("stale retention control")


def _apply_retention_delivery(
    self: Any,
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    payload: Mapping[str, Any],
) -> None:
    context = _retention_context(
        self,
        conn,
        attempt_id=attempt_id,
        payload=payload,
    )
    _active_retention_delivery(conn, context)
    attempt = context["attempt"]
    question = context["question"]
    obligation = context["obligation"]
    evaluation = self._json_loads(attempt["evaluation_json"], {})
    if not isinstance(evaluation, dict):
        raise ValueError("retention evaluation fact is invalid")
    verdict = str(evaluation.get("verdict") or "").strip().lower()
    if verdict not in {"correct", "wrong", "partial", "dont_know"}:
        raise ValueError("retention evaluation verdict is unsupported")
    observed = ""
    if verdict != "correct":
        observed = _retention_observed_hypothesis(
            conn,
            attempt_id=attempt_id,
            target_hypothesis=str(context["hypothesis_code"]),
        )
    used_hint = (
        None if attempt["used_hint"] is None else bool(attempt["used_hint"])
    )
    confidence = (
        None if attempt["confidence"] is None else float(attempt["confidence"])
    )
    question_family = str(
        _private_question_value(question, "cognitive_question_family_id") or ""
    ).strip()
    independence_group = str(
        _private_question_value(question, "cognitive_independence_group") or ""
    ).strip()
    blueprint_version = str(
        _private_question_value(question, "retention_blueprint_version") or ""
    ).strip()
    validator_version = str(
        _private_question_value(question, "retention_validator_version") or ""
    ).strip()
    previous_families, previous_groups = _previous_retention_provenance(
        self, conn, str(context["episode_id"])
    )
    validation = RetentionValidator().validate(
        RetentionValidationInput(
            episode_id=str(context["episode_id"]),
            obligation_id=str(context["obligation_id"]),
            hypothesis_code=str(context["hypothesis_code"]),
            verdict=verdict,
            observed_hypothesis_code=observed,
            used_hint=used_hint,
            evaluator_type=str(attempt["evaluator_type"] or ""),
            evaluator_version=str(attempt["evaluator_version"] or ""),
            evaluator_confidence=confidence,
            answered_at=str(attempt["submitted_at"] or ""),
            not_before=str(obligation["not_before"] or ""),
            eligibility_until=str(obligation["eligibility_until"] or ""),
            question_family_id=question_family,
            transfer_question_family_id=str(
                obligation["transfer_question_family_id"] or ""
            ),
            independence_group=independence_group,
            previous_question_family_ids=previous_families,
            previous_independence_groups=previous_groups,
            blueprint_version=blueprint_version,
            validator_version=validator_version,
        )
    )
    metadata = {
        "certified": validation.certified,
        "reasons": list(validation.reasons),
        "used_hint": used_hint,
        "evaluator_type": str(attempt["evaluator_type"] or ""),
        "evaluator_version": str(attempt["evaluator_version"] or ""),
        "evaluator_confidence": confidence,
        "question_family_id": question_family,
        "independence_group": independence_group,
        "blueprint_id": str(
            _private_question_value(question, "cognitive_blueprint_id") or ""
        ).strip(),
        "blueprint_version": blueprint_version,
        "validator_version": validator_version,
    }
    try:
        apply_cognitive_retention_disposition(
            self,
            obligation_id=str(context["obligation_id"]),
            claim_token=str(context["claim_token"]),
            worker_id=str(context["worker_id"]),
            attempt_id=attempt_id,
            disposition=validation.disposition,
            occurred_at=str(attempt["submitted_at"] or ""),
            metadata=metadata,
            _connection=conn,
        )
    except ValueError as exc:
        if any(
            marker in str(exc)
            for marker in ("stale or not owned", "lease expired", "lost ownership")
        ):
            raise _DiscardRetentionDelivery(str(exc)) from exc
        raise


def _discard_outbox(
    conn: sqlite3.Connection,
    *,
    outbox_id: str,
    lease_token: str,
    reason: str,
) -> bool:
    updated = conn.execute(
        """
        UPDATE cognitive_outbox
        SET status = 'discarded', last_error = ?, lease_token = '',
            lease_expires_at = '', updated_at = datetime('now')
        WHERE outbox_id = ?
          AND ((? = '' AND status IN ('pending', 'failed'))
               OR (status = 'processing' AND lease_token = ?))
        """,
        (reason[:1000], outbox_id, lease_token, lease_token),
    )
    return updated.rowcount == 1


def _insert_requested_transfer_episode(
    self: Any,
    conn: sqlite3.Connection,
    payload: Mapping[str, Any],
) -> None:
    """Certify a transfer attempt from canonical facts, then open monitoring."""

    if payload.get("retention_episode_requested") is not True:
        return
    hypothesis = payload.get("hypothesis_target")
    if not isinstance(hypothesis, Mapping):
        raise ValueError("retention transfer hypothesis is required")
    attempt_id = str(payload.get("attempt_id") or "").strip()
    question_id = str(payload.get("question_id") or "").strip()
    topic_id = str(hypothesis.get("topic_id") or "").strip()
    hypothesis_code = str(hypothesis.get("code") or "").strip()
    model_version = str(hypothesis.get("model_version") or "").strip()
    if (
        str(payload.get("event_type") or "") != "attempt_committed"
        or str(payload.get("learning_intent") or "") != "transfer_check"
        or str(payload.get("evaluation_verdict") or "") != "correct"
        or hypothesis_code != ACTIVE_RETENTION_HYPOTHESIS
    ):
        raise ValueError("retention episode requires a certified active transfer")
    versions = get_cognitive_version_set(DEFAULT_COGNITIVE_VERSION_SET)
    if versions is None or model_version != versions.projection_version:
        raise ValueError("retention transfer version set is unsupported")
    validator_version = str(payload.get("validator_version") or "").strip()
    if validator_version != versions.validator_version:
        raise ValueError("retention transfer validator version is unsupported")
    blueprint_id = str(payload.get("blueprint_id") or "").strip()
    question_family_id = str(payload.get("question_family_id") or "").strip()
    blueprint = COGNITIVE_CATALOG_V1.get_blueprint(blueprint_id)
    if (
        blueprint is None
        or blueprint.learning_intent != "transfer_check"
        or blueprint.hypothesis_code != hypothesis_code
        or blueprint.question_family_id != question_family_id
        or COGNITIVE_CATALOG_V1.canonical_topic_id(topic_id)
        != blueprint.topic_id
    ):
        raise ValueError("retention transfer blueprint is not certified")
    fact = conn.execute(
        """
        SELECT attempts.question_id, attempts.topic_id, attempts.used_hint,
               attempts.submitted_at, questions.question_json,
               evaluations.evaluation_json, evaluations.evaluator_type,
               evaluations.evaluator_version, evaluations.confidence
        FROM attempts
        JOIN question_instances questions
          ON questions.question_id = attempts.question_id
        JOIN evaluations ON evaluations.attempt_id = attempts.attempt_id
        WHERE attempts.attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    if (
        fact is None
        or str(fact["question_id"] or "") != question_id
        or str(fact["topic_id"] or "") != topic_id
        or fact["used_hint"] != 0
    ):
        raise ValueError("retention transfer attempt facts are detached")
    evaluation = self._json_loads(fact["evaluation_json"], {})
    question = self._json_loads(fact["question_json"], {})
    if not isinstance(evaluation, dict) or not isinstance(question, dict):
        raise ValueError("retention transfer canonical facts are invalid")
    if str(evaluation.get("verdict") or "").strip().lower() != "correct":
        raise ValueError("retention transfer canonical verdict is not correct")
    evaluator_type = str(evaluation.get("evaluator_type") or "").strip()
    evaluator_version = str(evaluation.get("evaluator_version") or "").strip()
    stored_evaluator_type = str(fact["evaluator_type"] or "").strip()
    stored_evaluator_version = str(fact["evaluator_version"] or "").strip()
    try:
        evaluator_confidence = float(evaluation.get("confidence"))
        stored_evaluator_confidence = float(fact["confidence"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "retention transfer evaluator confidence is invalid"
        ) from exc
    if (
        not evaluator_type
        or not evaluator_version
        or evaluator_type != stored_evaluator_type
        or evaluator_version != stored_evaluator_version
        or not math.isfinite(evaluator_confidence)
        or not math.isfinite(stored_evaluator_confidence)
        or not 0.0 <= evaluator_confidence <= 1.0
        or not 0.0 <= stored_evaluator_confidence <= 1.0
        or evaluator_confidence != stored_evaluator_confidence
    ):
        raise ValueError("retention transfer evaluator provenance is invalid")
    expected_question_fields = {
        "cognitive_blueprint_id": blueprint_id,
        "cognitive_question_family_id": question_family_id,
        "cognitive_validator_version": validator_version,
        "diagnostic_validation_id": str(
            payload.get("diagnostic_validation_id") or ""
        ).strip(),
    }
    if any(
        str(_private_question_value(question, key) or "").strip() != expected
        for key, expected in expected_question_fields.items()
    ):
        raise ValueError("retention transfer question provenance is detached")
    insert_certified_transfer_episode(
        self,
        conn,
        {
            "hypothesis_id": str(hypothesis.get("hypothesis_id") or "").strip(),
            "topic_id": topic_id,
            "hypothesis_code": hypothesis_code,
            "model_version": model_version,
            "source_attempt_id": attempt_id,
            "source_event_id": str(payload.get("event_id") or "").strip(),
            "question_family_id": question_family_id,
            "evaluation_verdict": "correct",
            "certified": True,
            "used_hint": False,
            "occurred_at": str(fact["submitted_at"] or ""),
            "evaluator_type": evaluator_type,
            "evaluator_version": evaluator_version,
            "evaluator_confidence": evaluator_confidence,
        },
    )


def _stale_intervention_reason(
    conn: sqlite3.Connection, payload: Mapping[str, Any]
) -> str:
    if str(payload.get("event_type") or "").strip() != "attempt_committed":
        return ""
    binding = payload.get("binding")
    hypothesis = payload.get("hypothesis_target")
    if not isinstance(binding, Mapping) or not isinstance(hypothesis, Mapping):
        return ""
    topic_id = str(binding.get("topic_id") or "").strip()
    hypothesis_code = str(hypothesis.get("code") or "").strip()
    if topic_id and hypothesis_code:
        control = conn.execute(
            """
            SELECT action, expires_at
            FROM cognitive_user_controls
            WHERE topic_id = ? AND hypothesis_code = ?
            ORDER BY root_fact_seq DESC, rowid DESC
            LIMIT 1
            """,
            (topic_id, hypothesis_code),
        ).fetchone()
        if control is not None:
            action = str(control["action"] or "")
            active = action in {"dismiss", "delete"} or (
                action == "suppress"
                and bool(str(control["expires_at"] or "").strip())
                and bool(
                    conn.execute(
                        "SELECT julianday(?) > julianday('now')",
                        (str(control["expires_at"]),),
                    ).fetchone()[0]
                )
            )
            if active:
                return "stale_control"
    decision_id = str(payload.get("decision_id") or "").strip()
    question_id = str(payload.get("question_id") or "").strip()
    intent = str(payload.get("learning_intent") or "").strip()
    if decision_id and intent:
        abandoned = conn.execute(
            """
            SELECT 1 FROM cognitive_intervention_events
            WHERE event_type = 'intervention_abandoned'
              AND decision_id = ? AND learning_intent = ?
              AND (question_id = '' OR question_id = ?)
            LIMIT 1
            """,
            (decision_id, intent, question_id),
        ).fetchone()
        if abandoned is not None:
            return "stale_intervention"
    return ""


def enqueue_cognitive_outbox(
    self,
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    event: Mapping[str, Any],
    operation: str = "intervention_event",
) -> str:
    """Append an idempotent delivery request inside the answer transaction."""

    attempt_key = str(attempt_id or "").strip()
    if not attempt_key:
        raise ValueError("cognitive outbox attempt_id is required")
    operation_key = str(operation or "").strip()
    if operation_key not in {
        "intervention_event",
        "projection_enqueue",
        "retention_disposition",
    }:
        raise ValueError("unsupported cognitive outbox operation")
    event_id = str(event.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("cognitive outbox event_id is required")
    payload = _identity_only(dict(event))
    payload_json = self._json_dumps(payload)
    outbox_id = f"cognitive-outbox:{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO cognitive_outbox (
            outbox_id, attempt_id, event_id, operation, payload_json, status,
            retry_count, last_error, lease_token, lease_expires_at,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', 0, '', '', '',
                  datetime('now'), datetime('now'))
        ON CONFLICT(event_id) DO NOTHING
        """,
        (
            outbox_id,
            attempt_key,
            event_id,
            operation_key,
            payload_json,
        ),
    )
    row = conn.execute(
        """
        SELECT outbox_id, attempt_id, operation, payload_json
        FROM cognitive_outbox WHERE event_id = ?
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("cognitive outbox enqueue failed")
    if (
        str(row["attempt_id"]) != attempt_key
        or str(row["operation"]) != operation_key
        or str(row["payload_json"]) != payload_json
    ):
        raise ValueError("cognitive outbox event identity collision")
    return str(row["outbox_id"])


def deliver_cognitive_outbox_inline(
    self,
    conn: sqlite3.Connection,
    *,
    outbox_id: str,
    lease_token: str = "",
) -> dict[str, Any]:
    """Attempt delivery under a savepoint without endangering answer facts."""

    row = conn.execute(
        "SELECT * FROM cognitive_outbox WHERE outbox_id = ?", (outbox_id,)
    ).fetchone()
    if row is None:
        return {"recorded": False, "error": "outbox_missing"}
    if str(row["status"]) == "done":
        return {"recorded": True, "error": ""}
    token = str(lease_token or "").strip()
    row_status = str(row["status"] or "")
    row_token = str(row["lease_token"] or "")
    if token:
        lease_active = conn.execute(
            "SELECT julianday(?) > julianday('now')",
            (str(row["lease_expires_at"] or ""),),
        ).fetchone()[0]
        if row_status != "processing" or row_token != token or not lease_active:
            return {"recorded": False, "error": "lease_lost"}
    elif row_status not in {"pending", "failed"}:
        return {"recorded": False, "error": "not_retryable"}
    payload = self._json_loads(row["payload_json"], {})
    if not isinstance(payload, dict):
        payload = {}
    if str(row["operation"] or "") == "intervention_event":
        stale_reason = _stale_intervention_reason(conn, payload)
        if stale_reason:
            if not _discard_outbox(
                conn,
                outbox_id=outbox_id,
                lease_token=token,
                reason=stale_reason,
            ):
                return {"recorded": False, "error": "lease_lost"}
            return {"recorded": False, "error": stale_reason}
    conn.execute("SAVEPOINT cognitive_outbox_delivery")
    try:
        operation = str(row["operation"] or "")
        if operation == "intervention_event":
            from .store_cognitive_intervention import (
                insert_cognitive_intervention_event,
            )

            insert_cognitive_intervention_event(self, conn, payload)
            _insert_requested_transfer_episode(self, conn, payload)
        elif operation == "projection_enqueue":
            from .store_cognitive import enqueue_cognitive_projection

            attempt_id = str(payload.get("attempt_id") or "").strip()
            extractor_version = str(payload.get("extractor_version") or "").strip()
            model_version = str(payload.get("model_version") or "").strip()
            if not attempt_id or not extractor_version or not model_version:
                raise ValueError("cognitive projection outbox identities are required")
            enqueue_cognitive_projection(
                self,
                conn,
                attempt_id=attempt_id,
                extractor_version=extractor_version,
                model_version=model_version,
            )
        elif operation == "retention_disposition":
            _apply_retention_delivery(
                self,
                conn,
                attempt_id=str(row["attempt_id"]),
                payload=payload,
            )
        else:
            raise ValueError("unsupported cognitive outbox operation")
        conn.execute("RELEASE SAVEPOINT cognitive_outbox_delivery")
    except _DiscardRetentionDelivery as exc:
        conn.execute("ROLLBACK TO SAVEPOINT cognitive_outbox_delivery")
        conn.execute("RELEASE SAVEPOINT cognitive_outbox_delivery")
        reason = str(exc) or "stale retention delivery"
        if not _discard_outbox(
            conn,
            outbox_id=outbox_id,
            lease_token=token,
            reason=reason,
        ):
            return {"recorded": False, "error": "lease_lost"}
        return {"recorded": False, "error": reason}
    except Exception as exc:
        conn.execute("ROLLBACK TO SAVEPOINT cognitive_outbox_delivery")
        conn.execute("RELEASE SAVEPOINT cognitive_outbox_delivery")
        if isinstance(exc, sqlite3.DatabaseError) and not isinstance(
            exc, sqlite3.IntegrityError
        ):
            raise
        error = f"{type(exc).__name__}: {exc}"[:1000]
        conn.execute(
            """
            UPDATE cognitive_outbox
            SET status = 'failed', retry_count = retry_count + 1,
                last_error = ?, lease_token = '', lease_expires_at = '',
                updated_at = datetime('now')
            WHERE outbox_id = ?
              AND ((? = '' AND status != 'processing')
                   OR (status = 'processing' AND lease_token = ?))
            """,
            (error, outbox_id, token, token),
        )
        return {"recorded": False, "error": error}
    conn.execute(
        """
        UPDATE cognitive_outbox
        SET status = 'done', last_error = '', lease_token = '',
            lease_expires_at = '', updated_at = datetime('now')
        WHERE outbox_id = ?
          AND ((? = '' AND status != 'processing')
               OR (status = 'processing' AND lease_token = ?))
        """,
        (outbox_id, token, token),
    )
    return {"recorded": True, "error": ""}


def enqueue_cognitive_projection_outbox(
    self,
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    extractor_version: str,
    model_version: str,
) -> str:
    attempt_key = str(attempt_id or "").strip()
    extractor_key = str(extractor_version or "").strip()
    model_key = str(model_version or "").strip()
    if not attempt_key:
        raise ValueError("cognitive projection outbox attempt_id is required")
    return enqueue_cognitive_outbox(
        self,
        conn,
        attempt_id=attempt_key,
        operation="projection_enqueue",
        event={
            "event_id": f"cognitive-projection:{attempt_key}:{extractor_key}:{model_key}",
            "attempt_id": attempt_key,
            "extractor_version": extractor_key,
            "model_version": model_key,
        },
    )


def claim_cognitive_outbox(
    self,
    *,
    limit: int = 20,
    lease_seconds: int = 60,
    include_retention: bool = False,
) -> list[dict[str, Any]]:
    """Claim retryable work with a short lease and per-row identity token."""

    safe_limit = max(1, min(100, int(limit)))
    safe_lease = max(1, min(300, int(lease_seconds)))
    claimed: list[dict[str, Any]] = []
    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                """
                SELECT outbox_id FROM cognitive_outbox
                WHERE (
                    status IN ('pending', 'failed')
                    OR (status = 'processing'
                        AND julianday(lease_expires_at) <= julianday('now'))
                )
                  AND (? = 1 OR operation != 'retention_disposition')
                ORDER BY created_at, outbox_id
                LIMIT ?
                """,
                (1 if include_retention else 0, safe_limit),
            ).fetchall()
            for row in rows:
                outbox_id = str(row["outbox_id"])
                token = f"cognitive-lease:{uuid.uuid4().hex}"
                updated = conn.execute(
                    """
                    UPDATE cognitive_outbox
                    SET status = 'processing', lease_token = ?,
                        lease_expires_at = datetime('now', ?),
                        updated_at = datetime('now')
                    WHERE outbox_id = ?
                      AND (status IN ('pending', 'failed')
                           OR (status = 'processing'
                               AND julianday(lease_expires_at) <= julianday('now')))
                    """,
                    (token, f"+{safe_lease} seconds", outbox_id),
                )
                if updated.rowcount != 1:
                    continue
                item = conn.execute(
                    "SELECT * FROM cognitive_outbox WHERE outbox_id = ?",
                    (outbox_id,),
                ).fetchone()
                if item is not None:
                    claimed.append(
                        {
                            "outbox_id": outbox_id,
                            "attempt_id": str(item["attempt_id"]),
                            "event_id": str(item["event_id"]),
                            "operation": str(item["operation"]),
                            "lease_token": token,
                            "lease_expires_at": str(item["lease_expires_at"]),
                        }
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return claimed


def list_cognitive_outbox(
    self, *, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    status_key = str(status or "").strip()
    params: list[Any] = []
    where = ""
    if status_key:
        if status_key not in {"pending", "processing", "done", "failed", "discarded"}:
            raise ValueError("unsupported cognitive outbox status")
        where = "WHERE status = ?"
        params.append(status_key)
    params.append(max(1, int(limit)))
    rows = self._require_read_conn().execute(
        f"SELECT * FROM cognitive_outbox {where} ORDER BY created_at, outbox_id LIMIT ?",
        params,
    ).fetchall()
    return [
        {
            "outbox_id": str(row["outbox_id"]),
            "attempt_id": str(row["attempt_id"]),
            "event_id": str(row["event_id"]),
            "operation": str(row["operation"]),
            "payload": self._json_loads(row["payload_json"], {}),
            "status": str(row["status"]),
            "retry_count": int(row["retry_count"]),
            "last_error": str(row["last_error"] or ""),
            "lease_token": str(row["lease_token"] or ""),
            "lease_expires_at": str(row["lease_expires_at"] or ""),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


def process_cognitive_outbox(
    self, *, limit: int = 20, include_retention: bool = False
) -> dict[str, int]:
    """Deliver one bounded batch; an old lease holder cannot finish a takeover."""

    claims = claim_cognitive_outbox(
        self,
        limit=limit,
        include_retention=include_retention,
    )
    completed = 0
    failed = 0
    lease_lost = 0
    for claim in claims:
        with self._lock:
            conn = self._require_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = deliver_cognitive_outbox_inline(
                    self,
                    conn,
                    outbox_id=str(claim["outbox_id"]),
                    lease_token=str(claim["lease_token"]),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if result.get("recorded") is True:
            completed += 1
        elif result.get("error") == "lease_lost":
            lease_lost += 1
        else:
            failed += 1
    return {
        "claimed": len(claims),
        "completed": completed,
        "failed": failed,
        "lease_lost": lease_lost,
    }


__all__ = [
    "deliver_cognitive_outbox_inline",
    "claim_cognitive_outbox",
    "enqueue_cognitive_outbox",
    "enqueue_cognitive_projection_outbox",
    "enqueue_cognitive_retention_outbox",
    "list_cognitive_outbox",
    "process_cognitive_outbox",
]
