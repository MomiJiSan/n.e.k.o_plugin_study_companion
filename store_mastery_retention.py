"""Transactional authoritative learning mastery and permanent card generations."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .adaptive_learning.mastery_retention import (
    ACQUIRE_THRESHOLD,
    BASELINE_VERSION,
    INITIAL_HALF_LIFE,
    MODEL_VERSION,
    current_mastery,
    evidence_baseline,
    feedback_half_life,
    finite_fraction,
)


def utc_timestamp() -> float:
    return datetime.now(timezone.utc).timestamp()


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def create_mastery_retention_schema(conn: sqlite3.Connection) -> None:
    # Individual statements preserve the caller's transaction (no executescript).
    for statement in (
        """CREATE TABLE IF NOT EXISTS mastery_retention_identity (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), dataset_id TEXT NOT NULL,
            watermark INTEGER NOT NULL DEFAULT 0, effective_time REAL NOT NULL,
            model_version TEXT NOT NULL, migration_max_id INTEGER NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS mastery_retention_state (
            topic_id TEXT PRIMARY KEY REFERENCES topics(id), baseline REAL NOT NULL,
            half_life REAL NOT NULL, anchor REAL NOT NULL, generation INTEGER NOT NULL,
            owned INTEGER NOT NULL, evidence_count INTEGER NOT NULL,
            evidence_status TEXT NOT NULL, model_version TEXT NOT NULL,
            baseline_version TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS mastery_retention_evidence (
            attempt_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL,
            question_key TEXT NOT NULL, session_id TEXT NOT NULL, effective_time REAL NOT NULL,
            score REAL NOT NULL, verdict TEXT NOT NULL, confidence REAL,
            used_hint INTEGER, evaluator_source TEXT NOT NULL, result_json TEXT NOT NULL)""",
        """CREATE INDEX IF NOT EXISTS idx_retention_evidence_topic_time
            ON mastery_retention_evidence(topic_id,effective_time DESC)""",
        """CREATE TABLE IF NOT EXISTS learning_card_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id TEXT NOT NULL,
            generation INTEGER NOT NULL, event TEXT NOT NULL,
            effective_time REAL NOT NULL, model_version TEXT NOT NULL)""",
    ):
        conn.execute(statement)
    conn.execute(
        "INSERT OR IGNORE INTO mastery_retention_identity VALUES(1,?,0,?,?,(SELECT COALESCE(MAX(id),0) FROM mastery_snapshots))",
        (str(uuid.uuid4()), utc_timestamp(), MODEL_VERSION),
    )


def _time(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT * FROM mastery_retention_identity WHERE singleton=1").fetchone()
    if row is None or row["model_version"] != MODEL_VERSION:
        raise ValueError("retention policy migration required")
    value = float(row["effective_time"])
    if not math.isfinite(value):
        raise ValueError("invalid persisted effective time")
    now = max(value, utc_timestamp())
    conn.execute("UPDATE mastery_retention_identity SET effective_time=? WHERE singleton=1", (now,))
    return now


def _event(conn: sqlite3.Connection, state: dict, event: str, now: float) -> None:
    conn.execute("INSERT INTO learning_card_events(topic_id,generation,event,effective_time,model_version) VALUES(?,?,?,?,?)",
                 (state["topic_id"], state["generation"], event, now, MODEL_VERSION))
    conn.execute("UPDATE mastery_retention_identity SET watermark=watermark+1 WHERE singleton=1")


def _save(conn: sqlite3.Connection, state: dict) -> None:
    conn.execute("""INSERT INTO mastery_retention_state VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(topic_id) DO UPDATE SET baseline=excluded.baseline,
        half_life=excluded.half_life, anchor=excluded.anchor, generation=excluded.generation,
        owned=excluded.owned, evidence_count=excluded.evidence_count,
        evidence_status=excluded.evidence_status, model_version=excluded.model_version,
        baseline_version=excluded.baseline_version""", tuple(state[key] for key in (
            "topic_id", "baseline", "half_life", "anchor", "generation", "owned",
            "evidence_count", "evidence_status", "model_version", "baseline_version")))


def _load(conn: sqlite3.Connection, topic_id: str, now: float) -> dict | None:
    row = conn.execute("SELECT * FROM mastery_retention_state WHERE topic_id=?", (topic_id,)).fetchone()
    if row is not None:
        state = dict(row)
        if state["model_version"] != MODEL_VERSION:
            raise ValueError("retention state migration required")
        return state
    # One-time legacy migration: retain its actual assessment timestamp. No old
    # FSRS parameter or today's recomputed V2 decay is used as another clock.
    legacy = conn.execute("SELECT mastery,updated_at,attempts FROM mastery_snapshots WHERE topic_id=? AND id <= (SELECT migration_max_id FROM mastery_retention_identity WHERE singleton=1) ORDER BY updated_at DESC,id DESC LIMIT 1", (topic_id,)).fetchone()
    if legacy is None:
        return None
    baseline = finite_fraction(legacy["mastery"])
    anchor = datetime.fromisoformat(str(legacy["updated_at"]).replace("Z", "+00:00"))
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    anchor_time = anchor.timestamp()
    if anchor_time > now:
        raise ValueError("legacy mastery timestamp is in the future")
    mastery = current_mastery(baseline, INITIAL_HALF_LIFE, (now-anchor_time)/86400)
    state = dict(topic_id=topic_id, baseline=baseline, half_life=INITIAL_HALF_LIFE,
                 anchor=anchor_time, generation=int(mastery >= ACQUIRE_THRESHOLD),
                 owned=int(mastery >= ACQUIRE_THRESHOLD), evidence_count=int(legacy["attempts"] or 0),
                 evidence_status="legacy_migration", model_version=MODEL_VERSION,
                 baseline_version="legacy-v1-migration")
    _save(conn, state)
    if state["owned"]:
        _event(conn, state, "acquired", now)
    return state


def _expire(conn: sqlite3.Connection, state: dict, now: float) -> float:
    anchor = float(state["anchor"])
    if not math.isfinite(anchor) or anchor > now:
        raise ValueError("invalid mastery anchor")
    mastery = current_mastery(state["baseline"], state["half_life"], (now-anchor)/86400)
    if mastery == 0 and state["owned"]:
        state["owned"] = 0
        _save(conn, state)
        _event(conn, state, "destroyed", now)
    return mastery


def write_retention_answer(self, conn: sqlite3.Connection, *, topic_id: str,
                           attempt_id: str, session_id: str, question: dict,
                           eval_result: dict, user_answer: str, used_hint: bool | None) -> dict | None:
    """Called only inside the answer transaction, after its idempotence check."""
    if not topic_id or not attempt_id:
        return None
    verdict = str(eval_result.get("verdict") or "")
    source = str(eval_result.get("evaluator_type") or "")
    if (not user_answer.strip() or verdict not in {"correct", "partial", "wrong", "dont_know"}
            or source not in {"llm_rubric", "deterministic", "human_verified", "exact_short_answer", "numeric_tolerance", "math_expression"}
            or eval_result.get("fallback_reason") or eval_result.get("evaluation_failed")
            or any(eval_result.get(key) is False for key in (
                "_evaluation_score_valid", "_evaluation_verdict_valid",
                "_evaluation_final_answer_correct_valid"))
            or (verdict == "correct" and eval_result.get("final_answer_correct") is False)):
        return None
    confidence = eval_result.get("confidence")
    if confidence is not None:
        try:
            confidence = finite_fraction(confidence)
        except (ValueError, TypeError, OverflowError):
            confidence = None
    raw_score = eval_result.get("score")
    try:
        score = finite_fraction(float(raw_score) / 100)
    except (ValueError, TypeError, OverflowError):
        return None
    if verdict in {"wrong", "dont_know"}:
        score = 0.0
    elif verdict == "partial":
        score = min(score, 0.75)
    if used_hint is not False:
        score *= 0.7
    # Content is part of identity so generating another attempt/question ID
    # cannot turn the identical exercise into independent new evidence.
    content = " ".join(str(question.get("question") or "").split()).casefold()
    if not content:
        return None
    question_key = hashlib.sha256(content.encode()).hexdigest()
    now = _time(conn)
    state: dict[str, Any] | None = _load(conn, topic_id, now)
    before = _expire(conn, state, now) if state else 0.0
    if state is None:
        state = dict(topic_id=topic_id, baseline=0.0, half_life=INITIAL_HALF_LIFE,
                     anchor=now, generation=0, owned=0, evidence_count=0,
                     evidence_status="trial", model_version=MODEL_VERSION, baseline_version=BASELINE_VERSION)
    half_life = feedback_half_life(state["half_life"], (now-state["anchor"])/86400,
                                    verdict, confidence, used_hint)
    rows = conn.execute("SELECT * FROM mastery_retention_evidence WHERE topic_id=? ORDER BY effective_time DESC,rowid DESC LIMIT 200", (topic_id,)).fetchall()
    # Rolling day correlation additionally survives an artificial session reset.
    # Keep the most recent result per content/session or content/day cluster.
    groups: list[dict] = [dict(question_key=question_key, session_id=session_id, effective_time=now, score=score)]
    for row in rows:
        if any(row["question_key"] == group["question_key"] and (
            row["session_id"] == group["session_id"] or abs(row["effective_time"]-group["effective_time"]) < 86400
        ) for group in groups):
            continue
        groups.append(dict(row))
        if len(groups) >= 10:
            break
    baseline = evidence_baseline([group["score"] for group in reversed(groups)])
    if verdict in {"wrong", "dont_know"}:
        baseline = min(before, baseline)
    elif verdict == "partial" or used_hint is not False:
        # Weak new evidence may establish its own conservative baseline, but
        # may not resurrect stale high scores merely by moving the anchor.
        baseline = min(baseline, max(before, evidence_baseline([score])))
    state.update(baseline=baseline, half_life=half_life, anchor=now,
                 evidence_count=len(groups), baseline_version=BASELINE_VERSION,
                 evidence_status="trial" if confidence is not None and used_hint is not None else "insufficient_retention_evidence")
    mastery = current_mastery(baseline, half_life, 0)
    if mastery == 0 and state["owned"]:
        state["owned"] = 0
        _event(conn, state, "destroyed", now)
    elif mastery >= ACQUIRE_THRESHOLD and not state["owned"]:
        state["generation"] += 1
        state["owned"] = 1
        _event(conn, state, "acquired", now)
    _save(conn, state)
    result = _view(state, mastery, now)
    conn.execute("INSERT INTO mastery_retention_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
        attempt_id, topic_id, question_key, session_id, now, score, verdict, confidence,
        None if used_hint is None else int(used_hint), source, json.dumps(result, allow_nan=False)))
    conn.execute("UPDATE mastery_retention_identity SET watermark=watermark+1 WHERE singleton=1")
    return result


def _view(state: dict, mastery: float, now: float) -> dict:
    return dict(topic_id=state["topic_id"], mastery=mastery, owned=bool(state["owned"]),
                generation=state["generation"], half_life=state["half_life"],
                attempts=state["evidence_count"], evidence_status=state["evidence_status"],
                baseline_version=state["baseline_version"],
                status="forgotten" if mastery == 0 else "active" if state["owned"] else "learning",
                model_version=MODEL_VERSION, updated_at=_iso(state["anchor"]), as_of=_iso(now))


def get_learning_card_snapshot(self) -> dict[str, Any]:
    with self._lock:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _time(conn)
            topics = []
            for topic in conn.execute("SELECT id,name,subject FROM topics ORDER BY id").fetchall():
                state = _load(conn, topic["id"], now)
                view = (_view(state, _expire(conn, state, now), now) if state else
                        dict(topic_id=topic["id"], mastery=None, owned=False, generation=0, status="unassessed"))
                view.update(name=topic["name"], subject=topic["subject"] or "")
                topics.append(view)
            identity = conn.execute("SELECT * FROM mastery_retention_identity WHERE singleton=1").fetchone()
            result = dict(dataset_id=identity["dataset_id"], model_version=MODEL_VERSION,
                          snapshot_version=identity["watermark"], as_of=_iso(now), topics=topics,
                          parameters=dict(initial_half_life_days=7, success_gain=1,
                                          failure_reduction=.5, zero_threshold=.001,
                                          acquire_threshold=.01, numeric_precision="float64",
                                          baseline_version=BASELINE_VERSION))
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise


def list_retention_mastery(self, topic_ids: list[str]) -> list[dict]:
    wanted = set(topic_ids)
    return [dict(topic, topic_name=topic["name"], flags=[]) for topic in
            get_learning_card_snapshot(self)["topics"] if topic["topic_id"] in wanted and topic["mastery"] is not None]
