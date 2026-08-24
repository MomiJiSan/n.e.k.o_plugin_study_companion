from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from typing import Any

_MAX_QUESTION_TEXT_LENGTH = 20_000
_SOURCE_TYPES = frozenset({"ocr", "manual", "document"})
_CONSENT_ORIGINS = frozenset(
    {"explicit_save", "explain", "generate", "evaluate", "auto_save"}
)


def _normalize_question_text(value: object) -> str:
    """Return stable, bounded text suitable for local question persistence."""
    raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(" ".join(line.split()) for line in raw.split("\n"))
    text = "\n".join(line for line in text.split("\n") if line).strip()
    if not text:
        raise ValueError("question text is required")
    if len(text) > _MAX_QUESTION_TEXT_LENGTH:
        raise ValueError(
            f"question text exceeds {_MAX_QUESTION_TEXT_LENGTH} character limit"
        )
    return text


def _optional_text(value: object, *, field: str, max_length: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > max_length:
        raise ValueError(f"{field} exceeds {max_length} character limit")
    return text


def _classification_value(classification: object, key: str) -> object:
    if isinstance(classification, Mapping):
        return classification.get(key)
    return getattr(classification, key, None)


def _classification_metadata(classification: object | None) -> tuple[str, float | None]:
    """Extract only allowlisted metadata, never OCR excerpts or window titles."""
    if classification is None:
        return "", None
    question_type = _optional_text(
        _classification_value(classification, "question_type")
        or _classification_value(classification, "screen_type"),
        field="question_type",
        max_length=120,
    )
    raw_confidence = _classification_value(classification, "confidence")
    if raw_confidence in (None, ""):
        return question_type, None
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("classification confidence must be numeric") from exc
    return question_type, max(0.0, min(1.0, confidence))


def _captured_question_from_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "source_type": str(row["source_type"] or ""),
        "question_text": str(row["question_text"] or ""),
        "content_hash": str(row["content_hash"] or ""),
        "topic_id": str(row["topic_id"] or ""),
        "subject": str(row["subject"] or ""),
        "question_type": str(row["question_type"] or ""),
        "classification_confidence": (
            float(row["classification_confidence"])
            if row["classification_confidence"] is not None
            else None
        ),
        "consent_origin": str(row["consent_origin"] or ""),
        "status": str(row["status"] or ""),
        "created_at": str(row["created_at"] or ""),
        "last_used_at": str(row["last_used_at"] or ""),
        "expires_at": str(row["expires_at"] or ""),
    }


