"""Append-only persistence for V3 shadow strategy attribution.

The module is deliberately independent from ``StudyStore`` and from the
strategy catalog.  Its functions can be bound to ``StudyStore`` later, while
the transaction helpers are also usable by small standalone stores in tests.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

STRATEGY_FACT_TYPES = frozenset(
    {
        "attempt",
        "transfer",
        "episode",
        "retention",
        "abandoned",
        "replaced",
        "attribution_excluded",
    }
)
QUESTION_PURPOSES = frozenset({"probe", "repair", "transfer", "retention"})
_FORBIDDEN_KEYS = frozenset(
    {
        "answer",
        "user_answer",
        "learner_answer",
        "reference_answer",
        "expected_answer",
        "prompt",
        "system_prompt",
        "tokens",
        "token_count",
        "input_tokens",
        "output_tokens",
        "question_json",
        "evaluation_json",
    }
)


def _required_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _utc_datetime(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(
            _required_text(value, field).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field} must be a positive integer")
    if number <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return number


def _optional_nonnegative_int(value: object, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise TypeError(f"{field} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field} must be a non-negative integer")
    if number < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return number


def _optional_bool(value: object, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int) and value in (0, 1):
        return value
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean")
    return int(value)


def _reject_sensitive_payload(value: object, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_KEYS:
                raise ValueError(f"{path}.{key} is not permitted in strategy ledger")
            _reject_sensitive_payload(item, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _reject_sensitive_payload(item, f"{path}[{index}]")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def compute_cognitive_strategy_exposure_id(
    question_id: object,
    strategy_id: object,
    strategy_version: object,
    catalog_version: object,
    version_set_id: object,
) -> str:
    """Return the frozen V3 exposure identity.

    The byte input is exactly the compact UTF-8 JSON representation of the
    five-element identity list defined by the V3 contract.
    """

    identity = [
        _required_text(question_id, "question_id"),
        _required_text(strategy_id, "strategy_id"),
        _required_text(strategy_version, "strategy_version"),
        _required_text(catalog_version, "catalog_version"),
        _required_text(version_set_id, "version_set_id"),
    ]
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return f"cognitive-strategy-exposure:{digest}"


def create_cognitive_strategy_schema(conn: sqlite3.Connection) -> None:
    """Create only the V3 shadow strategy ledger tables."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cognitive_strategy_exposures (
            exposure_id TEXT PRIMARY KEY,
            question_id TEXT NOT NULL UNIQUE,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            catalog_version TEXT NOT NULL,
            version_set_id TEXT NOT NULL,
            question_purpose TEXT NOT NULL CHECK(question_purpose IN (
                'probe', 'repair', 'transfer', 'retention'
            )),
            topic_id TEXT NOT NULL,
            learner_id TEXT NOT NULL DEFAULT 'local',
            hypothesis_id TEXT NOT NULL,
            hypothesis_code TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            blueprint_id TEXT NOT NULL,
            diagnostic_validation_id TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            validator_version TEXT NOT NULL,
            question_family_id TEXT NOT NULL,
            difficulty_bucket TEXT NOT NULL DEFAULT '',
            strategy_family TEXT NOT NULL,
            comparison_scope_id TEXT NOT NULL,
            baseline INTEGER NOT NULL CHECK(baseline IN (0, 1)),
            strategy_determined_before_commit INTEGER NOT NULL
                CHECK(strategy_determined_before_commit IN (0, 1)),
            provenance_complete INTEGER NOT NULL
                CHECK(provenance_complete IN (0, 1)),
            development_time_override INTEGER NOT NULL DEFAULT 0
                CHECK(development_time_override IN (0, 1)),
            source_id TEXT NOT NULL UNIQUE,
            root_fact_seq INTEGER NOT NULL CHECK(root_fact_seq > 0),
            occurred_at TEXT NOT NULL,
            answer_window_expires_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cognitive_strategy_exposure_facts (
            fact_id TEXT PRIMARY KEY,
            exposure_id TEXT NOT NULL
                REFERENCES cognitive_strategy_exposures(exposure_id),
            fact_type TEXT NOT NULL CHECK(fact_type IN (
                'attempt', 'transfer', 'episode', 'retention',
                'abandoned', 'replaced', 'attribution_excluded'
            )),
            source_id TEXT NOT NULL,
            root_fact_seq INTEGER NOT NULL CHECK(root_fact_seq > 0),
            outcome TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            attempt_id TEXT NOT NULL DEFAULT '',
            question_id TEXT NOT NULL DEFAULT '',
            episode_id TEXT NOT NULL DEFAULT '',
            linked_exposure_id TEXT NOT NULL DEFAULT '',
            evaluator_type TEXT NOT NULL DEFAULT '',
            evaluator_version TEXT NOT NULL DEFAULT '',
            evaluator_confidence REAL CHECK(
                evaluator_confidence IS NULL
                OR (evaluator_confidence >= 0 AND evaluator_confidence <= 1)
            ),
            used_hint INTEGER CHECK(used_hint IS NULL OR used_hint IN (0, 1)),
            certified INTEGER CHECK(certified IS NULL OR certified IN (0, 1)),
            response_time_ms INTEGER CHECK(
                response_time_ms IS NULL OR response_time_ms >= 0
            ),
            interval_hours REAL CHECK(interval_hours IS NULL OR interval_hours >= 0),
            independent_family INTEGER CHECK(
                independent_family IS NULL OR independent_family IN (0, 1)
            ),
            obligation_completed INTEGER CHECK(
                obligation_completed IS NULL OR obligation_completed IN (0, 1)
            ),
            provenance_complete INTEGER CHECK(
                provenance_complete IS NULL OR provenance_complete IN (0, 1)
            ),
            development_time_override INTEGER CHECK(
                development_time_override IS NULL
                OR development_time_override IN (0, 1)
            ),
            answer_disclosed INTEGER CHECK(
                answer_disclosed IS NULL OR answer_disclosed IN (0, 1)
            ),
            independence_known INTEGER CHECK(
                independence_known IS NULL OR independence_known IN (0, 1)
            ),
            reason TEXT NOT NULL DEFAULT '',
            occurred_at TEXT NOT NULL,
            UNIQUE(fact_type, source_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cognitive_strategy_exposures_scope
        ON cognitive_strategy_exposures(
            topic_id, hypothesis_code, root_fact_seq, source_id, exposure_id
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cognitive_strategy_facts_order
        ON cognitive_strategy_exposure_facts(
            exposure_id, root_fact_seq, source_id, fact_id
        )
        """
    )


def _normalize_exposure(event: Mapping[str, Any]) -> dict[str, Any]:
    _reject_sensitive_payload(event)
    if _required_text(event.get("event_type"), "event_type") != "question_committed":
        raise ValueError("strategy exposure requires question_committed provenance")
    purpose = _required_text(event.get("question_purpose"), "question_purpose")
    if purpose not in QUESTION_PURPOSES:
        raise ValueError("unsupported strategy question purpose")
    item = {
        "question_id": _required_text(event.get("question_id"), "question_id"),
        "strategy_id": _required_text(event.get("strategy_id"), "strategy_id"),
        "strategy_version": _required_text(
            event.get("strategy_version"), "strategy_version"
        ),
        "catalog_version": _required_text(
            event.get("catalog_version"), "catalog_version"
        ),
        "version_set_id": _required_text(
            event.get("version_set_id"), "version_set_id"
        ),
        "question_purpose": purpose,
        "topic_id": _required_text(event.get("topic_id"), "topic_id"),
        "learner_id": _required_text(
            event.get("learner_id", "local"), "learner_id"
        ),
        "hypothesis_id": _required_text(
            event.get("hypothesis_id"), "hypothesis_id"
        ),
        "hypothesis_code": _required_text(
            event.get("hypothesis_code"), "hypothesis_code"
        ),
        "decision_id": _required_text(event.get("decision_id"), "decision_id"),
        "blueprint_id": _required_text(event.get("blueprint_id"), "blueprint_id"),
        "diagnostic_validation_id": _required_text(
            event.get("diagnostic_validation_id"), "diagnostic_validation_id"
        ),
        "policy_version": _required_text(
            event.get("policy_version"), "policy_version"
        ),
        "validator_version": _required_text(
            event.get("validator_version"), "validator_version"
        ),
        "question_family_id": _required_text(
            event.get("question_family_id"), "question_family_id"
        ),
        "difficulty_bucket": str(event.get("difficulty_bucket") or "").strip(),
        "strategy_family": _required_text(
            event.get("strategy_family"), "strategy_family"
        ),
        "comparison_scope_id": _required_text(
            event.get("comparison_scope_id"), "comparison_scope_id"
        ),
        "baseline": _optional_bool(event.get("baseline"), "baseline"),
        "strategy_determined_before_commit": _optional_bool(
            event.get("strategy_determined_before_commit"),
            "strategy_determined_before_commit",
        ),
        "provenance_complete": _optional_bool(
            event.get("provenance_complete"), "provenance_complete"
        ),
        "development_time_override": _optional_bool(
            event.get("development_time_override", False),
            "development_time_override",
        ),
        "source_id": _required_text(event.get("source_id"), "source_id"),
        "root_fact_seq": _positive_int(event.get("root_fact_seq"), "root_fact_seq"),
        "occurred_at": _required_text(event.get("occurred_at"), "occurred_at"),
        "answer_window_expires_at": _required_text(
            event.get("answer_window_expires_at"), "answer_window_expires_at"
        ),
    }
    if item["baseline"] is None:
        raise ValueError("baseline is required")
    if item["strategy_determined_before_commit"] is None:
        raise ValueError("strategy_determined_before_commit is required")
    if item["provenance_complete"] is None:
        raise ValueError("provenance_complete is required")
    if _utc_datetime(
        item["answer_window_expires_at"], "answer_window_expires_at"
    ) <= _utc_datetime(item["occurred_at"], "occurred_at"):
        raise ValueError("answer_window_expires_at must follow occurred_at")
    expected_id = compute_cognitive_strategy_exposure_id(
        item["question_id"],
        item["strategy_id"],
        item["strategy_version"],
        item["catalog_version"],
        item["version_set_id"],
    )
    supplied_id = str(event.get("exposure_id") or "").strip()
    if supplied_id and supplied_id != expected_id:
        raise ValueError("strategy exposure_id does not match its canonical identity")
    return {"exposure_id": expected_id, **item}


def _fact_identity(
    exposure_id: str, fact_type: str, source_id: str
) -> str:
    value = [exposure_id, fact_type, source_id]
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_fact(fact: Mapping[str, Any]) -> dict[str, Any]:
    _reject_sensitive_payload(fact)
    exposure_id = _required_text(fact.get("exposure_id"), "exposure_id")
    fact_type = _required_text(fact.get("fact_type"), "fact_type")
    if fact_type not in STRATEGY_FACT_TYPES:
        raise ValueError("unsupported strategy exposure fact type")
    source_id = _required_text(fact.get("source_id"), "source_id")
    item = {
        "exposure_id": exposure_id,
        "fact_type": fact_type,
        "source_id": source_id,
        "root_fact_seq": _positive_int(fact.get("root_fact_seq"), "root_fact_seq"),
        "outcome": str(fact.get("outcome") or "").strip(),
        "status": str(fact.get("status") or "").strip(),
        "attempt_id": str(fact.get("attempt_id") or "").strip(),
        "question_id": str(fact.get("question_id") or "").strip(),
        "episode_id": str(fact.get("episode_id") or "").strip(),
        "linked_exposure_id": str(fact.get("linked_exposure_id") or "").strip(),
        "evaluator_type": str(fact.get("evaluator_type") or "").strip(),
        "evaluator_version": str(fact.get("evaluator_version") or "").strip(),
        "evaluator_confidence": (
            None
            if fact.get("evaluator_confidence") in (None, "")
            else float(fact.get("evaluator_confidence"))
        ),
        "used_hint": _optional_bool(fact.get("used_hint"), "used_hint"),
        "certified": _optional_bool(fact.get("certified"), "certified"),
        "response_time_ms": _optional_nonnegative_int(
            fact.get("response_time_ms"), "response_time_ms"
        ),
        "interval_hours": (
            None
            if fact.get("interval_hours") in (None, "")
            else float(fact.get("interval_hours"))
        ),
        "independent_family": _optional_bool(
            fact.get("independent_family"), "independent_family"
        ),
        "obligation_completed": _optional_bool(
            fact.get("obligation_completed"), "obligation_completed"
        ),
        "provenance_complete": _optional_bool(
            fact.get("provenance_complete"), "provenance_complete"
        ),
        "development_time_override": _optional_bool(
            fact.get("development_time_override"), "development_time_override"
        ),
        "answer_disclosed": _optional_bool(
            fact.get("answer_disclosed"), "answer_disclosed"
        ),
        "independence_known": _optional_bool(
            fact.get("independence_known"), "independence_known"
        ),
        "reason": str(fact.get("reason") or "").strip(),
        "occurred_at": _required_text(fact.get("occurred_at"), "occurred_at"),
    }
    if fact_type in {"attempt", "transfer"}:
        if not item["attempt_id"] or not item["question_id"] or not item["outcome"]:
            raise ValueError(
                f"{fact_type} fact requires attempt_id, question_id, and outcome"
            )
    elif fact_type == "episode" and not item["episode_id"]:
        raise ValueError("episode fact requires episode_id")
    elif fact_type == "retention":
        if not item["episode_id"] or not item["attempt_id"] or not item["outcome"]:
            raise ValueError(
                "retention fact requires episode_id, attempt_id, and outcome"
            )
    elif fact_type == "abandoned" and not item["reason"]:
        raise ValueError("abandoned fact requires reason")
    elif fact_type == "replaced":
        if not item["linked_exposure_id"]:
            raise ValueError("replaced fact requires linked_exposure_id")
        if item["linked_exposure_id"] == exposure_id:
            raise ValueError("strategy exposure cannot replace itself")
    elif fact_type == "attribution_excluded" and not item["reason"]:
        raise ValueError("attribution_excluded fact requires reason")
    confidence = item["evaluator_confidence"]
    if confidence is not None and not 0.0 <= confidence <= 1.0:
        raise ValueError("evaluator_confidence must be between zero and one")
    expected_id = _fact_identity(exposure_id, fact_type, source_id)
    supplied_id = str(fact.get("fact_id") or "").strip()
    if supplied_id and supplied_id != expected_id:
        raise ValueError("strategy fact_id does not match its canonical identity")
    return {"fact_id": expected_id, **item}


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = {key: row[key] for key in row.keys()}
    for field in ("root_fact_seq", "response_time_ms"):
        if field in item and item[field] is not None:
            item[field] = int(item[field])
    for field in (
        "used_hint",
        "certified",
        "baseline",
        "strategy_determined_before_commit",
        "provenance_complete",
        "development_time_override",
        "independent_family",
        "obligation_completed",
        "answer_disclosed",
        "independence_known",
    ):
        if field in item and item[field] is not None:
            item[field] = bool(item[field])
    return item


def _same_row(row: sqlite3.Row, item: Mapping[str, Any]) -> bool:
    return all(row[key] == value for key, value in item.items())


def insert_cognitive_strategy_exposure(
    _store: object,
    conn: sqlite3.Connection,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    """Insert one immutable exposure inside a caller-owned transaction."""

    item = _normalize_exposure(event)
    existing = conn.execute(
        "SELECT * FROM cognitive_strategy_exposures WHERE exposure_id = ?",
        (item["exposure_id"],),
    ).fetchone()
    if existing is not None:
        if not _same_row(existing, item):
            raise ValueError("cognitive strategy exposure identity collision")
        return _row_dict(existing) or {}
    conflicting = conn.execute(
        """
        SELECT exposure_id FROM cognitive_strategy_exposures
        WHERE question_id = ? OR source_id = ?
        LIMIT 1
        """,
        (item["question_id"], item["source_id"]),
    ).fetchone()
    if conflicting is not None:
        raise ValueError("cognitive strategy exposure provenance collision")
    columns = tuple(item)
    conn.execute(
        f"INSERT INTO cognitive_strategy_exposures ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(item[column] for column in columns),
    )
    return dict(item)


def insert_cognitive_strategy_fact(
    _store: object,
    conn: sqlite3.Connection,
    fact: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one outcome, link, or terminal-status fact."""

    item = _normalize_fact(fact)
    if conn.execute(
        "SELECT 1 FROM cognitive_strategy_exposures WHERE exposure_id = ?",
        (item["exposure_id"],),
    ).fetchone() is None:
        raise ValueError("strategy fact requires an existing exposure")
    existing = conn.execute(
        "SELECT * FROM cognitive_strategy_exposure_facts WHERE fact_id = ?",
        (item["fact_id"],),
    ).fetchone()
    if existing is not None:
        if not _same_row(existing, item):
            raise ValueError("cognitive strategy fact identity collision")
        return _row_dict(existing) or {}
    conflicting = conn.execute(
        """
        SELECT fact_id FROM cognitive_strategy_exposure_facts
        WHERE fact_type = ? AND source_id = ?
        LIMIT 1
        """,
        (item["fact_type"], item["source_id"]),
    ).fetchone()
    if conflicting is not None:
        raise ValueError("cognitive strategy fact provenance collision")
    columns = tuple(item)
    conn.execute(
        f"INSERT INTO cognitive_strategy_exposure_facts ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(item[column] for column in columns),
    )
    return dict(item)


def capture_cognitive_strategy_intervention(
    self: object,
    conn: sqlite3.Connection,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one persisted intervention event into the isolated Shadow ledger.

    Missing V3 metadata means collection was disabled and is a no-op.  The
    caller owns failure isolation; this helper never changes intervention state.
    """

    event_type = str(event.get("event_type") or "").strip()
    metadata = event.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    strategy = metadata.get("strategy_exposure")
    strategy = strategy if isinstance(strategy, Mapping) else {}
    question_id = str(event.get("question_id") or "").strip()
    exposure: dict[str, Any] | None = None
    if event_type == "question_committed" and strategy and question_id:
        exposure_payload = {
            "event_type": "question_committed",
            **dict(strategy),
            "question_id": question_id,
            "topic_id": str(
                (event.get("hypothesis_target") or {}).get("topic_id")
                if isinstance(event.get("hypothesis_target"), Mapping)
                else ""
            ).strip(),
            "learner_id": str(strategy.get("learner_id") or "local").strip(),
            "hypothesis_id": str(
                (event.get("hypothesis_target") or {}).get("hypothesis_id")
                if isinstance(event.get("hypothesis_target"), Mapping)
                else ""
            ).strip(),
            "hypothesis_code": str(
                (event.get("hypothesis_target") or {}).get("code")
                if isinstance(event.get("hypothesis_target"), Mapping)
                else ""
            ).strip(),
            "decision_id": str(event.get("decision_id") or "").strip(),
            "blueprint_id": str(event.get("blueprint_id") or "").strip(),
            "diagnostic_validation_id": str(
                event.get("diagnostic_validation_id") or ""
            ).strip(),
            "policy_version": str(event.get("policy_version") or "").strip(),
            "validator_version": str(
                event.get("validator_version") or ""
            ).strip(),
            "question_family_id": str(
                event.get("question_family_id") or ""
            ).strip(),
            "difficulty_bucket": str(
                strategy.get("difficulty_bucket") or ""
            ).strip(),
            "strategy_family": str(
                strategy.get("strategy_family") or ""
            ).strip(),
            "comparison_scope_id": str(
                strategy.get("comparison_scope_id") or ""
            ).strip(),
            "baseline": strategy.get("baseline"),
            "strategy_determined_before_commit": True,
            "provenance_complete": True,
            "development_time_override": bool(
                metadata.get("development_transfer_retest")
            ),
            "source_id": question_id,
            "root_fact_seq": event.get("root_fact_seq"),
            "occurred_at": str(event.get("created_at") or "").strip(),
            "answer_window_expires_at": str(
                strategy.get("answer_window_expires_at") or ""
            ).strip(),
        }
        exposure = insert_cognitive_strategy_exposure(
            self, conn, exposure_payload
        )
    elif question_id:
        row = conn.execute(
            "SELECT * FROM cognitive_strategy_exposures WHERE question_id = ?",
            (question_id,),
        ).fetchone()
        exposure = _row_dict(row)
    if exposure is None:
        return {"recorded": False, "reason": "strategy_shadow_disabled"}

    exposure_id = str(exposure["exposure_id"])
    if event_type == "question_committed":
        replacement_for = str(
            metadata.get("replacement_for_question_id") or ""
        ).strip()
        replacement_fact: dict[str, Any] | None = None
        if replacement_for and replacement_for != question_id:
            replaced = conn.execute(
                "SELECT exposure_id FROM cognitive_strategy_exposures WHERE question_id = ?",
                (replacement_for,),
            ).fetchone()
            if replaced is not None:
                replacement_fact = insert_cognitive_strategy_fact(
                    self,
                    conn,
                    {
                        "fact_type": "replaced",
                        "exposure_id": str(replaced["exposure_id"]),
                        "source_id": question_id,
                        "root_fact_seq": event.get("root_fact_seq"),
                        "occurred_at": str(event.get("created_at") or "").strip(),
                        "linked_exposure_id": exposure_id,
                        "reason": "user_replace",
                        "status": "replaced",
                    },
                )
        return {
            "recorded": True,
            "exposure_id": exposure_id,
            "replacement_fact": replacement_fact,
        }
    if event_type == "attempt_committed":
        attempt_id = _required_text(event.get("attempt_id"), "attempt_id")
        attempt = conn.execute(
            """
            SELECT attempts.question_id, attempts.response_time_ms,
                   attempts.used_hint, attempts.submitted_at,
                   evaluations.evaluator_type, evaluations.evaluator_version,
                   evaluations.confidence
            FROM attempts
            JOIN evaluations ON evaluations.attempt_id = attempts.attempt_id
            WHERE attempts.attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if attempt is None or str(attempt["question_id"] or "") != question_id:
            raise ValueError("strategy attempt provenance is detached")
        certified = bool(
            str(attempt["evaluator_type"] or "").strip()
            and str(attempt["evaluator_version"] or "").strip()
            and attempt["confidence"] is not None
        )
        fact_payload = {
            "fact_type": "attempt",
            "exposure_id": exposure_id,
            "source_id": attempt_id,
            "root_fact_seq": event.get("root_fact_seq"),
            "occurred_at": str(attempt["submitted_at"] or ""),
            "attempt_id": attempt_id,
            "question_id": question_id,
            "outcome": str(event.get("evaluation_verdict") or "").strip(),
            "status": "observed",
            "evaluator_type": str(attempt["evaluator_type"] or "").strip(),
            "evaluator_version": str(
                attempt["evaluator_version"] or ""
            ).strip(),
            "evaluator_confidence": attempt["confidence"],
            "used_hint": (
                None
                if attempt["used_hint"] is None
                else bool(attempt["used_hint"])
            ),
            "certified": certified,
            "provenance_complete": certified,
            "response_time_ms": attempt["response_time_ms"],
        }
        stored = insert_cognitive_strategy_fact(self, conn, fact_payload)
        if str(exposure["question_purpose"]) == "transfer":
            candidates = conn.execute(
                """
                SELECT repair.exposure_id
                FROM cognitive_strategy_exposures repair
                WHERE repair.topic_id = ? AND repair.hypothesis_code = ?
                  AND repair.question_purpose = 'repair'
                  AND repair.root_fact_seq < ?
                  AND EXISTS (
                      SELECT 1 FROM cognitive_strategy_exposure_facts result
                      WHERE result.exposure_id = repair.exposure_id
                        AND result.fact_type = 'attempt'
                        AND result.outcome = 'correct'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM cognitive_strategy_exposure_facts linked
                      WHERE linked.exposure_id = repair.exposure_id
                        AND linked.fact_type = 'transfer'
                  )
                ORDER BY repair.root_fact_seq, repair.source_id, repair.exposure_id
                """,
                (
                    str(exposure["topic_id"]),
                    str(exposure["hypothesis_code"]),
                    int(event.get("root_fact_seq") or 0),
                ),
            ).fetchall()
            if len(candidates) == 1:
                insert_cognitive_strategy_fact(
                    self,
                    conn,
                    {
                        **fact_payload,
                        "fact_type": "transfer",
                        "exposure_id": str(candidates[0]["exposure_id"]),
                        "linked_exposure_id": exposure_id,
                    },
                )
            elif len(candidates) > 1:
                for candidate in candidates:
                    candidate_id = str(candidate["exposure_id"])
                    insert_cognitive_strategy_fact(
                        self,
                        conn,
                        {
                            "fact_type": "attribution_excluded",
                            "exposure_id": candidate_id,
                            "source_id": f"{attempt_id}:{candidate_id}",
                            "root_fact_seq": event.get("root_fact_seq"),
                            "occurred_at": str(attempt["submitted_at"] or ""),
                            "reason": "ambiguous_attribution",
                            "status": "excluded",
                        },
                    )
        return {"recorded": True, "exposure_id": exposure_id, "fact": stored}
    if event_type == "intervention_abandoned":
        stored = insert_cognitive_strategy_fact(
            self,
            conn,
            {
                "fact_type": "abandoned",
                "exposure_id": exposure_id,
                "source_id": str(event.get("event_id") or "").strip(),
                "root_fact_seq": event.get("root_fact_seq"),
                "occurred_at": str(event.get("created_at") or "").strip(),
                "reason": str(event.get("abandonment_reason") or "").strip(),
                "status": "abandoned",
            },
        )
        return {"recorded": True, "exposure_id": exposure_id, "fact": stored}
    return {"recorded": False, "reason": "unsupported_intervention_event"}


def capture_cognitive_strategy_episode(
    self: object,
    conn: sqlite3.Connection,
    *,
    transfer_attempt_id: str,
    episode_id: str,
    occurred_at: str,
) -> dict[str, Any]:
    """Link a certified monitoring episode to one unambiguous repair exposure."""

    attempt_id = _required_text(transfer_attempt_id, "transfer_attempt_id")
    episode_key = _required_text(episode_id, "episode_id")
    matches = conn.execute(
        """
        SELECT facts.exposure_id, facts.root_fact_seq
        FROM cognitive_strategy_exposure_facts facts
        WHERE facts.fact_type = 'transfer' AND facts.source_id = ?
        ORDER BY facts.exposure_id
        """,
        (attempt_id,),
    ).fetchall()
    if len(matches) != 1:
        return {"recorded": False, "reason": "ambiguous_attribution"}
    stored = insert_cognitive_strategy_fact(
        self,
        conn,
        {
            "fact_type": "episode",
            "exposure_id": str(matches[0]["exposure_id"]),
            "source_id": episode_key,
            "root_fact_seq": int(matches[0]["root_fact_seq"]),
            "episode_id": episode_key,
            "status": "open",
            "occurred_at": _required_text(occurred_at, "occurred_at"),
        },
    )
    return {"recorded": True, "fact": stored}


def capture_cognitive_strategy_retention(
    self: object,
    conn: sqlite3.Connection,
    *,
    episode_id: str,
    attempt_id: str,
    outcome: str,
    occurred_at: str,
    evaluator_type: str,
    evaluator_version: str,
    evaluator_confidence: float | None,
    used_hint: bool | None,
    certified: bool,
    interval_hours: float | None,
    independent_family: bool,
    development_time_override: bool = False,
) -> dict[str, Any]:
    """Append a certified retention endpoint to its repair exposure."""

    episode_key = _required_text(episode_id, "episode_id")
    matches = conn.execute(
        """
        SELECT exposure_id FROM cognitive_strategy_exposure_facts
        WHERE fact_type = 'episode' AND episode_id = ?
        ORDER BY exposure_id
        """,
        (episode_key,),
    ).fetchall()
    if len(matches) != 1:
        return {"recorded": False, "reason": "ambiguous_attribution"}
    root = conn.execute(
        "SELECT root_fact_seq FROM attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    if root is None:
        raise ValueError("retention attempt root fact is missing")
    stored = insert_cognitive_strategy_fact(
        self,
        conn,
        {
            "fact_type": "retention",
            "exposure_id": str(matches[0]["exposure_id"]),
            "source_id": attempt_id,
            "root_fact_seq": int(root["root_fact_seq"]),
            "episode_id": episode_key,
            "attempt_id": attempt_id,
            "outcome": _required_text(outcome, "outcome"),
            "status": "observed",
            "occurred_at": _required_text(occurred_at, "occurred_at"),
            "evaluator_type": str(evaluator_type or "").strip(),
            "evaluator_version": str(evaluator_version or "").strip(),
            "evaluator_confidence": evaluator_confidence,
            "used_hint": used_hint,
            "certified": certified,
            "interval_hours": interval_hours,
            "independent_family": independent_family,
            "obligation_completed": True,
            "provenance_complete": bool(
                evaluator_type and evaluator_version and certified
            ),
            "development_time_override": development_time_override,
            "independence_known": True,
        },
    )
    return {"recorded": True, "fact": stored}


def _write_transaction(store: object):
    lock = getattr(store, "_lock", None)
    return lock if lock is not None else nullcontext()


def record_cognitive_strategy_exposure(
    self: object, event: Mapping[str, Any]
) -> dict[str, Any]:
    with _write_transaction(self):
        conn = self._require_conn()  # type: ignore[attr-defined]
        try:
            result = insert_cognitive_strategy_exposure(self, conn, event)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def record_cognitive_strategy_fact(
    self: object, fact: Mapping[str, Any]
) -> dict[str, Any]:
    with _write_transaction(self):
        conn = self._require_conn()  # type: ignore[attr-defined]
        try:
            result = insert_cognitive_strategy_fact(self, conn, fact)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def _read_conn(store: object) -> sqlite3.Connection:
    getter = getattr(store, "_require_read_conn", None)
    if getter is not None:
        return getter()
    return store._require_conn()  # type: ignore[attr-defined]


def _database_cutoffs(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    present = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'cognitive_delete_cutoffs'
        """
    ).fetchone()
    if present is None:
        return {}
    return {
        (str(row["topic_id"]), str(row["hypothesis_code"])): int(
            row["delete_cutoff_seq"]
        )
        for row in conn.execute(
            """
            SELECT topic_id, hypothesis_code, delete_cutoff_seq
            FROM cognitive_delete_cutoffs
            """
        ).fetchall()
    }


def _normalize_cutoffs(
    cutoffs: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], int]:
    result: dict[tuple[str, str], int] = {}
    for item in cutoffs:
        key = (
            _required_text(item.get("topic_id"), "cutoff topic_id"),
            _required_text(
                item.get("hypothesis_code"), "cutoff hypothesis_code"
            ),
        )
        seq = _positive_int(item.get("delete_cutoff_seq"), "delete_cutoff_seq")
        result[key] = max(result.get(key, 0), seq)
    return result


def _visible_exposure(
    exposure: Mapping[str, Any], cutoffs: Mapping[tuple[str, str], int]
) -> bool:
    cutoff = cutoffs.get(
        (str(exposure["topic_id"]), str(exposure["hypothesis_code"])), 0
    )
    return int(exposure["root_fact_seq"]) > cutoff


def list_cognitive_strategy_exposures(
    self: object,
    *,
    topic_id: str = "",
    hypothesis_code: str = "",
    root_fact_seq_lte: int | None = None,
    include_deleted: bool = False,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    conn = _read_conn(self)
    clauses: list[str] = []
    values: list[object] = []
    if topic_id:
        clauses.append("topic_id = ?")
        values.append(str(topic_id))
    if hypothesis_code:
        clauses.append("hypothesis_code = ?")
        values.append(str(hypothesis_code))
    if root_fact_seq_lte is not None:
        clauses.append("root_fact_seq <= ?")
        values.append(_positive_int(root_fact_seq_lte, "root_fact_seq_lte"))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    safe_limit = max(1, min(int(limit), 100_000))
    rows = conn.execute(
        f"""
        SELECT * FROM cognitive_strategy_exposures
        {where}
        ORDER BY root_fact_seq, source_id, exposure_id
        LIMIT ?
        """,
        (*values, safe_limit),
    ).fetchall()
    items = [_row_dict(row) or {} for row in rows]
    if not include_deleted:
        cutoffs = _database_cutoffs(conn)
        items = [item for item in items if _visible_exposure(item, cutoffs)]
    return items


def list_cognitive_strategy_facts(
    self: object,
    *,
    exposure_id: str = "",
    root_fact_seq_lte: int | None = None,
    include_deleted: bool = False,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    conn = _read_conn(self)
    clauses: list[str] = []
    values: list[object] = []
    if exposure_id:
        clauses.append("facts.exposure_id = ?")
        values.append(str(exposure_id))
    if root_fact_seq_lte is not None:
        clauses.append("facts.root_fact_seq <= ?")
        values.append(_positive_int(root_fact_seq_lte, "root_fact_seq_lte"))
    if not include_deleted:
        clauses.append(
            "facts.root_fact_seq > COALESCE(cutoffs.delete_cutoff_seq, 0)"
        )
        clauses.append(
            "exposures.root_fact_seq > COALESCE(cutoffs.delete_cutoff_seq, 0)"
        )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    safe_limit = max(1, min(int(limit), 100_000))
    cutoff_table = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'cognitive_delete_cutoffs'
        """
    ).fetchone()
    if cutoff_table is None:
        join = ""
        where = where.replace(
            "facts.root_fact_seq > COALESCE(cutoffs.delete_cutoff_seq, 0) AND ", ""
        ).replace(
            "exposures.root_fact_seq > COALESCE(cutoffs.delete_cutoff_seq, 0)", "1 = 1"
        )
    else:
        join = """
            LEFT JOIN cognitive_delete_cutoffs cutoffs
              ON cutoffs.topic_id = exposures.topic_id
             AND cutoffs.hypothesis_code = exposures.hypothesis_code
        """
    rows = conn.execute(
        f"""
        SELECT facts.*
        FROM cognitive_strategy_exposure_facts facts
        JOIN cognitive_strategy_exposures exposures
          ON exposures.exposure_id = facts.exposure_id
        {join}
        {where}
        ORDER BY facts.root_fact_seq, facts.source_id, facts.fact_id
        LIMIT ?
        """,
        (*values, safe_limit),
    ).fetchall()
    return [_row_dict(row) or {} for row in rows]


def get_cognitive_strategy_exposure_snapshot(
    self: object,
    exposure_id: str,
    *,
    as_of_root_fact_seq: int | None = None,
    include_deleted: bool = False,
) -> dict[str, Any] | None:
    exposures = list_cognitive_strategy_exposures(
        self,
        root_fact_seq_lte=as_of_root_fact_seq,
        include_deleted=include_deleted,
    )
    exposure = next(
        (item for item in exposures if item["exposure_id"] == exposure_id), None
    )
    if exposure is None:
        return None
    facts = list_cognitive_strategy_facts(
        self,
        exposure_id=exposure_id,
        root_fact_seq_lte=as_of_root_fact_seq,
        include_deleted=include_deleted,
    )
    current_status = "delivered"
    latest: dict[str, dict[str, Any]] = {}
    for fact in facts:
        fact_type = str(fact["fact_type"])
        latest[fact_type] = fact
        current_status = {
            "attempt": "attempted",
            "transfer": "transfer_observed",
            "episode": "monitoring",
            "retention": "retention_observed",
            "abandoned": "abandoned",
            "replaced": "replaced",
            "attribution_excluded": "excluded",
        }[fact_type]
    return {
        "exposure": exposure,
        "current_status": current_status,
        "latest_by_type": latest,
        "facts": facts,
    }


def rebuild_cognitive_strategy_ledger(
    self: object,
    exposure_events: Iterable[Mapping[str, Any]],
    facts: Iterable[Mapping[str, Any]],
    *,
    delete_cutoffs: Iterable[Mapping[str, Any]] = (),
) -> dict[str, int]:
    """Deterministically rebuild the two V3 tables from persisted sources."""

    normalized_exposures = [_normalize_exposure(item) for item in exposure_events]
    by_exposure: dict[str, dict[str, Any]] = {}
    question_ids: dict[str, str] = {}
    source_ids: dict[str, str] = {}
    for item in normalized_exposures:
        exposure_id = str(item["exposure_id"])
        prior = by_exposure.get(exposure_id)
        if prior is not None and prior != item:
            raise ValueError("cognitive strategy exposure identity collision")
        for field, seen in (
            ("question_id", question_ids),
            ("source_id", source_ids),
        ):
            value = str(item[field])
            if value in seen and seen[value] != exposure_id:
                raise ValueError("cognitive strategy exposure provenance collision")
            seen[value] = exposure_id
        by_exposure[exposure_id] = item
    cutoffs = _normalize_cutoffs(delete_cutoffs)
    kept_exposures = {
        key: item
        for key, item in by_exposure.items()
        if _visible_exposure(item, cutoffs)
    }

    normalized_facts = [_normalize_fact(item) for item in facts]
    by_fact: dict[str, dict[str, Any]] = {}
    provenance: dict[tuple[str, str], str] = {}
    for item in normalized_facts:
        fact_id = str(item["fact_id"])
        prior = by_fact.get(fact_id)
        if prior is not None and prior != item:
            raise ValueError("cognitive strategy fact identity collision")
        key = (str(item["fact_type"]), str(item["source_id"]))
        if key in provenance and provenance[key] != fact_id:
            raise ValueError("cognitive strategy fact provenance collision")
        provenance[key] = fact_id
        exposure = kept_exposures.get(str(item["exposure_id"]))
        if exposure is None:
            continue
        cutoff = cutoffs.get(
            (str(exposure["topic_id"]), str(exposure["hypothesis_code"])), 0
        )
        if int(item["root_fact_seq"]) > cutoff:
            by_fact[fact_id] = item

    with _write_transaction(self):
        conn = self._require_conn()  # type: ignore[attr-defined]
        try:
            conn.execute("DELETE FROM cognitive_strategy_exposure_facts")
            conn.execute("DELETE FROM cognitive_strategy_exposures")
            for exposure_id in sorted(kept_exposures):
                insert_cognitive_strategy_exposure(
                    self, conn, {"event_type": "question_committed", **kept_exposures[exposure_id]}
                )
            ordered_facts = sorted(
                by_fact.values(),
                key=lambda item: (
                    int(item["root_fact_seq"]),
                    str(item["source_id"]),
                    str(item["fact_id"]),
                ),
            )
            for fact in ordered_facts:
                insert_cognitive_strategy_fact(self, conn, fact)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"exposures": len(kept_exposures), "facts": len(by_fact)}


def rebuild_cognitive_strategy_from_persisted_sources(
    self: object,
) -> dict[str, int]:
    """Rebuild V3 solely from canonical V2 question, answer, and retention facts."""

    list_events = getattr(self, "list_cognitive_intervention_events", None)
    if not callable(list_events):
        raise RuntimeError("cognitive intervention source ledger is unavailable")
    with _write_transaction(self):
        conn = self._require_conn()  # type: ignore[attr-defined]
        try:
            raw_events = list_events(limit=100_000)
            raw_event_rows: list[object] = (
                raw_events if isinstance(raw_events, list) else []
            )
            events = sorted(
                (
                    dict(item)
                    for item in raw_event_rows
                    if isinstance(item, Mapping)
                ),
                key=lambda item: (
                    int(item.get("root_fact_seq") or 0),
                    int(item.get("event_seq") or 0),
                    str(item.get("event_id") or ""),
                ),
            )
            episodes = conn.execute(
                """
                SELECT episode_id, source_attempt_id, opened_at
                FROM cognitive_monitoring_episodes
                ORDER BY opened_at, episode_id
                """
            ).fetchall()
            retentions = conn.execute(
                """
                SELECT satisfactions.attempt_id, satisfactions.episode_id,
                       satisfactions.disposition, satisfactions.metadata_json,
                       satisfactions.occurred_at, episodes.opened_at,
                       attempts.used_hint, evaluations.evaluator_type,
                       evaluations.evaluator_version, evaluations.confidence,
                       (julianday(satisfactions.occurred_at)
                        - julianday(episodes.opened_at)) * 24.0 AS interval_hours
                FROM cognitive_obligation_satisfactions satisfactions
                JOIN cognitive_monitoring_episodes episodes
                  ON episodes.episode_id = satisfactions.episode_id
                JOIN attempts ON attempts.attempt_id = satisfactions.attempt_id
                LEFT JOIN evaluations
                  ON evaluations.attempt_id = satisfactions.attempt_id
                ORDER BY attempts.root_fact_seq, satisfactions.satisfaction_id
                """
            ).fetchall()
            cutoffs = _database_cutoffs(conn)
            conn.execute("DELETE FROM cognitive_strategy_exposure_facts")
            conn.execute("DELETE FROM cognitive_strategy_exposures")

            for event in events:
                target = event.get("hypothesis_target")
                hypothesis = target if isinstance(target, Mapping) else {}
                cutoff = cutoffs.get(
                    (
                        str(hypothesis.get("topic_id") or ""),
                        str(hypothesis.get("code") or ""),
                    ),
                    0,
                )
                if int(event.get("root_fact_seq") or 0) <= cutoff:
                    continue
                capture_cognitive_strategy_intervention(self, conn, event)

            for episode in episodes:
                capture_cognitive_strategy_episode(
                    self,
                    conn,
                    transfer_attempt_id=str(episode["source_attempt_id"] or ""),
                    episode_id=str(episode["episode_id"] or ""),
                    occurred_at=str(episode["opened_at"] or ""),
                )

            independence_errors = {
                "transfer_question_family_reused",
                "retention_question_family_reused",
                "independence_group_reused",
            }
            loads = getattr(self, "_json_loads", None)
            for retention in retentions:
                metadata = (
                    loads(retention["metadata_json"], {})
                    if callable(loads)
                    else json.loads(str(retention["metadata_json"] or "{}"))
                )
                metadata = metadata if isinstance(metadata, Mapping) else {}
                reasons = {
                    str(item)
                    for item in metadata.get("reasons", ())
                    if str(item)
                }
                capture_cognitive_strategy_retention(
                    self,
                    conn,
                    episode_id=str(retention["episode_id"] or ""),
                    attempt_id=str(retention["attempt_id"] or ""),
                    outcome=str(retention["disposition"] or ""),
                    occurred_at=str(retention["occurred_at"] or ""),
                    evaluator_type=str(retention["evaluator_type"] or ""),
                    evaluator_version=str(retention["evaluator_version"] or ""),
                    evaluator_confidence=(
                        None
                        if retention["confidence"] is None
                        else float(retention["confidence"])
                    ),
                    used_hint=(
                        None
                        if retention["used_hint"] is None
                        else bool(retention["used_hint"])
                    ),
                    certified=bool(metadata.get("certified")),
                    interval_hours=(
                        None
                        if retention["interval_hours"] is None
                        else float(retention["interval_hours"])
                    ),
                    independent_family=not bool(
                        independence_errors.intersection(reasons)
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    exposure_count = conn.execute(
        "SELECT COUNT(*) FROM cognitive_strategy_exposures"
    ).fetchone()[0]
    fact_count = conn.execute(
        "SELECT COUNT(*) FROM cognitive_strategy_exposure_facts"
    ).fetchone()[0]
    return {"exposures": int(exposure_count), "facts": int(fact_count)}


def build_cognitive_strategy_report_snapshot(
    self: object,
    *,
    as_of_root_fact_seq: int | None = None,
    catalog_version: str = "",
    version_set_id: str = "",
) -> dict[str, Any]:
    """Build the canonical, answer-free input consumed by the pure reporter."""

    try:
        from .adaptive_learning.cognitive_strategy_catalog import (
            COGNITIVE_STRATEGY_CATALOG_V1,
        )
    except ImportError:  # pragma: no cover - direct repository CLI import
        from adaptive_learning.cognitive_strategy_catalog import (  # type: ignore[no-redef]
            COGNITIVE_STRATEGY_CATALOG_V1,
        )

    conn = _read_conn(self)
    if as_of_root_fact_seq is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(root_fact_seq), 0) AS seq FROM cognitive_fact_roots"
        ).fetchone()
        as_of = int(row["seq"] if row is not None else 0)
    else:
        as_of = int(as_of_root_fact_seq)
        if as_of < 0:
            raise ValueError("as_of_root_fact_seq must be non-negative")
    boundary_row = conn.execute(
        """
        SELECT effective_at FROM cognitive_fact_roots
        WHERE root_fact_seq <= ?
        ORDER BY root_fact_seq DESC LIMIT 1
        """,
        (as_of,),
    ).fetchone()
    as_of_effective_at = str(
        boundary_row["effective_at"] if boundary_row is not None else ""
    ).strip()
    catalog_name = str(
        catalog_version or COGNITIVE_STRATEGY_CATALOG_V1.catalog_version
    ).strip()
    versions = str(version_set_id or "cognitive-v2.1-1").strip()
    exposures = (
        list_cognitive_strategy_exposures(
            self,
            root_fact_seq_lte=as_of,
            limit=100_000,
        )
        if as_of > 0
        else []
    )
    facts = (
        list_cognitive_strategy_facts(
            self,
            root_fact_seq_lte=as_of,
            limit=100_000,
        )
        if as_of > 0
        else []
    )
    facts_by_exposure: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        facts_by_exposure.setdefault(str(fact["exposure_id"]), []).append(fact)

    def utc(value: object) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def endpoint(
        fact: Mapping[str, Any],
        *,
        answer_window_expires_at: str = "",
        retention: bool = False,
    ) -> dict[str, Any]:
        outcome = str(fact.get("outcome") or "").strip().lower()
        occurred = utc(fact.get("occurred_at"))
        expires = utc(answer_window_expires_at)
        result: dict[str, Any] = {
            "status": str(fact.get("status") or "observed"),
            "outcome": outcome,
            "success": outcome in {"correct", "success", "passed", "resolved"},
            "root_fact_seq": int(fact["root_fact_seq"]),
            "certified": fact.get("certified") is True,
            "provenance_complete": fact.get("provenance_complete") is True,
            "late": bool(occurred and expires and occurred > expires),
        }
        if retention:
            result.update(
                {
                    "interval_hours": fact.get("interval_hours"),
                    "independent_family": fact.get("independent_family") is True,
                    "hint_used": fact.get("used_hint") is not False,
                    "obligation_completed": fact.get("obligation_completed") is True,
                    "development_time_override": fact.get(
                        "development_time_override"
                    )
                    is True,
                    "answer_disclosed": fact.get("answer_disclosed") is True,
                    "independence_known": fact.get("independence_known") is True,
                }
            )
        return result

    report_exposures: list[dict[str, Any]] = []
    for exposure in exposures:
        if (
            str(exposure["question_purpose"]) != "repair"
            or str(exposure["catalog_version"]) != catalog_name
            or str(exposure["version_set_id"]) != versions
        ):
            continue
        linked = facts_by_exposure.get(str(exposure["exposure_id"]), [])
        latest: dict[str, dict[str, Any]] = {}
        for fact in linked:
            latest[str(fact["fact_type"])] = fact
        status = "delivered"
        if "abandoned" in latest:
            status = "abandoned"
        if "replaced" in latest:
            status = "replaced"
        attribution_excluded = latest.get("attribution_excluded")
        immediate = latest.get("attempt")
        transfer = latest.get("transfer")
        retention = latest.get("retention")
        episode = latest.get("episode")
        hint_state = (
            "used"
            if immediate and immediate.get("used_hint") is True
            else "none"
            if immediate and immediate.get("used_hint") is False
            else "unknown"
        )
        outcomes: dict[str, Any] = {}
        if immediate:
            outcomes["immediate"] = endpoint(
                immediate,
                answer_window_expires_at=str(
                    exposure["answer_window_expires_at"]
                ),
            )
        elif status not in {"abandoned", "replaced"}:
            boundary_time = utc(as_of_effective_at)
            answer_deadline = utc(exposure["answer_window_expires_at"])
            outcomes["immediate"] = {
                "status": (
                    "pending"
                    if boundary_time and answer_deadline and boundary_time <= answer_deadline
                    else "missing_outcome"
                )
            }
        if transfer:
            outcomes["transfer"] = endpoint(transfer)
        if retention:
            outcomes["retention"] = endpoint(retention, retention=True)
        report_exposures.append(
            {
                "root_fact_seq": int(exposure["root_fact_seq"]),
                "source_id": str(exposure["source_id"]),
                "exposure_id": str(exposure["exposure_id"]),
                "learner_id": str(exposure["learner_id"]),
                "topic_id": str(exposure["topic_id"]),
                "hypothesis_id": str(exposure["hypothesis_id"]),
                "hypothesis_code": str(exposure["hypothesis_code"]),
                "episode_id": str(episode.get("episode_id") or "")
                if episode
                else "",
                "question_family_id": str(exposure["question_family_id"]),
                "difficulty_bucket": str(exposure["difficulty_bucket"]),
                "hint_state": hint_state,
            "strategy_id": str(exposure["strategy_id"]),
            "strategy_version": str(exposure["strategy_version"]),
            "question_purpose": str(exposure["question_purpose"]),
            "comparison_scope_id": str(exposure["comparison_scope_id"]),
                "catalog_version": str(exposure["catalog_version"]),
                "version_set_id": str(exposure["version_set_id"]),
                "question_committed": True,
                "strategy_determined_before_commit": bool(
                    exposure["strategy_determined_before_commit"]
                ),
                "provenance_complete": bool(exposure["provenance_complete"]),
                "development_time_override": bool(
                    exposure["development_time_override"]
                ),
                "exclusion_reasons": (
                    [str(attribution_excluded.get("reason") or "")]
                    if attribution_excluded
                    else []
                ),
                "catalog_valid": True,
                "exposed_at": str(exposure["occurred_at"]),
                "answer_window_expires_at": str(
                    exposure["answer_window_expires_at"]
                ),
                "status": status,
                "outcomes": outcomes,
            }
        )

    catalog_entries = [
        {
            "strategy_id": entry.strategy_id,
            "strategy_version": entry.strategy_version,
            "catalog_version": entry.catalog_version,
            "version_set_id": versions,
            "baseline": entry.baseline,
        }
        for entry in COGNITIVE_STRATEGY_CATALOG_V1.entries(
            measurement_purpose="repair"
        )
    ]
    return {
        "as_of_root_fact_seq": as_of,
        "catalog_version": catalog_name,
        "version_set_id": versions,
        "catalog": catalog_entries,
        "exposures": report_exposures,
    }


__all__ = [
    "QUESTION_PURPOSES",
    "STRATEGY_FACT_TYPES",
    "capture_cognitive_strategy_intervention",
    "capture_cognitive_strategy_episode",
    "capture_cognitive_strategy_retention",
    "build_cognitive_strategy_report_snapshot",
    "compute_cognitive_strategy_exposure_id",
    "create_cognitive_strategy_schema",
    "get_cognitive_strategy_exposure_snapshot",
    "insert_cognitive_strategy_exposure",
    "insert_cognitive_strategy_fact",
    "list_cognitive_strategy_exposures",
    "list_cognitive_strategy_facts",
    "rebuild_cognitive_strategy_ledger",
    "rebuild_cognitive_strategy_from_persisted_sources",
    "record_cognitive_strategy_exposure",
    "record_cognitive_strategy_fact",
]
