"""Read-only health snapshots for the cognitive-engine SQLite pipeline.

This module deliberately bypasses ``StudyStore``.  It opens an existing
database through SQLite's read-only URI mode, enables ``query_only``, and
selects only aggregate operational fields.  User answers, prompts, evidence
spans, payloads, and lease/claim tokens are never selected or returned.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from adaptive_learning.cognitive_versions import (
    get_cognitive_version_set,
    supported_cognitive_version_sets,
)

HealthStatus = Literal["healthy", "degraded", "blocked"]
RuntimeMode = Literal["disabled", "shadow", "active", "unknown"]

SNAPSHOT_SCHEMA_VERSION: Final[int] = 1
_QUEUE_LEASE_SECONDS: Final[int] = 300

_TABLE_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "cognitive_extraction_queue": frozenset(
        {
            "attempt_id",
            "extractor_version",
            "status",
            "retry_count",
            "last_error",
            "lease_token",
            "created_at",
            "updated_at",
        }
    ),
    "cognitive_topic_projection_queue": frozenset(
        {
            "topic_id",
            "model_version",
            "status",
            "requested_generation",
            "claimed_generation",
            "projected_generation",
            "retry_count",
            "last_error",
            "lease_token",
            "created_at",
            "updated_at",
        }
    ),
    "cognitive_outbox": frozenset(
        {
            "outbox_id",
            "operation",
            "payload_json",
            "status",
            "retry_count",
            "last_error",
            "lease_token",
            "lease_expires_at",
            "created_at",
            "updated_at",
        }
    ),
    "cognitive_monitoring_episodes": frozenset(
        {
            "episode_id",
            "model_version",
            "status",
            "due_by",
            "eligibility_until",
            "updated_at",
        }
    ),
    "cognitive_learning_obligations": frozenset(
        {
            "obligation_id",
            "episode_id",
            "obligation_type",
            "status",
            "due_by",
            "eligibility_until",
            "current_claim_id",
            "updated_at",
        }
    ),
    "cognitive_obligation_claims": frozenset(
        {
            "claim_id",
            "obligation_id",
            "claim_token",
            "worker_id",
            "status",
            "lease_expires_at",
        }
    ),
    "cognitive_user_controls": frozenset(
        {
            "control_id",
            "topic_id",
            "hypothesis_code",
            "action",
            "expires_at",
            "root_fact_seq",
            "created_at",
        }
    ),
}

_STATUS_VALUES: Final[dict[str, tuple[str, ...]]] = {
    "cognitive_extraction_queue": ("pending", "processing", "done", "failed"),
    "cognitive_topic_projection_queue": (
        "pending",
        "processing",
        "done",
        "failed",
    ),
    "cognitive_outbox": ("pending", "processing", "done", "failed", "discarded"),
    "cognitive_monitoring_episodes": (
        "open",
        "paused",
        "resolved",
        "relapsed",
        "expired",
        "cancelled",
    ),
    "cognitive_learning_obligations": (
        "pending",
        "claimed",
        "paused",
        "completed",
        "cancelled",
    ),
    "cognitive_obligation_claims": (
        "active",
        "released",
        "completed",
        "superseded",
    ),
}


class CognitiveHealthError(RuntimeError):
    """The health tool could not safely inspect the requested database."""


@dataclass(frozen=True, slots=True)
class HealthReason:
    code: str
    severity: Literal["degraded", "blocked"]
    count: int
    resources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CognitiveHealthSnapshot:
    schema_version: int
    as_of: str
    runtime: dict[str, str]
    health: dict[str, Any]
    schema: dict[str, Any]
    queues: dict[str, Any]
    outbox: dict[str, Any]
    retention: dict[str, Any]
    controls: dict[str, int]
    versions: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise CognitiveHealthError("as_of must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _open_read_only(database: Path) -> sqlite3.Connection:
    resolved = database.expanduser().resolve()
    if not resolved.is_file():
        raise CognitiveHealthError("database file does not exist")
    try:
        conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        query_only = int(conn.execute("PRAGMA query_only").fetchone()[0])
        if query_only != 1:
            conn.close()
            raise CognitiveHealthError("SQLite query_only could not be enabled")
        return conn
    except sqlite3.Error as exc:
        raise CognitiveHealthError("database could not be opened read-only") from exc


def _schema_state(conn: sqlite3.Connection) -> tuple[list[str], dict[str, list[str]]]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    available = {str(row["name"]) for row in rows}
    missing_tables = sorted(set(_TABLE_COLUMNS) - available)
    missing_columns: dict[str, list[str]] = {}
    for table, expected in _TABLE_COLUMNS.items():
        if table not in available:
            continue
        columns = {
            str(row["name"])
            for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        missing = sorted(expected - columns)
        if missing:
            missing_columns[table] = missing
    return missing_tables, missing_columns


def _status_counts(conn: sqlite3.Connection, table: str) -> tuple[dict[str, int], int]:
    known = _STATUS_VALUES[table]
    counts = {status: 0 for status in known}
    unknown = 0
    for row in conn.execute(
        f'SELECT status, COUNT(*) AS count FROM "{table}" GROUP BY status'
    ).fetchall():
        status = str(row["status"] or "")
        count = int(row["count"] or 0)
        if status in counts:
            counts[status] = count
        else:
            unknown += count
    return counts, unknown


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row is not None else 0


def _oldest_actionable(conn: sqlite3.Connection, table: str) -> str | None:
    row = conn.execute(
        f"""SELECT created_at FROM "{table}"
        WHERE status IN ('pending', 'processing', 'failed')
        ORDER BY julianday(created_at), created_at LIMIT 1"""
    ).fetchone()
    return None if row is None else str(row["created_at"])


def _supported_components() -> tuple[set[str], set[str], tuple[str, ...]]:
    names = supported_cognitive_version_sets()
    extractor_versions: set[str] = set()
    projection_versions: set[str] = set()
    for name in names:
        version_set = get_cognitive_version_set(name)
        if version_set is not None:
            extractor_versions.add(version_set.extractor_version)
            projection_versions.add(version_set.projection_version)
    return extractor_versions, projection_versions, names


def _empty_sections() -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, int], dict[str, Any]
]:
    queues = {
        "extraction": {
            "counts": {status: 0 for status in _STATUS_VALUES["cognitive_extraction_queue"]},
            "oldest_actionable_at": None,
            "expired_leases": 0,
            "max_retry_count": 0,
        },
        "topic_projection": {
            "counts": {
                status: 0
                for status in _STATUS_VALUES["cognitive_topic_projection_queue"]
            },
            "oldest_actionable_at": None,
            "expired_leases": 0,
            "stale": 0,
            "generation_gap_total": 0,
            "max_generation_gap": 0,
            "max_retry_count": 0,
        },
    }
    outbox = {
        "counts": {status: 0 for status in _STATUS_VALUES["cognitive_outbox"]},
        "oldest_actionable_at": None,
        "expired_leases": 0,
        "max_retry_count": 0,
        "error_categories": {},
    }
    retention = {
        "episodes": {
            "counts": {
                status: 0
                for status in _STATUS_VALUES["cognitive_monitoring_episodes"]
            }
        },
        "obligations": {
            "counts": {
                status: 0
                for status in _STATUS_VALUES["cognitive_learning_obligations"]
            },
            "overdue": 0,
            "duplicate_active": 0,
            "orphaned": 0,
        },
        "claims": {
            "counts": {
                status: 0
                for status in _STATUS_VALUES["cognitive_obligation_claims"]
            },
            "expired": 0,
            "orphaned": 0,
            "ownership_mismatches": 0,
        },
    }
    controls = {"dismiss": 0, "suppress": 0, "delete": 0}
    versions = {
        "supported_version_sets": [],
        "observed_extractor_versions": [],
        "observed_projection_versions": [],
        "unsupported": 0,
    }
    return queues, outbox, retention, controls, versions


def collect_cognitive_health(
    database: str | Path,
    *,
    as_of: datetime | str | None = None,
) -> CognitiveHealthSnapshot:
    """Collect one aggregate snapshot without mutating or migrating ``database``."""

    observed_at = _utc(as_of)
    observed_iso = _iso(observed_at)
    conn = _open_read_only(Path(database))
    reasons: list[HealthReason] = []
    queues, outbox, retention, controls, versions = _empty_sections()
    try:
        conn.execute("BEGIN")
        missing_tables, missing_columns = _schema_state(conn)
        if missing_tables:
            reasons.append(
                HealthReason(
                    code="schema_missing_table",
                    severity="blocked",
                    count=len(missing_tables),
                    resources=tuple(missing_tables),
                )
            )
        if missing_columns:
            resources = tuple(
                f"{table}.{column}"
                for table in sorted(missing_columns)
                for column in missing_columns[table]
            )
            reasons.append(
                HealthReason(
                    code="schema_missing_column",
                    severity="blocked",
                    count=len(resources),
                    resources=resources,
                )
            )

        valid_tables = {
            table
            for table in _TABLE_COLUMNS
            if table not in missing_tables and table not in missing_columns
        }

        unknown_statuses = 0
        extraction = "cognitive_extraction_queue"
        if extraction in valid_tables:
            counts, unknown = _status_counts(conn, extraction)
            unknown_statuses += unknown
            queues["extraction"].update(
                counts=counts,
                oldest_actionable_at=_oldest_actionable(conn, extraction),
                expired_leases=_scalar(
                    conn,
                    """SELECT COUNT(*) FROM cognitive_extraction_queue
                    WHERE status = 'processing'
                      AND (
                        julianday(updated_at) IS NULL
                        OR julianday(updated_at) <= julianday(?) - (? / 86400.0)
                      )""",
                    (observed_iso, _QUEUE_LEASE_SECONDS),
                ),
                max_retry_count=_scalar(
                    conn,
                    "SELECT COALESCE(MAX(retry_count), 0) FROM cognitive_extraction_queue",
                ),
            )

        projection = "cognitive_topic_projection_queue"
        if projection in valid_tables:
            counts, unknown = _status_counts(conn, projection)
            unknown_statuses += unknown
            queues["topic_projection"].update(
                counts=counts,
                oldest_actionable_at=_oldest_actionable(conn, projection),
                expired_leases=_scalar(
                    conn,
                    """SELECT COUNT(*) FROM cognitive_topic_projection_queue
                    WHERE status = 'processing'
                      AND (
                        julianday(updated_at) IS NULL
                        OR julianday(updated_at) <= julianday(?) - (? / 86400.0)
                      )""",
                    (observed_iso, _QUEUE_LEASE_SECONDS),
                ),
                stale=_scalar(
                    conn,
                    """SELECT COUNT(*) FROM cognitive_topic_projection_queue
                    WHERE requested_generation > projected_generation""",
                ),
                generation_gap_total=_scalar(
                    conn,
                    """SELECT COALESCE(SUM(
                        MAX(requested_generation - projected_generation, 0)
                    ), 0) FROM cognitive_topic_projection_queue""",
                ),
                max_generation_gap=_scalar(
                    conn,
                    """SELECT COALESCE(MAX(
                        MAX(requested_generation - projected_generation, 0)
                    ), 0) FROM cognitive_topic_projection_queue""",
                ),
                max_retry_count=_scalar(
                    conn,
                    """SELECT COALESCE(MAX(retry_count), 0)
                    FROM cognitive_topic_projection_queue""",
                ),
            )
            invalid_generations = _scalar(
                conn,
                """SELECT COUNT(*) FROM cognitive_topic_projection_queue
                WHERE projected_generation > requested_generation
                   OR claimed_generation > requested_generation""",
            )
            if invalid_generations:
                reasons.append(
                    HealthReason(
                        "projection_generation_invalid", "blocked", invalid_generations
                    )
                )

        outbox_table = "cognitive_outbox"
        if outbox_table in valid_tables:
            counts, unknown = _status_counts(conn, outbox_table)
            unknown_statuses += unknown
            categories: dict[str, int] = {}
            for row in conn.execute(
                """SELECT
                    CASE
                        WHEN last_error IS NULL OR TRIM(last_error) = ''
                            THEN 'unspecified'
                        WHEN LOWER(last_error) LIKE '%timeout%'
                          OR LOWER(last_error) LIKE '%timed out%'
                            THEN 'timeout'
                        WHEN LOWER(last_error) LIKE '%connect%'
                          OR LOWER(last_error) LIKE '%network%'
                          OR LOWER(last_error) LIKE '%dns%'
                          OR LOWER(last_error) LIKE '%socket%'
                            THEN 'network'
                        WHEN LOWER(last_error) LIKE '%auth%'
                          OR LOWER(last_error) LIKE '%permission%'
                          OR LOWER(last_error) LIKE '%forbidden%'
                          OR LOWER(last_error) LIKE '%unauthorized%'
                            THEN 'authorization'
                        WHEN LOWER(last_error) LIKE '%schema%'
                          OR LOWER(last_error) LIKE '%validation%'
                          OR LOWER(last_error) LIKE '%invalid%'
                          OR LOWER(last_error) LIKE '%malformed%'
                            THEN 'validation'
                        WHEN LOWER(last_error) LIKE '%lease%'
                          OR LOWER(last_error) LIKE '%stale%'
                            THEN 'lease'
                        WHEN LOWER(last_error) LIKE '%unavailable%'
                          OR LOWER(last_error) LIKE '%overload%'
                            THEN 'unavailable'
                        ELSE 'other'
                    END AS category,
                    COUNT(*) AS count
                FROM cognitive_outbox
                WHERE status IN ('failed', 'discarded')
                GROUP BY category"""
            ).fetchall():
                categories[str(row["category"])] = int(row["count"])
            outbox.update(
                counts=counts,
                oldest_actionable_at=_oldest_actionable(conn, outbox_table),
                expired_leases=_scalar(
                    conn,
                    """SELECT COUNT(*) FROM cognitive_outbox
                    WHERE status = 'processing'
                      AND (
                        lease_expires_at = ''
                        OR julianday(lease_expires_at) IS NULL
                        OR julianday(lease_expires_at) <= julianday(?)
                      )""",
                    (observed_iso,),
                ),
                max_retry_count=_scalar(
                    conn, "SELECT COALESCE(MAX(retry_count), 0) FROM cognitive_outbox"
                ),
                error_categories=dict(sorted(categories.items())),
            )

        episodes_table = "cognitive_monitoring_episodes"
        if episodes_table in valid_tables:
            counts, unknown = _status_counts(conn, episodes_table)
            unknown_statuses += unknown
            retention["episodes"]["counts"] = counts

        obligations_table = "cognitive_learning_obligations"
        if obligations_table in valid_tables:
            counts, unknown = _status_counts(conn, obligations_table)
            unknown_statuses += unknown
            retention["obligations"].update(
                counts=counts,
                overdue=_scalar(
                    conn,
                    """SELECT COUNT(*) FROM cognitive_learning_obligations
                    WHERE status IN ('pending', 'claimed')
                      AND (
                        julianday(due_by) IS NULL
                        OR julianday(due_by) < julianday(?)
                      )""",
                    (observed_iso,),
                ),
                duplicate_active=_scalar(
                    conn,
                    """SELECT COUNT(*) FROM (
                        SELECT episode_id
                        FROM cognitive_learning_obligations
                        WHERE obligation_type = 'retention'
                          AND status IN ('pending', 'claimed', 'paused')
                        GROUP BY episode_id HAVING COUNT(*) > 1
                    )""",
                ),
            )
            if episodes_table in valid_tables:
                retention["obligations"]["orphaned"] = _scalar(
                    conn,
                    """SELECT COUNT(*) FROM cognitive_learning_obligations obligations
                    LEFT JOIN cognitive_monitoring_episodes episodes
                      ON episodes.episode_id = obligations.episode_id
                    WHERE episodes.episode_id IS NULL""",
                )

        claims_table = "cognitive_obligation_claims"
        if claims_table in valid_tables:
            counts, unknown = _status_counts(conn, claims_table)
            unknown_statuses += unknown
            retention["claims"].update(
                counts=counts,
                expired=_scalar(
                    conn,
                    """SELECT COUNT(*) FROM cognitive_obligation_claims
                    WHERE status = 'active'
                      AND (
                        julianday(lease_expires_at) IS NULL
                        OR julianday(lease_expires_at) <= julianday(?)
                      )""",
                    (observed_iso,),
                ),
            )
            if obligations_table in valid_tables:
                retention["claims"].update(
                    orphaned=_scalar(
                        conn,
                        """SELECT COUNT(*) FROM cognitive_obligation_claims claims
                        LEFT JOIN cognitive_learning_obligations obligations
                          ON obligations.obligation_id = claims.obligation_id
                        WHERE obligations.obligation_id IS NULL""",
                    ),
                    ownership_mismatches=_scalar(
                        conn,
                        """SELECT COUNT(*) FROM cognitive_obligation_claims claims
                        JOIN cognitive_learning_obligations obligations
                          ON obligations.obligation_id = claims.obligation_id
                        WHERE claims.status = 'active'
                          AND (
                            obligations.status != 'claimed'
                            OR obligations.current_claim_id != claims.claim_id
                          )""",
                    ),
                )

        controls_table = "cognitive_user_controls"
        if controls_table in valid_tables:
            rows = conn.execute(
                """WITH ranked AS (
                    SELECT action, expires_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY topic_id, hypothesis_code
                               ORDER BY root_fact_seq DESC, created_at DESC, control_id DESC
                           ) AS position
                    FROM cognitive_user_controls
                )
                SELECT action, COUNT(*) AS count
                FROM ranked
                WHERE position = 1
                  AND action IN ('dismiss', 'suppress', 'delete')
                  AND (
                    action != 'suppress'
                    OR expires_at = ''
                    OR julianday(expires_at) > julianday(?)
                  )
                GROUP BY action""",
                (observed_iso,),
            ).fetchall()
            for row in rows:
                controls[str(row["action"])] = int(row["count"])

        supported_extractors, supported_projections, version_sets = (
            _supported_components()
        )
        observed_extractors: set[str] = set()
        observed_projections: set[str] = set()
        if extraction in valid_tables:
            observed_extractors.update(
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT extractor_version FROM cognitive_extraction_queue"
                ).fetchall()
                if str(row[0] or "")
            )
        for table, column in (
            (projection, "model_version"),
            (episodes_table, "model_version"),
        ):
            if table in valid_tables:
                observed_projections.update(
                    str(row[0])
                    for row in conn.execute(
                        f'SELECT DISTINCT "{column}" FROM "{table}"'
                    ).fetchall()
                    if str(row[0] or "")
                )
        unsupported = (observed_extractors - supported_extractors) | (
            observed_projections - supported_projections
        )
        versions.update(
            supported_version_sets=list(version_sets),
            observed_extractor_versions=sorted(observed_extractors),
            observed_projection_versions=sorted(observed_projections),
            unsupported=len(unsupported),
        )

        if unknown_statuses:
            reasons.append(
                HealthReason("unknown_status", "blocked", unknown_statuses)
            )
        if unsupported:
            reasons.append(
                HealthReason("unsupported_version", "blocked", len(unsupported))
            )

        invariant_counts = {
            "duplicate_active_obligation": retention["obligations"][
                "duplicate_active"
            ],
            "orphan_obligation": retention["obligations"]["orphaned"],
            "orphan_claim": retention["claims"]["orphaned"],
            "claim_ownership_mismatch": retention["claims"][
                "ownership_mismatches"
            ],
        }
        for code, count in invariant_counts.items():
            if count:
                reasons.append(HealthReason(code, "blocked", int(count)))

        degraded_counts = {
            "extraction_failed": queues["extraction"]["counts"]["failed"],
            "extraction_lease_expired": queues["extraction"]["expired_leases"],
            "projection_stale": queues["topic_projection"]["stale"],
            "projection_failed": queues["topic_projection"]["counts"]["failed"],
            "projection_lease_expired": queues["topic_projection"][
                "expired_leases"
            ],
            "outbox_failed": outbox["counts"]["failed"],
            "outbox_discarded": outbox["counts"]["discarded"],
            "outbox_lease_expired": outbox["expired_leases"],
            "obligation_overdue": retention["obligations"]["overdue"],
            "claim_lease_expired": retention["claims"]["expired"],
        }
        for code, count in degraded_counts.items():
            if count:
                reasons.append(HealthReason(code, "degraded", int(count)))

        conn.rollback()
    except sqlite3.Error as exc:
        raise CognitiveHealthError("database inspection failed") from exc
    finally:
        conn.close()

    reasons.sort(key=lambda item: (item.severity != "blocked", item.code))
    status: HealthStatus = "healthy"
    if any(reason.severity == "blocked" for reason in reasons):
        status = "blocked"
    elif reasons:
        status = "degraded"

    return CognitiveHealthSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        as_of=observed_iso,
        runtime={"mode": "unknown", "source": "offline_database"},
        health={
            "status": status,
            "reasons": [asdict(reason) for reason in reasons],
        },
        schema={
            "missing_tables": missing_tables,
            "missing_columns": missing_columns,
        },
        queues=queues,
        outbox=outbox,
        retention=retention,
        controls=controls,
        versions=versions,
    )


__all__ = [
    "CognitiveHealthError",
    "CognitiveHealthSnapshot",
    "HealthReason",
    "collect_cognitive_health",
]
