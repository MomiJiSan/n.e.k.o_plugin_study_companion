"""SQLite projection storage for the opt-in mastery V2 shadow model.

The answer writer owns queue insertion and its surrounding transaction.  The
public worker helpers deliberately use separate transactions so projection
failures can never roll back an answer that was already committed.
"""

from __future__ import annotations

import math

from .store_common import Any, sqlite3

_QUEUE_STATUSES = frozenset({"pending", "processing", "done", "failed"})
_PROJECTION_LEASE_SECONDS = 300
_PROJECTION_RETRY_DELAY_SECONDS = 300


def _bounded_unit_float(value: object, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    return max(0.0, min(1.0, result))


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
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def _snapshot_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "topic_id": str(row["topic_id"]),
        "mastery": float(row["mastery"]),
        "accuracy": float(row["accuracy"]),
        "recency": float(row["recency"]),
        "consistency": float(row["consistency"]),
        "confidence": float(row["confidence"]),
        "evidence_count": int(row["evidence_count"] or 0),
        "unresolved_wrong_count": int(row["unresolved_wrong_count"] or 0),
        "mastery_model_version": str(row["mastery_model_version"]),
        "source_attempt_id": str(row["source_attempt_id"]),
        "computed_at": str(row["computed_at"] or ""),
    }


def _normalized_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    topic_id = str(snapshot.get("topic_id") or "").strip()
    model_version = str(snapshot.get("mastery_model_version") or "").strip()
    source_attempt_id = str(snapshot.get("source_attempt_id") or "").strip()
    if not topic_id or not model_version or not source_attempt_id:
        raise ValueError(
            "topic_id, mastery_model_version, and source_attempt_id are required"
        )
    return {
        "topic_id": topic_id,
        "mastery": _bounded_unit_float(snapshot.get("mastery"), "mastery"),
        "accuracy": _bounded_unit_float(snapshot.get("accuracy"), "accuracy"),
        "recency": _bounded_unit_float(snapshot.get("recency"), "recency"),
        "consistency": _bounded_unit_float(
            snapshot.get("consistency"), "consistency"
        ),
        "confidence": _bounded_unit_float(snapshot.get("confidence"), "confidence"),
        "evidence_count": _non_negative_int(
            snapshot.get("evidence_count", 0), "evidence_count"
        ),
        "unresolved_wrong_count": _non_negative_int(
            snapshot.get("unresolved_wrong_count", 0), "unresolved_wrong_count"
        ),
        "mastery_model_version": model_version,
        "source_attempt_id": source_attempt_id,
        "computed_at": str(snapshot.get("computed_at") or "").strip(),
    }


def _write_mastery_snapshot_v2(
    conn: sqlite3.Connection, snapshot: dict[str, Any]
) -> None:
    item = _normalized_snapshot(snapshot)
    conn.execute(
        """
        INSERT INTO mastery_snapshots_v2 (
            topic_id, mastery, accuracy, recency, consistency, confidence,
            evidence_count, unresolved_wrong_count, mastery_model_version,
            source_attempt_id, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(NULLIF(?, ''), datetime('now')))
        ON CONFLICT(topic_id, mastery_model_version, source_attempt_id) DO UPDATE SET
            mastery = excluded.mastery,
            accuracy = excluded.accuracy,
            recency = excluded.recency,
            consistency = excluded.consistency,
            confidence = excluded.confidence,
            evidence_count = excluded.evidence_count,
            unresolved_wrong_count = excluded.unresolved_wrong_count,
            computed_at = excluded.computed_at
        """,
        (
            item["topic_id"],
            item["mastery"],
            item["accuracy"],
            item["recency"],
            item["consistency"],
            item["confidence"],
            item["evidence_count"],
            item["unresolved_wrong_count"],
            item["mastery_model_version"],
            item["source_attempt_id"],
            item["computed_at"],
        ),
    )


