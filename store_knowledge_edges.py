"""Rebuildable SQLite read projection for canonical knowledge-graph edges.

``topics`` remains the source of truth.  This module persists the result of
``build_topic_edges`` under a content-addressed revision so graph consumers can
read a stable projection without re-parsing every topic on each request.
"""

from __future__ import annotations

import base64
import hashlib

from .knowledge_graph_edges import build_topic_edges
from .store_common import Any, json

_PROJECTION_STATE_KEY = "active"
_PROJECTION_FORMAT = "knowledge-edges-v1"
_MAX_PAGE_SIZE = 200
_DEFAULT_PAGE_SIZE = 100
_MAX_BOUNDARY_NODES = 200
_MAX_PAGE_EDGES = 1000


def _json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(str(value or ""), ensure_ascii=False)


def _catalog_revision(topics: list[dict[str, Any]]) -> str:
    """Hash every topic property that can change a persisted edge payload."""
    canonical = [
        {
            "id": str(topic.get("id") or "").strip(),
            "name": str(topic.get("name") or "").strip(),
            "subject": str(topic.get("subject") or "").strip(),
            "stage": str(topic.get("stage") or "").strip(),
            "chapter": str(topic.get("chapter") or "").strip(),
            "unit": str(topic.get("unit") or "").strip(),
            "course_family": str(topic.get("course_family") or "").strip(),
            "prerequisites": topic.get("prerequisites") or [],
            "related": topic.get("related") or [],
        }
        for topic in topics
        if str(topic.get("id") or "").strip()
    ]
    canonical.sort(key=lambda topic: topic["id"])
    digest = hashlib.sha256(
        _json({"format": _PROJECTION_FORMAT, "topics": canonical}).encode("utf-8")
    ).hexdigest()
    return f"{_PROJECTION_FORMAT}:{digest}"


def _state_from_row(row: Any) -> dict[str, Any]:
    if row is None:
        return {
            "catalog_revision": "",
            "edge_count": 0,
            "built_at": "",
            "available": False,
        }
    return {
        "catalog_revision": str(row["active_revision"] or ""),
        "edge_count": int(row["edge_count"] or 0),
        "built_at": str(row["built_at"] or ""),
        "available": bool(str(row["active_revision"] or "")),
    }


def _scope_filters(
    *,
    stage: str = "",
    subject: str = "",
    course_family: str = "",
    chapter: str = "",
    unit: str = "",
) -> dict[str, str]:
    def machine_key(value: str) -> str:
        return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")

    return {
        "stage": machine_key(stage),
        "subject": machine_key(subject),
        "course_family": machine_key(course_family),
        "chapter": str(chapter or "").strip(),
        "unit": str(unit or "").strip(),
    }


