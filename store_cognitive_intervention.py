"""Persistence for append-only cognitive intervention facts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .store_cognitive import _mark_topic_dirty
from .store_common import sqlite3

_EVENT_TYPES = frozenset(
    {
        "intent_proposed",
        "question_committed",
        "attempt_committed",
        "intervention_abandoned",
    }
)
_INTENTS = frozenset(
    {"misconception_probe", "misconception_repair", "transfer_check"}
)
_VERDICTS = frozenset({"", "correct", "partial", "wrong", "dont_know"})


def _required(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _nonnegative(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be an integer")
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field} must be an integer")
    if number < 0:
        raise ValueError(f"{field} must be non-negative")
    return number


def _probability(value: object) -> float:
    if isinstance(value, bool):
        raise TypeError("hypothesis probability must be numeric")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError("hypothesis probability must be between zero and one")
    return number


def _normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    hypothesis = event.get("hypothesis_target")
    binding = event.get("binding")
    if not isinstance(hypothesis, Mapping) or not isinstance(binding, Mapping):
        raise TypeError("cognitive intervention hypothesis and binding are required")
    event_type = _required(event.get("event_type"), "event_type")
    intent = _required(event.get("learning_intent"), "learning_intent")
    verdict = str(event.get("evaluation_verdict") or "").strip()
    if event_type not in _EVENT_TYPES:
        raise ValueError("unsupported cognitive intervention event type")
    if intent not in _INTENTS:
        raise ValueError("unsupported cognitive intervention intent")
    if verdict not in _VERDICTS:
        raise ValueError("unsupported cognitive intervention verdict")
    topic_id = _required(binding.get("topic_id"), "topic_id")
    if topic_id != _required(hypothesis.get("topic_id"), "hypothesis topic_id"):
        raise ValueError("intervention binding and hypothesis topic differ")
    eligible = binding.get("eligible_topic_ids") or ()
    if not isinstance(eligible, Sequence) or isinstance(eligible, (str, bytes)):
        raise TypeError("eligible_topic_ids must be a sequence")
    target_binding = binding.get("target_binding") or {}
    if not isinstance(target_binding, Mapping):
        raise TypeError("target_binding must be a mapping")
    question_id = str(event.get("question_id") or "").strip()
    attempt_id = str(event.get("attempt_id") or "").strip()
    blueprint_id = str(event.get("blueprint_id") or "").strip()
    question_family_id = str(event.get("question_family_id") or "").strip()
    diagnostic_validation_id = str(
        event.get("diagnostic_validation_id") or ""
    ).strip()
    validator_version = str(event.get("validator_version") or "").strip()
    abandonment_reason = str(event.get("abandonment_reason") or "").strip()
    if event_type == "intent_proposed":
        if attempt_id or verdict:
            raise ValueError("intent_proposed cannot contain attempt results")
    elif event_type == "intervention_abandoned":
        if not abandonment_reason:
            raise ValueError("intervention_abandoned requires a reason")
        if attempt_id or verdict:
            raise ValueError("abandoned intervention cannot contain attempt results")
    else:
        if not all(
            (
                question_id,
                blueprint_id,
                question_family_id,
                diagnostic_validation_id,
                validator_version,
            )
        ):
            raise ValueError("committed cognitive question provenance is required")
        if event_type == "question_committed" and (attempt_id or verdict):
            raise ValueError("question_committed cannot contain attempt results")
        if event_type == "attempt_committed" and (not attempt_id or not verdict):
            raise ValueError("attempt_committed requires an evaluated attempt")
    metadata = event.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise TypeError("cognitive intervention metadata must be a mapping")
    return {
        "event_id": _required(event.get("event_id"), "event_id"),
        "event_type": event_type,
        "decision_id": _required(event.get("decision_id"), "decision_id"),
        "hypothesis_id": _required(hypothesis.get("hypothesis_id"), "hypothesis_id"),
        "topic_id": topic_id,
        "hypothesis_code": _required(hypothesis.get("code"), "hypothesis code"),
        "hypothesis_status": _required(hypothesis.get("status"), "hypothesis status"),
        "hypothesis_probability": _probability(hypothesis.get("probability")),
        "hypothesis_model_version": _required(
            hypothesis.get("model_version"), "hypothesis model_version"
        ),
        "hypothesis_source_snapshot_id": _required(
            hypothesis.get("source_snapshot_id"), "hypothesis source_snapshot_id"
        ),
        "hypothesis_source_attempt_id": str(
            hypothesis.get("source_attempt_id") or ""
        ).strip(),
        "hypothesis_projection_generation": _nonnegative(
            hypothesis.get("projection_generation"), "projection_generation"
        ),
        "learning_intent": intent,
        "repair_strategy": _required(event.get("repair_strategy"), "repair_strategy"),
        "plan_id": _required(binding.get("plan_id"), "plan_id"),
        "selection_reason": _required(
            binding.get("selection_reason"), "selection_reason"
        ),
        "eligible_topic_ids_json": tuple(str(item) for item in eligible),
        "learning_plan_id": str(binding.get("learning_plan_id") or "").strip(),
        "learning_plan_revision": _nonnegative(
            binding.get("learning_plan_revision"), "learning_plan_revision"
        ),
        "scope_key": str(binding.get("scope_key") or "").strip(),
        "scope_revision": _nonnegative(binding.get("scope_revision"), "scope_revision"),
        "origin_wrong_question_id": str(
            binding.get("origin_wrong_question_id") or ""
        ).strip(),
        "source_question_id": str(binding.get("source_question_id") or "").strip(),
        "target_binding_json": dict(target_binding),
        "question_id": question_id,
        "attempt_id": attempt_id,
        "blueprint_id": blueprint_id,
        "question_family_id": question_family_id,
        "diagnostic_validation_id": diagnostic_validation_id,
        "evaluation_verdict": verdict,
        "abandonment_reason": abandonment_reason,
        "policy_version": _required(event.get("policy_version"), "policy_version"),
        "validator_version": validator_version,
        "schema_version": max(1, _nonnegative(event.get("schema_version") or 1, "schema_version")),
        "metadata_json": dict(metadata),
        "occurred_at": _required(event.get("created_at"), "created_at"),
    }


def _row_to_event(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    attempt_id = str(row["attempt_id"])
    if "attempt_session_id" in row.keys():
        session_id = str(row["attempt_session_id"] or "")
    elif attempt_id:
        attempt_row = self._require_conn().execute(
            "SELECT session_id FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        session_id = str(attempt_row["session_id"] or "") if attempt_row else ""
    else:
        session_id = ""
    return {
        "event_seq": int(row["event_seq"]),
        "event_id": str(row["event_id"]),
        "event_type": str(row["event_type"]),
        "decision_id": str(row["decision_id"]),
        "hypothesis_target": {
            "hypothesis_id": str(row["hypothesis_id"]),
            "topic_id": str(row["topic_id"]),
            "code": str(row["hypothesis_code"]),
            "status": str(row["hypothesis_status"]),
            "probability": float(row["hypothesis_probability"]),
            "model_version": str(row["hypothesis_model_version"]),
            "source_snapshot_id": str(row["hypothesis_source_snapshot_id"]),
            "source_attempt_id": str(row["hypothesis_source_attempt_id"]),
            "projection_generation": int(row["hypothesis_projection_generation"]),
        },
        "learning_intent": str(row["learning_intent"]),
        "repair_strategy": str(row["repair_strategy"]),
        "binding": {
            "plan_id": str(row["plan_id"]),
            "topic_id": str(row["topic_id"]),
            "selection_reason": str(row["selection_reason"]),
            "eligible_topic_ids": tuple(
                self._json_loads(row["eligible_topic_ids_json"], [])
            ),
            "learning_plan_id": str(row["learning_plan_id"]),
            "learning_plan_revision": int(row["learning_plan_revision"]),
            "scope_key": str(row["scope_key"]),
            "scope_revision": int(row["scope_revision"]),
            "origin_wrong_question_id": str(row["origin_wrong_question_id"]),
            "source_question_id": str(row["source_question_id"]),
            "target_binding": self._json_loads(row["target_binding_json"], {}),
        },
        "question_id": str(row["question_id"]),
        "attempt_id": attempt_id,
        "session_id": session_id,
        "blueprint_id": str(row["blueprint_id"]),
        "question_family_id": str(row["question_family_id"]),
        "diagnostic_validation_id": str(row["diagnostic_validation_id"]),
        "evaluation_verdict": str(row["evaluation_verdict"]),
        "abandonment_reason": str(row["abandonment_reason"]),
        "policy_version": str(row["policy_version"]),
        "validator_version": str(row["validator_version"]),
        "schema_version": int(row["schema_version"]),
        "metadata": self._json_loads(row["metadata_json"], {}),
        "created_at": str(row["occurred_at"]),
    }


def insert_cognitive_intervention_event(
    self,
    conn: sqlite3.Connection,
    event: Mapping[str, Any],
    *,
    mark_dirty: bool = True,
) -> dict[str, Any]:
    """Insert one event into a caller-owned transaction.

    This function never begins, commits, or rolls back a transaction, allowing
    ``attempt_committed`` to share the answer-fact atomic boundary.
    """

    item = _normalize_event(event)
    columns = tuple(item)
    values = [
        self._json_dumps(value) if key.endswith("_json") else value
        for key, value in item.items()
    ]
    stored_item = dict(zip(columns, values))
    placeholders = ", ".join("?" for _ in columns)
    existing = conn.execute(
        "SELECT * FROM cognitive_intervention_events WHERE event_id = ?",
        (item["event_id"],),
    ).fetchone()
    if existing is not None:
        if any(existing[column] != value for column, value in stored_item.items()):
            raise ValueError("cognitive intervention event identity collision")
    else:
        if item["event_type"] == "question_committed":
            current = conn.execute(
                """
                SELECT 1
                FROM cognitive_hypothesis_current current
                JOIN cognitive_topic_projection_queue queue
                  ON queue.topic_id = current.topic_id
                 AND queue.model_version = current.model_version
                WHERE current.hypothesis_id = ?
                  AND current.topic_id = ?
                  AND current.hypothesis_code = ?
                  AND current.model_version = ?
                  AND current.source_snapshot_id = ?
                  AND current.projected_generation = ?
                  AND current.status = ?
                  AND current.probability = ?
                  AND current.source_attempt_id = ?
                  AND current.evidence_status = 'supported'
                  AND current.user_override = ''
                  AND queue.status = 'done'
                  AND queue.requested_generation = queue.projected_generation
                  AND queue.projected_generation = current.projected_generation
                LIMIT 1
                """,
                (
                    item["hypothesis_id"],
                    item["topic_id"],
                    item["hypothesis_code"],
                    item["hypothesis_model_version"],
                    item["hypothesis_source_snapshot_id"],
                    item["hypothesis_projection_generation"],
                    item["hypothesis_status"],
                    item["hypothesis_probability"],
                    item["hypothesis_source_attempt_id"],
                ),
            ).fetchone()
            if current is None:
                raise ValueError(
                    "question_committed requires a fresh supported hypothesis"
                )
        if item["event_type"] == "attempt_committed":
            committed = conn.execute(
                """
                SELECT * FROM cognitive_intervention_events
                WHERE event_type = 'question_committed'
                  AND decision_id = ? AND question_id = ?
                  AND topic_id = ? AND hypothesis_code = ?
                  AND hypothesis_model_version = ?
                  AND learning_intent = ?
                LIMIT 1
                """,
                (
                    item["decision_id"],
                    item["question_id"],
                    item["topic_id"],
                    item["hypothesis_code"],
                    item["hypothesis_model_version"],
                    item["learning_intent"],
                ),
            ).fetchone()
            if committed is None:
                raise ValueError(
                    "attempt_committed requires a matching committed question"
                )
            immutable_fields = (
                "blueprint_id",
                "question_family_id",
                "diagnostic_validation_id",
                "validator_version",
                "repair_strategy",
                "plan_id",
                "selection_reason",
                "eligible_topic_ids_json",
                "learning_plan_id",
                "learning_plan_revision",
                "scope_key",
                "scope_revision",
                "origin_wrong_question_id",
                "source_question_id",
                "target_binding_json",
                "hypothesis_source_snapshot_id",
                "hypothesis_source_attempt_id",
                "hypothesis_projection_generation",
                "hypothesis_status",
                "hypothesis_probability",
            )
            if any(
                committed[field] != stored_item[field] for field in immutable_fields
            ):
                raise ValueError("attempt_committed does not match its committed question")
            prior_attempt = conn.execute(
                """
                SELECT 1 FROM cognitive_intervention_events
                WHERE event_type = 'attempt_committed'
                  AND decision_id = ? AND question_id = ?
                LIMIT 1
                """,
                (item["decision_id"], item["question_id"]),
            ).fetchone()
            if prior_attempt is not None:
                raise ValueError("cognitive intervention question already has an attempt")
            abandoned = conn.execute(
                """
                SELECT 1 FROM cognitive_intervention_events
                WHERE event_type = 'intervention_abandoned'
                  AND decision_id = ? AND learning_intent = ?
                  AND (question_id = '' OR question_id = ?)
                LIMIT 1
                """,
                (item["decision_id"], item["learning_intent"], item["question_id"]),
            ).fetchone()
            if abandoned is not None:
                raise ValueError("cannot commit an attempt for an abandoned intervention")
        if item["event_type"] == "intervention_abandoned":
            terminal_attempt = conn.execute(
                """
                SELECT 1 FROM cognitive_intervention_events
                WHERE event_type = 'attempt_committed'
                  AND decision_id = ?
                  AND (? = '' OR question_id = ?)
                LIMIT 1
                """,
                (item["decision_id"], item["question_id"], item["question_id"]),
            ).fetchone()
            if terminal_attempt is not None:
                raise ValueError("attempt_committed is terminal and cannot be abandoned")
            predecessor_type = (
                "question_committed" if item["question_id"] else "intent_proposed"
            )
            predecessor = conn.execute(
                """
                SELECT 1 FROM cognitive_intervention_events
                WHERE event_type = ? AND decision_id = ?
                  AND topic_id = ? AND hypothesis_code = ?
                  AND hypothesis_model_version = ? AND learning_intent = ?
                  AND (? = '' OR question_id = ?)
                LIMIT 1
                """,
                (
                    predecessor_type,
                    item["decision_id"],
                    item["topic_id"],
                    item["hypothesis_code"],
                    item["hypothesis_model_version"],
                    item["learning_intent"],
                    item["question_id"],
                    item["question_id"],
                ),
            ).fetchone()
            if predecessor is None:
                raise ValueError(
                    "intervention_abandoned requires a matching prior event"
                )
        conn.execute(
            f"INSERT INTO cognitive_intervention_events "
            f"({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        if mark_dirty and item["event_type"] != "intent_proposed":
            _mark_topic_dirty(
                conn,
                topic_id=item["topic_id"],
                model_version=item["hypothesis_model_version"],
            )
    row = conn.execute(
        "SELECT * FROM cognitive_intervention_events WHERE event_id = ?",
        (item["event_id"],),
    ).fetchone()
    result = _row_to_event(self, row)
    if result is None:
        raise RuntimeError("cognitive intervention event write failed")
    return result


def record_cognitive_intervention_event(
    self, event: Mapping[str, Any]
) -> dict[str, Any]:
    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = insert_cognitive_intervention_event(self, conn, event)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return result


def list_cognitive_intervention_events(
    self,
    *,
    topic_id: str | None = None,
    hypothesis_code: str | None = None,
    decision_id: str | None = None,
    model_version: str | None = None,
    event_types: Sequence[str] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("topic_id", topic_id),
        ("hypothesis_code", hypothesis_code),
        ("decision_id", decision_id),
        ("hypothesis_model_version", model_version),
    ):
        key = str(value or "").strip()
        if key:
            clauses.append(f"events.{column} = ?")
            params.append(key)
    requested_types = tuple(dict.fromkeys(str(item or "").strip() for item in (event_types or ())))
    if any(item not in _EVENT_TYPES for item in requested_types):
        raise ValueError("unsupported cognitive intervention event type")
    if requested_types:
        clauses.append(
            "events.event_type IN ("
            + ", ".join("?" for _ in requested_types)
            + ")"
        )
        params.extend(requested_types)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    rows = self._require_read_conn().execute(
        f"""
        SELECT events.*, attempts.session_id AS attempt_session_id
        FROM cognitive_intervention_events events
        LEFT JOIN attempts ON attempts.attempt_id = events.attempt_id
        {where}
        ORDER BY events.occurred_at, events.event_seq
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [item for row in rows if (item := _row_to_event(self, row)) is not None]


__all__ = [
    "insert_cognitive_intervention_event",
    "list_cognitive_intervention_events",
    "record_cognitive_intervention_event",
]