def enqueue_mastery_projection(
    self, conn: sqlite3.Connection, *, attempt_id: str
) -> bool:
    """Idempotently enqueue an attempt in the caller-owned answer transaction."""
    attempt_key = str(attempt_id or "").strip()
    if not attempt_key:
        return False
    cursor = conn.execute(
        """
        INSERT INTO mastery_projection_queue (
            attempt_id, status, retry_count, last_error, created_at, updated_at
        ) VALUES (?, 'pending', 0, NULL, datetime('now'), datetime('now'))
        ON CONFLICT(attempt_id) DO NOTHING
        """,
        (attempt_key,),
    )
    return int(cursor.rowcount or 0) > 0


def list_mastery_projection_queue(
    self,
    *,
    statuses: tuple[str, ...] | list[str] | set[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    requested = tuple(
        dict.fromkeys(str(status or "").strip() for status in (statuses or ()))
    )
    invalid = set(requested) - _QUEUE_STATUSES
    if invalid:
        raise ValueError(f"unsupported mastery projection status: {sorted(invalid)[0]}")
    params: list[Any] = []
    where = ""
    if requested:
        where = "WHERE status IN (" + ", ".join("?" for _ in requested) + ")"
        params.extend(requested)
    params.append(max(1, int(limit)))
    rows = self._require_read_conn().execute(
        f"""
        SELECT * FROM mastery_projection_queue
        {where}
        ORDER BY created_at, attempt_id
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [item for item in (_queue_from_row(row) for row in rows) if item]


def claim_mastery_projections(
    self, *, limit: int = 1
) -> list[dict[str, Any]]:
    """Atomically claim pending or retryable failed projection work."""
    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                UPDATE mastery_projection_queue
                SET status = 'failed', retry_count = retry_count + 1,
                    last_error = 'projection lease expired'
                WHERE status = 'processing'
                  AND updated_at <= datetime('now', ?)
                """,
                (f"-{_PROJECTION_LEASE_SECONDS} seconds",),
            )
            rows = conn.execute(
                """
                SELECT attempt_id
                FROM mastery_projection_queue
                WHERE status = 'pending'
                   OR (
                       status = 'failed'
                       AND updated_at <= datetime('now', ?)
                   )
                ORDER BY created_at, attempt_id
                LIMIT ?
                """,
                (
                    f"-{_PROJECTION_RETRY_DELAY_SECONDS} seconds",
                    max(1, int(limit)),
                ),
            ).fetchall()
            attempt_ids = [str(row["attempt_id"]) for row in rows]
            if attempt_ids:
                placeholders = ", ".join("?" for _ in attempt_ids)
                conn.execute(
                    f"""
                    UPDATE mastery_projection_queue
                    SET status = 'processing', last_error = NULL,
                        updated_at = datetime('now')
                    WHERE status IN ('pending', 'failed')
                      AND attempt_id IN ({placeholders})
                    """,
                    attempt_ids,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if not attempt_ids:
        return []
    placeholders = ", ".join("?" for _ in attempt_ids)
    claimed_rows = self._require_read_conn().execute(
        f"""
        SELECT * FROM mastery_projection_queue
        WHERE status = 'processing' AND attempt_id IN ({placeholders})
        ORDER BY created_at, attempt_id
        """,
        attempt_ids,
    ).fetchall()
    return [
        item for item in (_queue_from_row(row) for row in claimed_rows) if item
    ]


def mark_mastery_projection_failed(
    self, *, attempt_id: str, error: str
) -> bool:
    attempt_key = str(attempt_id or "").strip()
    if not attempt_key:
        return False
    with self._lock:
        conn = self._require_conn()
        cursor = conn.execute(
            """
            UPDATE mastery_projection_queue
            SET status = 'failed', retry_count = retry_count + 1,
                last_error = ?, updated_at = datetime('now')
            WHERE attempt_id = ? AND status != 'done'
            """,
            (str(error or "")[:2000], attempt_key),
        )
        conn.commit()
    return int(cursor.rowcount or 0) > 0


def upsert_mastery_snapshot_v2(
    self, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Idempotently write a snapshot, including during full rebuilds."""
    item = _normalized_snapshot(snapshot)
    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            _write_mastery_snapshot_v2(conn, item)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    result = get_mastery_snapshot_v2(
        self,
        topic_id=item["topic_id"],
        mastery_model_version=item["mastery_model_version"],
        source_attempt_id=item["source_attempt_id"],
    )
    if result is None:
        raise RuntimeError("mastery V2 snapshot upsert failed")
    return result


def complete_mastery_projection(
    self, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Atomically store a snapshot and mark its source queue item done."""
    item = _normalized_snapshot(snapshot)
    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            _write_mastery_snapshot_v2(conn, item)
            cursor = conn.execute(
                """
                UPDATE mastery_projection_queue
                SET status = 'done', last_error = NULL,
                    updated_at = datetime('now')
                WHERE attempt_id = ?
                """,
                (item["source_attempt_id"],),
            )
            if int(cursor.rowcount or 0) < 1:
                raise ValueError("source attempt is not queued for mastery projection")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    result = get_mastery_snapshot_v2(
        self,
        topic_id=item["topic_id"],
        mastery_model_version=item["mastery_model_version"],
        source_attempt_id=item["source_attempt_id"],
    )
    if result is None:
        raise RuntimeError("mastery V2 projection completion failed")
    return result


def get_mastery_snapshot_v2(
    self,
    *,
    topic_id: str,
    mastery_model_version: str,
    source_attempt_id: str,
) -> dict[str, Any] | None:
    row = self._require_read_conn().execute(
        """
        SELECT * FROM mastery_snapshots_v2
        WHERE topic_id = ? AND mastery_model_version = ? AND source_attempt_id = ?
        """,
        (
            str(topic_id or "").strip(),
            str(mastery_model_version or "").strip(),
            str(source_attempt_id or "").strip(),
        ),
    ).fetchone()
    return _snapshot_from_row(row)


def get_latest_mastery_v2(
    self, *, topic_id: str, mastery_model_version: str
) -> dict[str, Any] | None:
    row = self._require_read_conn().execute(
        """
        SELECT ms.*
        FROM mastery_snapshots_v2 ms
        JOIN attempts a ON a.attempt_id = ms.source_attempt_id
        WHERE ms.topic_id = ? AND ms.mastery_model_version = ?
        ORDER BY a.submitted_at DESC, a.attempt_id DESC, ms.id DESC
        LIMIT 1
        """,
        (
            str(topic_id or "").strip(),
            str(mastery_model_version or "").strip(),
        ),
    ).fetchone()
    return _snapshot_from_row(row)


def list_latest_mastery_v2_for_topics(
    self,
    topic_ids: list[str] | tuple[str, ...] | set[str],
    *,
    mastery_model_version: str,
) -> list[dict[str, Any]]:
    topic_keys = [
        key
        for key in dict.fromkeys(str(item or "").strip() for item in topic_ids)
        if key
    ]
    model_version = str(mastery_model_version or "").strip()
    if not topic_keys or not model_version:
        return []
    result: list[dict[str, Any]] = []
    conn = self._require_read_conn()
    for start in range(0, len(topic_keys), 500):
        chunk = topic_keys[start : start + 500]
        placeholders = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT ms.*
            FROM mastery_snapshots_v2 ms
            WHERE ms.mastery_model_version = ?
              AND ms.topic_id IN ({placeholders})
              AND ms.id = (
                  SELECT candidate.id
                  FROM mastery_snapshots_v2 candidate
                  JOIN attempts candidate_attempt
                    ON candidate_attempt.attempt_id = candidate.source_attempt_id
                  WHERE candidate.topic_id = ms.topic_id
                    AND candidate.mastery_model_version = ms.mastery_model_version
                  ORDER BY candidate_attempt.submitted_at DESC,
                           candidate_attempt.attempt_id DESC,
                           candidate.id DESC
                  LIMIT 1
              )
            """,
            [model_version, *chunk],
        ).fetchall()
        result.extend(
            item for item in (_snapshot_from_row(row) for row in rows) if item
        )
    return result


def count_active_wrong_questions(self, topic_id: str) -> int:
    row = self._require_read_conn().execute(
        """
        SELECT COUNT(*) AS count
        FROM wrong_questions
        WHERE topic_id = ? AND status IN ('active', 'retrying')
        """,
        (str(topic_id or "").strip(),),
    ).fetchone()
    return int(row["count"] if row is not None else 0)


def list_mastery_v2_attempt_ids(
    self, *, topic_id: str | None = None
) -> list[str]:
    """List only complete PR-7 fact identities in deterministic rebuild order."""
    topic_key = str(topic_id or "").strip()
    where = "AND a.topic_id = ?" if topic_key else ""
    params: tuple[str, ...] = (topic_key,) if topic_key else ()
    rows = self._require_read_conn().execute(
        f"""
        SELECT a.attempt_id
        FROM attempts a
        JOIN question_instances q ON q.question_id = a.question_id
        JOIN evaluations e ON e.attempt_id = a.attempt_id
        WHERE a.topic_id IS NOT NULL AND a.topic_id != ''
          {where}
        ORDER BY a.submitted_at, a.attempt_id
        """,
        params,
    ).fetchall()
    return [str(row["attempt_id"]) for row in rows]


def list_mastery_v2_evidence(
    self,
    *,
    topic_id: str,
    through_attempt_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return only stable PR-7 facts in deterministic chronological order."""
    topic_key = str(topic_id or "").strip()
    if not topic_key:
        return []
    rows = self._require_read_conn().execute(
        """
        SELECT a.attempt_id, a.topic_id, a.response_time_ms, a.used_hint,
               a.submitted_at, q.difficulty, e.evaluation_json, e.confidence
        FROM attempts a
        JOIN question_instances q ON q.question_id = a.question_id
        JOIN evaluations e ON e.attempt_id = a.attempt_id
        WHERE a.topic_id = ?
        ORDER BY a.submitted_at, a.attempt_id
        """,
        (topic_key,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    through_key = str(through_attempt_id or "").strip()
    found_through = not through_key
    for row in rows:
        evaluation = self._json_loads(row["evaluation_json"], {})
        attempt_id = str(row["attempt_id"])
        result.append(
            {
                "attempt_id": attempt_id,
                "topic_id": str(row["topic_id"] or ""),
                "verdict": str(evaluation.get("verdict") or ""),
                "score": evaluation.get("score"),
                "difficulty": (
                    None if row["difficulty"] is None else int(row["difficulty"])
                ),
                "used_hint": (
                    None if row["used_hint"] is None else bool(row["used_hint"])
                ),
                "response_time_ms": (
                    None
                    if row["response_time_ms"] is None
                    else int(row["response_time_ms"])
                ),
                "evaluator_confidence": (
                    None if row["confidence"] is None else float(row["confidence"])
                ),
                "submitted_at": str(row["submitted_at"] or ""),
            }
        )
        if through_key and attempt_id == through_key:
            found_through = True
            break
    if not found_through:
        return []
    return result


def get_mastery_v2_projection_input(
    self, attempt_id: str
) -> dict[str, Any] | None:
    attempt_key = str(attempt_id or "").strip()
    if not attempt_key:
        return None
    row = self._require_read_conn().execute(
        "SELECT topic_id FROM attempts WHERE attempt_id = ?",
        (attempt_key,),
    ).fetchone()
    if row is None or not str(row["topic_id"] or "").strip():
        return None
    topic_id = str(row["topic_id"])
    evidence = list_mastery_v2_evidence(
        self, topic_id=topic_id, through_attempt_id=attempt_key
    )
    if not evidence:
        return None
    return {
        "topic_id": topic_id,
        "source_attempt_id": attempt_key,
        "evidence": evidence,
        "unresolved_wrong_count": count_active_wrong_questions(self, topic_id),
    }
