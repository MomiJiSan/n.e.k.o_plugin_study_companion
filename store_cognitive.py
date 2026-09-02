"""SQLite persistence for the versioned Cognitive Evidence Engine shadow model.

Answer submission owns queue insertion and its transaction.  Worker helpers use
separate, lease-fenced transactions so extraction or projection failures can
never roll back an answer that was already committed.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .adaptive_learning.cognitive_contracts import DEFAULT_COGNITIVE_MODEL_VERSION
from .store_common import Any, sqlite3, uuid

_QUEUE_STATUSES = frozenset({"pending", "processing", "done", "failed"})
_HYPOTHESIS_STATUSES = frozenset(
    {
        "hypothesized",
        "supported",
        "contradicted",
        "dismissed",
        "remediating",
        "provisionally_resolved",
        "monitored",
        "resolved",
    }
)
_CONTROL_ACTIONS = frozenset({"dismiss", "suppress", "restore", "delete"})
_SUPPRESSING_ACTIONS = frozenset({"dismiss", "suppress", "delete"})
_PROJECTION_LEASE_SECONDS = 300
_PROJECTION_RETRY_DELAY_SECONDS = 300


def _required_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _bounded_unit_float(value: object, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    if result < 0.0 or result > 1.0:
        raise ValueError(f"{field} must be between 0 and 1")
    return result


def _non_negative_int(value: object, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if result < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return result


def _queue_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "attempt_id": str(row["attempt_id"]),
        "status": str(row["status"]),
        "retry_count": int(row["retry_count"] or 0),
        "last_error": str(row["last_error"] or ""),
        "lease_token": str(row["lease_token"] or ""),
        "extractor_version": str(row["extractor_version"] or ""),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def _evidence_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "evidence_id": str(row["evidence_id"]),
        "attempt_id": str(row["attempt_id"]),
        "topic_id": str(row["topic_id"]),
        "hypothesis_code": str(row["hypothesis_code"]),
        "direction": str(row["direction"]),
        "strength": float(row["strength"]),
        "extractor_confidence": float(row["extractor_confidence"]),
        "diagnosticity": float(row["diagnosticity"]),
        "source_kind": str(row["source_kind"]),
        "evidence_span": str(row["evidence_span"]),
        "extractor_version": str(row["extractor_version"]),
        "evidence_family_id": str(row["evidence_family_id"] or ""),
        "question_id": str(row["question_id"] or ""),
        "session_id": str(row["session_id"] or ""),
        "diagnostic_validation_id": str(row["diagnostic_validation_id"] or ""),
        "created_at": str(row["created_at"] or ""),
    }


def _snapshot_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "snapshot_id": int(row["snapshot_id"]),
        "hypothesis_id": str(row["hypothesis_id"]),
        "topic_id": str(row["topic_id"]),
        "hypothesis_code": str(row["hypothesis_code"]),
        "status": str(row["status"]),
        "probability": float(row["probability"]),
        "support_count": int(row["support_count"] or 0),
        "counter_count": int(row["counter_count"] or 0),
        "diagnostic_support_count": int(row["diagnostic_support_count"] or 0),
        "relapse_count": int(row["relapse_count"] or 0),
        "source_attempt_id": str(row["source_attempt_id"]),
        "model_version": str(row["model_version"]),
        "computed_at": str(row["computed_at"] or ""),
    }


def _control_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "control_id": str(row["control_id"]),
        "topic_id": str(row["topic_id"]),
        "hypothesis_code": str(row["hypothesis_code"]),
        "action": str(row["action"]),
        "reason": str(row["reason"] or ""),
        "expires_at": str(row["expires_at"] or ""),
        "created_at": str(row["created_at"] or ""),
    }


def _topic_projection_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "topic_id": str(row["topic_id"]),
        "model_version": str(row["model_version"]),
        "status": str(row["status"]),
        "requested_generation": int(row["requested_generation"] or 0),
        "claimed_generation": int(row["claimed_generation"] or 0),
        "projected_generation": int(row["projected_generation"] or 0),
        "retry_count": int(row["retry_count"] or 0),
        "last_error": str(row["last_error"] or ""),
        "lease_token": str(row["lease_token"] or ""),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def _current_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "hypothesis_id": str(row["hypothesis_id"]),
        "topic_id": str(row["topic_id"]),
        "hypothesis_code": str(row["hypothesis_code"]),
        "evidence_status": str(row["evidence_status"]),
        "intervention_stage": str(row["intervention_stage"]),
        "user_override": str(row["user_override"] or ""),
        "status": str(row["status"]),
        "probability": float(row["probability"]),
        "support_count": int(row["support_count"] or 0),
        "counter_count": int(row["counter_count"] or 0),
        "diagnostic_support_count": int(row["diagnostic_support_count"] or 0),
        "relapse_count": int(row["relapse_count"] or 0),
        "source_attempt_id": str(row["source_attempt_id"]),
        "source_snapshot_id": str(row["source_snapshot_id"]),
        "model_version": str(row["model_version"]),
        "projected_generation": int(row["projected_generation"] or 0),
        "last_intent": str(row["last_intent"] or ""),
        "last_outcome": str(row["last_outcome"] or ""),
        "consecutive_repair_failures": int(
            row["consecutive_repair_failures"] or 0
        ),
        "computed_at": str(row["computed_at"] or ""),
    }


def _normalize_evidence(
    evidence: dict[str, Any],
    *,
    attempt_id: str,
    extractor_version: str,
) -> dict[str, Any]:
    item_attempt_id = str(evidence.get("attempt_id") or attempt_id).strip()
    if item_attempt_id != attempt_id:
        raise ValueError("cognitive evidence attempt_id does not match queue item")
    item_version = str(evidence.get("extractor_version") or extractor_version).strip()
    if item_version != extractor_version:
        raise ValueError("cognitive evidence extractor_version does not match queue item")
    direction = _required_text(evidence.get("direction"), "direction")
    if direction not in {"support", "counter"}:
        raise ValueError("direction must be support or counter")
    return {
        "evidence_id": _required_text(
            evidence.get("evidence_id") or uuid.uuid4(), "evidence_id"
        ),
        "attempt_id": item_attempt_id,
        "topic_id": _required_text(evidence.get("topic_id"), "topic_id"),
        "hypothesis_code": _required_text(
            evidence.get("hypothesis_code"), "hypothesis_code"
        ),
        "direction": direction,
        "strength": _bounded_unit_float(evidence.get("strength"), "strength"),
        "extractor_confidence": _bounded_unit_float(
            evidence.get("extractor_confidence"), "extractor_confidence"
        ),
        "diagnosticity": _bounded_unit_float(
            evidence.get("diagnosticity"), "diagnosticity"
        ),
        "source_kind": _required_text(
            evidence.get("source_kind") or "structured_attempt", "source_kind"
        ),
        "evidence_span": _required_text(
            evidence.get("evidence_span"), "evidence_span"
        ),
        "extractor_version": item_version,
        "evidence_family_id": _required_text(
            evidence.get("evidence_family_id") or f"attempt:{item_attempt_id}",
            "evidence_family_id",
        ),
        "question_id": str(evidence.get("question_id") or "").strip(),
        "session_id": str(evidence.get("session_id") or "").strip(),
        "diagnostic_validation_id": str(
            evidence.get("diagnostic_validation_id") or ""
        ).strip(),
        "created_at": str(evidence.get("created_at") or "").strip(),
    }


def _normalize_snapshot(
    snapshot: dict[str, Any],
    *,
    default_attempt_id: str = "",
    default_model_version: str = "",
) -> dict[str, Any]:
    topic_id = _required_text(snapshot.get("topic_id"), "topic_id")
    hypothesis_code = _required_text(
        snapshot.get("hypothesis_code"), "hypothesis_code"
    )
    status = _required_text(snapshot.get("status"), "status")
    if status not in _HYPOTHESIS_STATUSES:
        raise ValueError(f"unsupported cognitive hypothesis status: {status}")
    evidence_status = str(snapshot.get("evidence_status") or "").strip()
    if not evidence_status:
        if status == "contradicted":
            evidence_status = "contradicted"
        elif status in {
            "supported",
            "remediating",
            "provisionally_resolved",
            "monitored",
            "resolved",
        }:
            evidence_status = "supported"
        else:
            evidence_status = "hypothesized"
    if evidence_status not in {"hypothesized", "supported", "contradicted"}:
        raise ValueError("unsupported cognitive evidence status")
    intervention_stage = str(snapshot.get("intervention_stage") or "").strip()
    if not intervention_stage:
        intervention_stage = (
            status
            if status
            in {
                "remediating",
                "provisionally_resolved",
                "monitored",
                "resolved",
            }
            else "idle"
        )
    if intervention_stage not in {
        "idle",
        "probing",
        "remediating",
        "provisionally_resolved",
        "monitored",
        "resolved",
    }:
        raise ValueError("unsupported cognitive intervention stage")
    user_override = str(snapshot.get("user_override") or "").strip()
    if not user_override and status == "dismissed":
        user_override = "dismissed"
    if user_override not in {"", "dismissed", "suppressed", "deleted"}:
        raise ValueError("unsupported cognitive user override")
    return {
        "hypothesis_id": _required_text(
            snapshot.get("hypothesis_id") or f"{topic_id}:{hypothesis_code}",
            "hypothesis_id",
        ),
        "topic_id": topic_id,
        "hypothesis_code": hypothesis_code,
        "status": status,
        "probability": _bounded_unit_float(
            snapshot.get("probability"), "probability"
        ),
        "support_count": _non_negative_int(
            snapshot.get("support_count", 0), "support_count"
        ),
        "counter_count": _non_negative_int(
            snapshot.get("counter_count", 0), "counter_count"
        ),
        "diagnostic_support_count": _non_negative_int(
            snapshot.get("diagnostic_support_count", 0),
            "diagnostic_support_count",
        ),
        "relapse_count": _non_negative_int(
            snapshot.get("relapse_count", 0), "relapse_count"
        ),
        "source_attempt_id": _required_text(
            snapshot.get("source_attempt_id") or default_attempt_id,
            "source_attempt_id",
        ),
        "model_version": _required_text(
            snapshot.get("model_version") or default_model_version,
            "model_version",
        ),
        "computed_at": str(snapshot.get("computed_at") or "").strip(),
        "evidence_status": evidence_status,
        "intervention_stage": intervention_stage,
        "user_override": user_override,
        "last_intent": str(snapshot.get("last_intent") or "").strip(),
        "last_outcome": str(snapshot.get("last_outcome") or "").strip(),
        "consecutive_repair_failures": _non_negative_int(
            snapshot.get("consecutive_repair_failures", 0),
            "consecutive_repair_failures",
        ),
    }


def _write_evidence(conn: sqlite3.Connection, item: dict[str, Any]) -> bool:
    cursor = conn.execute(
        """
        INSERT INTO cognitive_evidence (
            evidence_id, attempt_id, topic_id, hypothesis_code, direction,
            strength, extractor_confidence, diagnosticity, source_kind,
            evidence_span, extractor_version, evidence_family_id, question_id,
            session_id, diagnostic_validation_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  COALESCE(NULLIF(?, ''), datetime('now')))
        ON CONFLICT(attempt_id, hypothesis_code, extractor_version) DO NOTHING
        """,
        (
            item["evidence_id"],
            item["attempt_id"],
            item["topic_id"],
            item["hypothesis_code"],
            item["direction"],
            item["strength"],
            item["extractor_confidence"],
            item["diagnosticity"],
            item["source_kind"],
            item["evidence_span"],
            item["extractor_version"],
            item["evidence_family_id"],
            item["question_id"],
            item["session_id"],
            item["diagnostic_validation_id"],
            item["created_at"],
        ),
    )
    return int(cursor.rowcount or 0) > 0


def _write_snapshot(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO cognitive_hypothesis_snapshots (
            hypothesis_id, topic_id, hypothesis_code, status, probability,
            support_count, counter_count, diagnostic_support_count,
            relapse_count, source_attempt_id, model_version, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  COALESCE(NULLIF(?, ''), datetime('now')))
        ON CONFLICT(hypothesis_id, model_version, source_attempt_id) DO UPDATE SET
            topic_id = excluded.topic_id,
            hypothesis_code = excluded.hypothesis_code,
            status = excluded.status,
            probability = excluded.probability,
            support_count = excluded.support_count,
            counter_count = excluded.counter_count,
            diagnostic_support_count = excluded.diagnostic_support_count,
            relapse_count = excluded.relapse_count,
            computed_at = excluded.computed_at
        """,
        (
            item["hypothesis_id"],
            item["topic_id"],
            item["hypothesis_code"],
            item["status"],
            item["probability"],
            item["support_count"],
            item["counter_count"],
            item["diagnostic_support_count"],
            item["relapse_count"],
            item["source_attempt_id"],
            item["model_version"],
            item["computed_at"],
        ),
    )


