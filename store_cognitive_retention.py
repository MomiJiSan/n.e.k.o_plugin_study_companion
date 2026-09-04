"""Persistence primitives for bounded cognitive retention monitoring.

This module intentionally owns only cognitive monitoring episodes and their
obligations.  It never reads or writes FSRS scheduling state.  Public methods
are shaped for binding onto :class:`StudyStore`; ``insert_*``/``apply_*``
helpers that accept a connection participate in a caller-owned transaction.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

ACTIVE_RETENTION_HYPOTHESIS = "omit_inner_derivative"
RETENTION_DELAY = timedelta(hours=24)
RETENTION_DUE = timedelta(hours=72)
RETENTION_ELIGIBILITY = timedelta(days=7)
RETENTION_FREQUENCY = timedelta(hours=24)
RELAPSE_COOLDOWN = timedelta(hours=24)

_EPISODE_TERMINAL = frozenset({"resolved", "relapsed", "expired", "cancelled"})
_ACTIVE_OBLIGATION_STATUSES = frozenset({"pending", "claimed", "paused"})
_RETENTION_DISPOSITIONS = frozenset(
    {"resolved", "relapse", "reschedule", "ordinary_evidence"}
)


COGNITIVE_RETENTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS cognitive_monitoring_episodes (
    episode_id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL,
    topic_id TEXT NOT NULL REFERENCES topics(id),
    hypothesis_code TEXT NOT NULL,
    model_version TEXT NOT NULL,
    source_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    source_event_id TEXT NOT NULL DEFAULT '',
    transfer_question_family_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'open', 'paused', 'resolved', 'relapsed', 'expired', 'cancelled'
    )),
    opened_at TEXT NOT NULL,
    not_before TEXT NOT NULL,
    due_by TEXT NOT NULL,
    eligibility_until TEXT NOT NULL,
    last_retention_at TEXT NOT NULL DEFAULT '',
    relapse_count INTEGER NOT NULL DEFAULT 0 CHECK(relapse_count >= 0),
    resolved_at TEXT NOT NULL DEFAULT '',
    expired_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(hypothesis_id, model_version, source_attempt_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_cognitive_open_episode
ON cognitive_monitoring_episodes(hypothesis_id, model_version)
WHERE status IN ('open', 'paused');

CREATE TABLE IF NOT EXISTS cognitive_learning_obligations (
    obligation_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES cognitive_monitoring_episodes(episode_id),
    hypothesis_id TEXT NOT NULL,
    topic_id TEXT NOT NULL REFERENCES topics(id),
    hypothesis_code TEXT NOT NULL,
    obligation_type TEXT NOT NULL CHECK(obligation_type IN (
        'retention', 'transfer_check'
    )),
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'claimed', 'paused', 'completed', 'cancelled'
    )),
    not_before TEXT NOT NULL,
    due_by TEXT NOT NULL,
    eligibility_until TEXT NOT NULL,
    current_claim_id TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 1 CHECK(generation > 0),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    UNIQUE(episode_id, obligation_type, generation)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_cognitive_active_retention_obligation
ON cognitive_learning_obligations(episode_id)
WHERE obligation_type = 'retention'
  AND status IN ('pending', 'claimed', 'paused');

CREATE TABLE IF NOT EXISTS cognitive_obligation_claims (
    claim_id TEXT PRIMARY KEY,
    obligation_id TEXT NOT NULL REFERENCES cognitive_learning_obligations(obligation_id),
    claim_token TEXT NOT NULL UNIQUE,
    worker_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'active', 'released', 'completed', 'superseded'
    )),
    claimed_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_cognitive_active_obligation_claim
ON cognitive_obligation_claims(obligation_id)
WHERE status = 'active';

CREATE TABLE IF NOT EXISTS cognitive_obligation_satisfactions (
    satisfaction_id TEXT PRIMARY KEY,
    obligation_id TEXT NOT NULL REFERENCES cognitive_learning_obligations(obligation_id),
    episode_id TEXT NOT NULL REFERENCES cognitive_monitoring_episodes(episode_id),
    claim_id TEXT NOT NULL REFERENCES cognitive_obligation_claims(claim_id),
    attempt_id TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK(disposition IN (
        'resolved', 'relapse', 'reschedule', 'ordinary_evidence', 'completed'
    )),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(attempt_id),
    UNIQUE(claim_id)
);

CREATE TABLE IF NOT EXISTS cognitive_monitoring_episode_facts (
    fact_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES cognitive_monitoring_episodes(episode_id),
    fact_type TEXT NOT NULL CHECK(fact_type IN ('expired')),
    root_fact_seq INTEGER NOT NULL REFERENCES cognitive_fact_roots(root_fact_seq),
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(episode_id, fact_type)
);

CREATE INDEX IF NOT EXISTS idx_cognitive_episode_facts_root
ON cognitive_monitoring_episode_facts(root_fact_seq, fact_id);
"""


def create_cognitive_retention_schema(conn: sqlite3.Connection) -> None:
    """Create the additive V2.1 tables on an existing SQLite connection."""

    conn.executescript(COGNITIVE_RETENTION_SCHEMA)