def _cursor_encode(payload: dict[str, Any]) -> str:
    raw = _json(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cursor_decode(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        padded = text + "=" * (-len(text) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("invalid_knowledge_map_cursor") from None
    if not isinstance(payload, dict):
        raise ValueError("invalid_knowledge_map_cursor")
    return payload


def _scope_sql(filters: dict[str, str]) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for field in ("stage", "subject", "course_family"):
        value = filters[field]
        if value:
            clauses.append(
                f"lower(replace(replace(COALESCE({field}, ''), '-', '_'), ' ', '_')) = ?"
            )
            params.append(value.lower().replace("-", "_").replace(" ", "_"))
    for field in ("chapter", "unit"):
        if filters[field]:
            clauses.append(f"COALESCE({field}, '') = ?")
            params.append(filters[field])
    # This mirrors store_topics._active_seed_membership_clause without loading
    # a full catalog into Python.
    clauses.append(
        """(source != 'seed'
        OR NOT EXISTS (SELECT 1 FROM knowledge_seed_membership retired WHERE retired.topic_id = topics.id)
        OR EXISTS (SELECT 1 FROM knowledge_seed_membership active WHERE active.topic_id = topics.id AND active.active = 1))"""
    )
    return clauses, params


def _cursor_key(row: Any) -> list[Any]:
    return [
        str(row["stage_key"] or ""),
        str(row["subject_key"] or ""),
        str(row["course_family_key"] or ""),
        str(row["chapter_key"] or ""),
        str(row["unit_key"] or ""),
        int(row["depth_key"] or 0),
        str(row["id"] or ""),
    ]


def get_knowledge_edge_revision(self) -> dict[str, Any]:
    row = self._require_read_conn().execute(
        """SELECT active_revision, edge_count, dirty, built_at
        FROM knowledge_edge_projection_state
        WHERE projection_key = ?""",
        (_PROJECTION_STATE_KEY,),
    ).fetchone()
    return _state_from_row(row)


def mark_knowledge_edge_projection_dirty(self, *, conn: Any | None = None) -> None:
    """Mark the read projection stale inside the caller's topic transaction."""
    target = conn if conn is not None else self._require_conn()
    target.execute(
        """INSERT INTO knowledge_edge_projection_state (
                projection_key, active_revision, edge_count, dirty, built_at
            ) VALUES (?, '', 0, 1, datetime('now'))
            ON CONFLICT(projection_key) DO UPDATE SET dirty = 1""",
        (_PROJECTION_STATE_KEY,),
    )


def _rebuild_dirty_knowledge_edge_projection(self) -> dict[str, Any]:
    """Synchronize a dirty projection before a V2 map read.

    Readers must not combine current topic rows with a prior edge revision.
    Preserve that revision on a failed rebuild, but fail the read explicitly so
    callers never mistake the partial view for a complete graph.
    """
    with self._lock:
        state = self._require_conn().execute(
            """SELECT active_revision, edge_count, dirty, built_at
            FROM knowledge_edge_projection_state WHERE projection_key = ?""",
            (_PROJECTION_STATE_KEY,),
        ).fetchone()
        if state is None or not bool(int(state["dirty"] or 0)):
            return _state_from_row(state)
    try:
        return self.rebuild_knowledge_edge_projection()
    except Exception as exc:
        raise ValueError("knowledge_edge_projection_rebuild_failed") from exc


def _edges_from_rows(rows: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            use_cases = json.loads(str(row["use_cases_json"] or "[]"))
        except (TypeError, ValueError):
            use_cases = []
        try:
            required_mastery = json.loads(str(row["required_mastery_json"] or "null"))
        except (TypeError, ValueError):
            required_mastery = None
        edge = {
            "from": str(row["source_topic_id"] or ""),
            "to": str(row["target_topic_id"] or ""),
            "relation": str(row["relation_type"] or ""),
            "priority": str(row["priority"] or ""),
            "confidence": float(row["confidence"] or 0.0),
            "context": str(row["context"] or ""),
            "catalog_revision": str(row["catalog_revision"] or ""),
        }
        if str(row["reason"] or ""):
            edge["reason"] = str(row["reason"])
        if isinstance(use_cases, list) and use_cases:
            edge["use_cases"] = use_cases
        if required_mastery is not None:
            edge["required_mastery"] = required_mastery
        result.append(edge)
    return result


def list_knowledge_edges(
    self,
    *,
    topic_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    relation_types: set[str] | list[str] | tuple[str, ...] | None = None,
    catalog_revision: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    revision = str(catalog_revision or "").strip()
    if not revision:
        revision = str(self.get_knowledge_edge_revision()["catalog_revision"] or "")
    if not revision:
        return []
    clauses = ["catalog_revision = ?"]
    params: list[Any] = [revision]
    normalized_topic_ids = sorted({str(value or "").strip() for value in topic_ids or [] if str(value or "").strip()})
    if normalized_topic_ids:
        placeholders = ", ".join("?" for _ in normalized_topic_ids)
        clauses.append(
            f"(source_topic_id IN ({placeholders}) OR target_topic_id IN ({placeholders}))"
        )
        params.extend(normalized_topic_ids)
        params.extend(normalized_topic_ids)
    normalized_relations = sorted({str(value or "").strip() for value in relation_types or [] if str(value or "").strip()})
    if normalized_relations:
        placeholders = ", ".join("?" for _ in normalized_relations)
        clauses.append(f"relation_type IN ({placeholders})")
        params.extend(normalized_relations)
    limit_sql = ""
    if limit is not None:
        params.append(max(1, int(limit)))
        limit_sql = " LIMIT ?"
    rows = self._require_read_conn().execute(
        """SELECT source_topic_id, target_topic_id, relation_type, priority,
                  confidence, context, reason, use_cases_json,
                  required_mastery_json, catalog_revision
           FROM knowledge_edges
           WHERE """
        + " AND ".join(clauses)
        + " ORDER BY source_topic_id, target_topic_id, relation_type"
        + limit_sql,
        tuple(params),
    ).fetchall()
    return _edges_from_rows(rows)


def query_knowledge_map_page(
    self,
    *,
    stage: str = "",
    subject: str = "",
    course_family: str = "",
    chapter: str = "",
    unit: str = "",
    page_size: int = _DEFAULT_PAGE_SIZE,
    cursor: str = "",
    include_boundary: bool = True,
    boundary_limit: int = _MAX_BOUNDARY_NODES,
) -> dict[str, Any]:
    """Read one filtered map page from the persisted edge projection.

    Topics are keyset-paginated in SQL.  Only the page's incident edges and
    one-hop boundary topics are queried; no full catalog graph is rebuilt in
    this read path.
    """
    # This must run before decoding the cursor: a successful rebuild changes
    # the revision, making every cursor from the prior projection stale.
    revision_state = _rebuild_dirty_knowledge_edge_projection(self)
    revision = str(revision_state["catalog_revision"] or "")
    filters = _scope_filters(
        stage=stage,
        subject=subject,
        course_family=course_family,
        chapter=chapter,
        unit=unit,
    )
    safe_page_size = max(1, min(_MAX_PAGE_SIZE, int(page_size or _DEFAULT_PAGE_SIZE)))
    safe_boundary_limit = max(0, min(_MAX_BOUNDARY_NODES, int(boundary_limit)))
    cursor_payload = _cursor_decode(cursor)
    if cursor_payload:
        if (
            cursor_payload.get("version") != 1
            or cursor_payload.get("revision") != revision
            or cursor_payload.get("filters") != filters
            or not isinstance(cursor_payload.get("key"), list)
            or len(cursor_payload["key"]) != 7
        ):
            raise ValueError("knowledge_map_cursor_stale")
    clauses, params = _scope_sql(filters)
    where_sql = " WHERE " + " AND ".join(clauses)
    total = int(
        self._require_read_conn()
        .execute("SELECT COUNT(*) AS count FROM topics" + where_sql, tuple(params))
        .fetchone()["count"]
    )
    key_select = """COALESCE(stage, '') AS stage_key,
        COALESCE(subject, '') AS subject_key,
        COALESCE(course_family, '') AS course_family_key,
        COALESCE(chapter, '') AS chapter_key,
        COALESCE(unit, '') AS unit_key,
        COALESCE(depth, 0) AS depth_key"""
    page_clauses = list(clauses)
    page_params = list(params)
    if cursor_payload:
        page_clauses.append(
            """(COALESCE(stage, ''), COALESCE(subject, ''), COALESCE(course_family, ''),
                COALESCE(chapter, ''), COALESCE(unit, ''), COALESCE(depth, 0), id)
                > (?, ?, ?, ?, ?, ?, ?)"""
        )
        page_params.extend(cursor_payload["key"])
    page_params.append(safe_page_size + 1)
    rows = self._require_read_conn().execute(
        "SELECT topics.*, "
        + key_select
        + " FROM topics WHERE "
        + " AND ".join(page_clauses)
        + " ORDER BY COALESCE(stage, ''), COALESCE(subject, ''), COALESCE(course_family, ''), "
        + "COALESCE(chapter, ''), COALESCE(unit, ''), COALESCE(depth, 0), id LIMIT ?",
        tuple(page_params),
    ).fetchall()
    has_more = len(rows) > safe_page_size
    page_rows = rows[:safe_page_size]
    page_nodes = [node for node in (self._topic_from_row(row) for row in page_rows) if node]
    page_ids = [str(node["id"] or "") for node in page_nodes]
    next_cursor = ""
    if has_more and page_rows:
        next_cursor = _cursor_encode(
            {
                "version": 1,
                "revision": revision,
                "filters": filters,
                "key": _cursor_key(page_rows[-1]),
            }
        )
    result: dict[str, Any] = {
        "schema_version": 2,
        "catalog_revision": revision,
        "scope": filters,
        "scope_total_count": total,
        "scope_returned_count": len(page_nodes),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "nodes": page_nodes,
        "edges": [],
        "boundary": {
            "nodes": [],
            "returned_count": 0,
            "total_count": 0,
            "truncated": False,
        },
        "edge_truncated": False,
        "omitted_edge_count": 0,
    }
    if not revision or not page_ids:
        return result

    page_placeholders = ", ".join("?" for _ in page_ids)
    incident_sql = (
        "catalog_revision = ? AND (source_topic_id IN ("
        + page_placeholders
        + ") OR target_topic_id IN ("
        + page_placeholders
        + "))"
    )
    incident_params: list[Any] = [revision, *page_ids, *page_ids]
    boundary_ids: list[str] = []
    boundary_total = 0
    if include_boundary:
        boundary_rows = self._require_read_conn().execute(
            """SELECT DISTINCT CASE
                    WHEN source_topic_id IN ("""
            + page_placeholders
            + ") THEN target_topic_id ELSE source_topic_id END AS topic_id "
            "FROM knowledge_edges WHERE "
            + incident_sql
            + " AND CASE WHEN source_topic_id IN ("
            + page_placeholders
            + ") THEN target_topic_id ELSE source_topic_id END NOT IN ("
            + page_placeholders
            + ") ORDER BY topic_id LIMIT ?",
            tuple([*page_ids, *incident_params, *page_ids, *page_ids, safe_boundary_limit + 1]),
        ).fetchall()
        boundary_ids = [str(row["topic_id"] or "") for row in boundary_rows[:safe_boundary_limit]]
        boundary_total = int(
            self._require_read_conn().execute(
                "SELECT COUNT(*) AS count FROM (SELECT DISTINCT CASE WHEN source_topic_id IN ("
                + page_placeholders
                + ") THEN target_topic_id ELSE source_topic_id END AS topic_id FROM knowledge_edges WHERE "
                + incident_sql
                + " AND CASE WHEN source_topic_id IN ("
                + page_placeholders
                + ") THEN target_topic_id ELSE source_topic_id END NOT IN ("
                + page_placeholders
                + "))",
                tuple([*page_ids, *incident_params, *page_ids, *page_ids]),
            ).fetchone()["count"]
        )
        if boundary_ids:
            placeholders = ", ".join("?" for _ in boundary_ids)
            boundary_rows = self._require_read_conn().execute(
                "SELECT * FROM topics WHERE id IN ("
                + placeholders
                + ") ORDER BY stage, subject, course_family, chapter, unit, depth, id",
                tuple(boundary_ids),
            ).fetchall()
            result["boundary"]["nodes"] = [
                node for node in (self._topic_from_row(row) for row in boundary_rows) if node
            ]
        result["boundary"].update(
            {
                "returned_count": len(result["boundary"]["nodes"]),
                "total_count": boundary_total,
                "truncated": boundary_total > len(boundary_ids),
            }
        )

    visible_ids = [*page_ids, *boundary_ids]
    visible_placeholders = ", ".join("?" for _ in visible_ids)
    edge_rows = self._require_read_conn().execute(
        "SELECT source_topic_id, target_topic_id, relation_type, priority, confidence, context, reason, "
        "use_cases_json, required_mastery_json, catalog_revision FROM knowledge_edges WHERE "
        + incident_sql
        + " AND source_topic_id IN ("
        + visible_placeholders
        + ") AND target_topic_id IN ("
        + visible_placeholders
        + ") ORDER BY source_topic_id, target_topic_id, relation_type LIMIT ?",
        tuple([*incident_params, *visible_ids, *visible_ids, _MAX_PAGE_EDGES + 1]),
    ).fetchall()
    edge_total = int(
        self._require_read_conn().execute(
            "SELECT COUNT(*) AS count FROM knowledge_edges WHERE "
            + incident_sql
            + " AND source_topic_id IN ("
            + visible_placeholders
            + ") AND target_topic_id IN ("
            + visible_placeholders
            + ")",
            tuple([*incident_params, *visible_ids, *visible_ids]),
        ).fetchone()["count"]
    )
    result["edge_truncated"] = len(edge_rows) > _MAX_PAGE_EDGES
    result["edges"] = _edges_from_rows(edge_rows[:_MAX_PAGE_EDGES])
    if result["edge_truncated"]:
        result["omitted_edge_count"] = edge_total - _MAX_PAGE_EDGES
    return result


def rebuild_knowledge_edge_projection(
    self, *, force: bool = False
) -> dict[str, Any]:
    """Build a complete new edge revision, then atomically make it active.

    An exception rolls back the new revision and leaves the previously active
    revision untouched.  Stale revisions are retained intentionally: they are
    a safe fallback until a later maintenance policy explicitly prunes them.
    """
    with self._lock:
        conn = self._require_conn()
        # Hold the write lock while taking the topic snapshot and constructing
        # its edge set, so a concurrent in-process topic write cannot be
        # acknowledged between this snapshot and the active-revision switch.
        topics = self.list_topics(limit=None)
        revision = _catalog_revision(topics)
        edges = build_topic_edges(topics)
        active = conn.execute(
            """SELECT active_revision, edge_count, dirty, built_at
            FROM knowledge_edge_projection_state
            WHERE projection_key = ?""",
            (_PROJECTION_STATE_KEY,),
        ).fetchone()
        if (
            active is not None
            and str(active["active_revision"] or "") == revision
            and not bool(int(active["dirty"] or 0))
            and not force
        ):
            persisted_count = conn.execute(
                "SELECT COUNT(*) AS count FROM knowledge_edges WHERE catalog_revision = ?",
                (revision,),
            ).fetchone()
            if int(persisted_count["count"] or 0) == int(active["edge_count"] or 0):
                return _state_from_row(active)

        nested_transaction = bool(conn.in_transaction)
        savepoint_started = False
        try:
            if nested_transaction:
                conn.execute("SAVEPOINT knowledge_edge_projection_rebuild")
                savepoint_started = True
            else:
                conn.execute("BEGIN IMMEDIATE")
            # A partial copy of this revision can only exist after an older
            # interrupted build; remove it inside this transaction before the
            # complete replacement is written.
            conn.execute(
                "DELETE FROM knowledge_edges WHERE catalog_revision = ?", (revision,)
            )
            conn.executemany(
                """INSERT INTO knowledge_edges (
                    source_topic_id, target_topic_id, relation_type, priority,
                    confidence, context, reason, use_cases_json,
                    required_mastery_json, catalog_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        str(edge.get("from") or "").strip(),
                        str(edge.get("to") or "").strip(),
                        str(edge.get("relation") or "").strip(),
                        str(edge.get("priority") or ""),
                        float(edge.get("confidence") or 0.0),
                        str(edge.get("context") or ""),
                        str(edge.get("reason") or ""),
                        _json(edge.get("use_cases") or []),
                        _json(edge.get("required_mastery")) if edge.get("required_mastery") is not None else "null",
                        revision,
                    )
                    for edge in edges
                    if str(edge.get("from") or "").strip()
                    and str(edge.get("to") or "").strip()
                    and str(edge.get("relation") or "").strip()
                ],
            )
            edge_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM knowledge_edges WHERE catalog_revision = ?",
                    (revision,),
                ).fetchone()["count"]
            )
            conn.execute(
                """INSERT INTO knowledge_edge_projection_state (
                    projection_key, active_revision, edge_count, dirty, built_at
                ) VALUES (?, ?, ?, 0, datetime('now'))
                ON CONFLICT(projection_key) DO UPDATE SET
                    active_revision = excluded.active_revision,
                    edge_count = excluded.edge_count,
                    dirty = 0,
                    built_at = excluded.built_at""",
                (_PROJECTION_STATE_KEY, revision, edge_count),
            )
            if savepoint_started:
                conn.execute("RELEASE SAVEPOINT knowledge_edge_projection_rebuild")
            else:
                conn.commit()
        except Exception:
            if savepoint_started:
                conn.execute("ROLLBACK TO SAVEPOINT knowledge_edge_projection_rebuild")
                conn.execute("RELEASE SAVEPOINT knowledge_edge_projection_rebuild")
            elif not nested_transaction and conn.in_transaction:
                conn.rollback()
            raise
    return self.get_knowledge_edge_revision()
