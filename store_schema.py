from __future__ import annotations

import re

from .store_common import (
    STORE_CONFIG,
    STORE_STATE,
    ensure_memory_schema,
    json,
    sqlite3,
)

_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COLUMN_DEFINITION_ALLOWLIST = {
    "TEXT",
    "TEXT NOT NULL DEFAULT ''",
    "TEXT NOT NULL DEFAULT '[]'",
    "TEXT NOT NULL DEFAULT 'LLM_RUBRIC'",
    "TEXT NOT NULL DEFAULT 'LEGACY-V1'",
    "INTEGER NOT NULL DEFAULT 0",
    "INTEGER CHECK(USED_HINT IS NULL OR USED_HINT IN (0, 1))",
    "REAL",
}


def _validate_sql_identifier(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not _SQL_IDENT_RE.fullmatch(text):
        raise ValueError(f"invalid SQL identifier for {field}: {value!r}")
    return text


def _validate_sql_order_by(value: str) -> str:
    terms: list[str] = []
    for raw_term in str(value or "").split(","):
        parts = raw_term.strip().split()
        if len(parts) not in {1, 2}:
            raise ValueError(f"invalid SQL order_by term: {raw_term!r}")
        column = _validate_sql_identifier(parts[0], "order_by")
        if len(parts) == 1:
            terms.append(column)
            continue
        direction = parts[1].upper()
        if direction not in {"ASC", "DESC"}:
            raise ValueError(f"invalid SQL order_by direction: {parts[1]!r}")
        terms.append(f"{column} {direction}")
    if not terms:
        raise ValueError("invalid SQL order_by: empty expression")
    return ", ".join(terms)


def _validate_column_definition(value: str) -> str:
    definition = str(value or "").strip()
    if definition.upper() not in _COLUMN_DEFINITION_ALLOWLIST:
        raise ValueError(f"invalid SQL column definition: {value!r}")
    return definition


def _init_notes_fts(self, conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                title,
                content_plain,
                tags,
                content='notes',
                content_rowid='rowid'
            )
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
                INSERT INTO notes_fts(rowid, title, content_plain, tags)
                VALUES (new.rowid, new.title, new.content_plain, new.tags);
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
                INSERT INTO notes_fts(notes_fts, rowid, title, content_plain, tags)
                VALUES ('delete', old.rowid, old.title, old.content_plain, old.tags);
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
                INSERT INTO notes_fts(notes_fts, rowid, title, content_plain, tags)
                VALUES ('delete', old.rowid, old.title, old.content_plain, old.tags);
                INSERT INTO notes_fts(rowid, title, content_plain, tags)
                VALUES (new.rowid, new.title, new.content_plain, new.tags);
            END
            """
        )
    except sqlite3.Error as exc:
        self._log_warning(
            "study notes FTS unavailable; falling back to LIKE search: {}",
            exc,
        )


def _init_db(self) -> None:
    conn = self._require_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            input_text TEXT NOT NULL,
            output_text TEXT NOT NULL,
            metadata TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS topics (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            subject TEXT NOT NULL,
            chapter TEXT,
            stage TEXT NOT NULL DEFAULT '',
            unit TEXT NOT NULL DEFAULT '',
            depth INTEGER DEFAULT 1,
            difficulty REAL DEFAULT 0.5,
            prerequisites TEXT NOT NULL DEFAULT '[]',
            related TEXT NOT NULL DEFAULT '[]',
            typical_misconceptions TEXT NOT NULL DEFAULT '[]',
            skills TEXT NOT NULL DEFAULT '[]',
            question_types TEXT NOT NULL DEFAULT '[]',
            examples TEXT NOT NULL DEFAULT '[]',
            course_family TEXT NOT NULL DEFAULT '',
            curriculum_version TEXT NOT NULL DEFAULT '[]',
            exam_region TEXT NOT NULL DEFAULT '[]',
            exam_type TEXT NOT NULL DEFAULT '[]',
            aliases TEXT NOT NULL DEFAULT '[]',
            source TEXT NOT NULL DEFAULT 'runtime',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mastery_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id TEXT NOT NULL REFERENCES topics(id),
            mastery REAL NOT NULL,
            accuracy REAL,
            recency REAL,
            consistency REAL,
            confidence REAL,
            level TEXT,
            attempts INTEGER DEFAULT 0,
            flags TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_seed_state (
            seed_key TEXT PRIMARY KEY,
            protocol INTEGER NOT NULL,
            revision TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            topic_count INTEGER NOT NULL DEFAULT 0,
            edge_count INTEGER NOT NULL DEFAULT 0,
            applied_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_seed_membership (
            seed_key TEXT NOT NULL,
            topic_id TEXT NOT NULL REFERENCES topics(id),
            protocol INTEGER NOT NULL,
            revision TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
            retired_at TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (seed_key, topic_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_edge_projection_state (
            projection_key TEXT PRIMARY KEY,
            active_revision TEXT NOT NULL,
            edge_count INTEGER NOT NULL DEFAULT 0,
            built_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_edges (
            source_topic_id TEXT NOT NULL,
            target_topic_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.0,
            context TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            use_cases_json TEXT NOT NULL DEFAULT '[]',
            required_mastery_json TEXT NOT NULL DEFAULT 'null',
            catalog_revision TEXT NOT NULL,
            PRIMARY KEY (source_topic_id, target_topic_id, relation_type, catalog_revision)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wrong_questions (
            id TEXT PRIMARY KEY,
            topic_id TEXT NOT NULL REFERENCES topics(id),
            question TEXT NOT NULL,
            user_answer TEXT NOT NULL,
            expected_answer TEXT NOT NULL,
            error_type TEXT NOT NULL,
            verdict TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            retry_count INTEGER DEFAULT 0,
            consecutive_correct INTEGER DEFAULT 0,
            max_correct_difficulty INTEGER DEFAULT 0,
            last_error_at TEXT DEFAULT (datetime('now')),
            last_retry_at TEXT,
            resolved_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fsrs_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id TEXT NOT NULL UNIQUE REFERENCES topics(id),
            card_data TEXT NOT NULL,
            fsrs_state TEXT,
            last_rating INTEGER,
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at TEXT,
            duration_minutes REAL,
            question_count INTEGER DEFAULT 0,
            topics_touched TEXT NOT NULL DEFAULT '[]',
            summary_markdown TEXT,
            notes_exported INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notebooks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT UNIQUE NOT NULL,
            notebook_id TEXT REFERENCES notebooks(id) ON DELETE SET NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            content_plain TEXT NOT NULL,
            snippet TEXT NOT NULL DEFAULT '',
            is_ai_generated INTEGER NOT NULL DEFAULT 0,
            source_type TEXT NOT NULL DEFAULT 'manual',
            source_ref TEXT NOT NULL DEFAULT '',
            topic_ids TEXT NOT NULL DEFAULT '[]',
            tags TEXT NOT NULL DEFAULT '[]',
            word_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            edited_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    _init_notes_fts(self, conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS captured_questions (
            id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            question_text TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            topic_id TEXT,
            subject TEXT,
            question_type TEXT,
            classification_confidence REAL,
            consent_origin TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qa_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            topic_id TEXT REFERENCES topics(id),
            source_question_id TEXT REFERENCES captured_questions(id) ON DELETE SET NULL,
            question TEXT,
            user_answer TEXT,
            eval_result TEXT,
            mode TEXT NOT NULL,
            response_time_ms INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS question_instances (
            question_id TEXT PRIMARY KEY,
            topic_id TEXT REFERENCES topics(id),
            source_question_id TEXT REFERENCES captured_questions(id) ON DELETE SET NULL,
            question_json TEXT NOT NULL,
            question_type TEXT NOT NULL DEFAULT '',
            difficulty INTEGER,
            status TEXT NOT NULL DEFAULT 'answered',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attempts (
            attempt_id TEXT PRIMARY KEY,
            question_id TEXT NOT NULL REFERENCES question_instances(question_id),
            session_id TEXT NOT NULL REFERENCES sessions(id),
            topic_id TEXT REFERENCES topics(id),
            user_answer TEXT NOT NULL,
            mode TEXT NOT NULL,
            response_time_ms INTEGER,
            used_hint INTEGER CHECK(used_hint IS NULL OR used_hint IN (0, 1)),
            submitted_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluations (
            attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
            evaluation_json TEXT NOT NULL,
            evaluator_type TEXT NOT NULL DEFAULT 'llm_rubric',
            evaluator_version TEXT NOT NULL DEFAULT 'legacy-v1',
            confidence REAL,
            fallback_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mastery_snapshots_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id TEXT NOT NULL REFERENCES topics(id),
            mastery REAL NOT NULL,
            accuracy REAL NOT NULL,
            recency REAL NOT NULL,
            consistency REAL NOT NULL,
            confidence REAL NOT NULL,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            unresolved_wrong_count INTEGER NOT NULL DEFAULT 0,
            mastery_model_version TEXT NOT NULL,
            source_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
            computed_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(topic_id, mastery_model_version, source_attempt_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mastery_projection_queue (
            attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'processing', 'done', 'failed')),
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id TEXT NOT NULL REFERENCES topics(id),
            card_id INTEGER REFERENCES fsrs_cards(id),
            rating INTEGER,
            scheduled_days INTEGER,
            actual_days INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_knowledge_items (
            id TEXT PRIMARY KEY,
            item_type TEXT NOT NULL,
            dedupe_key TEXT,
            payload_json TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT DEFAULT 'candidate',
            score REAL DEFAULT 0.0,
            evidence_count INTEGER DEFAULT 0,
            positive_count INTEGER DEFAULT 0,
            negative_count INTEGER DEFAULT 0,
            conflict_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL REFERENCES candidate_knowledge_items(id),
            event_type TEXT NOT NULL,
            weight REAL NOT NULL,
            context_json TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS anonymous_knowledge_stats (
            id TEXT PRIMARY KEY,
            stat_type TEXT NOT NULL,
            stat_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            sample_count INTEGER DEFAULT 0,
            outcome_json TEXT NOT NULL DEFAULT '{}',
            min_sample_met INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_contribution_queue (
            id TEXT PRIMARY KEY,
            stats_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'preview',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    ensure_memory_schema(conn)
    self._ensure_column(conn, "topics", "stage", "TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE topics SET stage = '' WHERE stage IS NULL")
    self._ensure_column(conn, "topics", "unit", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        UPDATE topics
        SET unit = COALESCE(NULLIF(chapter, ''), 'general')
        WHERE unit IS NULL OR unit = ''
        """
    )
    self._ensure_column(conn, "topics", "skills", "TEXT NOT NULL DEFAULT '[]'")
    conn.execute("UPDATE topics SET skills = '[]' WHERE skills IS NULL OR skills = ''")
    self._ensure_column(conn, "topics", "question_types", "TEXT NOT NULL DEFAULT '[]'")
    conn.execute("UPDATE topics SET question_types = '[]' WHERE question_types IS NULL OR question_types = ''")
    self._ensure_column(conn, "topics", "examples", "TEXT NOT NULL DEFAULT '[]'")
    conn.execute("UPDATE topics SET examples = '[]' WHERE examples IS NULL OR examples = ''")
    self._ensure_column(conn, "topics", "course_family", "TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE topics SET course_family = '' WHERE course_family IS NULL")
    for context_column in ("curriculum_version", "exam_region", "exam_type"):
        self._ensure_column(
            conn, "topics", context_column, "TEXT NOT NULL DEFAULT '[]'"
        )
        conn.execute(
            f"UPDATE topics SET {context_column} = '[]' "
            f"WHERE {context_column} IS NULL OR {context_column} = ''"
        )
    self._ensure_column(conn, "topics", "aliases", "TEXT NOT NULL DEFAULT '[]'")
    conn.execute("UPDATE topics SET aliases = '[]' WHERE aliases IS NULL OR aliases = ''")
    self._ensure_column(
        conn, "knowledge_seed_state", "topic_count", "INTEGER NOT NULL DEFAULT 0"
    )
    self._ensure_column(
        conn, "knowledge_seed_state", "edge_count", "INTEGER NOT NULL DEFAULT 0"
    )
    self._ensure_column(
        conn, "knowledge_seed_state", "applied_at", "TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "UPDATE knowledge_seed_state SET applied_at = updated_at WHERE applied_at IS NULL OR applied_at = ''"
    )
    self._ensure_column(conn, "knowledge_seed_membership", "retired_at", "TEXT")
    self._ensure_column(conn, "candidate_knowledge_items", "dedupe_key", "TEXT")
    self._ensure_column(conn, "qa_records", "source_question_id", "TEXT")
    self._ensure_column(
        conn,
        "attempts",
        "used_hint",
        "INTEGER CHECK(used_hint IS NULL OR used_hint IN (0, 1))",
    )
    # PR-8 adds evaluator provenance without replacing the evaluation JSON.
    # Defaults make records created before this migration read as the legacy
    # LLM rubric path while keeping the original JSON untouched.
    self._ensure_column(
        conn,
        "evaluations",
        "evaluator_type",
        "TEXT NOT NULL DEFAULT 'llm_rubric'",
    )
    self._ensure_column(
        conn,
        "evaluations",
        "evaluator_version",
        "TEXT NOT NULL DEFAULT 'legacy-v1'",
    )
    self._ensure_column(conn, "evaluations", "confidence", "REAL")
    self._ensure_column(
        conn, "evaluations", "fallback_reason", "TEXT NOT NULL DEFAULT ''"
    )
    expected_idx_topics_stage = ["stage", "subject", "chapter", "unit", "depth", "id"]
    current_idx_topics_stage = [
        row["name"] if isinstance(row, sqlite3.Row) else row[2]
        for row in conn.execute("PRAGMA index_info(idx_topics_stage)").fetchall()
    ]
    if current_idx_topics_stage and current_idx_topics_stage != expected_idx_topics_stage:
        conn.execute("DROP INDEX IF EXISTS idx_topics_stage")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_topics_stage ON topics(stage, subject, chapter, unit, depth, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_seed_membership_topic_active ON knowledge_seed_membership(topic_id, active)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_edges_revision_from ON knowledge_edges(catalog_revision, source_topic_id, relation_type, target_topic_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_edges_revision_to ON knowledge_edges(catalog_revision, target_topic_id, relation_type, source_topic_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mastery_topic_updated ON mastery_snapshots(topic_id, updated_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wrong_topic_status ON wrong_questions(topic_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qa_topic_created ON qa_records(topic_id, created_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qa_source_question ON qa_records(source_question_id, created_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_question_instances_topic_created ON question_instances(topic_id, created_at DESC, question_id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempts_question_submitted ON attempts(question_id, submitted_at DESC, attempt_id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempts_topic_submitted ON attempts(topic_id, submitted_at DESC, attempt_id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mastery_v2_topic_model_computed ON mastery_snapshots_v2(topic_id, mastery_model_version, computed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mastery_projection_status_updated ON mastery_projection_queue(status, updated_at, attempt_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_captured_questions_topic_used ON captured_questions(topic_id, last_used_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_captured_questions_status_expires ON captured_questions(status, expires_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_topic_created ON review_log(topic_id, created_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_knowledge_dedupe ON candidate_knowledge_items(item_type, dedupe_key) WHERE dedupe_key IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_knowledge_status ON candidate_knowledge_items(status, item_type, updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_evidence_item ON knowledge_evidence(item_id, created_at DESC)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_anonymous_knowledge_stats_key ON anonymous_knowledge_stats(stat_type, stat_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contribution_queue_status ON knowledge_contribution_queue(status, updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notes_notebook_updated ON notes(notebook_id, updated_at DESC, rowid DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notes_source ON notes(source_type, source_ref)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notes_edited ON notes(edited_at DESC, rowid DESC)"
    )
    conn.commit()


@staticmethod
def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    table = _validate_sql_identifier(table, "table")
    column = _validate_sql_identifier(column, "column")
    definition = _validate_column_definition(definition)
    rows = conn.execute("PRAGMA table_info(" + table + ")").fetchall()
    if column in {str(row["name"]) for row in rows}:
        return
    conn.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + definition)


@staticmethod
def _trim_append_only_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    group_column: str,
    group_value: str | None,
    history_limit: int,
    order_by: str = "id DESC",
) -> None:
    table = _validate_sql_identifier(table, "table")
    group_column = _validate_sql_identifier(group_column, "group_column")
    order_by = _validate_sql_order_by(order_by)
    limit = max(1, int(history_limit))
    if group_value is None:
        conn.execute(
            """
            DELETE FROM """
            + table
            + """
            WHERE """
            + group_column
            + """ IS NULL
              AND id NOT IN (
                  SELECT id
                  FROM """
            + table
            + """
                  WHERE """
            + group_column
            + """ IS NULL
                  ORDER BY """
            + order_by
            + """
                  LIMIT ?
              )
            """,
            (limit,),
        )
        return
    conn.execute(
        """
        DELETE FROM """
        + table
        + """
        WHERE """
        + group_column
        + """ = ?
          AND id NOT IN (
              SELECT id
              FROM """
        + table
        + """
              WHERE """
        + group_column
        + """ = ?
              ORDER BY """
        + order_by
        + """
              LIMIT ?
          )
        """,
        (group_value, group_value, limit),
    )


def _load_seed_if_empty(self) -> None:
    if not self.seed_json_path.is_file():
        return
    if self.get_raw(STORE_CONFIG) is not None or self.get_raw(STORE_STATE) is not None:
        return
    if self.get_raw("interactions") or self._has_interactions():
        return
    try:
        payload = json.loads(self.seed_json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        self._log_warning("study seed load failed: {}", exc)
        return
    if not isinstance(payload, dict):
        return
    for key in (STORE_CONFIG, STORE_STATE):
        value = payload.get(key)
        if isinstance(value, dict):
            self.set_raw(key, value)