def _write_current(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    *,
    projected_generation: int,
) -> None:
    conn.execute(
        """
        INSERT INTO cognitive_hypothesis_current (
            hypothesis_id, topic_id, hypothesis_code, evidence_status,
            intervention_stage, user_override, status, probability,
            support_count, counter_count, diagnostic_support_count,
            relapse_count, source_attempt_id, model_version,
            source_snapshot_id, projected_generation, last_intent,
            last_outcome, consecutive_repair_failures, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  COALESCE(NULLIF(?, ''), datetime('now')))
        ON CONFLICT(hypothesis_id, model_version) DO UPDATE SET
            topic_id = excluded.topic_id,
            hypothesis_code = excluded.hypothesis_code,
            evidence_status = excluded.evidence_status,
            intervention_stage = excluded.intervention_stage,
            user_override = excluded.user_override,
            status = excluded.status,
            probability = excluded.probability,
            support_count = excluded.support_count,
            counter_count = excluded.counter_count,
            diagnostic_support_count = excluded.diagnostic_support_count,
            relapse_count = excluded.relapse_count,
            source_attempt_id = excluded.source_attempt_id,
            source_snapshot_id = excluded.source_snapshot_id,
            projected_generation = excluded.projected_generation,
            last_intent = excluded.last_intent,
            last_outcome = excluded.last_outcome,
            consecutive_repair_failures = excluded.consecutive_repair_failures,
            computed_at = excluded.computed_at
        """,
        (
            item["hypothesis_id"],
            item["topic_id"],
            item["hypothesis_code"],
            item["evidence_status"],
            item["intervention_stage"],
            item["user_override"],
            item["status"],
            item["probability"],
            item["support_count"],
            item["counter_count"],
            item["diagnostic_support_count"],
            item["relapse_count"],
            item["source_attempt_id"],
            item["model_version"],
            (
                f"{item['hypothesis_id']}:{item['model_version']}:"
                f"generation-{projected_generation}"
            ),
            projected_generation,
            item["last_intent"],
            item["last_outcome"],
            item["consecutive_repair_failures"],
            item["computed_at"],
        ),
    )


