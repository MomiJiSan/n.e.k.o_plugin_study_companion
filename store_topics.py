from __future__ import annotations

import hashlib
import hmac

from .knowledge_graph_edges import build_topic_edges
from .store_common import (
    Any,
    Path,
    json,
    safe_float,
    safe_int,
)

_KNOWLEDGE_SEED_PROTOCOL = 1


def _seed_stage(payload: dict[str, Any], fallback: str = "") -> str:
    return str(
        payload.get("stage")
        or payload.get("grade_level")
        or payload.get("education_level")
        or payload.get("course_level")
        or fallback
    ).strip()


def _normalize_seed_topic(item: object, defaults: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("invalid_topic")
    topic_id = str(item.get("id") or "").strip()
    name = str(item.get("name") or "").strip()
    subject = str(item.get("subject") or defaults["subject"]).strip()
    chapter = str(item.get("chapter") or item.get("unit") or "general").strip()
    unit = str(item.get("unit") or item.get("section") or item.get("module") or chapter).strip()
    stage = _seed_stage(item, str(defaults["stage"]))
    if not all((topic_id, name, subject, chapter, unit, stage)):
        raise ValueError("invalid_topic")
    return {
        "id": topic_id,
        "name": name,
        "subject": subject,
        "chapter": chapter,
        "stage": stage,
        "unit": unit,
        "depth": safe_int(item.get("depth"), 1),
        "difficulty": safe_float(item.get("difficulty"), 0.5),
        "prerequisites": item.get("prerequisites") if isinstance(item.get("prerequisites"), list) else [],
        "related": item.get("related") if isinstance(item.get("related"), list) else [],
        "typical_misconceptions": item.get("typical_misconceptions") if isinstance(item.get("typical_misconceptions"), list) else [],
        "skills": item.get("skills") if isinstance(item.get("skills"), list) else [],
        "question_types": item.get("question_types") if isinstance(item.get("question_types"), list) else [],
        "examples": item.get("examples") if isinstance(item.get("examples"), list) else item.get("typical_examples") if isinstance(item.get("typical_examples"), list) else [],
        "course_family": str(item.get("course_family") or "").strip(),
        "curriculum_version": item.get("curriculum_version") if isinstance(item.get("curriculum_version"), (str, list)) else [],
        "exam_region": item.get("exam_region") if isinstance(item.get("exam_region"), (str, list)) else [],
        "exam_type": item.get("exam_type") if isinstance(item.get("exam_type"), (str, list)) else [],
        "aliases": item.get("aliases") if isinstance(item.get("aliases"), list) else [],
        "source": "seed",
    }


def _read_seed_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid_manifest")
    return payload


def _read_knowledge_seed_bundle(
    seed_path: Path,
) -> tuple[str, int, str, str, list[dict[str, Any]], int | None]:
    manifest = _read_seed_payload(seed_path)
    protocol = manifest.get(
        "seed_protocol_version",
        manifest.get("seed_protocol", manifest.get("protocol", _KNOWLEDGE_SEED_PROTOCOL)),
    )
    if (
        isinstance(protocol, bool)
        or not isinstance(protocol, int)
        or protocol != _KNOWLEDGE_SEED_PROTOCOL
    ):
        raise ValueError("unsupported_protocol")
    raw_revision = manifest.get(
        "content_revision", manifest.get("revision", manifest.get("version", "legacy"))
    )
    numeric_revision = raw_revision if isinstance(raw_revision, int) and not isinstance(raw_revision, bool) else None
    if numeric_revision is not None and numeric_revision < 0:
        raise ValueError("invalid_revision")
    revision = str("legacy" if raw_revision is None else raw_revision).strip()
    if not revision or len(revision) > 128:
        raise ValueError("invalid_revision")
    seed_key = str(manifest.get("seed_id") or "knowledge_seed").strip()
    if not seed_key or len(seed_key) > 128:
        raise ValueError("invalid_seed_key")
    root = seed_path.parent.resolve()
    source_payloads: list[dict[str, Any]] = []
    files = manifest.get("files")
    if files is None:
        source_payloads.append(manifest)
    elif isinstance(files, list):
        for entry in files:
            child_name = str((entry.get("path") or entry.get("file")) if isinstance(entry, dict) else entry or "").strip()
            if not child_name:
                raise ValueError("invalid_manifest")
            child = (seed_path.parent / child_name).resolve()
            try:
                child.relative_to(root)
            except ValueError as exc:
                raise ValueError("invalid_manifest") from exc
            child_payload = _read_seed_payload(child)
            if not isinstance(child_payload.get("topics"), list):
                raise ValueError("invalid_topics")
            if isinstance(entry, dict) and isinstance(entry.get("topic_count"), int) and entry["topic_count"] != len(child_payload["topics"]):
                raise ValueError("invalid_manifest")
            source_payloads.append(child_payload)
    else:
        raise ValueError("invalid_manifest")
    topics: list[dict[str, Any]] = []
    for payload in source_payloads:
        raw_topics = payload.get("topics")
        if not isinstance(raw_topics, list):
            raise ValueError("invalid_topics")
        defaults = {"subject": str(payload.get("subject") or manifest.get("subject") or "math").strip(), "stage": _seed_stage(payload, _seed_stage(manifest))}
        topics.extend(_normalize_seed_topic(item, defaults) for item in raw_topics)
    if len({topic["id"] for topic in topics}) != len(topics):
        raise ValueError("duplicate_topic_id")
    expected_total = manifest.get("total_topics")
    if isinstance(expected_total, int) and expected_total != len(topics):
        raise ValueError("invalid_manifest")
    topics.sort(key=lambda item: item["id"])
    canonical = json.dumps({"protocol": protocol, "revision": revision, "topics": topics}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    declared_hash = manifest.get("manifest_sha256")
    if declared_hash is not None and (
        not isinstance(declared_hash, str)
        or not hmac.compare_digest(declared_hash.lower(), content_hash)
    ):
        raise ValueError("manifest_hash_mismatch")
    return seed_key, protocol, revision, content_hash, topics, numeric_revision


def _seed_topic_hash(topic: dict[str, Any]) -> str:
    canonical = json.dumps(topic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_knowledge_seed(self, path: Path | str | None = None, _visited: set[str] | None = None) -> int:
    del _visited  # Legacy caller compatibility: lifecycle loads one full manifest atomically.
    seed_path = Path(path) if path is not None else self.knowledge_seed_json_path
    if seed_path is None or not seed_path.is_file():
        return 0
    try:
        seed_key, protocol, revision, content_hash, topics, numeric_revision = _read_knowledge_seed_bundle(seed_path)
    except (OSError, ValueError, TypeError, UnicodeError):
        self._log_warning("study knowledge seed rejected: invalid or unsupported manifest")
        return 0
    with self._lock:
        conn = self._require_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT protocol, revision, content_hash FROM knowledge_seed_state WHERE seed_key = ?", (seed_key,)).fetchone()
            if existing is not None:
                existing_protocol = int(existing["protocol"])
                existing_revision = str(existing["revision"])
                existing_hash = str(existing["content_hash"])
                if existing_protocol == protocol and existing_revision == revision:
                    if not hmac.compare_digest(existing_hash, content_hash):
                        raise ValueError("revision_hash_conflict")
                    conn.execute("COMMIT")
                    return len(topics)
                if (
                    existing_protocol == protocol
                    and numeric_revision is not None
                    and existing_revision.isdecimal()
                    and numeric_revision < int(existing_revision)
                ):
                    raise ValueError("revision_downgrade")
            for topic in topics:
                _upsert_topic_no_commit(self, topic)
            conn.execute(
                """UPDATE knowledge_seed_membership
                SET active = 0, retired_at = COALESCE(retired_at, datetime('now')), updated_at = datetime('now')
                WHERE seed_key = ? AND active = 1""",
                (seed_key,),
            )
            for topic in topics:
                conn.execute(
                    """INSERT INTO knowledge_seed_membership (seed_key, topic_id, protocol, revision, content_hash, active, retired_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, NULL, datetime('now'))
                    ON CONFLICT(seed_key, topic_id) DO UPDATE SET protocol = excluded.protocol, revision = excluded.revision, content_hash = excluded.content_hash, active = 1, retired_at = NULL, updated_at = datetime('now')""",
                    (seed_key, topic["id"], protocol, revision, _seed_topic_hash(topic)),
                )
            edge_count = len(build_topic_edges(topics))
            conn.execute(
                """INSERT INTO knowledge_seed_state (seed_key, protocol, revision, content_hash, topic_count, edge_count, applied_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(seed_key) DO UPDATE SET protocol = excluded.protocol, revision = excluded.revision, content_hash = excluded.content_hash, topic_count = excluded.topic_count, edge_count = excluded.edge_count, applied_at = datetime('now'), updated_at = datetime('now')""",
                (seed_key, protocol, revision, content_hash, len(topics), edge_count),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            self._log_warning("study knowledge seed transaction failed")
            return 0
    return len(topics)


def _upsert_topic_no_commit(self, topic: dict[str, Any]) -> None:
    """Shared SQL upsert used by explicit commits and seed transactions."""
    topic_id = str(topic.get("id") or "").strip()
    name = str(topic.get("name") or topic_id).strip()
    if not topic_id or not name:
        return
    with self._lock:
        self._require_conn().execute(
            """
            INSERT INTO topics (
                id, name, subject, chapter, stage, unit, depth, difficulty,
                prerequisites, related, typical_misconceptions, skills,
                question_types, examples, course_family, curriculum_version,
                exam_region, exam_type, aliases, source, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                name = CASE WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.name ELSE excluded.name END,
                subject = CASE WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.subject ELSE excluded.subject END,
                chapter = CASE WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.chapter ELSE excluded.chapter END,
                stage = CASE
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' AND topics.stage = '' AND excluded.stage != '' THEN excluded.stage
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.stage
                    ELSE excluded.stage
                END,
                unit = CASE
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' AND (topics.unit = '' OR topics.unit = topics.chapter) AND excluded.unit != '' THEN excluded.unit
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.unit
                    ELSE excluded.unit
                END,
                depth = CASE WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.depth ELSE excluded.depth END,
                difficulty = CASE WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.difficulty ELSE excluded.difficulty END,
                prerequisites = CASE WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.prerequisites ELSE excluded.prerequisites END,
                related = CASE WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.related ELSE excluded.related END,
                typical_misconceptions = CASE WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.typical_misconceptions ELSE excluded.typical_misconceptions END,
                skills = CASE
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' AND topics.skills = '[]' AND excluded.skills != '[]' THEN excluded.skills
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.skills
                    ELSE excluded.skills
                END,
                question_types = CASE
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' AND topics.question_types = '[]' AND excluded.question_types != '[]' THEN excluded.question_types
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.question_types
                    ELSE excluded.question_types
                END,
                examples = CASE
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' AND topics.examples = '[]' AND excluded.examples != '[]' THEN excluded.examples
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.examples
                    ELSE excluded.examples
                END,
                course_family = CASE
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' AND topics.course_family = '' AND excluded.course_family != '' THEN excluded.course_family
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.course_family
                    ELSE excluded.course_family
                END,
                curriculum_version = CASE
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' AND topics.curriculum_version = '[]' AND excluded.curriculum_version != '[]' THEN excluded.curriculum_version
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.curriculum_version
                    ELSE excluded.curriculum_version
                END,
                exam_region = CASE
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' AND topics.exam_region = '[]' AND excluded.exam_region != '[]' THEN excluded.exam_region
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.exam_region
                    ELSE excluded.exam_region
                END,
                exam_type = CASE
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' AND topics.exam_type = '[]' AND excluded.exam_type != '[]' THEN excluded.exam_type
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.exam_type
                    ELSE excluded.exam_type
                END,
                aliases = CASE
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' AND topics.aliases = '[]' AND excluded.aliases != '[]' THEN excluded.aliases
                    WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.aliases
                    ELSE excluded.aliases
                END,
                source = CASE WHEN topics.source = 'seed' AND excluded.source != 'seed' THEN topics.source ELSE excluded.source END,
                updated_at = datetime('now')
            """,
            (
                topic_id,
                name,
                str(topic.get("subject") or "math"),
                str(topic.get("chapter") or ""),
                str(
                    topic.get("stage")
                    or topic.get("grade_level")
                    or topic.get("education_level")
                    or topic.get("course_level")
                    or ""
                ).strip(),
                str(topic.get("unit") or topic.get("chapter") or ""),
                safe_int(topic.get("depth"), 1),
                safe_float(topic.get("difficulty"), 0.5),
                self._json_dumps(
                    topic.get("prerequisites")
                    if isinstance(topic.get("prerequisites"), list)
                    else []
                ),
                self._json_dumps(
                    topic.get("related")
                    if isinstance(topic.get("related"), list)
                    else []
                ),
                self._json_dumps(
                    topic.get("typical_misconceptions")
                    if isinstance(topic.get("typical_misconceptions"), list)
                    else []
                ),
                self._json_dumps(
                    topic.get("skills") if isinstance(topic.get("skills"), list) else []
                ),
                self._json_dumps(
                    topic.get("question_types")
                    if isinstance(topic.get("question_types"), list)
                    else []
                ),
                self._json_dumps(
                    topic.get("examples") if isinstance(topic.get("examples"), list) else []
                ),
                str(topic.get("course_family") or "").strip(),
                self._json_dumps(
                    topic.get("curriculum_version")
                    if isinstance(topic.get("curriculum_version"), (str, list))
                    else []
                ),
                self._json_dumps(
                    topic.get("exam_region")
                    if isinstance(topic.get("exam_region"), (str, list))
                    else []
                ),
                self._json_dumps(
                    topic.get("exam_type")
                    if isinstance(topic.get("exam_type"), (str, list))
                    else []
                ),
                self._json_dumps(
                    topic.get("aliases") if isinstance(topic.get("aliases"), list) else []
                ),
                str(topic.get("source") or "runtime"),
            ),
        )


def upsert_topic(self, topic: dict[str, Any], *, commit: bool = True) -> None:
    """Public topic upsert preserving the existing optional commit behavior."""

    with self._lock:
        _upsert_topic_no_commit(self, topic)
        if commit:
            self._require_conn().commit()


def ensure_topic(
    self,
    *,
    topic_id: str,
    name: str,
    subject: str = "math",
    chapter: str = "runtime",
    difficulty: float = 0.5,
) -> None:
    if self.get_topic(topic_id):
        return
    self.upsert_topic(
        {
            "id": topic_id,
            "name": name or topic_id,
            "subject": subject or "math",
            "chapter": chapter or "runtime",
            "stage": "",
            "unit": chapter or "runtime",
            "depth": 2,
            "difficulty": difficulty,
            "prerequisites": [],
            "related": [],
            "typical_misconceptions": [],
            "skills": [],
            "question_types": [],
            "examples": [],
            "source": "runtime",
        }
    )


def get_topic(self, topic_id: str) -> dict[str, Any] | None:
    row = (
        self._require_read_conn()
        .execute("SELECT * FROM topics WHERE id = ?", (str(topic_id or ""),))
        .fetchone()
    )
    return self._topic_from_row(row)


def find_topic_by_name(self, name: str) -> dict[str, Any] | None:
    text = str(name or "").strip()
    if not text:
        return None
    row = (
        self._require_read_conn()
        .execute(
            "SELECT * FROM topics WHERE name = ? OR id = ? LIMIT 1",
            (text, text),
        )
        .fetchone()
    )
    return self._topic_from_row(row)


def _active_seed_membership_clause() -> str:
    return """(source != 'seed'
        OR NOT EXISTS (SELECT 1 FROM knowledge_seed_membership retired WHERE retired.topic_id = topics.id)
        OR EXISTS (SELECT 1 FROM knowledge_seed_membership active WHERE active.topic_id = topics.id AND active.active = 1))"""


def list_topics(
    self,
    limit: int = 100,
    subject: str | None = None,
    stage: str | None = None,
    *,
    chapter: str | None = None,
    unit: str | None = None,
    course_family: str | None = None,
) -> list[dict[str, Any]]:
    filters = {
        "subject": str(subject or "").strip(),
        "stage": str(stage or "").strip(),
        "chapter": str(chapter or "").strip(),
        "unit": str(unit or "").strip(),
        "course_family": str(course_family or "").strip(),
    }
    machine_key_columns = {"subject", "stage", "course_family"}
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in filters.items():
        if not value:
            continue
        if column in machine_key_columns:
            clauses.append(
                f"lower(replace(replace({column}, '-', '_'), ' ', '_')) = ?"
            )
            params.append(value.lower().replace("-", "_").replace(" ", "_"))
        else:
            clauses.append(f"{column} = ?")
            params.append(value)
    clauses.append(_active_seed_membership_clause())
    where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, int(limit)))
    rows = (
        self._require_read_conn()
        .execute(
            f"SELECT * FROM topics{where_sql} "
            "ORDER BY stage, subject, course_family, chapter, unit, depth, id LIMIT ?",
            tuple(params),
        )
        .fetchall()
    )
    return [
        topic
        for topic in (self._topic_from_row(row) for row in rows)
        if topic is not None
    ]


def count_topics(self) -> int:
    row = (
        self._require_read_conn()
        .execute(f"SELECT COUNT(*) AS count FROM topics WHERE {_active_seed_membership_clause()}")
        .fetchone()
    )
    return int(row["count"] if row is not None else 0)


def count_tracked_mastery_topics(self) -> int:
    row = (
        self._require_read_conn()
        .execute("SELECT COUNT(DISTINCT topic_id) AS count FROM mastery_snapshots")
        .fetchone()
    )
    return int(row["count"] if row is not None else 0)


def average_latest_mastery(self) -> float:
    row = (
        self._require_read_conn()
        .execute(
            """
            SELECT AVG(ms.mastery) AS average_mastery
            FROM mastery_snapshots ms
            JOIN (
                SELECT topic_id, MAX(id) AS max_id
                FROM mastery_snapshots
                GROUP BY topic_id
            ) latest ON latest.max_id = ms.id
            """
        )
        .fetchone()
    )
    return float(row["average_mastery"] or 0.0) if row is not None else 0.0
