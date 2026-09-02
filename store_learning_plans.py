from __future__ import annotations

from typing import Any


def _learning_plan_from_row(self, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "schema_version": int(row["schema_version"] or 1),
        "status": str(row["status"] or ""),
        "source_kind": str(row["source_kind"] or ""),
        "source_digest": str(row["source_digest"] or ""),
        "display_title": str(row["display_title"] or ""),
        "revision": int(row["revision"] or 1),
        "unmatched_count": int(row["unmatched_count"] or 0),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "activated_at": str(row["activated_at"] or ""),
        "completed_at": str(row["completed_at"] or ""),
        "canceled_at": str(row["canceled_at"] or ""),
    }


def _learning_plan_item_from_row(self, row: Any) -> dict[str, Any]:
    return {
        "plan_id": str(row["plan_id"]),
        "topic_id": str(row["topic_id"]),
        "topic_name": str(row["topic_name"] or ""),
        "subject": str(row["subject"] or ""),
        "ordinal": int(row["ordinal"] or 0),
        "role": str(row["role"] or ""),
        "mapping_score": float(row["mapping_score"] or 0.0),
        "mapping_confidence": str(row["mapping_confidence"] or ""),
        "reason_code": str(row["reason_code"] or ""),
        "required": bool(row["required"]),
        "created_at": str(row["created_at"] or ""),
    }