def _validate_snapshot_attempt_topics(
    conn: sqlite3.Connection, snapshots: list[dict[str, Any]]
) -> None:
    by_topic: dict[str, set[str]] = {}
    for item in snapshots:
        by_topic.setdefault(item["topic_id"], set()).add(item["source_attempt_id"])
    for topic_id, attempt_ids in by_topic.items():
        found: set[str] = set()
        ordered_ids = sorted(attempt_ids)
        for start in range(0, len(ordered_ids), 500):
            chunk = ordered_ids[start : start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT attempt_id
                FROM attempts
                WHERE topic_id = ? AND attempt_id IN ({placeholders})
                """,
                [topic_id, *chunk],
            ).fetchall()
            found.update(str(row["attempt_id"]) for row in rows)
        if found != attempt_ids:
            raise ValueError("snapshot topic_id does not match source attempt")


def _iso_datetime(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _mark_topic_dirty(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    model_version: str,
) -> int:
    topic_key = _required_text(topic_id, "topic_id")
    model_key = _required_text(model_version, "model_version")
    conn.execute(
        """
        INSERT INTO cognitive_topic_projection_queue (
            topic_id, model_version, status, requested_generation,
            claimed_generation, projected_generation, retry_count,
            last_error, lease_token, created_at, updated_at
        ) VALUES (?, ?, 'pending', 1, 0, 0, 0, NULL, '',
                  datetime('now'), datetime('now'))
        ON CONFLICT(topic_id, model_version) DO UPDATE SET
            requested_generation = requested_generation + 1,
            status = CASE
                WHEN status = 'processing' THEN status
                ELSE 'pending'
            END,
            last_error = CASE
                WHEN status = 'processing' THEN last_error
                ELSE NULL
            END,
            updated_at = datetime('now')
        """,
        (topic_key, model_key),
    )
    row = conn.execute(
        """
        SELECT requested_generation
        FROM cognitive_topic_projection_queue
        WHERE topic_id = ? AND model_version = ?
        """,
        (topic_key, model_key),
    ).fetchone()
    if row is None:
        raise RuntimeError("cognitive topic projection dirty mark failed")
    return int(row["requested_generation"])


def _compat_queue_update(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    extractor_version: str,
) -> None:
    """Mirror the first version into the V1 one-row-per-attempt table."""

    row = conn.execute(
        """
        SELECT * FROM cognitive_extraction_queue
        WHERE attempt_id = ? AND extractor_version = ?
        """,
        (attempt_id, extractor_version),
    ).fetchone()
    if row is None:
        return
    conn.execute(
        """
        INSERT INTO cognitive_projection_queue (
            attempt_id, status, retry_count, last_error, lease_token,
            extractor_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(attempt_id) DO UPDATE SET
            status = CASE
                WHEN cognitive_projection_queue.extractor_version = excluded.extractor_version
                    THEN excluded.status
                ELSE cognitive_projection_queue.status
            END,
            retry_count = CASE
                WHEN cognitive_projection_queue.extractor_version = excluded.extractor_version
                    THEN excluded.retry_count
                ELSE cognitive_projection_queue.retry_count
            END,
            last_error = CASE
                WHEN cognitive_projection_queue.extractor_version = excluded.extractor_version
                    THEN excluded.last_error
                ELSE cognitive_projection_queue.last_error
            END,
            lease_token = CASE
                WHEN cognitive_projection_queue.extractor_version = excluded.extractor_version
                    THEN excluded.lease_token
                ELSE cognitive_projection_queue.lease_token
            END,
            updated_at = CASE
                WHEN cognitive_projection_queue.extractor_version = excluded.extractor_version
                    THEN excluded.updated_at
                ELSE cognitive_projection_queue.updated_at
            END
        """,
        (
            row["attempt_id"],
            row["status"],
            row["retry_count"],
            row["last_error"],
            row["lease_token"],
            row["extractor_version"],
            row["created_at"],
            row["updated_at"],
        ),
    )


def enqueue_cognitive_projection(
    self,
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    extractor_version: str,
    model_version: str = DEFAULT_COGNITIVE_MODEL_VERSION,
) -> bool:
    """Idempotently enqueue an attempt in the caller-owned answer transaction."""
    attempt_key = str(attempt_id or "").strip()
    version_key = str(extractor_version or "").strip()
    model_key = str(model_version or "").strip()
    if not attempt_key or not version_key or not model_key:
        return False
    cursor = conn.execute(
        """
        INSERT INTO cognitive_extraction_queue (
            attempt_id, status, retry_count, last_error, lease_token,
            extractor_version, created_at, updated_at
        ) VALUES (?, 'pending', 0, NULL, '', ?, datetime('now'), datetime('now'))
        ON CONFLICT(attempt_id, extractor_version) DO NOTHING
        """,
        (attempt_key, version_key),
    )
    inserted = int(cursor.rowcount or 0) > 0
    if inserted:
        _compat_queue_update(
            conn,
            attempt_id=attempt_key,
            extractor_version=version_key,
        )
        attempt_row = conn.execute(
            "SELECT topic_id FROM attempts WHERE attempt_id = ?",
            (attempt_key,),
        ).fetchone()
        topic_id = str(attempt_row["topic_id"] or "").strip() if attempt_row else ""
        if topic_id:
            # A queued extraction is unprojected evidence.  Advance the topic
            # generation immediately so readers fail closed until extraction
            # and the subsequent topic rebuild have both completed.
            _mark_topic_dirty(
                conn,
                topic_id=topic_id,
                model_version=model_key,
            )
    return inserted


def list_cognitive_projection_queue(
    self,
    *,
    statuses: tuple[str, ...] | list[str] | set[str] | None = None,
    extractor_version: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    requested = tuple(
        dict.fromkeys(str(status or "").strip() for status in (statuses or ()))
    )
    invalid = set(requested) - _QUEUE_STATUSES
    if invalid:
        raise ValueError(
            f"unsupported cognitive projection status: {sorted(invalid)[0]}"
        )
    clauses: list[str] = []
    params: list[Any] = []
    if requested:
        clauses.append("status IN (" + ", ".join("?" for _ in requested) + ")")
        params.extend(requested)
    version_key = str(extractor_version or "").strip()
    if version_key:
        clauses.append("extractor_version = ?")
        params.append(version_key)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    rows = self._require_read_conn().execute(
        f"""
        SELECT * FROM cognitive_extraction_queue
        {where}
        ORDER BY created_at, attempt_id
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [item for item in (_queue_from_row(row) for row in rows) if item]


def claim_cognitive_projections(
    self, *, limit: int = 1, extractor_version: str | None = None
) -> list[dict[str, Any]]:
    """Atomically claim pending or retryable failed work with lease fencing."""
    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # A V1 test/client may have adjusted the compatibility row to
            # force lease expiry. Reflect that update before claiming.
            conn.execute(
                """
                UPDATE cognitive_extraction_queue
                SET updated_at = (
                    SELECT legacy.updated_at
                    FROM cognitive_projection_queue legacy
                    WHERE legacy.attempt_id = cognitive_extraction_queue.attempt_id
                      AND legacy.extractor_version = cognitive_extraction_queue.extractor_version
                )
                WHERE EXISTS (
                    SELECT 1 FROM cognitive_projection_queue legacy
                    WHERE legacy.attempt_id = cognitive_extraction_queue.attempt_id
                      AND legacy.extractor_version = cognitive_extraction_queue.extractor_version
                      AND legacy.updated_at < cognitive_extraction_queue.updated_at
                )
                """
            )
            conn.execute(
                """
                UPDATE cognitive_extraction_queue
                SET status = 'failed', retry_count = retry_count + 1,
                    last_error = 'projection lease expired', lease_token = ''
                WHERE status = 'processing'
                  AND updated_at <= datetime('now', ?)
                """,
                (f"-{_PROJECTION_LEASE_SECONDS} seconds",),
            )
            version_key = str(extractor_version or "").strip()
            version_clause = "AND extractor_version = ?" if version_key else ""
            params: list[Any] = [f"-{_PROJECTION_RETRY_DELAY_SECONDS} seconds"]
            if version_key:
                params.append(version_key)
            params.append(max(1, int(limit)))
            rows = conn.execute(
                f"""
                SELECT attempt_id, extractor_version
                FROM cognitive_extraction_queue
                WHERE (status = 'pending'
                   OR (status = 'failed' AND updated_at <= datetime('now', ?)))
                {version_clause}
                ORDER BY created_at, attempt_id, extractor_version
                LIMIT ?
                """,
                params,
            ).fetchall()
            claimed_items: list[dict[str, Any]] = []
            for row in rows:
                attempt_id = str(row["attempt_id"])
                item_version = str(row["extractor_version"])
                lease_token = str(uuid.uuid4())
                cursor = conn.execute(
                    """
                    UPDATE cognitive_extraction_queue
                    SET status = 'processing', last_error = NULL,
                        lease_token = ?, updated_at = datetime('now')
                    WHERE attempt_id = ? AND extractor_version = ?
                      AND status IN ('pending', 'failed')
                    """,
                    (lease_token, attempt_id, item_version),
                )
                if int(cursor.rowcount or 0) != 1:
                    continue
                item = _queue_from_row(
                    conn.execute(
                        """SELECT * FROM cognitive_extraction_queue
                        WHERE attempt_id = ? AND extractor_version = ?
                          AND status = 'processing'
                          AND lease_token = ?""",
                        (attempt_id, item_version, lease_token),
                    ).fetchone()
                )
                if item is not None:
                    _compat_queue_update(
                        conn,
                        attempt_id=attempt_id,
                        extractor_version=item_version,
                    )
                    claimed_items.append(item)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return claimed_items


def get_cognitive_projection_input(
    self, attempt_id: str, extractor_version: str | None = None
) -> dict[str, Any] | None:
    attempt_key = str(attempt_id or "").strip()
    if not attempt_key:
        return None
    version_key = str(extractor_version or "").strip()
    version_clause = "AND p.extractor_version = ?" if version_key else ""
    params: list[Any] = [attempt_key]
    if version_key:
        params.append(version_key)
    row = self._require_read_conn().execute(
        f"""
        SELECT a.attempt_id, a.question_id, a.session_id, a.topic_id,
               a.user_answer, a.mode, a.response_time_ms, a.used_hint,
               a.submitted_at, q.source_question_id, q.question_json,
               q.question_type, q.difficulty, e.evaluation_json,
               e.evaluator_type, e.evaluator_version, e.confidence,
               e.fallback_reason, p.extractor_version
        FROM attempts a
        JOIN question_instances q ON q.question_id = a.question_id
        JOIN evaluations e ON e.attempt_id = a.attempt_id
        JOIN cognitive_extraction_queue p ON p.attempt_id = a.attempt_id
        WHERE a.attempt_id = ? AND a.topic_id IS NOT NULL AND a.topic_id != ''
        {version_clause}
        ORDER BY p.created_at DESC, p.extractor_version DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        return None
    question = self._json_loads(row["question_json"], {})
    return {
        "attempt_id": str(row["attempt_id"]),
        "question_id": str(row["question_id"]),
        "session_id": str(row["session_id"]),
        "topic_id": str(row["topic_id"]),
        "source_question_id": str(row["source_question_id"] or ""),
        "question": question,
        "question_text": str(
            question.get("question") or question.get("prompt") or ""
        ),
        "expected_answer": str(
            question.get("answer") or question.get("expected_answer") or ""
        ),
        "question_type": str(row["question_type"] or ""),
        "difficulty": None if row["difficulty"] is None else int(row["difficulty"]),
        "learner_answer": str(row["user_answer"] or ""),
        "evaluation": self._json_loads(row["evaluation_json"], {}),
        "evaluation_metadata": {
            "evaluator_type": str(row["evaluator_type"] or ""),
            "evaluator_version": str(row["evaluator_version"] or ""),
            "confidence": (
                None if row["confidence"] is None else float(row["confidence"])
            ),
            "fallback_reason": str(row["fallback_reason"] or ""),
        },
        "mode": str(row["mode"] or ""),
        "response_time_ms": (
            None if row["response_time_ms"] is None else int(row["response_time_ms"])
        ),
        "used_hint": None if row["used_hint"] is None else bool(row["used_hint"]),
        "submitted_at": str(row["submitted_at"] or ""),
        "extractor_version": str(row["extractor_version"]),
    }


def complete_cognitive_projection(
    self,
    *,
    attempt_id: str,
    lease_token: str,
    evidence: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    snapshots: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    extractor_version: str | None = None,
    model_version: str = DEFAULT_COGNITIVE_MODEL_VERSION,
) -> dict[str, Any]:
    """Commit extraction facts and dirty the independently leased topic fold.

    ``snapshots`` remains accepted for V1 callers, but V2 projectors pass none;
    current state is written only by :func:`complete_cognitive_topic_projection`.
    """
    attempt_key = _required_text(attempt_id, "attempt_id")
    lease_key = _required_text(lease_token, "lease_token")
    if len(evidence) > 3:
        raise ValueError("a cognitive extraction may emit at most three evidence items")
    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            version_key = str(extractor_version or "").strip()
            version_clause = "AND p.extractor_version = ?" if version_key else ""
            params: list[Any] = [attempt_key, lease_key]
            if version_key:
                params.append(version_key)
            queue_row = conn.execute(
                f"""
                SELECT p.extractor_version, a.topic_id
                FROM cognitive_extraction_queue p
                JOIN attempts a ON a.attempt_id = p.attempt_id
                WHERE p.attempt_id = ? AND p.status = 'processing'
                  AND p.lease_token = ?
                  {version_clause}
                """,
                params,
            ).fetchone()
            if queue_row is None:
                raise ValueError("cognitive projection lease is no longer active")
            extractor_version = str(queue_row["extractor_version"])
            topic_id = str(queue_row["topic_id"] or "").strip()
            if not topic_id:
                raise ValueError("cognitive projection attempt is not bound to a topic")
            normalized_evidence = [
                _normalize_evidence(
                    dict(item),
                    attempt_id=attempt_key,
                    extractor_version=extractor_version,
                )
                for item in evidence
            ]
            if len({item["hypothesis_code"] for item in normalized_evidence}) != len(
                normalized_evidence
            ):
                raise ValueError(
                    "an attempt may contribute only one evidence item per hypothesis"
                )
            if any(item["topic_id"] != topic_id for item in normalized_evidence):
                raise ValueError("cognitive evidence topic_id does not match attempt")
            normalized_evidence = [
                item
                for item in normalized_evidence
                if not _has_delete_tombstone(
                    conn,
                    topic_id=item["topic_id"],
                    hypothesis_code=item["hypothesis_code"],
                )
            ]
            normalized_snapshots = [
                _normalize_snapshot(
                    dict(item),
                    default_attempt_id=attempt_key,
                    default_model_version=DEFAULT_COGNITIVE_MODEL_VERSION,
                )
                for item in snapshots
            ]
            normalized_snapshots = [
                item
                for item in normalized_snapshots
                if not _has_delete_tombstone(
                    conn,
                    topic_id=item["topic_id"],
                    hypothesis_code=item["hypothesis_code"],
                )
            ]
            if any(
                item["source_attempt_id"] != attempt_key
                for item in normalized_snapshots
            ):
                raise ValueError("snapshot source_attempt_id does not match queue item")
            if any(item["topic_id"] != topic_id for item in normalized_snapshots):
                raise ValueError("snapshot topic_id does not match attempt")
            inserted_evidence = sum(
                1 for item in normalized_evidence if _write_evidence(conn, item)
            )
            for item in normalized_snapshots:
                _write_snapshot(conn, item)
            requested_generation = _mark_topic_dirty(
                conn,
                topic_id=topic_id,
                model_version=model_version,
            )
            cursor = conn.execute(
                """
                UPDATE cognitive_extraction_queue
                SET status = 'done', last_error = NULL, lease_token = '',
                    updated_at = datetime('now')
                WHERE attempt_id = ? AND extractor_version = ?
                  AND status = 'processing' AND lease_token = ?
                """,
                (attempt_key, extractor_version, lease_key),
            )
            if int(cursor.rowcount or 0) != 1:
                raise ValueError("cognitive projection lease is no longer active")
            _compat_queue_update(
                conn,
                attempt_id=attempt_key,
                extractor_version=extractor_version,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "attempt_id": attempt_key,
        "extractor_version": extractor_version,
        "evidence_inserted": inserted_evidence,
        "snapshots_upserted": len(normalized_snapshots),
        "requested_generation": requested_generation,
        "status": "done",
    }


def _has_delete_tombstone(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    hypothesis_code: str,
) -> bool:
    """Fence late extraction so a user deletion remains a real deletion."""

    row = conn.execute(
        """
        SELECT action
        FROM cognitive_user_controls
        WHERE topic_id = ? AND hypothesis_code = ?
        ORDER BY created_at DESC, rowid DESC
        LIMIT 1
        """,
        (topic_id, hypothesis_code),
    ).fetchone()
    return row is not None and str(row["action"] or "").strip() == "delete"


def mark_cognitive_projection_failed(
    self,
    *,
    attempt_id: str,
    lease_token: str,
    error: str,
    extractor_version: str | None = None,
) -> bool:
    attempt_key = str(attempt_id or "").strip()
    lease_key = str(lease_token or "").strip()
    if not attempt_key or not lease_key:
        return False
    with self._lock:
        conn = self._require_conn()
        version_key = str(extractor_version or "").strip()
        version_clause = "AND extractor_version = ?" if version_key else ""
        params: list[Any] = [str(error or "")[:2000], attempt_key, lease_key]
        if version_key:
            params.append(version_key)
        cursor = conn.execute(
            f"""
            UPDATE cognitive_extraction_queue
            SET status = 'failed', retry_count = retry_count + 1,
                last_error = ?, lease_token = '', updated_at = datetime('now')
            WHERE attempt_id = ? AND status = 'processing' AND lease_token = ?
              {version_clause}
            """,
            params,
        )
        if int(cursor.rowcount or 0) > 0:
            row = conn.execute(
                """
                SELECT extractor_version FROM cognitive_extraction_queue
                WHERE attempt_id = ? AND status = 'failed'
                ORDER BY updated_at DESC, extractor_version DESC LIMIT 1
                """,
                (attempt_key,),
            ).fetchone()
            if row is not None:
                _compat_queue_update(
                    conn,
                    attempt_id=attempt_key,
                    extractor_version=str(row["extractor_version"]),
                )
        conn.commit()
    return int(cursor.rowcount or 0) > 0


def mark_cognitive_topic_projection_dirty(
    self,
    *,
    topic_id: str,
    model_version: str = DEFAULT_COGNITIVE_MODEL_VERSION,
) -> int:
    """Advance a topic generation without disturbing an active lease."""

    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            generation = _mark_topic_dirty(
                conn,
                topic_id=topic_id,
                model_version=model_version,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return generation


def get_cognitive_topic_projection_state(
    self,
    *,
    topic_id: str,
    model_version: str = DEFAULT_COGNITIVE_MODEL_VERSION,
) -> dict[str, Any] | None:
    row = self._require_read_conn().execute(
        """
        SELECT * FROM cognitive_topic_projection_queue
        WHERE topic_id = ? AND model_version = ?
        """,
        (
            _required_text(topic_id, "topic_id"),
            _required_text(model_version, "model_version"),
        ),
    ).fetchone()
    return _topic_projection_from_row(row)


def list_cognitive_topic_projection_queue(
    self,
    *,
    statuses: tuple[str, ...] | list[str] | set[str] | None = None,
    model_version: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    requested = tuple(
        dict.fromkeys(str(status or "").strip() for status in (statuses or ()))
    )
    invalid = set(requested) - _QUEUE_STATUSES
    if invalid:
        raise ValueError(
            f"unsupported cognitive topic projection status: {sorted(invalid)[0]}"
        )
    clauses: list[str] = []
    params: list[Any] = []
    if requested:
        clauses.append("status IN (" + ", ".join("?" for _ in requested) + ")")
        params.extend(requested)
    model_key = str(model_version or "").strip()
    if model_key:
        clauses.append("model_version = ?")
        params.append(model_key)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    rows = self._require_read_conn().execute(
        f"""
        SELECT * FROM cognitive_topic_projection_queue
        {where}
        ORDER BY created_at, topic_id, model_version
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [
        item for item in (_topic_projection_from_row(row) for row in rows) if item
    ]


def claim_cognitive_topic_projections(
    self,
    *,
    limit: int = 1,
    model_version: str | None = None,
) -> list[dict[str, Any]]:
    """Claim dirty topics at a fixed generation with lease fencing."""

    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                UPDATE cognitive_topic_projection_queue
                SET status = 'failed', retry_count = retry_count + 1,
                    last_error = 'topic projection lease expired',
                    lease_token = ''
                WHERE status = 'processing'
                  AND updated_at <= datetime('now', ?)
                """,
                (f"-{_PROJECTION_LEASE_SECONDS} seconds",),
            )
            model_key = str(model_version or "").strip()
            version_clause = "AND model_version = ?" if model_key else ""
            params: list[Any] = [f"-{_PROJECTION_RETRY_DELAY_SECONDS} seconds"]
            if model_key:
                params.append(model_key)
            params.append(max(1, int(limit)))
            rows = conn.execute(
                f"""
                SELECT topic_id, model_version
                FROM cognitive_topic_projection_queue
                WHERE requested_generation > projected_generation
                  AND (
                    status = 'pending'
                    OR (status = 'failed' AND updated_at <= datetime('now', ?))
                  )
                  {version_clause}
                ORDER BY created_at, topic_id, model_version
                LIMIT ?
                """,
                params,
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                topic_key = str(row["topic_id"])
                item_model = str(row["model_version"])
                lease_token = str(uuid.uuid4())
                cursor = conn.execute(
                    """
                    UPDATE cognitive_topic_projection_queue
                    SET status = 'processing', claimed_generation = requested_generation,
                        last_error = NULL, lease_token = ?, updated_at = datetime('now')
                    WHERE topic_id = ? AND model_version = ?
                      AND requested_generation > projected_generation
                      AND status IN ('pending', 'failed')
                    """,
                    (lease_token, topic_key, item_model),
                )
                if int(cursor.rowcount or 0) != 1:
                    continue
                item = _topic_projection_from_row(
                    conn.execute(
                        """
                        SELECT * FROM cognitive_topic_projection_queue
                        WHERE topic_id = ? AND model_version = ?
                          AND status = 'processing' AND lease_token = ?
                        """,
                        (topic_key, item_model, lease_token),
                    ).fetchone()
                )
                if item is not None:
                    claimed.append(item)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return claimed


def complete_cognitive_topic_projection(
    self,
    *,
    topic_id: str,
    model_version: str,
    lease_token: str,
    claimed_generation: int,
    snapshots: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Replace history/current and acknowledge exactly the claimed generation."""

    topic_key = _required_text(topic_id, "topic_id")
    model_key = _required_text(model_version, "model_version")
    lease_key = _required_text(lease_token, "lease_token")
    generation = _non_negative_int(claimed_generation, "claimed_generation")
    normalized = [
        _normalize_snapshot(dict(item), default_model_version=model_key)
        for item in snapshots
    ]
    if any(item["topic_id"] != topic_key for item in normalized):
        raise ValueError("all topic projection snapshots must match topic_id")
    if any(item["model_version"] != model_key for item in normalized):
        raise ValueError("all topic projection snapshots must match model_version")
    current_by_hypothesis: dict[str, dict[str, Any]] = {}
    for item in normalized:
        current_by_hypothesis[item["hypothesis_id"]] = item
    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            queue_row = conn.execute(
                """
                SELECT requested_generation, claimed_generation
                FROM cognitive_topic_projection_queue
                WHERE topic_id = ? AND model_version = ?
                  AND status = 'processing' AND lease_token = ?
                """,
                (topic_key, model_key, lease_key),
            ).fetchone()
            if queue_row is None or int(queue_row["claimed_generation"]) != generation:
                raise ValueError("cognitive topic projection lease is no longer active")
            _validate_snapshot_attempt_topics(conn, normalized)
            conn.execute(
                """
                DELETE FROM cognitive_hypothesis_snapshots
                WHERE topic_id = ? AND model_version = ?
                """,
                (topic_key, model_key),
            )
            conn.execute(
                """
                DELETE FROM cognitive_hypothesis_current
                WHERE topic_id = ? AND model_version = ?
                """,
                (topic_key, model_key),
            )
            for item in normalized:
                _write_snapshot(conn, item)
            for item in current_by_hypothesis.values():
                _write_current(conn, item, projected_generation=generation)
            requested_generation = int(queue_row["requested_generation"])
            next_status = "done" if requested_generation == generation else "pending"
            cursor = conn.execute(
                """
                UPDATE cognitive_topic_projection_queue
                SET status = ?, projected_generation = ?, last_error = NULL,
                    lease_token = '', updated_at = datetime('now')
                WHERE topic_id = ? AND model_version = ?
                  AND status = 'processing' AND lease_token = ?
                  AND claimed_generation = ?
                """,
                (
                    next_status,
                    generation,
                    topic_key,
                    model_key,
                    lease_key,
                    generation,
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise ValueError("cognitive topic projection lease is no longer active")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "topic_id": topic_key,
        "model_version": model_key,
        "projected_generation": generation,
        "requested_generation": requested_generation,
        "status": next_status,
        "current_upserted": len(current_by_hypothesis),
    }


def mark_cognitive_topic_projection_failed(
    self,
    *,
    topic_id: str,
    model_version: str,
    lease_token: str,
    claimed_generation: int,
    error: str,
) -> bool:
    with self._lock:
        conn = self._require_conn()
        cursor = conn.execute(
            """
            UPDATE cognitive_topic_projection_queue
            SET status = 'failed', retry_count = retry_count + 1,
                last_error = ?, lease_token = '', updated_at = datetime('now')
            WHERE topic_id = ? AND model_version = ?
              AND status = 'processing' AND lease_token = ?
              AND claimed_generation = ?
            """,
            (
                str(error or "")[:2000],
                _required_text(topic_id, "topic_id"),
                _required_text(model_version, "model_version"),
                _required_text(lease_token, "lease_token"),
                _non_negative_int(claimed_generation, "claimed_generation"),
            ),
        )
        conn.commit()
    return int(cursor.rowcount or 0) > 0


def list_cognitive_evidence(
    self,
    *,
    topic_id: str | None = None,
    hypothesis_code: str | None = None,
    extractor_version: str | None = None,
    through_attempt_id: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("ce.topic_id", topic_id),
        ("ce.hypothesis_code", hypothesis_code),
        ("ce.extractor_version", extractor_version),
    ):
        key = str(value or "").strip()
        if key:
            clauses.append(f"{column} = ?")
            params.append(key)
    through_key = str(through_attempt_id or "").strip()
    if through_key:
        through_row = self._require_read_conn().execute(
            "SELECT submitted_at FROM attempts WHERE attempt_id = ?",
            (through_key,),
        ).fetchone()
        if through_row is None:
            return []
        submitted_at = str(through_row["submitted_at"])
        clauses.append(
            "(a.submitted_at < ? OR (a.submitted_at = ? AND a.attempt_id <= ?))"
        )
        params.extend((submitted_at, submitted_at, through_key))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(max(1, int(limit)))
    rows = self._require_read_conn().execute(
        f"""
        SELECT ce.*
        FROM cognitive_evidence ce
        JOIN attempts a ON a.attempt_id = ce.attempt_id
        {where}
        ORDER BY a.submitted_at, a.attempt_id, ce.evidence_id
        {limit_sql}
        """,
        params,
    ).fetchall()
    return [item for item in (_evidence_from_row(row) for row in rows) if item]


def upsert_cognitive_hypothesis_snapshot(
    self, snapshot: dict[str, Any]
) -> dict[str, Any]:
    item = _normalize_snapshot(dict(snapshot))
    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            _validate_snapshot_attempt_topics(conn, [item])
            _write_snapshot(conn, item)
            row = conn.execute(
                """
                SELECT * FROM cognitive_hypothesis_snapshots
                WHERE hypothesis_id = ? AND model_version = ?
                  AND source_attempt_id = ?
                """,
                (
                    item["hypothesis_id"],
                    item["model_version"],
                    item["source_attempt_id"],
                ),
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    result = _snapshot_from_row(row)
    if result is None:
        raise RuntimeError("cognitive hypothesis snapshot upsert failed")
    return result


def replace_cognitive_hypothesis_snapshots(
    self,
    *,
    topic_id: str,
    model_version: str,
    snapshots: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    """Atomically replace one topic/model projection during a full rebuild."""
    topic_key = _required_text(topic_id, "topic_id")
    model_key = _required_text(model_version, "model_version")
    normalized = [
        _normalize_snapshot(dict(item), default_model_version=model_key)
        for item in snapshots
    ]
    if any(item["topic_id"] != topic_key for item in normalized):
        raise ValueError("all rebuilt snapshots must match topic_id")
    if any(item["model_version"] != model_key for item in normalized):
        raise ValueError("all rebuilt snapshots must match model_version")
    current_by_hypothesis: dict[str, dict[str, Any]] = {}
    for item in normalized:
        current_by_hypothesis[item["hypothesis_id"]] = item
    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            _validate_snapshot_attempt_topics(conn, normalized)
            conn.execute(
                """DELETE FROM cognitive_hypothesis_snapshots
                WHERE topic_id = ? AND model_version = ?""",
                (topic_key, model_key),
            )
            for item in normalized:
                _write_snapshot(conn, item)
            queue_row = conn.execute(
                """
                SELECT status, requested_generation, projected_generation
                FROM cognitive_topic_projection_queue
                WHERE topic_id = ? AND model_version = ?
                """,
                (topic_key, model_key),
            ).fetchone()
            if (
                queue_row is not None
                and str(queue_row["status"] or "") == "done"
                and int(queue_row["requested_generation"])
                == int(queue_row["projected_generation"])
            ):
                generation = int(queue_row["projected_generation"])
                conn.execute(
                    """
                    DELETE FROM cognitive_hypothesis_current
                    WHERE topic_id = ? AND model_version = ?
                    """,
                    (topic_key, model_key),
                )
                for item in current_by_hypothesis.values():
                    _write_current(conn, item, projected_generation=generation)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return list_cognitive_hypothesis_snapshots(
        self, topic_id=topic_key, model_version=model_key
    )


def list_cognitive_hypothesis_snapshots(
    self,
    *,
    topic_id: str | None = None,
    hypothesis_code: str | None = None,
    model_version: str | None = None,
    latest_only: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("chs.topic_id", topic_id),
        ("chs.hypothesis_code", hypothesis_code),
        ("chs.model_version", model_version),
    ):
        key = str(value or "").strip()
        if key:
            clauses.append(f"{column} = ?")
            params.append(key)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = self._require_read_conn().execute(
        f"""
        SELECT chs.*
        FROM cognitive_hypothesis_snapshots chs
        JOIN attempts a ON a.attempt_id = chs.source_attempt_id
        {where}
        ORDER BY a.submitted_at DESC, a.attempt_id DESC, chs.snapshot_id DESC
        """,
        params,
    ).fetchall()
    items = [item for item in (_snapshot_from_row(row) for row in rows) if item]
    if latest_only:
        latest: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            key = (item["hypothesis_id"], item["model_version"])
            if key in seen:
                continue
            seen.add(key)
            latest.append(item)
        items = latest
    if limit is not None:
        items = items[: max(1, int(limit))]
    return items


def list_cognitive_hypothesis_current(
    self,
    *,
    topic_id: str | None = None,
    hypothesis_code: str | None = None,
    model_version: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("topic_id", topic_id),
        ("hypothesis_code", hypothesis_code),
        ("model_version", model_version),
    ):
        key = str(value or "").strip()
        if key:
            clauses.append(f"{column} = ?")
            params.append(key)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    rows = self._require_read_conn().execute(
        f"""
        SELECT * FROM cognitive_hypothesis_current
        {where}
        ORDER BY computed_at DESC, topic_id, hypothesis_code
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [item for item in (_current_from_row(row) for row in rows) if item]


def record_cognitive_user_control(
    self,
    *,
    topic_id: str,
    hypothesis_code: str,
    action: str,
    reason: str = "",
    control_id: str = "",
    expires_at: str = "",
) -> dict[str, Any]:
    topic_key = _required_text(topic_id, "topic_id")
    code_key = _required_text(hypothesis_code, "hypothesis_code")
    action_key = _required_text(action, "action")
    if action_key not in _CONTROL_ACTIONS:
        raise ValueError(f"unsupported cognitive user control action: {action_key}")
    control_key = _required_text(control_id or uuid.uuid4(), "control_id")
    expiry = _iso_datetime(expires_at, "expires_at")
    if action_key != "suppress" and expiry:
        raise ValueError("expires_at is only valid for suppress controls")
    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            model_rows = conn.execute(
                """
                SELECT model_version FROM cognitive_topic_projection_queue
                WHERE topic_id = ?
                UNION
                SELECT model_version FROM cognitive_hypothesis_snapshots
                WHERE topic_id = ? AND hypothesis_code = ?
                UNION
                SELECT model_version FROM cognitive_hypothesis_current
                WHERE topic_id = ? AND hypothesis_code = ?
                """,
                (topic_key, topic_key, code_key, topic_key, code_key),
            ).fetchall()
            model_versions = {
                str(row["model_version"] or "").strip()
                for row in model_rows
                if str(row["model_version"] or "").strip()
            } or {DEFAULT_COGNITIVE_MODEL_VERSION}
            conn.execute(
                """
                INSERT INTO cognitive_user_controls (
                    control_id, topic_id, hypothesis_code, action, reason,
                    expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    control_key,
                    topic_key,
                    code_key,
                    action_key,
                    str(reason or ""),
                    expiry,
                ),
            )
            if action_key == "delete":
                # Raw attempts/evaluations remain immutable. Only cognitive
                # derivations are erased; the control row is the tombstone.
                conn.execute(
                    """
                    DELETE FROM cognitive_intervention_events
                    WHERE topic_id = ? AND hypothesis_code = ?
                    """,
                    (topic_key, code_key),
                )
                conn.execute(
                    """
                    DELETE FROM cognitive_evidence
                    WHERE topic_id = ? AND hypothesis_code = ?
                    """,
                    (topic_key, code_key),
                )
                conn.execute(
                    """
                    DELETE FROM cognitive_hypothesis_snapshots
                    WHERE topic_id = ? AND hypothesis_code = ?
                    """,
                    (topic_key, code_key),
                )
                conn.execute(
                    """
                    DELETE FROM cognitive_hypothesis_current
                    WHERE topic_id = ? AND hypothesis_code = ?
                    """,
                    (topic_key, code_key),
                )
            for version in model_versions:
                _mark_topic_dirty(
                    conn,
                    topic_id=topic_key,
                    model_version=version,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    rows = list_cognitive_user_controls(
        self, topic_id=topic_key, hypothesis_code=code_key, limit=1
    )
    if not rows:
        raise RuntimeError("cognitive user control write failed")
    return rows[0]


def list_cognitive_user_controls(
    self,
    *,
    topic_id: str | None = None,
    hypothesis_code: str | None = None,
    limit: int = 100,
    active_only: bool = False,
    as_of: str | datetime | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    topic_key = str(topic_id or "").strip()
    code_key = str(hypothesis_code or "").strip()
    if topic_key:
        clauses.append("topic_id = ?")
        params.append(topic_key)
    if code_key:
        clauses.append("hypothesis_code = ?")
        params.append(code_key)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    rows = self._require_read_conn().execute(
        f"""
        SELECT * FROM cognitive_user_controls
        {where}
        ORDER BY created_at DESC, rowid DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    items = [item for item in (_control_from_row(row) for row in rows) if item]
    if not active_only:
        return items
    point = datetime.now(timezone.utc) if as_of is None else _parse_datetime(as_of)
    return [item for item in items if _control_is_active(item, point)]


def is_cognitive_hypothesis_suppressed(
    self,
    *,
    topic_id: str,
    hypothesis_code: str,
    as_of: str | datetime | None = None,
) -> bool:
    controls = list_cognitive_user_controls(
        self,
        topic_id=topic_id,
        hypothesis_code=hypothesis_code,
        limit=1,
    )
    if not controls:
        return False
    point = datetime.now(timezone.utc) if as_of is None else _parse_datetime(as_of)
    return bool(
        controls[0]["action"] in _SUPPRESSING_ACTIONS
        and _control_is_active(controls[0], point)
    )


def _parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _iso_datetime(value, "as_of")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _control_is_active(item: dict[str, Any], point: datetime) -> bool:
    if item["action"] != "suppress":
        return True
    expiry = str(item.get("expires_at") or "").strip()
    return not expiry or _parse_datetime(expiry) > point