def _utc(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{field} is required")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _now(value: object | None = None) -> datetime:
    return datetime.now(timezone.utc) if value is None else _utc(value, "now")


def _required(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _positive_seconds(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be a positive number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field} must be a positive number")
    return number


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"


def _json_dumps(self: Any, value: Mapping[str, Any]) -> str:
    serializer = getattr(self, "_json_dumps", None)
    if callable(serializer):
        return str(serializer(dict(value)))
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {key: row[key] for key in row.keys()}


def _transaction(self: Any, operation: Any) -> Any:
    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = operation(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return result


def _normalize_transfer(transfer: Mapping[str, Any]) -> dict[str, str]:
    hypothesis_code = _required(transfer.get("hypothesis_code"), "hypothesis_code")
    if hypothesis_code != ACTIVE_RETENTION_HYPOTHESIS:
        raise ValueError("retention is active only for omit_inner_derivative")
    verdict = str(
        transfer.get("evaluation_verdict", transfer.get("verdict", "")) or ""
    ).strip()
    if verdict != "correct":
        raise ValueError("monitoring episode requires a correct transfer result")
    if transfer.get("certified") is not True:
        raise ValueError("monitoring episode requires certified transfer evidence")
    if transfer.get("used_hint") is not False:
        raise ValueError("monitoring episode requires an unassisted transfer result")
    opened_at = _utc(
        transfer.get("occurred_at", transfer.get("answered_at")), "occurred_at"
    )
    attempt_id = _required(
        transfer.get("source_attempt_id", transfer.get("attempt_id")),
        "source_attempt_id",
    )
    hypothesis_id = _required(transfer.get("hypothesis_id"), "hypothesis_id")
    model_version = _required(transfer.get("model_version"), "model_version")
    episode_id = str(transfer.get("episode_id") or "").strip() or _stable_id(
        "cognitive-episode", hypothesis_id, model_version, attempt_id
    )
    return {
        "episode_id": episode_id,
        "hypothesis_id": hypothesis_id,
        "topic_id": _required(transfer.get("topic_id"), "topic_id"),
        "hypothesis_code": hypothesis_code,
        "model_version": model_version,
        "source_attempt_id": attempt_id,
        "source_event_id": str(transfer.get("source_event_id") or "").strip(),
        "transfer_question_family_id": _required(
            transfer.get("question_family_id"), "question_family_id"
        ),
        "opened_at": _iso(opened_at),
    }


def _obligation_row(conn: sqlite3.Connection, obligation_id: str) -> dict[str, Any]:
    result = _row(
        conn.execute(
            "SELECT * FROM cognitive_learning_obligations WHERE obligation_id = ?",
            (obligation_id,),
        ).fetchone()
    )
    if result is None:
        raise RuntimeError("cognitive obligation write failed")
    return result


def _episode_row(conn: sqlite3.Connection, episode_id: str) -> dict[str, Any]:
    result = _row(
        conn.execute(
            "SELECT * FROM cognitive_monitoring_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
    )
    if result is None:
        raise RuntimeError("cognitive monitoring episode write failed")
    return result


def _mark_retention_projection_dirty(
    self: Any,
    conn: sqlite3.Connection,
    episode: Mapping[str, Any],
) -> int:
    """Request one reducer fold in the same retention fact transaction."""

    table = conn.execute(
        """SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'cognitive_topic_projection_queue'"""
    ).fetchone()
    if table is None:
        # Lightweight storage-contract fixtures intentionally omit the V2
        # reducer queue. Production StudyStore always creates it.
        return 0
    from .store_cognitive import _mark_topic_dirty

    return _mark_topic_dirty(
        conn,
        topic_id=str(episode["topic_id"]),
        model_version=str(episode["model_version"]),
    )


def _insert_obligation(
    conn: sqlite3.Connection,
    *,
    episode: Mapping[str, Any],
    obligation_type: str,
    not_before: datetime,
    due_by: datetime,
    eligibility_until: datetime,
    generation: int,
    reason: str,
    now: datetime,
    paused: bool = False,
) -> dict[str, Any]:
    obligation_id = _stable_id(
        "cognitive-obligation",
        str(episode["episode_id"]),
        obligation_type,
        str(generation),
    )
    stored = {
        "obligation_id": obligation_id,
        "episode_id": str(episode["episode_id"]),
        "hypothesis_id": str(episode["hypothesis_id"]),
        "topic_id": str(episode["topic_id"]),
        "hypothesis_code": str(episode["hypothesis_code"]),
        "obligation_type": obligation_type,
        "status": "paused" if paused else "pending",
        "not_before": _iso(not_before),
        "due_by": _iso(due_by),
        "eligibility_until": _iso(eligibility_until),
        "generation": generation,
        "reason": reason,
        "created_at": _iso(now),
        "updated_at": _iso(now),
    }
    existing = conn.execute(
        "SELECT * FROM cognitive_learning_obligations WHERE obligation_id = ?",
        (obligation_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO cognitive_learning_obligations (
                obligation_id, episode_id, hypothesis_id, topic_id,
                hypothesis_code, obligation_type, status, not_before,
                due_by, eligibility_until, generation, reason,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(stored.values()),
        )
    else:
        immutable = (
            "episode_id",
            "hypothesis_id",
            "topic_id",
            "hypothesis_code",
            "obligation_type",
            "not_before",
            "due_by",
            "eligibility_until",
            "generation",
        )
        if any(existing[key] != stored[key] for key in immutable):
            raise ValueError("cognitive obligation identity collision")
    return _obligation_row(conn, obligation_id)


def insert_certified_transfer_episode(
    self: Any,
    conn: sqlite3.Connection,
    transfer: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically create an episode and its first retention obligation.

    The caller must keep this operation in the same transaction as the
    certified transfer outcome when that outcome is first persisted.
    """

    item = _normalize_transfer(transfer)
    opened_at = _utc(item["opened_at"], "opened_at")
    existing = conn.execute(
        "SELECT * FROM cognitive_monitoring_episodes WHERE episode_id = ?",
        (item["episode_id"],),
    ).fetchone()
    if existing is None:
        conflicting = conn.execute(
            """
            SELECT episode_id FROM cognitive_monitoring_episodes
            WHERE hypothesis_id = ? AND model_version = ?
              AND status IN ('open', 'paused')
            LIMIT 1
            """,
            (item["hypothesis_id"], item["model_version"]),
        ).fetchone()
        if conflicting is not None:
            raise ValueError("hypothesis already has an active monitoring episode")
        conn.execute(
            """
            INSERT INTO cognitive_monitoring_episodes (
                episode_id, hypothesis_id, topic_id, hypothesis_code,
                model_version, source_attempt_id, source_event_id,
                transfer_question_family_id, status, opened_at, not_before,
                due_by, eligibility_until, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
            """,
            (
                item["episode_id"],
                item["hypothesis_id"],
                item["topic_id"],
                item["hypothesis_code"],
                item["model_version"],
                item["source_attempt_id"],
                item["source_event_id"],
                item["transfer_question_family_id"],
                item["opened_at"],
                _iso(opened_at + RETENTION_DELAY),
                _iso(opened_at + RETENTION_DUE),
                _iso(opened_at + RETENTION_ELIGIBILITY),
                item["opened_at"],
            ),
        )
    else:
        immutable = (
            "hypothesis_id",
            "topic_id",
            "hypothesis_code",
            "model_version",
            "source_attempt_id",
            "source_event_id",
            "transfer_question_family_id",
            "opened_at",
        )
        if any(existing[key] != item[key] for key in immutable):
            raise ValueError("cognitive monitoring episode identity collision")

    episode = _episode_row(conn, item["episode_id"])
    obligation = _insert_obligation(
        conn,
        episode=episode,
        obligation_type="retention",
        not_before=opened_at + RETENTION_DELAY,
        due_by=opened_at + RETENTION_DUE,
        eligibility_until=opened_at + RETENTION_ELIGIBILITY,
        generation=1,
        reason="certified_transfer_success",
        now=opened_at,
    )
    return {"episode": episode, "obligation": obligation}


def record_certified_transfer_success(
    self: Any, transfer: Mapping[str, Any]
) -> dict[str, Any]:
    return _transaction(
        self, lambda conn: insert_certified_transfer_episode(self, conn, transfer)
    )


def rebuild_cognitive_retention_from_transfers(
    self: Any, transfers: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Idempotently materialize episodes without replacing terminal rows."""

    def operation(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [insert_certified_transfer_episode(self, conn, item) for item in transfers]

    return _transaction(self, operation)


def _supersede_claim(
    conn: sqlite3.Connection, claim_id: str, *, finished_at: str
) -> None:
    if claim_id:
        conn.execute(
            """
            UPDATE cognitive_obligation_claims
            SET status = 'superseded', finished_at = ?
            WHERE claim_id = ? AND status = 'active'
            """,
            (finished_at, claim_id),
        )


def _record_episode_expired_fact(
    conn: sqlite3.Connection,
    *,
    episode_id: str,
    occurred_at: str,
) -> dict[str, Any]:
    """Append the immutable lifecycle fact paired with an episode expiry."""

    fact_id = f"cognitive-episode-fact:{episode_id}:expired"
    conn.execute(
        """
        INSERT OR IGNORE INTO cognitive_fact_roots (
            fact_type, source_id, effective_at, recorded_at
        ) VALUES ('cognitive_episode', ?, ?, ?)
        """,
        (fact_id, occurred_at, occurred_at),
    )
    root = conn.execute(
        """
        SELECT root_fact_seq FROM cognitive_fact_roots
        WHERE fact_type = 'cognitive_episode' AND source_id = ?
        """,
        (fact_id,),
    ).fetchone()
    if root is None or int(root["root_fact_seq"] or 0) <= 0:
        raise RuntimeError("cognitive episode fact root allocation failed")
    conn.execute(
        """
        INSERT OR IGNORE INTO cognitive_monitoring_episode_facts (
            fact_id, episode_id, fact_type, root_fact_seq,
            occurred_at, created_at
        ) VALUES (?, ?, 'expired', ?, ?, ?)
        """,
        (fact_id, episode_id, int(root["root_fact_seq"]), occurred_at, occurred_at),
    )
    row = conn.execute(
        "SELECT * FROM cognitive_monitoring_episode_facts WHERE fact_id = ?",
        (fact_id,),
    ).fetchone()
    result = _row(row)
    if result is None:
        raise RuntimeError("cognitive episode expiry fact insert failed")
    return result


def _expire_episodes(
    self: Any, conn: sqlite3.Connection, *, as_of: datetime
) -> list[dict[str, Any]]:
    as_of_text = _iso(as_of)
    rows = conn.execute(
        """
        SELECT * FROM cognitive_monitoring_episodes
        WHERE status IN ('open', 'paused') AND eligibility_until < ?
        ORDER BY eligibility_until, episode_id
        """,
        (as_of_text,),
    ).fetchall()
    created: list[dict[str, Any]] = []
    for raw in rows:
        episode = _row(raw)
        assert episode is not None
        active_suppress = False
        if episode["status"] == "paused":
            active_suppress = (
                conn.execute(
                    """
                    SELECT 1 FROM cognitive_user_controls controls
                    WHERE controls.topic_id = ?
                      AND controls.hypothesis_code = ?
                      AND controls.action = 'suppress'
                      AND controls.expires_at != ''
                      AND julianday(controls.expires_at) > julianday(?)
                      AND controls.rowid = (
                          SELECT latest.rowid
                          FROM cognitive_user_controls latest
                          WHERE latest.topic_id = controls.topic_id
                            AND latest.hypothesis_code = controls.hypothesis_code
                          ORDER BY latest.root_fact_seq DESC, latest.rowid DESC
                          LIMIT 1
                      )
                    """,
                    (episode["topic_id"], episode["hypothesis_code"], as_of_text),
                ).fetchone()
                is not None
            )
        active = conn.execute(
            """
            SELECT obligation_id, current_claim_id
            FROM cognitive_learning_obligations
            WHERE episode_id = ? AND status IN ('pending', 'claimed', 'paused')
            """,
            (episode["episode_id"],),
        ).fetchall()
        for obligation in active:
            _supersede_claim(
                conn, str(obligation["current_claim_id"]), finished_at=as_of_text
            )
        conn.execute(
            """
            UPDATE cognitive_learning_obligations
            SET status = 'cancelled', current_claim_id = '',
                reason = 'monitoring_window_expired', updated_at = ?
            WHERE episode_id = ? AND status IN ('pending', 'claimed', 'paused')
            """,
            (as_of_text, episode["episode_id"]),
        )
        changed = conn.execute(
            """
            UPDATE cognitive_monitoring_episodes
            SET status = 'expired', expired_at = ?, updated_at = ?
            WHERE episode_id = ? AND status IN ('open', 'paused')
            """,
            (as_of_text, as_of_text, episode["episode_id"]),
        ).rowcount
        if not changed:
            continue
        _record_episode_expired_fact(
            conn,
            episode_id=str(episode["episode_id"]),
            occurred_at=as_of_text,
        )
        episode = _episode_row(conn, str(episode["episode_id"]))
        if not active_suppress:
            created.append(
                _insert_obligation(
                    conn,
                    episode=episode,
                    obligation_type="transfer_check",
                    not_before=as_of,
                    due_by=as_of + RETENTION_DUE,
                    eligibility_until=as_of + RETENTION_ELIGIBILITY,
                    generation=1,
                    reason="monitoring_window_expired",
                    now=as_of,
                )
            )
        _mark_retention_projection_dirty(self, conn, episode)
    return created


def expire_cognitive_monitoring_episodes(
    self: Any, *, as_of: object | None = None
) -> list[dict[str, Any]]:
    instant = _now(as_of)
    return _transaction(
        self, lambda conn: _expire_episodes(self, conn, as_of=instant)
    )


def _resume_expired_suppressions(
    conn: sqlite3.Connection, *, as_of: datetime
) -> int:
    """Resume paused obligations when the latest suppress fact has expired."""

    as_of_text = _iso(as_of)
    rows = conn.execute(
        """
        SELECT episodes.episode_id
        FROM cognitive_monitoring_episodes episodes
        JOIN cognitive_user_controls controls
          ON controls.topic_id = episodes.topic_id
         AND controls.hypothesis_code = episodes.hypothesis_code
        WHERE episodes.status = 'paused'
          AND controls.action = 'suppress'
          AND controls.expires_at != ''
          AND controls.expires_at <= ?
          AND controls.rowid = (
              SELECT latest.rowid
              FROM cognitive_user_controls latest
              WHERE latest.topic_id = episodes.topic_id
                AND latest.hypothesis_code = episodes.hypothesis_code
              ORDER BY latest.root_fact_seq DESC, latest.rowid DESC
              LIMIT 1
          )
        """,
        (as_of_text,),
    ).fetchall()
    episode_ids = tuple(str(row["episode_id"]) for row in rows)
    if not episode_ids:
        return 0
    placeholders = ", ".join("?" for _ in episode_ids)
    conn.execute(
        f"""
        UPDATE cognitive_monitoring_episodes
        SET status = 'open', updated_at = ?
        WHERE episode_id IN ({placeholders}) AND status = 'paused'
        """,
        (as_of_text, *episode_ids),
    )
    conn.execute(
        f"""
        UPDATE cognitive_learning_obligations
        SET status = 'pending', updated_at = ?
        WHERE episode_id IN ({placeholders}) AND status = 'paused'
        """,
        (as_of_text, *episode_ids),
    )
    return len(episode_ids)


def _claim_candidates(
    self: Any,
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    lease_seconds: float,
    as_of: datetime,
    limit: int,
    obligation_types: Sequence[str],
    obligation_ids: Sequence[str],
) -> list[dict[str, Any]]:
    _expire_episodes(self, conn, as_of=as_of)
    _resume_expired_suppressions(conn, as_of=as_of)
    allowed = tuple(dict.fromkeys(str(item or "").strip() for item in obligation_types))
    if not allowed or any(item not in {"retention", "transfer_check"} for item in allowed):
        raise ValueError("unsupported cognitive obligation type")
    requested_ids = tuple(
        dict.fromkeys(str(item or "").strip() for item in obligation_ids)
    )
    if any(not item for item in requested_ids):
        raise ValueError("cognitive obligation id is required")
    as_of_text = _iso(as_of)
    frequency_cutoff = _iso(as_of - RETENTION_FREQUENCY)
    placeholders = ", ".join("?" for _ in allowed)
    id_filter = (
        "AND obligations.obligation_id IN ("
        + ", ".join("?" for _ in requested_ids)
        + ")"
        if requested_ids
        else ""
    )
    rows = conn.execute(
        f"""
        SELECT obligations.*
        FROM cognitive_learning_obligations obligations
        JOIN cognitive_monitoring_episodes episodes
          ON episodes.episode_id = obligations.episode_id
        LEFT JOIN cognitive_obligation_claims claims
          ON claims.claim_id = obligations.current_claim_id
        WHERE obligations.obligation_type IN ({placeholders})
          {id_filter}
          AND obligations.status IN ('pending', 'claimed')
          AND obligations.not_before <= ?
          AND obligations.eligibility_until >= ?
          AND (
              obligations.status = 'pending'
              OR claims.claim_id IS NULL
              OR claims.status != 'active'
              OR claims.lease_expires_at <= ?
          )
          AND (
              (obligations.obligation_type = 'retention' AND episodes.status = 'open')
              OR (
                  obligations.obligation_type = 'transfer_check'
                  AND episodes.status IN ('expired', 'relapsed')
              )
          )
          AND (
              obligations.obligation_type != 'retention'
              OR NOT EXISTS (
                  SELECT 1 FROM cognitive_obligation_satisfactions recent
                  JOIN cognitive_learning_obligations recent_obligation
                    ON recent_obligation.obligation_id = recent.obligation_id
                  WHERE recent_obligation.hypothesis_id = obligations.hypothesis_id
                    AND recent_obligation.obligation_type = 'retention'
                    AND recent.occurred_at > ?
              )
          )
        ORDER BY
            CASE WHEN obligations.due_by < ? THEN 0 ELSE 1 END,
            obligations.due_by,
            obligations.obligation_id
        LIMIT ?
        """,
        (
            *allowed,
            *requested_ids,
            as_of_text,
            as_of_text,
            as_of_text,
            frequency_cutoff,
            as_of_text,
            limit,
        ),
    ).fetchall()
    claimed: list[dict[str, Any]] = []
    for raw in rows:
        obligation = _row(raw)
        assert obligation is not None
        prior_claim_id = str(obligation["current_claim_id"] or "")
        claim_id = f"cognitive-claim:{uuid.uuid4().hex}"
        claim_token = uuid.uuid4().hex
        lease_expires_at = _iso(as_of + timedelta(seconds=lease_seconds))
        changed = conn.execute(
            """
            UPDATE cognitive_learning_obligations
            SET status = 'claimed', current_claim_id = ?, updated_at = ?
            WHERE obligation_id = ? AND status = ? AND current_claim_id = ?
            """,
            (
                claim_id,
                as_of_text,
                obligation["obligation_id"],
                obligation["status"],
                prior_claim_id,
            ),
        ).rowcount
        if changed != 1:
            continue
        _supersede_claim(conn, prior_claim_id, finished_at=as_of_text)
        conn.execute(
            """
            INSERT INTO cognitive_obligation_claims (
                claim_id, obligation_id, claim_token, worker_id, status,
                claimed_at, lease_expires_at
            ) VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                claim_id,
                obligation["obligation_id"],
                claim_token,
                worker_id,
                as_of_text,
                lease_expires_at,
            ),
        )
        current = _obligation_row(conn, str(obligation["obligation_id"]))
        current.update(
            {
                "claim_id": claim_id,
                "claim_token": claim_token,
                "worker_id": worker_id,
                "lease_expires_at": lease_expires_at,
            }
        )
        claimed.append(current)
    return claimed


def claim_cognitive_obligations(
    self: Any,
    *,
    worker_id: str,
    lease_seconds: object = 300,
    as_of: object | None = None,
    limit: int = 1,
    obligation_types: Sequence[str] = ("retention", "transfer_check"),
    obligation_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    worker = _required(worker_id, "worker_id")
    lease = _positive_seconds(lease_seconds, "lease_seconds")
    bounded_limit = max(1, min(100, int(limit)))
    instant = _now(as_of)
    return _transaction(
        self,
        lambda conn: _claim_candidates(
            self,
            conn,
            worker_id=worker,
            lease_seconds=lease,
            as_of=instant,
            limit=bounded_limit,
            obligation_types=obligation_types,
            obligation_ids=obligation_ids,
        ),
    )


def _owned_claim(
    conn: sqlite3.Connection,
    *,
    obligation_id: str,
    claim_token: str,
    worker_id: str,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT claims.*, obligations.status AS obligation_status,
               obligations.current_claim_id, obligations.obligation_type,
               obligations.episode_id, obligations.hypothesis_id,
               obligations.hypothesis_code, obligations.eligibility_until
        FROM cognitive_obligation_claims claims
        JOIN cognitive_learning_obligations obligations
          ON obligations.obligation_id = claims.obligation_id
        WHERE claims.obligation_id = ? AND claims.claim_token = ?
          AND claims.worker_id = ? AND claims.status = 'active'
          AND obligations.status = 'claimed'
          AND obligations.current_claim_id = claims.claim_id
        """,
        (obligation_id, claim_token, worker_id),
    ).fetchone()
    if row is None:
        raise ValueError("cognitive obligation claim is stale or not owned")
    return row


def release_cognitive_obligation_claim(
    self: Any,
    *,
    obligation_id: str,
    claim_token: str,
    worker_id: str,
    released_at: object | None = None,
) -> dict[str, Any]:
    obligation = _required(obligation_id, "obligation_id")
    token = _required(claim_token, "claim_token")
    worker = _required(worker_id, "worker_id")
    instant = _now(released_at)
    timestamp = _iso(instant)

    def operation(conn: sqlite3.Connection) -> dict[str, Any]:
        claim = _owned_claim(
            conn,
            obligation_id=obligation,
            claim_token=token,
            worker_id=worker,
        )
        conn.execute(
            """
            UPDATE cognitive_obligation_claims
            SET status = 'released', finished_at = ?
            WHERE claim_id = ? AND status = 'active'
            """,
            (timestamp, claim["claim_id"]),
        )
        changed = conn.execute(
            """
            UPDATE cognitive_learning_obligations
            SET status = 'pending', current_claim_id = '', updated_at = ?
            WHERE obligation_id = ? AND status = 'claimed'
              AND current_claim_id = ?
            """,
            (timestamp, obligation, claim["claim_id"]),
        ).rowcount
        if changed != 1:
            raise ValueError("cognitive obligation claim lost ownership")
        return _obligation_row(conn, obligation)

    return _transaction(self, operation)


def complete_cognitive_obligation_claim(
    self: Any,
    *,
    obligation_id: str,
    claim_token: str,
    worker_id: str,
    attempt_id: str,
    completed_at: object | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Complete a non-retention obligation with an identity fence."""

    obligation = _required(obligation_id, "obligation_id")
    token = _required(claim_token, "claim_token")
    worker = _required(worker_id, "worker_id")
    attempt = _required(attempt_id, "attempt_id")
    instant = _now(completed_at)
    timestamp = _iso(instant)

    def operation(conn: sqlite3.Connection) -> dict[str, Any]:
        claim = _owned_claim(
            conn,
            obligation_id=obligation,
            claim_token=token,
            worker_id=worker,
        )
        if claim["obligation_type"] == "retention":
            raise ValueError("retention obligations require a retention disposition")
        if _utc(claim["lease_expires_at"], "lease_expires_at") < instant:
            raise ValueError("cognitive obligation claim lease expired")
        satisfaction_id = _stable_id("cognitive-satisfaction", attempt)
        conn.execute(
            """
            INSERT INTO cognitive_obligation_satisfactions (
                satisfaction_id, obligation_id, episode_id, claim_id,
                attempt_id, disposition, metadata_json, occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)
            """,
            (
                satisfaction_id,
                obligation,
                claim["episode_id"],
                claim["claim_id"],
                attempt,
                _json_dumps(self, metadata or {}),
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            UPDATE cognitive_obligation_claims
            SET status = 'completed', finished_at = ? WHERE claim_id = ?
            """,
            (timestamp, claim["claim_id"]),
        )
        conn.execute(
            """
            UPDATE cognitive_learning_obligations
            SET status = 'completed', completed_at = ?, updated_at = ?
            WHERE obligation_id = ? AND current_claim_id = ?
            """,
            (timestamp, timestamp, obligation, claim["claim_id"]),
        )
        return _obligation_row(conn, obligation)

    return _transaction(self, operation)


def _transfer_check_after(
    conn: sqlite3.Connection,
    *,
    episode: Mapping[str, Any],
    instant: datetime,
    reason: str,
) -> dict[str, Any]:
    return _insert_obligation(
        conn,
        episode=episode,
        obligation_type="transfer_check",
        not_before=instant + RELAPSE_COOLDOWN,
        due_by=instant + RETENTION_DUE,
        eligibility_until=instant + RETENTION_ELIGIBILITY,
        generation=1,
        reason=reason,
        now=instant,
    )


def apply_cognitive_retention_disposition(
    self: Any,
    *,
    obligation_id: str,
    claim_token: str,
    worker_id: str,
    attempt_id: str,
    disposition: str,
    occurred_at: object | None = None,
    metadata: Mapping[str, Any] | None = None,
    _connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Apply a validator disposition without touching FSRS scheduling."""

    obligation = _required(obligation_id, "obligation_id")
    token = _required(claim_token, "claim_token")
    worker = _required(worker_id, "worker_id")
    attempt = _required(attempt_id, "attempt_id")
    result = str(disposition or "").strip()
    if result not in _RETENTION_DISPOSITIONS:
        raise ValueError("unsupported retention disposition")
    instant = _now(occurred_at)
    timestamp = _iso(instant)

    def operation(conn: sqlite3.Connection) -> dict[str, Any]:
        claim = _owned_claim(
            conn,
            obligation_id=obligation,
            claim_token=token,
            worker_id=worker,
        )
        if claim["obligation_type"] != "retention":
            raise ValueError("retention disposition requires a retention obligation")
        if claim["hypothesis_code"] != ACTIVE_RETENTION_HYPOTHESIS:
            raise ValueError("retention hypothesis is not active")
        if _utc(claim["lease_expires_at"], "lease_expires_at") < instant:
            raise ValueError("cognitive obligation claim lease expired")
        satisfaction_id = _stable_id("cognitive-satisfaction", attempt)
        conn.execute(
            """
            INSERT INTO cognitive_obligation_satisfactions (
                satisfaction_id, obligation_id, episode_id, claim_id,
                attempt_id, disposition, metadata_json, occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                satisfaction_id,
                obligation,
                claim["episode_id"],
                claim["claim_id"],
                attempt,
                result,
                _json_dumps(self, metadata or {}),
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            UPDATE cognitive_obligation_claims
            SET status = 'completed', finished_at = ?
            WHERE claim_id = ? AND status = 'active'
            """,
            (timestamp, claim["claim_id"]),
        )
        episode = _episode_row(conn, str(claim["episode_id"]))
        next_obligation: dict[str, Any] | None = None
        if result == "resolved":
            conn.execute(
                """
                UPDATE cognitive_learning_obligations
                SET status = 'completed', completed_at = ?, updated_at = ?
                WHERE obligation_id = ? AND current_claim_id = ?
                """,
                (timestamp, timestamp, obligation, claim["claim_id"]),
            )
            conn.execute(
                """
                UPDATE cognitive_monitoring_episodes
                SET status = 'resolved', resolved_at = ?,
                    last_retention_at = ?, updated_at = ?
                WHERE episode_id = ? AND status = 'open'
                """,
                (timestamp, timestamp, timestamp, claim["episode_id"]),
            )
        elif result == "relapse":
            conn.execute(
                """
                UPDATE cognitive_learning_obligations
                SET status = 'completed', completed_at = ?, updated_at = ?
                WHERE obligation_id = ? AND current_claim_id = ?
                """,
                (timestamp, timestamp, obligation, claim["claim_id"]),
            )
            conn.execute(
                """
                UPDATE cognitive_monitoring_episodes
                SET status = 'relapsed', relapse_count = relapse_count + 1,
                    last_retention_at = ?, updated_at = ?
                WHERE episode_id = ? AND status = 'open'
                """,
                (timestamp, timestamp, claim["episode_id"]),
            )
            episode = _episode_row(conn, str(claim["episode_id"]))
            next_obligation = _transfer_check_after(
                conn, episode=episode, instant=instant, reason="retention_relapse"
            )
        else:
            eligibility_until = _utc(
                claim["eligibility_until"], "eligibility_until"
            )
            next_time = instant + RETENTION_FREQUENCY
            if next_time > eligibility_until:
                conn.execute(
                    """
                    UPDATE cognitive_learning_obligations
                    SET status = 'cancelled', current_claim_id = '',
                        reason = 'monitoring_window_expired', updated_at = ?
                    WHERE obligation_id = ? AND current_claim_id = ?
                    """,
                    (timestamp, obligation, claim["claim_id"]),
                )
                conn.execute(
                    """
                    UPDATE cognitive_monitoring_episodes
                    SET status = 'expired', expired_at = ?,
                        last_retention_at = ?, updated_at = ?
                    WHERE episode_id = ? AND status = 'open'
                    """,
                    (timestamp, timestamp, timestamp, claim["episode_id"]),
                )
                _record_episode_expired_fact(
                    conn,
                    episode_id=str(claim["episode_id"]),
                    occurred_at=timestamp,
                )
                episode = _episode_row(conn, str(claim["episode_id"]))
                next_obligation = _insert_obligation(
                    conn,
                    episode=episode,
                    obligation_type="transfer_check",
                    not_before=instant,
                    due_by=instant + RETENTION_DUE,
                    eligibility_until=instant + RETENTION_ELIGIBILITY,
                    generation=1,
                    reason="monitoring_window_expired",
                    now=instant,
                )
            else:
                conn.execute(
                    """
                    UPDATE cognitive_learning_obligations
                    SET status = 'pending', current_claim_id = '',
                        not_before = ?, updated_at = ?, reason = ?
                    WHERE obligation_id = ? AND current_claim_id = ?
                    """,
                    (next_time_iso := _iso(next_time), timestamp, result, obligation, claim["claim_id"]),
                )
                conn.execute(
                    """
                    UPDATE cognitive_monitoring_episodes
                    SET not_before = ?, last_retention_at = ?, updated_at = ?
                    WHERE episode_id = ? AND status = 'open'
                    """,
                    (next_time_iso, timestamp, timestamp, claim["episode_id"]),
                )
        current_episode = _episode_row(conn, str(claim["episode_id"]))
        _mark_retention_projection_dirty(self, conn, current_episode)
        return {
            "episode": current_episode,
            "obligation": _obligation_row(conn, obligation),
            "next_obligation": next_obligation,
            "disposition": result,
        }

    if _connection is not None:
        return operation(_connection)
    return _transaction(self, operation)


def apply_cognitive_obligation_control(
    self: Any,
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    hypothesis_code: str,
    action: str,
    occurred_at: object | None = None,
) -> dict[str, int]:
    """Apply user control inside the control fact's transaction."""

    topic = _required(topic_id, "topic_id")
    hypothesis = _required(hypothesis_code, "hypothesis_code")
    control = str(action or "").strip()
    if control not in {"dismiss", "suppress", "restore", "delete"}:
        raise ValueError("unsupported cognitive control action")
    if hypothesis != ACTIVE_RETENTION_HYPOTHESIS:
        return {"episodes": 0, "obligations": 0, "claims": 0}
    timestamp = _iso(_now(occurred_at))
    episodes = conn.execute(
        """
        SELECT episode_id FROM cognitive_monitoring_episodes
        WHERE topic_id = ? AND hypothesis_code = ?
          AND status IN ('open', 'paused')
        """,
        (topic, hypothesis),
    ).fetchall()
    episode_ids = tuple(str(row["episode_id"]) for row in episodes)
    if not episode_ids:
        return {"episodes": 0, "obligations": 0, "claims": 0}
    placeholders = ", ".join("?" for _ in episode_ids)
    if control == "restore":
        episode_count = conn.execute(
            f"""
            UPDATE cognitive_monitoring_episodes
            SET status = 'open', updated_at = ?
            WHERE episode_id IN ({placeholders}) AND status = 'paused'
            """,
            (timestamp, *episode_ids),
        ).rowcount
        obligation_count = conn.execute(
            f"""
            UPDATE cognitive_learning_obligations
            SET status = 'pending', updated_at = ?
            WHERE episode_id IN ({placeholders}) AND status = 'paused'
            """,
            (timestamp, *episode_ids),
        ).rowcount
        return {
            "episodes": max(0, episode_count),
            "obligations": max(0, obligation_count),
            "claims": 0,
        }

    active_claims = conn.execute(
        f"""
        SELECT claims.claim_id
        FROM cognitive_obligation_claims claims
        JOIN cognitive_learning_obligations obligations
          ON obligations.obligation_id = claims.obligation_id
        WHERE obligations.episode_id IN ({placeholders})
          AND claims.status = 'active'
          AND obligations.current_claim_id = claims.claim_id
        """,
        episode_ids,
    ).fetchall()
    for claim in active_claims:
        _supersede_claim(conn, str(claim["claim_id"]), finished_at=timestamp)
    target_episode = "paused" if control == "suppress" else "cancelled"
    target_obligation = "paused" if control == "suppress" else "cancelled"
    episode_count = conn.execute(
        f"""
        UPDATE cognitive_monitoring_episodes
        SET status = ?, updated_at = ?
        WHERE episode_id IN ({placeholders}) AND status IN ('open', 'paused')
        """,
        (target_episode, timestamp, *episode_ids),
    ).rowcount
    obligation_count = conn.execute(
        f"""
        UPDATE cognitive_learning_obligations
        SET status = ?, current_claim_id = '', reason = ?, updated_at = ?
        WHERE episode_id IN ({placeholders})
          AND status IN ('pending', 'claimed', 'paused')
        """,
        (target_obligation, f"user_{control}", timestamp, *episode_ids),
    ).rowcount
    return {
        "episodes": max(0, episode_count),
        "obligations": max(0, obligation_count),
        "claims": len(active_claims),
    }


def record_cognitive_obligation_control(
    self: Any,
    *,
    topic_id: str,
    hypothesis_code: str,
    action: str,
    occurred_at: object | None = None,
) -> dict[str, int]:
    return _transaction(
        self,
        lambda conn: apply_cognitive_obligation_control(
            self,
            conn,
            topic_id=topic_id,
            hypothesis_code=hypothesis_code,
            action=action,
            occurred_at=occurred_at,
        ),
    )


def list_cognitive_monitoring_episodes(
    self: Any,
    *,
    hypothesis_id: str | None = None,
    statuses: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if hypothesis_id:
        clauses.append("hypothesis_id = ?")
        params.append(str(hypothesis_id))
    requested = tuple(dict.fromkeys(str(item) for item in (statuses or ())))
    if requested:
        clauses.append("status IN (" + ", ".join("?" for _ in requested) + ")")
        params.extend(requested)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = self._require_read_conn().execute(
        f"""
        SELECT * FROM cognitive_monitoring_episodes {where}
        ORDER BY opened_at, episode_id
        """,
        params,
    ).fetchall()
    return [_row(row) for row in rows if row is not None]  # type: ignore[misc]


def list_cognitive_learning_obligations(
    self: Any,
    *,
    episode_id: str | None = None,
    statuses: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if episode_id:
        clauses.append("episode_id = ?")
        params.append(str(episode_id))
    requested = tuple(dict.fromkeys(str(item) for item in (statuses or ())))
    if requested:
        clauses.append("status IN (" + ", ".join("?" for _ in requested) + ")")
        params.extend(requested)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    conn = self._require_read_conn()
    rows = conn.execute(
        f"""
        SELECT * FROM cognitive_learning_obligations {where}
        ORDER BY due_by, obligation_id
        """,
        params,
    ).fetchall()
    results = [_row(row) for row in rows if row is not None]
    for item in results:
        if item is None:
            continue
        history = conn.execute(
            """
            SELECT metadata_json
            FROM cognitive_obligation_satisfactions
            WHERE episode_id = ?
            ORDER BY occurred_at, satisfaction_id
            """,
            (item["episode_id"],),
        ).fetchall()
        families: list[str] = []
        groups: list[str] = []
        for history_row in history:
            try:
                metadata = json.loads(str(history_row["metadata_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict):
                continue
            family = str(
                metadata.get("cognitive_question_family_id")
                or metadata.get("question_family_id")
                or ""
            ).strip()
            group = str(
                metadata.get("cognitive_independence_group")
                or metadata.get("independence_group")
                or ""
            ).strip()
            if family and family not in families:
                families.append(family)
            if group and group not in groups:
                groups.append(group)
        item["previous_question_family_ids"] = tuple(families)
        item["previous_independence_groups"] = tuple(groups)
    return [item for item in results if item is not None]


__all__ = [
    "ACTIVE_RETENTION_HYPOTHESIS",
    "COGNITIVE_RETENTION_SCHEMA",
    "apply_cognitive_obligation_control",
    "apply_cognitive_retention_disposition",
    "claim_cognitive_obligations",
    "complete_cognitive_obligation_claim",
    "create_cognitive_retention_schema",
    "expire_cognitive_monitoring_episodes",
    "insert_certified_transfer_episode",
    "list_cognitive_learning_obligations",
    "list_cognitive_monitoring_episodes",
    "rebuild_cognitive_retention_from_transfers",
    "record_certified_transfer_success",
    "record_cognitive_obligation_control",
    "release_cognitive_obligation_claim",
]
