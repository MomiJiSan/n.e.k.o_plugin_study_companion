"""Additive question/attempt/evaluation facts alongside legacy QA records."""

from __future__ import annotations

import math

from .store_common import Any, sqlite3


def _question_instance_id(
    *, question: dict[str, Any], source_question_id: str | None, attempt_id: str
) -> str:
    """Use only existing stable identities; never invent a random question ID."""
    return str(
        question.get("question_id") or source_question_id or attempt_id or ""
    ).strip()


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_confidence(value: object) -> float | None:
    """Return a finite evaluator confidence without changing evaluation JSON."""
    try:
        confidence = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(confidence):
        return None
    return max(0.0, min(1.0, confidence))


def _evaluation_metadata(eval_result: dict[str, Any] | None) -> tuple[str, str, float | None, str]:
    """Extract additive storage metadata with conservative legacy defaults."""
    payload = dict(eval_result or {})
    evaluator_type = str(payload.get("evaluator_type") or "llm_rubric").strip()
    evaluator_version = str(payload.get("evaluator_version") or "legacy-v1").strip()
    fallback_reason = str(payload.get("fallback_reason") or "")
    return (
        evaluator_type or "llm_rubric",
        evaluator_version or "legacy-v1",
        _optional_confidence(payload.get("confidence")),
        fallback_reason,
    )


def write_attempt_facts(
    self,
    conn: sqlite3.Connection,
    *,
    session_id: str,
    topic_id: str | None,
    source_question_id: str | None,
    question: dict[str, Any],
    user_answer: str,
    eval_result: dict[str, Any],
    mode: str,
    response_time_ms: int | None,
    attempt_id: str,
) -> bool:
    """Write the new facts in the caller-owned answer transaction.

    Calls without an existing ``attempt_id`` intentionally remain legacy-only:
    they have no stable idempotency identity to place in ``attempts``.
    """
    attempt_key = str(attempt_id or "").strip()
    if not attempt_key:
        return False
    question_payload = dict(question or {})
    question_key = _question_instance_id(
        question=question_payload,
        source_question_id=source_question_id,
        attempt_id=attempt_key,
    )
    if not question_key:
        return False
    # ``attempt_id`` belongs to an attempt, not to the immutable generated
    # question.  Keep the legacy QA payload unchanged; only the new question
    # fact removes this answer-specific field.
    question_instance_payload = dict(question_payload)
    question_instance_payload.pop("attempt_id", None)
    conn.execute(
        """INSERT INTO question_instances (
            question_id, topic_id, source_question_id, question_json,
            question_type, difficulty, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'answered', datetime('now'), datetime('now'))
        ON CONFLICT(question_id) DO NOTHING""",
        (
            question_key,
            topic_id,
            source_question_id,
            self._json_dumps(question_instance_payload),
            str(question_payload.get("question_type") or question_payload.get("type") or ""),
            _optional_int(question_payload.get("difficulty")),
        ),
    )
    conn.execute(
        """INSERT INTO attempts (
            attempt_id, question_id, session_id, topic_id, user_answer, mode,
            response_time_ms, submitted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (
            attempt_key,
            question_key,
            session_id,
            topic_id,
            str(user_answer or ""),
            str(mode or "companion"),
            int(response_time_ms) if response_time_ms is not None else None,
        ),
    )
    evaluator_type, evaluator_version, confidence, fallback_reason = _evaluation_metadata(
        eval_result
    )
    conn.execute(
        """INSERT INTO evaluations (
            attempt_id, evaluation_json, evaluator_type, evaluator_version,
            confidence, fallback_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        (
            attempt_key,
            self._json_dumps(dict(eval_result or {})),
            evaluator_type,
            evaluator_version,
            confidence,
            fallback_reason,
        ),
    )
    return True


def get_attempt_fact(self, attempt_id: str) -> dict[str, Any] | None:
    """Read canonical facts first, then fall back to legacy ``qa_records``."""
    attempt_key = str(attempt_id or "").strip()
    if not attempt_key:
        return None
    row = self._require_read_conn().execute(
        """SELECT a.attempt_id, a.question_id, a.session_id, a.topic_id,
                  a.user_answer, a.mode, a.response_time_ms, a.submitted_at,
                  q.source_question_id, q.question_json, e.evaluation_json,
                  e.evaluator_type, e.evaluator_version, e.confidence,
                  e.fallback_reason
           FROM attempts a
           JOIN question_instances q ON q.question_id = a.question_id
           LEFT JOIN evaluations e ON e.attempt_id = a.attempt_id
           WHERE a.attempt_id = ?""",
        (attempt_key,),
    ).fetchone()
    if row is not None:
        return {
            "attempt_id": str(row["attempt_id"]),
            "question_id": str(row["question_id"]),
            "session_id": str(row["session_id"]),
            "topic_id": str(row["topic_id"] or ""),
            "source_question_id": str(row["source_question_id"] or ""),
            "question": self._json_loads(row["question_json"], {}),
            "user_answer": str(row["user_answer"] or ""),
            "eval_result": self._json_loads(row["evaluation_json"], {}),
            "evaluation_metadata": {
                "evaluator_type": str(row["evaluator_type"] or "llm_rubric"),
                "evaluator_version": str(row["evaluator_version"] or "legacy-v1"),
                "confidence": _optional_confidence(row["confidence"]),
                "fallback_reason": str(row["fallback_reason"] or ""),
            },
            "mode": str(row["mode"] or ""),
            "response_time_ms": int(row["response_time_ms"] or 0),
            "submitted_at": str(row["submitted_at"] or ""),
            "storage": "attempt_facts",
        }
    legacy_row = self._require_read_conn().execute(
        """SELECT * FROM qa_records
        WHERE json_valid(question)
          AND json_extract(question, '$.attempt_id') = ?
        ORDER BY id DESC
        LIMIT 1""",
        (attempt_key,),
    ).fetchone()
    legacy = self._qa_record_from_row(legacy_row)
    if legacy is None:
        return None
    question = dict(legacy.get("question") or {})
    return {
        "attempt_id": attempt_key,
        "question_id": _question_instance_id(
            question=question,
            source_question_id=str(legacy.get("source_question_id") or "") or None,
            attempt_id=attempt_key,
        ),
        "session_id": str(legacy.get("session_id") or ""),
        "topic_id": str(legacy.get("topic_id") or ""),
        "source_question_id": str(legacy.get("source_question_id") or ""),
        "question": question,
        "user_answer": str(legacy.get("user_answer") or ""),
        "eval_result": dict(legacy.get("eval_result") or {}),
        "evaluation_metadata": {
            "evaluator_type": "llm_rubric",
            "evaluator_version": "legacy-v1",
            "confidence": None,
            "fallback_reason": "",
        },
        "mode": str(legacy.get("mode") or ""),
        "response_time_ms": int(legacy.get("response_time_ms") or 0),
        "submitted_at": str(legacy.get("created_at") or ""),
        "storage": "legacy_qa_record",
    }
