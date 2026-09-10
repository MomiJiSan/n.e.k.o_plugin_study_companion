"""Server-owned evidence reads and transactional personalization delivery fence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .adaptive_learning.cognitive_personalization import (
    HYPOTHESIS,
    MAX_ALTERNATES,
    MAX_FAILURES,
    POLICY_VERSION,
    VERSION_SET,
    canonical_audit,
    select_personalized_strategy,
    utc,
)
from .store_cognitive_strategy import build_cognitive_strategy_report_snapshot


class _SnapshotStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _require_conn(self) -> sqlite3.Connection:
        return self.conn


def personalization_history(
    conn: sqlite3.Connection,
    hypothesis_id: str,
    now: datetime,
) -> dict[str, Any]:
    """Count actual deliveries even if optional Shadow writes failed."""
    rows = conn.execute(
        """
        SELECT question.event_seq, question.question_id, question.root_fact_seq,
               question.repair_strategy, question.occurred_at,
               attempt.evaluation_verdict AS event_verdict,
               attempt.root_fact_seq AS event_answer_seq,
               (SELECT COUNT(*) FROM attempts canonical_attempt
                WHERE canonical_attempt.question_id = question.question_id)
                   AS canonical_attempt_n,
               (SELECT json_extract(evaluation.evaluation_json, '$.verdict')
                FROM attempts canonical_attempt
                JOIN evaluations evaluation
                  ON evaluation.attempt_id = canonical_attempt.attempt_id
                WHERE canonical_attempt.question_id = question.question_id
                ORDER BY canonical_attempt.submitted_at,
                         canonical_attempt.attempt_id
                LIMIT 1) AS canonical_verdict,
               (SELECT canonical_attempt.root_fact_seq
                FROM attempts canonical_attempt
                WHERE canonical_attempt.question_id = question.question_id
                ORDER BY canonical_attempt.submitted_at,
                         canonical_attempt.attempt_id
                LIMIT 1) AS canonical_answer_seq
        FROM cognitive_intervention_events question
        LEFT JOIN cognitive_intervention_events attempt
          ON attempt.event_type = 'attempt_committed'
         AND attempt.question_id = question.question_id
         AND attempt.decision_id = question.decision_id
        WHERE question.event_type = 'question_committed'
          AND question.hypothesis_id = ?
          AND question.topic_id IN ('calculus.chain_rule', 'college_chain_rule')
          AND question.hypothesis_code = ?
          AND question.learning_intent = 'misconception_repair'
        ORDER BY question.occurred_at, question.event_seq
        LIMIT 10001
        """,
        (hypothesis_id, HYPOTHESIS),
    ).fetchall()
    if len(rows) > 10000:
        raise ValueError("personalization history exceeds bounded read")
    recent = []
    for row in rows:
        occurred = utc(row["occurred_at"])
        if occurred > now:
            raise ValueError("future delivery in personalization history")
        if occurred > now - timedelta(days=7):
            record = dict(row)
            if int(record.pop("canonical_attempt_n") or 0) > 1:
                raise ValueError("multiple canonical attempts for personalized question")
            event_verdict = str(record.pop("event_verdict") or "").strip()
            canonical_verdict = str(record.pop("canonical_verdict") or "").strip()
            if event_verdict and canonical_verdict and event_verdict != canonical_verdict:
                raise ValueError("canonical and cognitive attempt verdicts conflict")
            verdict = event_verdict or canonical_verdict
            if verdict and verdict not in {"correct", "partial", "wrong", "dont_know"}:
                raise ValueError("unsupported canonical personalization verdict")
            record["evaluation_verdict"] = verdict or None
            record["answer_seq"] = max(
                int(record.pop("event_answer_seq") or 0),
                int(record.pop("canonical_answer_seq") or 0),
            )
            recent.append(record)
    alternate = [row for row in recent if row["repair_strategy"] == "minimal_change"]
    failures = 0
    for row in reversed(alternate):
        verdict = row["evaluation_verdict"]
        if verdict is None:
            continue
        if verdict == "correct":
            break
        failures += 1
    # Outcome changes (including delayed outcomes) fence decisions as well.
    facts = conn.execute(
        """SELECT COALESCE(MAX(f.root_fact_seq), 0) FROM cognitive_strategy_exposure_facts f
        JOIN cognitive_strategy_exposures e ON e.exposure_id = f.exposure_id
        WHERE e.hypothesis_id = ?""",
        (hypothesis_id,),
    ).fetchone()[0]
    controls = conn.execute("SELECT COALESCE(MAX(root_fact_seq), 0) FROM cognitive_fact_roots").fetchone()[0]
    digest = hashlib.sha256(
        canonical_audit(
            {
                "history": recent,
                "outcome_seq": facts,
            }
        ).encode("utf-8")
    ).hexdigest()
    return {
        "alternate_n": len(alternate),
        "consecutive_failures": failures,
        "history_sha256": digest,
        "as_of_root_fact_seq": int(controls),
    }


def _snapshot_sha256(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_audit(snapshot).encode("utf-8")).hexdigest()


def _bounded_snapshot(
    conn: sqlite3.Connection,
    *,
    as_of_root_fact_seq: int,
) -> dict[str, Any]:
    # Never make an online decision from a silently truncated ledger.
    for table in ("cognitive_strategy_exposures", "cognitive_strategy_exposure_facts"):
        if conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= 100000:
            raise ValueError("personalization snapshot exceeds bounded read")
    return build_cognitive_strategy_report_snapshot(
        _SnapshotStore(conn),
        version_set_id=VERSION_SET,
        as_of_root_fact_seq=as_of_root_fact_seq,
    )


def personalization_runtime_ledger_status(
    store: Any,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Return an answer-free, lock-consistent runtime summary."""
    with store.answer_write_lock():
        conn = store._require_conn()
        conn.execute("SAVEPOINT cognitive_personalization_status")
        try:
            current = conn.execute(
                """
                SELECT hypothesis_id
                FROM cognitive_hypothesis_current
                WHERE topic_id IN ('calculus.chain_rule', 'college_chain_rule')
                  AND hypothesis_code = ?
                  AND model_version = ?
                  AND evidence_status = 'supported'
                  AND user_override = ''
                ORDER BY projected_generation DESC, computed_at DESC, hypothesis_id
                LIMIT 1
                """,
                (HYPOTHESIS, VERSION_SET),
            ).fetchone()
            latest_rows = conn.execute(
                """
                SELECT hypothesis_id, repair_strategy, metadata_json,
                       occurred_at, event_seq
                FROM cognitive_intervention_events
                WHERE event_type = 'question_committed'
                  AND topic_id IN ('calculus.chain_rule', 'college_chain_rule')
                  AND hypothesis_code = ?
                  AND hypothesis_model_version = ?
                  AND learning_intent = 'misconception_repair'
                ORDER BY occurred_at DESC, event_seq DESC
                LIMIT 200
                """,
                (HYPOTHESIS, VERSION_SET),
            ).fetchall()
            latest_any = latest_rows[0] if latest_rows else None
            hypothesis_id = str(current["hypothesis_id"]) if current else (
                str(latest_any["hypothesis_id"]) if latest_any else ""
            )
            scoped_rows = [
                row for row in latest_rows if str(row["hypothesis_id"]) == hypothesis_id
            ]
            latest = scoped_rows[0] if scoped_rows else None
            history = (
                personalization_history(conn, hypothesis_id, now)
                if hypothesis_id
                else {
                    "alternate_n": 0,
                    "consecutive_failures": 0,
                    "as_of_root_fact_seq": 0,
                }
            )
            last_audit: dict[str, Any] = {}
            for row in scoped_rows:
                try:
                    metadata = json.loads(str(row["metadata_json"] or "{}"))
                except (TypeError, ValueError):
                    continue
                audit = metadata.get("strategy_personalization") if isinstance(metadata, dict) else None
                if isinstance(audit, dict):
                    last_audit = audit
                    break
            repair_strategy = str(latest["repair_strategy"]) if latest else "complete_inner_derivative"
            return {
                "hypothesis_id": hypothesis_id,
                "last_delivered_strategy": (
                    "alternate" if repair_strategy == "minimal_change" else "baseline"
                ),
                "last_delivered_repair_strategy": repair_strategy,
                "decision_reason": str(last_audit.get("reason") or "not_evaluated"),
                "alternate_deliveries_7d": int(history["alternate_n"]),
                "consecutive_alternate_failures": int(history["consecutive_failures"]),
                "as_of_root_fact_seq": int(history["as_of_root_fact_seq"]),
            }
        finally:
            conn.execute("RELEASE SAVEPOINT cognitive_personalization_status")