def save_captured_question(
    self,
    *,
    text: str,
    consent_origin: str,
    source_type: str = "ocr",
    topic_id: str = "",
    subject: str = "",
    question_type: str = "",
    classification: object | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Persist a user-intended question asset and return its canonical record.

    This accepts only question content and allowlisted learning metadata. Callers
    must not pass screenshots, OCR images, window titles, or text excerpts.
    """
    question_text = _normalize_question_text(text)
    source = str(source_type or "").strip().lower()
    origin = str(consent_origin or "").strip().lower()
    if source not in _SOURCE_TYPES:
        raise ValueError(f"unsupported question source_type: {source_type!r}")
    if origin not in _CONSENT_ORIGINS:
        raise ValueError(f"unsupported consent_origin: {consent_origin!r}")
    if origin == "auto_save" and source != "ocr":
        raise ValueError("auto_save is only valid for OCR questions")
    topic = _optional_text(topic_id, field="topic_id", max_length=255)
    subject_value = _optional_text(subject, field="subject", max_length=120)
    classified_question_type, confidence = _classification_metadata(classification)
    screen_type = _optional_text(
        _classification_value(classification, "screen_type"),
        field="classification screen_type",
        max_length=120,
    ).lower()
    if origin == "auto_save" and (
        screen_type != "question" or confidence is None or confidence < 0.8
    ):
        raise ValueError(
            "auto_save requires question classification with confidence >= 0.80"
        )
    type_value = _optional_text(
        question_type or classified_question_type,
        field="question_type",
        max_length=120,
    )
    expiry = _optional_text(expires_at, field="expires_at", max_length=40)
    content_hash = hashlib.sha256(question_text.encode("utf-8")).hexdigest()

    with self._lock:
        conn = self._require_conn()
        conn.execute(
            """
            INSERT INTO captured_questions (
                id, source_type, question_text, content_hash, topic_id, subject,
                question_type, classification_confidence, consent_origin, status,
                created_at, last_used_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'), ?)
            ON CONFLICT(content_hash) DO UPDATE SET
                topic_id = COALESCE(NULLIF(excluded.topic_id, ''), captured_questions.topic_id),
                subject = COALESCE(NULLIF(excluded.subject, ''), captured_questions.subject),
                question_type = COALESCE(NULLIF(excluded.question_type, ''), captured_questions.question_type),
                classification_confidence = COALESCE(excluded.classification_confidence, captured_questions.classification_confidence),
                consent_origin = excluded.consent_origin,
                status = 'active',
                last_used_at = datetime('now'),
                expires_at = COALESCE(excluded.expires_at, captured_questions.expires_at)
            """,
            (
                str(uuid.uuid4()),
                source,
                question_text,
                content_hash,
                topic or None,
                subject_value or None,
                type_value or None,
                confidence,
                origin,
                expiry or None,
            ),
        )
        row = conn.execute(
            "SELECT * FROM captured_questions WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        conn.commit()
    result = _captured_question_from_row(row)
    if result is None:
        raise RuntimeError("captured question persistence failed")
    return result


def get_captured_question(self, question_id: str) -> dict[str, Any] | None:
    key = str(question_id or "").strip()
    if not key:
        return None
    row = self._require_read_conn().execute(
        "SELECT * FROM captured_questions WHERE id = ?", (key,)
    ).fetchone()
    return _captured_question_from_row(row)


def list_captured_questions(
    self,
    *,
    topic_id: str = "",
    status: str = "active",
    limit: int = 100,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(5000, int(limit)))
    topic = _optional_text(topic_id, field="topic_id", max_length=255)
    status_value = _optional_text(status, field="status", max_length=32)
    where: list[str] = []
    params: list[Any] = []
    if topic:
        where.append("topic_id = ?")
        params.append(topic)
    if status_value:
        where.append("status = ?")
        params.append(status_value)
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = self._require_read_conn().execute(
        "SELECT * FROM captured_questions"
        + clause
        + " ORDER BY last_used_at DESC, created_at DESC, id DESC LIMIT ?",
        (*params, safe_limit),
    ).fetchall()
    return [
        item
        for item in (_captured_question_from_row(row) for row in rows)
        if item is not None
    ]


def delete_captured_question(self, question_id: str) -> bool:
    """Delete a question and preserve historical QA rows by unlinking it."""
    key = str(question_id or "").strip()
    if not key:
        return False
    with self._lock:
        conn = self._require_conn()
        conn.execute(
            "UPDATE qa_records SET source_question_id = NULL WHERE source_question_id = ?",
            (key,),
        )
        cursor = conn.execute("DELETE FROM captured_questions WHERE id = ?", (key,))
        conn.commit()
    return cursor.rowcount > 0


def clear_captured_questions(
    self, *, topic_id: str = "", status: str = ""
) -> int:
    """Clear captured question assets and preserve QA history without source links."""
    topic = _optional_text(topic_id, field="topic_id", max_length=255)
    status_value = _optional_text(status, field="status", max_length=32)
    where: list[str] = []
    params: list[Any] = []
    if topic:
        where.append("topic_id = ?")
        params.append(topic)
    if status_value:
        where.append("status = ?")
        params.append(status_value)
    clause = " WHERE " + " AND ".join(where) if where else ""
    with self._lock:
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT id FROM captured_questions" + clause, params
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        conn.execute(
            "UPDATE qa_records SET source_question_id = NULL WHERE source_question_id IN ("
            + placeholders
            + ")",
            ids,
        )
        conn.execute(
            "DELETE FROM captured_questions WHERE id IN (" + placeholders + ")", ids
        )
        conn.commit()
    return len(ids)


def purge_expired_captured_questions(self) -> int:
    """Remove expired assets while retaining QA history without a source link."""
    with self._lock:
        conn = self._require_conn()
        rows = conn.execute(
            """
            SELECT id FROM captured_questions
            WHERE expires_at IS NOT NULL AND expires_at <> ''
              AND expires_at <= datetime('now')
            """
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        conn.execute(
            "UPDATE qa_records SET source_question_id = NULL WHERE source_question_id IN ("
            + placeholders
            + ")",
            ids,
        )
        conn.execute(
            "DELETE FROM captured_questions WHERE id IN (" + placeholders + ")", ids
        )
        conn.commit()
    return len(ids)