def create_learning_plan(
    self,
    *,
    plan: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    with self._lock:
        conn = self._require_conn()
        try:
            conn.execute(
                """
                INSERT INTO learning_plans (
                    id, schema_version, status, source_kind, source_digest,
                    display_title, revision, unmatched_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(plan["id"]),
                    int(plan.get("schema_version") or 1),
                    str(plan.get("status") or "draft"),
                    str(plan.get("source_kind") or ""),
                    str(plan.get("source_digest") or ""),
                    str(plan.get("display_title") or ""),
                    int(plan.get("revision") or 1),
                    max(0, int(plan.get("unmatched_count") or 0)),
                    str(plan.get("created_at") or ""),
                    str(plan.get("updated_at") or ""),
                ),
            )
            for item in items:
                conn.execute(
                    """
                    INSERT INTO learning_plan_items (
                        plan_id, topic_id, ordinal, role, mapping_score,
                        mapping_confidence, reason_code, required, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(plan["id"]),
                        str(item["topic_id"]),
                        int(item.get("ordinal") or 0),
                        str(item.get("role") or "core"),
                        float(item.get("mapping_score") or 0.0),
                        str(item.get("mapping_confidence") or "medium"),
                        str(item.get("reason_code") or "material_match"),
                        1 if item.get("required", True) else 0,
                        str(item.get("created_at") or plan.get("created_at") or ""),
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    result = self.get_learning_plan(str(plan["id"]))
    assert result is not None
    return result


def get_learning_plan(self, plan_id: str) -> dict[str, Any] | None:
    plan_key = str(plan_id or "").strip()
    if not plan_key:
        return None
    conn = self._require_read_conn()
    row = conn.execute(
        "SELECT * FROM learning_plans WHERE id = ?", (plan_key,)
    ).fetchone()
    plan = _learning_plan_from_row(self, row)
    if plan is None:
        return None
    item_rows = conn.execute(
        """
        SELECT lpi.*, t.name AS topic_name, t.subject AS subject
        FROM learning_plan_items lpi
        JOIN topics t ON t.id = lpi.topic_id
        WHERE lpi.plan_id = ?
        ORDER BY lpi.ordinal, lpi.topic_id
        """,
        (plan_key,),
    ).fetchall()
    plan["items"] = [_learning_plan_item_from_row(self, item) for item in item_rows]
    return plan


def get_active_learning_plan(self) -> dict[str, Any] | None:
    row = self._require_read_conn().execute(
        """
        SELECT id
        FROM learning_plans
        WHERE status = 'active'
        ORDER BY activated_at DESC, updated_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return self.get_learning_plan(str(row["id"])) if row is not None else None


def list_learning_plans(
    self, *, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    status_key = str(status or "").strip().lower()
    safe_limit = max(1, min(5000, int(limit)))
    if status_key:
        rows = self._require_read_conn().execute(
            """
            SELECT id FROM learning_plans
            WHERE status = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (status_key, safe_limit),
        ).fetchall()
    else:
        rows = self._require_read_conn().execute(
            """
            SELECT id FROM learning_plans
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [
        plan
        for row in rows
        if (plan := self.get_learning_plan(str(row["id"]))) is not None
    ]


def activate_learning_plan(
    self,
    *,
    plan_id: str,
    expected_revision: int,
    accepted_topic_ids: list[str],
) -> dict[str, Any]:
    plan_key = str(plan_id or "").strip()
    accepted = list(dict.fromkeys(str(item or "").strip() for item in accepted_topic_ids))
    accepted = [item for item in accepted if item]
    with self._lock:
        conn = self._require_conn()
        try:
            row = conn.execute(
                "SELECT status, revision FROM learning_plans WHERE id = ?", (plan_key,)
            ).fetchone()
            if row is None:
                raise KeyError("LEARNING_PLAN_NOT_FOUND")
            current_revision = int(row["revision"] or 1)
            current_status = str(row["status"] or "")
            stored_ids = {
                str(item["topic_id"])
                for item in conn.execute(
                    "SELECT topic_id FROM learning_plan_items WHERE plan_id = ?",
                    (plan_key,),
                ).fetchall()
            }
            if current_status == "active" and set(accepted) == stored_ids and int(expected_revision) in {
                current_revision,
                max(1, current_revision - 1),
            }:
                conn.rollback()
                result = self.get_learning_plan(plan_key)
                assert result is not None
                return result
            if current_status not in {"draft", "paused"}:
                raise ValueError("LEARNING_PLAN_NOT_DRAFT")
            if current_revision != int(expected_revision):
                raise ValueError("LEARNING_PLAN_CHANGED")
            conflict = conn.execute(
                "SELECT id FROM learning_plans WHERE status = 'active' AND id <> ? LIMIT 1",
                (plan_key,),
            ).fetchone()
            if conflict is not None:
                raise ValueError("ACTIVE_LEARNING_PLAN_EXISTS")
            if current_status == "paused" and set(accepted) != stored_ids:
                raise ValueError("LEARNING_PLAN_CHANGED")
            if current_status == "draft":
                conn.execute(
                    "DELETE FROM learning_plan_items WHERE plan_id = ? AND topic_id NOT IN (SELECT value FROM json_each(?))",
                    (plan_key, self._json_dumps(accepted)),
                )
            cursor = conn.execute(
                """
                UPDATE learning_plans
                SET status = 'active', revision = revision + 1,
                    activated_at = datetime('now'), updated_at = datetime('now'),
                    completed_at = NULL, canceled_at = NULL
                WHERE id = ? AND status = ? AND revision = ?
                """,
                (plan_key, current_status, int(expected_revision)),
            )
            if int(cursor.rowcount or 0) != 1:
                raise ValueError("LEARNING_PLAN_CHANGED")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    result = self.get_learning_plan(plan_key)
    assert result is not None
    return result


def update_learning_plan_status(
    self,
    *,
    plan_id: str,
    status: str,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    plan_key = str(plan_id or "").strip()
    status_key = str(status or "").strip().lower()
    timestamp_columns = {
        "active": "activated_at",
        "completed": "completed_at",
        "canceled": "canceled_at",
    }
    with self._lock:
        conn = self._require_conn()
        try:
            row = conn.execute(
                "SELECT status, revision FROM learning_plans WHERE id = ?", (plan_key,)
            ).fetchone()
            if row is None:
                raise KeyError("LEARNING_PLAN_NOT_FOUND")
            current_revision = int(row["revision"] or 1)
            if expected_revision is not None and current_revision != int(expected_revision):
                raise ValueError("LEARNING_PLAN_CHANGED")
            if str(row["status"] or "") == status_key:
                conn.rollback()
                result = self.get_learning_plan(plan_key)
                assert result is not None
                return result
            if status_key == "active":
                conflict = conn.execute(
                    "SELECT id FROM learning_plans WHERE status = 'active' AND id <> ? LIMIT 1",
                    (plan_key,),
                ).fetchone()
                if conflict is not None:
                    raise ValueError("ACTIVE_LEARNING_PLAN_EXISTS")
            timestamp_column = timestamp_columns.get(status_key)
            timestamp_sql = f", {timestamp_column} = datetime('now')" if timestamp_column else ""
            conn.execute(
                f"UPDATE learning_plans SET status = ?, revision = revision + 1, updated_at = datetime('now'){timestamp_sql} WHERE id = ?",
                (status_key, plan_key),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    result = self.get_learning_plan(plan_key)
    assert result is not None
    return result
