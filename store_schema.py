from __future__ import annotations

import re

from .adaptive_learning.cognitive_versions import (
    get_cognitive_version_set,
    supported_cognitive_version_sets,
)
from .store_cognitive_retention import create_cognitive_retention_schema
from .store_common import (
    STORE_CONFIG,
    STORE_STATE,
    ensure_memory_schema,
    json,
    sqlite3,
)

_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUPPORTED_COGNITIVE_PROJECTION_VERSIONS = tuple(
    dict.fromkeys(
        version_set.projection_version
        for name in supported_cognitive_version_sets()
        if (version_set := get_cognitive_version_set(name)) is not None
    )
)
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

_COGNITIVE_OUTBOX_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cognitive_outbox (
    outbox_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    event_id TEXT NOT NULL UNIQUE,
    operation TEXT NOT NULL CHECK(operation IN (
        'intervention_event', 'projection_enqueue', 'retention_disposition'
    )),
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
        'pending', 'processing', 'done', 'failed', 'discarded'
    )),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
    last_error TEXT NOT NULL DEFAULT '',
    lease_token TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def _ensure_cognitive_outbox_schema(conn: sqlite3.Connection) -> None:
    """Add the retention operation without losing an existing outbox."""

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cognitive_outbox'"
    ).fetchone()
    if row is None:
        conn.execute(_COGNITIVE_OUTBOX_TABLE_SQL)
        return
    if "retention_disposition" in str(row["sql"] or ""):
        return
    interrupted = conn.execute(
        """SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'cognitive_outbox_pre_retention'"""
    ).fetchone()
    if interrupted is not None:
        raise RuntimeError("incomplete cognitive outbox migration requires recovery")
    before = int(conn.execute("SELECT COUNT(*) FROM cognitive_outbox").fetchone()[0])
    conn.execute(
        "ALTER TABLE cognitive_outbox RENAME TO cognitive_outbox_pre_retention"
    )
    conn.execute(_COGNITIVE_OUTBOX_TABLE_SQL)
    conn.execute(
        """
        INSERT INTO cognitive_outbox (
            outbox_id, attempt_id, event_id, operation, payload_json, status,
            retry_count, last_error, lease_token, lease_expires_at,
            created_at, updated_at
        )
        SELECT outbox_id, attempt_id, event_id, operation, payload_json, status,
               retry_count, last_error, lease_token, lease_expires_at,
               created_at, updated_at
        FROM cognitive_outbox_pre_retention
        """
    )
    after = int(conn.execute("SELECT COUNT(*) FROM cognitive_outbox").fetchone()[0])
    if after != before:
        raise RuntimeError("cognitive outbox migration did not preserve every row")
    conn.execute("DROP TABLE cognitive_outbox_pre_retention")


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
            dirty INTEGER NOT NULL DEFAULT 0 CHECK(dirty IN (0, 1)),
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
        CREATE TABLE IF NOT EXISTS cognitive_fact_roots (
            root_fact_seq INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(fact_type, source_id)
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
            root_fact_seq INTEGER NOT NULL DEFAULT 0,
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
            root_fact_seq INTEGER NOT NULL DEFAULT 0,
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
            lease_token TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cognitive_projection_queue (
            attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'processing', 'done', 'failed')),
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            lease_token TEXT NOT NULL DEFAULT '',
            extractor_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # ``cognitive_projection_queue`` is retained as the V1 compatibility
    # surface.  V2 extraction work is keyed by both attempt and extractor
    # version so historical attempts can be safely reprocessed.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cognitive_extraction_queue (
            attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
            extractor_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'processing', 'done', 'failed')),
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            lease_token TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY(attempt_id, extractor_version)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cognitive_evidence (
            evidence_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
            topic_id TEXT NOT NULL REFERENCES topics(id),
            hypothesis_code TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('support', 'counter')),
            strength REAL NOT NULL CHECK(strength >= 0.0 AND strength <= 1.0),
            extractor_confidence REAL NOT NULL
                CHECK(extractor_confidence >= 0.0 AND extractor_confidence <= 1.0),
            diagnosticity REAL NOT NULL
                CHECK(diagnosticity >= 0.0 AND diagnosticity <= 1.0),
            source_kind TEXT NOT NULL,
            evidence_span TEXT NOT NULL,
            extractor_version TEXT NOT NULL,
            evidence_family_id TEXT NOT NULL DEFAULT '',
            question_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            diagnostic_validation_id TEXT NOT NULL DEFAULT '',
            root_fact_seq INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(attempt_id, hypothesis_code, extractor_version)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cognitive_hypothesis_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_id TEXT NOT NULL,
            topic_id TEXT NOT NULL REFERENCES topics(id),
            hypothesis_code TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'hypothesized', 'supported', 'contradicted', 'dismissed',
                'remediating', 'provisionally_resolved', 'monitored', 'resolved'
            )),
            probability REAL NOT NULL
                CHECK(probability >= 0.0 AND probability <= 1.0),
            support_count INTEGER NOT NULL DEFAULT 0 CHECK(support_count >= 0),
            counter_count INTEGER NOT NULL DEFAULT 0 CHECK(counter_count >= 0),
            diagnostic_support_count INTEGER NOT NULL DEFAULT 0
                CHECK(diagnostic_support_count >= 0),
            relapse_count INTEGER NOT NULL DEFAULT 0 CHECK(relapse_count >= 0),
            source_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
            model_version TEXT NOT NULL,
            computed_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(hypothesis_id, model_version, source_attempt_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cognitive_topic_projection_queue (
            topic_id TEXT NOT NULL REFERENCES topics(id),
            model_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'processing', 'done', 'failed')),
            requested_generation INTEGER NOT NULL DEFAULT 0
                CHECK(requested_generation >= 0),
            claimed_generation INTEGER NOT NULL DEFAULT 0
                CHECK(claimed_generation >= 0),
            projected_generation INTEGER NOT NULL DEFAULT 0
                CHECK(projected_generation >= 0),
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            lease_token TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY(topic_id, model_version)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cognitive_hypothesis_current (
            hypothesis_id TEXT NOT NULL,
            topic_id TEXT NOT NULL REFERENCES topics(id),
            hypothesis_code TEXT NOT NULL,
            evidence_status TEXT NOT NULL
                CHECK(evidence_status IN ('hypothesized', 'supported', 'contradicted')),
            intervention_stage TEXT NOT NULL DEFAULT 'idle'
                CHECK(intervention_stage IN (
                    'idle', 'probing', 'remediating',
                    'provisionally_resolved', 'monitored', 'resolved'
                )),
            user_override TEXT NOT NULL DEFAULT ''
                CHECK(user_override IN ('', 'dismissed', 'suppressed', 'deleted')),
            status TEXT NOT NULL CHECK(status IN (
                'hypothesized', 'supported', 'contradicted', 'dismissed',
                'remediating', 'provisionally_resolved', 'monitored', 'resolved'
            )),
            probability REAL NOT NULL
                CHECK(probability >= 0.0 AND probability <= 1.0),
            support_count INTEGER NOT NULL DEFAULT 0 CHECK(support_count >= 0),
            counter_count INTEGER NOT NULL DEFAULT 0 CHECK(counter_count >= 0),
            diagnostic_support_count INTEGER NOT NULL DEFAULT 0
                CHECK(diagnostic_support_count >= 0),
            relapse_count INTEGER NOT NULL DEFAULT 0 CHECK(relapse_count >= 0),
            source_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
            source_snapshot_id TEXT NOT NULL DEFAULT '',
            model_version TEXT NOT NULL,
            projected_generation INTEGER NOT NULL CHECK(projected_generation >= 0),
            last_intent TEXT NOT NULL DEFAULT '',
            last_outcome TEXT NOT NULL DEFAULT '',
            consecutive_repair_failures INTEGER NOT NULL DEFAULT 0
                CHECK(consecutive_repair_failures >= 0),
            computed_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY(hypothesis_id, model_version),
            UNIQUE(topic_id, hypothesis_code, model_version)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cognitive_user_controls (
            control_id TEXT PRIMARY KEY,
            topic_id TEXT NOT NULL REFERENCES topics(id),
            hypothesis_code TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('dismiss', 'suppress', 'restore', 'delete')),
            reason TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL DEFAULT '',
            root_fact_seq INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cognitive_delete_cutoffs (
            topic_id TEXT NOT NULL REFERENCES topics(id),
            hypothesis_code TEXT NOT NULL,
            delete_cutoff_seq INTEGER NOT NULL CHECK(delete_cutoff_seq > 0),
            control_id TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            PRIMARY KEY(topic_id, hypothesis_code)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cognitive_intervention_events (
            event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL CHECK(event_type IN (
                'intent_proposed', 'question_committed',
                'attempt_committed', 'intervention_abandoned'
            )),
            decision_id TEXT NOT NULL,
            hypothesis_id TEXT NOT NULL,
            topic_id TEXT NOT NULL REFERENCES topics(id),
            hypothesis_code TEXT NOT NULL,
            hypothesis_status TEXT NOT NULL,
            hypothesis_probability REAL NOT NULL,
            hypothesis_model_version TEXT NOT NULL,
            hypothesis_source_snapshot_id TEXT NOT NULL,
            hypothesis_source_attempt_id TEXT NOT NULL DEFAULT '',
            hypothesis_projection_generation INTEGER NOT NULL
                CHECK(hypothesis_projection_generation >= 0),
            learning_intent TEXT NOT NULL CHECK(learning_intent IN (
                'misconception_probe', 'misconception_repair', 'transfer_check'
            )),
            repair_strategy TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            selection_reason TEXT NOT NULL,
            eligible_topic_ids_json TEXT NOT NULL DEFAULT '[]',
            learning_plan_id TEXT NOT NULL DEFAULT '',
            learning_plan_revision INTEGER NOT NULL DEFAULT 0,
            scope_key TEXT NOT NULL DEFAULT '',
            scope_revision INTEGER NOT NULL DEFAULT 0,
            origin_wrong_question_id TEXT NOT NULL DEFAULT '',
            source_question_id TEXT NOT NULL DEFAULT '',
            target_binding_json TEXT NOT NULL DEFAULT '{}',
            question_id TEXT NOT NULL DEFAULT '',
            attempt_id TEXT NOT NULL DEFAULT '',
            blueprint_id TEXT NOT NULL DEFAULT '',
            question_family_id TEXT NOT NULL DEFAULT '',
            diagnostic_validation_id TEXT NOT NULL DEFAULT '',
            evaluation_verdict TEXT NOT NULL DEFAULT '' CHECK(
                evaluation_verdict IN ('', 'correct', 'partial', 'wrong', 'dont_know')
            ),
            abandonment_reason TEXT NOT NULL DEFAULT '',
            policy_version TEXT NOT NULL,
            validator_version TEXT NOT NULL DEFAULT '',
            schema_version INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            root_fact_seq INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
        CREATE TABLE IF NOT EXISTS learning_plans (
            id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL CHECK(status IN ('draft', 'active', 'paused', 'completed', 'canceled')),
            source_kind TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            display_title TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1,
            unmatched_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            activated_at TEXT,
            completed_at TEXT,
            canceled_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_plan_items (
            plan_id TEXT NOT NULL REFERENCES learning_plans(id) ON DELETE CASCADE,
            topic_id TEXT NOT NULL REFERENCES topics(id),
            ordinal INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('core', 'prerequisite', 'optional')),
            mapping_score REAL NOT NULL,
            mapping_confidence TEXT NOT NULL CHECK(mapping_confidence IN ('high', 'medium')),
            reason_code TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0, 1)),
            created_at TEXT NOT NULL,
            PRIMARY KEY (plan_id, topic_id)
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
    self._ensure_column(
        conn,
        "knowledge_edge_projection_state",
        "dirty",
        "INTEGER NOT NULL DEFAULT 0",
    )
    self._ensure_column(conn, "candidate_knowledge_items", "dedupe_key", "TEXT")
    self._ensure_column(conn, "qa_records", "source_question_id", "TEXT")
    self._ensure_column(
        conn,
        "attempts",
        "used_hint",
        "INTEGER CHECK(used_hint IS NULL OR used_hint IN (0, 1))",
    )
    self._ensure_column(
        conn,
        "mastery_projection_queue",
        "lease_token",
        "TEXT NOT NULL DEFAULT ''",
    )
    for column in (
        "evidence_family_id",
        "question_id",
        "session_id",
        "diagnostic_validation_id",
    ):
        self._ensure_column(
            conn,
            "cognitive_evidence",
            column,
            "TEXT NOT NULL DEFAULT ''",
        )
    self._ensure_column(
        conn,
        "cognitive_user_controls",
        "expires_at",
        "TEXT NOT NULL DEFAULT ''",
    )
    # Before suppress controls became bounded, legacy rows had no expiry.
    # Preserve their active, indefinite semantics using the established
    # dismiss representation before the new insert validator is installed.
    conn.execute(
        """
        UPDATE cognitive_user_controls
        SET action = 'dismiss'
        WHERE action = 'suppress' AND expires_at = ''
        """
    )
    for table in (
        "question_instances",
        "attempts",
        "cognitive_evidence",
        "cognitive_user_controls",
        "cognitive_intervention_events",
    ):
        self._ensure_column(
            conn,
            table,
            "root_fact_seq",
            "INTEGER NOT NULL DEFAULT 0",
        )
    # Allocate one stable, database-wide sequence for each immutable source
    # fact.  Existing rows are backfilled deterministically; late cognitive
    # extraction later reuses the originating attempt's sequence.
    conn.execute(
        """
        INSERT OR IGNORE INTO cognitive_fact_roots (
            fact_type, source_id, effective_at, recorded_at
        )
        SELECT fact_type, source_id, effective_at, recorded_at
        FROM (
            SELECT 'question' AS fact_type, question_id AS source_id,
                   created_at AS effective_at, created_at AS recorded_at,
                   1 AS fact_rank
            FROM question_instances
            UNION ALL
            SELECT 'attempt', attempt_id, submitted_at, submitted_at, 2
            FROM attempts
            UNION ALL
            SELECT 'control', control_id, created_at, created_at, 3
            FROM cognitive_user_controls
            UNION ALL
            SELECT CASE
                       WHEN event_type = 'question_committed' THEN 'question'
                       WHEN event_type = 'attempt_committed' THEN 'attempt'
                       ELSE 'intervention'
                   END,
                   CASE
                       WHEN event_type = 'question_committed' THEN question_id
                       WHEN event_type = 'attempt_committed' THEN attempt_id
                       ELSE event_id
                   END,
                   occurred_at, created_at, 4
            FROM cognitive_intervention_events
        ) facts
        WHERE source_id != ''
        ORDER BY effective_at, fact_rank, source_id
        """
    )
    _ensure_cognitive_outbox_schema(conn)
    create_cognitive_retention_schema(conn)
    # Older development databases may already contain terminal episode rows
    # from before lifecycle facts were introduced.  Preserve those expiries as
    # immutable cognitive facts instead of rebuilding from mutable episode
    # status.
    conn.execute(
        """
        INSERT OR IGNORE INTO cognitive_fact_roots (
            fact_type, source_id, effective_at, recorded_at
        )
        SELECT 'cognitive_episode',
               'cognitive-episode-fact:' || episode_id || ':expired',
               expired_at,
               CASE WHEN updated_at != '' THEN updated_at ELSE expired_at END
        FROM cognitive_monitoring_episodes
        WHERE status = 'expired' AND expired_at != ''
        ORDER BY expired_at, episode_id
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO cognitive_monitoring_episode_facts (
            fact_id, episode_id, fact_type, root_fact_seq,
            occurred_at, created_at
        )
        SELECT 'cognitive-episode-fact:' || episodes.episode_id || ':expired',
               episodes.episode_id,
               'expired',
               roots.root_fact_seq,
               episodes.expired_at,
               CASE
                   WHEN episodes.updated_at != '' THEN episodes.updated_at
                   ELSE episodes.expired_at
               END
        FROM cognitive_monitoring_episodes episodes
        JOIN cognitive_fact_roots roots
          ON roots.fact_type = 'cognitive_episode'
         AND roots.source_id =
             'cognitive-episode-fact:' || episodes.episode_id || ':expired'
        WHERE episodes.status = 'expired' AND episodes.expired_at != ''
        """
    )
    conn.execute(
        """
        UPDATE question_instances
        SET root_fact_seq = (
            SELECT roots.root_fact_seq FROM cognitive_fact_roots roots
            WHERE roots.fact_type = 'question'
              AND roots.source_id = question_instances.question_id
        )
        WHERE root_fact_seq <= 0
        """
    )
    conn.execute(
        """
        UPDATE attempts
        SET root_fact_seq = (
            SELECT roots.root_fact_seq FROM cognitive_fact_roots roots
            WHERE roots.fact_type = 'attempt'
              AND roots.source_id = attempts.attempt_id
        )
        WHERE root_fact_seq <= 0
        """
    )
    conn.execute(
        """
        UPDATE cognitive_evidence
        SET root_fact_seq = COALESCE((
            SELECT attempts.root_fact_seq FROM attempts
            WHERE attempts.attempt_id = cognitive_evidence.attempt_id
        ), 0)
        WHERE root_fact_seq <= 0
        """
    )
    conn.execute(
        """
        UPDATE cognitive_user_controls
        SET root_fact_seq = (
            SELECT roots.root_fact_seq FROM cognitive_fact_roots roots
            WHERE roots.fact_type = 'control'
              AND roots.source_id = cognitive_user_controls.control_id
        )
        WHERE root_fact_seq <= 0
        """
    )
    conn.execute(
        """
        INSERT INTO cognitive_delete_cutoffs (
            topic_id, hypothesis_code, delete_cutoff_seq,
            control_id, deleted_at
        )
        SELECT controls.topic_id, controls.hypothesis_code,
               controls.root_fact_seq, controls.control_id, controls.created_at
        FROM cognitive_user_controls controls
        WHERE controls.action = 'delete'
          AND controls.root_fact_seq = (
              SELECT MAX(latest.root_fact_seq)
              FROM cognitive_user_controls latest
              WHERE latest.topic_id = controls.topic_id
                AND latest.hypothesis_code = controls.hypothesis_code
                AND latest.action = 'delete'
          )
        ON CONFLICT(topic_id, hypothesis_code) DO UPDATE SET
            delete_cutoff_seq = MAX(
                cognitive_delete_cutoffs.delete_cutoff_seq,
                excluded.delete_cutoff_seq
            ),
            control_id = CASE
                WHEN excluded.delete_cutoff_seq
                     >= cognitive_delete_cutoffs.delete_cutoff_seq
                    THEN excluded.control_id
                ELSE cognitive_delete_cutoffs.control_id
            END,
            deleted_at = CASE
                WHEN excluded.delete_cutoff_seq
                     >= cognitive_delete_cutoffs.delete_cutoff_seq
                    THEN excluded.deleted_at
                ELSE cognitive_delete_cutoffs.deleted_at
            END
        """
    )
    conn.execute(
        """
        UPDATE cognitive_intervention_events
        SET root_fact_seq = (
            SELECT roots.root_fact_seq FROM cognitive_fact_roots roots
            WHERE roots.fact_type = CASE
                    WHEN cognitive_intervention_events.event_type = 'question_committed'
                        THEN 'question'
                    WHEN cognitive_intervention_events.event_type = 'attempt_committed'
                        THEN 'attempt'
                    ELSE 'intervention'
                END
              AND roots.source_id = CASE
                    WHEN cognitive_intervention_events.event_type = 'question_committed'
                        THEN cognitive_intervention_events.question_id
                    WHEN cognitive_intervention_events.event_type = 'attempt_committed'
                        THEN cognitive_intervention_events.attempt_id
                    ELSE cognitive_intervention_events.event_id
                END
        )
        WHERE root_fact_seq <= 0
        """
    )
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS trg_cognitive_root_question_insert
        AFTER INSERT ON question_instances
        WHEN NEW.root_fact_seq <= 0
        BEGIN
            INSERT OR IGNORE INTO cognitive_fact_roots (
                fact_type, source_id, effective_at, recorded_at
            ) VALUES ('question', NEW.question_id, NEW.created_at, datetime('now'));
            UPDATE question_instances
            SET root_fact_seq = (
                SELECT root_fact_seq FROM cognitive_fact_roots
                WHERE fact_type = 'question' AND source_id = NEW.question_id
            )
            WHERE question_id = NEW.question_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_cognitive_root_attempt_insert
        AFTER INSERT ON attempts
        WHEN NEW.root_fact_seq <= 0
        BEGIN
            INSERT OR IGNORE INTO cognitive_fact_roots (
                fact_type, source_id, effective_at, recorded_at
            ) VALUES ('attempt', NEW.attempt_id, NEW.submitted_at, datetime('now'));
            UPDATE attempts
            SET root_fact_seq = (
                SELECT root_fact_seq FROM cognitive_fact_roots
                WHERE fact_type = 'attempt' AND source_id = NEW.attempt_id
            )
            WHERE attempt_id = NEW.attempt_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_cognitive_root_evidence_insert
        AFTER INSERT ON cognitive_evidence
        WHEN NEW.root_fact_seq <= 0
        BEGIN
            UPDATE cognitive_evidence
            SET root_fact_seq = COALESCE((
                SELECT root_fact_seq FROM attempts
                WHERE attempt_id = NEW.attempt_id
            ), 0)
            WHERE evidence_id = NEW.evidence_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_cognitive_root_control_insert
        AFTER INSERT ON cognitive_user_controls
        WHEN NEW.root_fact_seq <= 0
        BEGIN
            INSERT OR IGNORE INTO cognitive_fact_roots (
                fact_type, source_id, effective_at, recorded_at
            ) VALUES ('control', NEW.control_id, NEW.created_at, datetime('now'));
            UPDATE cognitive_user_controls
            SET root_fact_seq = (
                SELECT root_fact_seq FROM cognitive_fact_roots
                WHERE fact_type = 'control' AND source_id = NEW.control_id
            )
            WHERE control_id = NEW.control_id;
            INSERT INTO cognitive_delete_cutoffs (
                topic_id, hypothesis_code, delete_cutoff_seq,
                control_id, deleted_at
            )
            SELECT NEW.topic_id, NEW.hypothesis_code, roots.root_fact_seq,
                   NEW.control_id, NEW.created_at
            FROM cognitive_fact_roots roots
            WHERE NEW.action = 'delete'
              AND roots.fact_type = 'control'
              AND roots.source_id = NEW.control_id
            ON CONFLICT(topic_id, hypothesis_code) DO UPDATE SET
                delete_cutoff_seq = MAX(
                    cognitive_delete_cutoffs.delete_cutoff_seq,
                    excluded.delete_cutoff_seq
                ),
                control_id = CASE
                    WHEN excluded.delete_cutoff_seq
                         >= cognitive_delete_cutoffs.delete_cutoff_seq
                        THEN excluded.control_id
                    ELSE cognitive_delete_cutoffs.control_id
                END,
                deleted_at = CASE
                    WHEN excluded.delete_cutoff_seq
                         >= cognitive_delete_cutoffs.delete_cutoff_seq
                        THEN excluded.deleted_at
                    ELSE cognitive_delete_cutoffs.deleted_at
                END;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_cognitive_control_expiry_validate
        BEFORE INSERT ON cognitive_user_controls
        WHEN (
            NEW.action = 'suppress'
            AND (
                NEW.expires_at = ''
                OR julianday(NEW.expires_at) IS NULL
                OR julianday(NEW.expires_at) - julianday('now') > 1.0
            )
        ) OR (NEW.action != 'suppress' AND NEW.expires_at != '')
        BEGIN
            SELECT RAISE(ABORT, 'invalid cognitive suppress expiry');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_cognitive_root_intervention_insert
        AFTER INSERT ON cognitive_intervention_events
        WHEN NEW.root_fact_seq <= 0
        BEGIN
            INSERT OR IGNORE INTO cognitive_fact_roots (
                fact_type, source_id, effective_at, recorded_at
            ) VALUES (
                CASE
                    WHEN NEW.event_type = 'question_committed' THEN 'question'
                    WHEN NEW.event_type = 'attempt_committed' THEN 'attempt'
                    ELSE 'intervention'
                END,
                CASE
                    WHEN NEW.event_type = 'question_committed' THEN NEW.question_id
                    WHEN NEW.event_type = 'attempt_committed' THEN NEW.attempt_id
                    ELSE NEW.event_id
                END,
                NEW.occurred_at,
                datetime('now')
            );
            UPDATE cognitive_intervention_events
            SET root_fact_seq = (
                SELECT root_fact_seq FROM cognitive_fact_roots
                WHERE fact_type = CASE
                        WHEN NEW.event_type = 'question_committed' THEN 'question'
                        WHEN NEW.event_type = 'attempt_committed' THEN 'attempt'
                        ELSE 'intervention'
                    END
                  AND source_id = CASE
                        WHEN NEW.event_type = 'question_committed' THEN NEW.question_id
                        WHEN NEW.event_type = 'attempt_committed' THEN NEW.attempt_id
                        ELSE NEW.event_id
                    END
            )
            WHERE event_id = NEW.event_id;
        END;
        """
    )
    self._ensure_column(
        conn,
        "cognitive_hypothesis_current",
        "source_snapshot_id",
        "TEXT NOT NULL DEFAULT ''",
    )
    self._ensure_column(
        conn,
        "cognitive_hypothesis_current",
        "last_intent",
        "TEXT NOT NULL DEFAULT ''",
    )
    self._ensure_column(
        conn,
        "cognitive_hypothesis_current",
        "last_outcome",
        "TEXT NOT NULL DEFAULT ''",
    )
    self._ensure_column(
        conn,
        "cognitive_hypothesis_current",
        "consecutive_repair_failures",
        "INTEGER NOT NULL DEFAULT 0",
    )
    # Preserve pending V1 work when an existing database first opens under V2.
    # The legacy table remains a one-version compatibility mirror.
    conn.execute(
        """
        INSERT OR IGNORE INTO cognitive_extraction_queue (
            attempt_id, extractor_version, status, retry_count, last_error,
            lease_token, created_at, updated_at
        )
        SELECT attempt_id, extractor_version, status, retry_count, last_error,
               lease_token, created_at, updated_at
        FROM cognitive_projection_queue
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO cognitive_projection_queue (
            attempt_id, status, retry_count, last_error, lease_token,
            extractor_version, created_at, updated_at
        )
        SELECT attempt_id, status, retry_count, last_error, lease_token,
               extractor_version, created_at, updated_at
        FROM cognitive_extraction_queue
        ORDER BY created_at, attempt_id, extractor_version
        """
    )
    # A V1 database may already contain completed immutable evidence and
    # snapshots, while having no topic-level queue or current-state table.
    # Seed one pending generation per historical topic/model so the V2
    # projector rebuilds ``cognitive_hypothesis_current`` on its next wake.
    # ``INSERT OR IGNORE`` keeps repeated opens idempotent and never dirties an
    # already-managed V2 topic.
    projection_version_placeholders = ", ".join(
        "?" for _ in _SUPPORTED_COGNITIVE_PROJECTION_VERSIONS
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO cognitive_topic_projection_queue (
            topic_id, model_version, status, requested_generation,
            claimed_generation, projected_generation, retry_count,
            last_error, lease_token, created_at, updated_at
        )
        SELECT DISTINCT topic_id, model_version, 'pending', 1, 0, 0, 0,
                        NULL, '', datetime('now'), datetime('now')
        FROM cognitive_hypothesis_snapshots
        WHERE model_version IN ({projection_version_placeholders})
        """,
        _SUPPORTED_COGNITIVE_PROJECTION_VERSIONS,
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO cognitive_topic_projection_queue (
            topic_id, model_version, status, requested_generation,
            claimed_generation, projected_generation, retry_count,
            last_error, lease_token, created_at, updated_at
        )
        SELECT DISTINCT evidence.topic_id, 'cognitive-v1', 'pending', 1, 0, 0, 0,
                        NULL, '', datetime('now'), datetime('now')
        FROM cognitive_evidence evidence
        WHERE NOT EXISTS (
            SELECT 1
            FROM cognitive_hypothesis_snapshots snapshots
            WHERE snapshots.topic_id = evidence.topic_id
        )
        """
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
        "CREATE INDEX IF NOT EXISTS idx_cognitive_projection_status_updated ON cognitive_projection_queue(status, updated_at, attempt_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognitive_extraction_status_updated ON cognitive_extraction_queue(status, updated_at, attempt_id, extractor_version)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognitive_topic_projection_status_updated ON cognitive_topic_projection_queue(status, updated_at, topic_id, model_version)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognitive_evidence_topic_hypothesis ON cognitive_evidence(topic_id, hypothesis_code, extractor_version, created_at, evidence_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognitive_evidence_root ON cognitive_evidence(topic_id, hypothesis_code, root_fact_seq, evidence_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognitive_fact_roots_source ON cognitive_fact_roots(fact_type, source_id, root_fact_seq)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognitive_snapshots_topic_model ON cognitive_hypothesis_snapshots(topic_id, model_version, computed_at DESC, snapshot_id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognitive_current_topic_model ON cognitive_hypothesis_current(topic_id, model_version, computed_at DESC, hypothesis_code)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognitive_controls_hypothesis ON cognitive_user_controls(topic_id, hypothesis_code, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognitive_controls_root ON cognitive_user_controls(topic_id, hypothesis_code, root_fact_seq DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognitive_interventions_hypothesis ON cognitive_intervention_events(topic_id, hypothesis_code, hypothesis_model_version, occurred_at, event_seq)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognitive_interventions_root ON cognitive_intervention_events(topic_id, hypothesis_code, hypothesis_model_version, root_fact_seq, event_seq)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognitive_interventions_decision ON cognitive_intervention_events(decision_id, occurred_at, event_seq)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognitive_outbox_status_lease ON cognitive_outbox(status, lease_expires_at, created_at, outbox_id)"
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
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_plans_one_active ON learning_plans(status) WHERE status = 'active'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_learning_plans_status_updated ON learning_plans(status, updated_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_learning_plan_items_topic ON learning_plan_items(topic_id, plan_id)"
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