def decide_from_store(store: Any, decision: Any, *, now: datetime, explore: bool = False):
    """Read one consistent local snapshot; no external report is consumed."""
    hypothesis = decision.selected_hypothesis
    if hypothesis is None:
        return decision, {"reason": "outside_scope", "applied": False}
    with store.answer_write_lock():
        conn = store._require_conn()
        conn.execute("SAVEPOINT cognitive_personalization_read")
        try:
            history = personalization_history(conn, hypothesis.hypothesis_id, now)
            snapshot = _bounded_snapshot(
                conn,
                as_of_root_fact_seq=history["as_of_root_fact_seq"],
            )
            result, audit = select_personalized_strategy(
                decision,
                snapshot,
                now=now,
                enabled=True,
                explore=explore,
                alternate_n=history["alternate_n"],
                consecutive_failures=history["consecutive_failures"],
            )
            audit["history_sha256"] = history["history_sha256"]
            audit["snapshot_sha256"] = _snapshot_sha256(snapshot)
            return result, audit
        finally:
            conn.execute("RELEASE SAVEPOINT cognitive_personalization_read")


def validate_personalized_commit(
    conn: sqlite3.Connection,
    event: Any,
    *,
    now: datetime | None = None,
) -> None:
    """Recheck within the canonical question transaction before the insert."""
    audit = (event.get("metadata") or {}).get("strategy_personalization")
    if not audit or audit.get("applied") is not True:
        return
    now = now or datetime.now(timezone.utc)
    decided = utc(audit.get("decided_at"))
    if not timedelta(0) <= now - decided <= timedelta(minutes=5):
        raise ValueError("personalized decision expired")
    if (
        audit.get("policy_version") != POLICY_VERSION
        or audit.get("repair_strategy") != "minimal_change"
        or event.get("repair_strategy") != "minimal_change"
        or event.get("learning_intent") != "misconception_repair"
        or event.get("blueprint_id") != "chain.omit-inner.minimal-change.v1"
    ):
        raise ValueError("personalized delivery scope mismatch")
    hypothesis = event["hypothesis_target"]
    if hypothesis.get("model_version") != VERSION_SET or hypothesis.get("code") != HYPOTHESIS:
        raise ValueError("personalized delivery version mismatch")
    history = personalization_history(conn, hypothesis["hypothesis_id"], decided)
    snapshot = _bounded_snapshot(
        conn,
        as_of_root_fact_seq=int(audit.get("as_of_root_fact_seq")),
    )
    if (
        history["alternate_n"] >= MAX_ALTERNATES
        or history["consecutive_failures"] >= MAX_FAILURES
        or history["history_sha256"] != audit.get("history_sha256")
        or _snapshot_sha256(snapshot) != audit.get("snapshot_sha256")
    ):
        raise ValueError("personalized delivery history changed or stopped")
