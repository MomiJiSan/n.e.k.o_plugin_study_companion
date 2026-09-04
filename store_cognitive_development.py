"""Explicit, fail-closed development preparation for retention checks.

This module is intentionally not bound onto :class:`StudyStore`.  The local
development entry calls :func:`prepare_cognitive_retention_for_development`
directly, and the helper remains unavailable unless the process-level opt-in
and the full active retention configuration are both present.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .adaptive_learning.cognitive_catalog import COGNITIVE_CATALOG_V1
from .adaptive_learning.cognitive_versions import (
    DEFAULT_COGNITIVE_VERSION_SET,
    get_cognitive_version_set,
)
from .store_cognitive_outbox import (
    _insert_requested_transfer_episode,
    _private_question_value,
)
from .store_cognitive_retention import (
    ACTIVE_RETENTION_HYPOTHESIS,
    RETENTION_ELIGIBILITY,
    _stable_id,
)
from .store_common import sqlite3

_DEV_ENVIRONMENT_VARIABLE = "STUDY_COMPANION_COGNITIVE_DEV_TOOLS"
_DEVELOPMENT_TOPIC_ID = "college_chain_rule"
_DEVELOPMENT_HYPOTHESIS_CODE = "omit_inner_derivative"
_DEVELOPMENT_REASON = "development_time_override"


class _PreparationBlocked(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class _PreparedContext:
    payload: dict[str, Any]
    hypothesis_id: str
    model_version: str
    source_attempt_id: str
    episode_id: str
    obligation_id: str
    original_not_before: str
    due_by: str
    eligibility_until: str
    existing_episode: bool
    existing_override: bool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise _PreparationBlocked("transfer_not_certified")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _PreparationBlocked("transfer_not_certified") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _attempt_storage_now(value: datetime) -> datetime:
    """Match SQLite ``datetime('now')`` resolution used by attempt facts."""

    return value.astimezone(timezone.utc).replace(microsecond=0)


def _base_result(
    *,
    enabled: bool,
    status: str,
    topic_id: str,
    hypothesis_code: str,
    reason_code: str = "",
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "status": status,
        "reason_code": reason_code,
        "development_override": False,
        "topic_id": topic_id,
        "hypothesis_code": hypothesis_code,
    }


def _blocked_result(
    reason_code: str, *, topic_id: str, hypothesis_code: str
) -> dict[str, Any]:
    return _base_result(
        enabled=reason_code != "dev_tools_disabled",
        status="blocked",
        reason_code=reason_code,
        topic_id=topic_id,
        hypothesis_code=hypothesis_code,
    )


def _json_mapping(store: Any, value: object) -> dict[str, Any]:
    loader = getattr(store, "_json_loads", None)
    decoded = loader(value, {}) if callable(loader) else {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _require_active_retention_config(
    store: Any, conn: sqlite3.Connection
) -> str:
    row = conn.execute("SELECT value FROM kv WHERE key = 'config'").fetchone()
    raw = _json_mapping(store, row["value"] if row is not None else None)
    cognitive = raw.get("cognitive")
    config = cognitive if isinstance(cognitive, Mapping) else {}
    full_gate = (
        config.get("projection_enabled") is True
        and str(config.get("read_mode") or "").strip().lower() == "active"
        and str(config.get("intent_policy") or "").strip().lower() == "on"
        and config.get("retention_enabled") is True
    )
    supported_topics = config.get("supported_topics")
    topic_enabled = isinstance(supported_topics, (list, tuple)) and (
        _DEVELOPMENT_TOPIC_ID in supported_topics
    )
    if not full_gate or not topic_enabled:
        raise _PreparationBlocked("retention_disabled")
    versions = get_cognitive_version_set(config.get("version_set"))
    if versions is None or versions.name != DEFAULT_COGNITIVE_VERSION_SET:
        raise _PreparationBlocked("transfer_not_certified")
    return versions.projection_version


def _current_hypothesis(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    hypothesis_code: str,
    expected_source_attempt_id: str,
    model_version: str,
) -> sqlite3.Row:
    rows = conn.execute(
        """
        SELECT * FROM cognitive_hypothesis_current
        WHERE topic_id = ? AND hypothesis_code = ? AND model_version = ?
        ORDER BY computed_at DESC
        """,
        (topic_id, hypothesis_code, model_version),
    ).fetchall()
    if len(rows) != 1:
        raise _PreparationBlocked("transfer_not_certified")
    current = rows[0]
    if expected_source_attempt_id and (
        str(current["source_attempt_id"] or "") != expected_source_attempt_id
    ):
        raise _PreparationBlocked("source_attempt_mismatch")
    if (
        str(current["evidence_status"] or "") != "supported"
        or str(current["intervention_stage"] or "") != "monitored"
        or str(current["status"] or "") != "monitored"
        or str(current["last_intent"] or "") != "transfer_check"
        or str(current["last_outcome"] or "") != "correct"
    ):
        raise _PreparationBlocked("transfer_not_certified")
    hypothesis_id = str(current["hypothesis_id"] or "").strip()
    model_version = str(current["model_version"] or "").strip()
    if not hypothesis_id or not model_version:
        raise _PreparationBlocked("transfer_not_certified")
    queue = conn.execute(
        """
        SELECT * FROM cognitive_topic_projection_queue
        WHERE topic_id = ? AND model_version = ?
        """,
        (topic_id, model_version),
    ).fetchone()
    if (
        queue is None
        or str(queue["status"] or "") != "done"
        or int(queue["requested_generation"] or 0)
        != int(queue["projected_generation"] or 0)
        or int(queue["claimed_generation"] or 0)
        != int(queue["projected_generation"] or 0)
        or int(current["projected_generation"] or 0)
        != int(queue["projected_generation"] or 0)
    ):
        raise _PreparationBlocked("projection_stale")
    return current


def _completed_transfer_payload(
    store: Any,
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    topic_id: str,
    hypothesis_code: str,
    hypothesis_id: str,
    model_version: str,
) -> tuple[dict[str, Any], sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT outbox_id, event_id, payload_json
        FROM cognitive_outbox
        WHERE attempt_id = ? AND operation = 'intervention_event'
          AND status = 'done'
        ORDER BY created_at, outbox_id
        """,
        (attempt_id,),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_mapping(store, row["payload_json"])
        if str(payload.get("event_type") or "") == "attempt_committed":
            candidates.append(payload)
    if len(candidates) != 1:
        raise _PreparationBlocked("transfer_not_certified")
    payload = candidates[0]
    target = payload.get("hypothesis_target")
    binding = payload.get("binding")
    if not isinstance(target, Mapping) or not isinstance(binding, Mapping):
        raise _PreparationBlocked("transfer_not_certified")
    if (
        str(payload.get("attempt_id") or "") != attempt_id
        or str(payload.get("event_type") or "") != "attempt_committed"
        or str(payload.get("learning_intent") or "") != "transfer_check"
        or str(payload.get("evaluation_verdict") or "") != "correct"
        or str(target.get("hypothesis_id") or "") != hypothesis_id
        or str(target.get("topic_id") or "") != topic_id
        or str(target.get("code") or "") != hypothesis_code
        or str(target.get("model_version") or "") != model_version
        or str(binding.get("topic_id") or "") != topic_id
    ):
        raise _PreparationBlocked("transfer_not_certified")
    question_id = str(payload.get("question_id") or "").strip()
    if not question_id:
        raise _PreparationBlocked("transfer_not_certified")
    fact = conn.execute(
        """
        SELECT attempts.question_id, attempts.topic_id, attempts.used_hint,
               attempts.submitted_at, questions.question_json,
               evaluations.evaluation_json, evaluations.evaluator_type,
               evaluations.evaluator_version, evaluations.confidence
        FROM attempts
        JOIN question_instances questions
          ON questions.question_id = attempts.question_id
        JOIN evaluations ON evaluations.attempt_id = attempts.attempt_id
        WHERE attempts.attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    if (
        fact is None
        or str(fact["question_id"] or "") != question_id
        or str(fact["topic_id"] or "") != topic_id
        or fact["used_hint"] != 0
    ):
        raise _PreparationBlocked("transfer_not_certified")
    evaluation = _json_mapping(store, fact["evaluation_json"])
    question = _json_mapping(store, fact["question_json"])
    evaluator_type = str(evaluation.get("evaluator_type") or "").strip()
    evaluator_version = str(evaluation.get("evaluator_version") or "").strip()
    try:
        evaluator_confidence = float(evaluation.get("confidence"))
        stored_confidence = float(fact["confidence"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise _PreparationBlocked("transfer_not_certified") from exc
    if (
        str(evaluation.get("verdict") or "").strip().lower() != "correct"
        or not evaluator_type
        or not evaluator_version
        or evaluator_type != str(fact["evaluator_type"] or "").strip()
        or evaluator_version != str(fact["evaluator_version"] or "").strip()
        or not math.isfinite(evaluator_confidence)
        or not math.isfinite(stored_confidence)
        or not 0.0 <= evaluator_confidence <= 1.0
        or evaluator_confidence != stored_confidence
    ):
        raise _PreparationBlocked("transfer_not_certified")
    versions = get_cognitive_version_set(DEFAULT_COGNITIVE_VERSION_SET)
    blueprint_id = str(payload.get("blueprint_id") or "").strip()
    question_family_id = str(payload.get("question_family_id") or "").strip()
    validator_version = str(payload.get("validator_version") or "").strip()
    blueprint = COGNITIVE_CATALOG_V1.get_blueprint(blueprint_id)
    if (
        versions is None
        or model_version != versions.projection_version
        or validator_version != versions.validator_version
        or blueprint is None
        or blueprint.learning_intent != "transfer_check"
        or blueprint.hypothesis_code != hypothesis_code
        or blueprint.question_family_id != question_family_id
        or COGNITIVE_CATALOG_V1.canonical_topic_id(topic_id) != blueprint.topic_id
    ):
        raise _PreparationBlocked("transfer_not_certified")
    expected_question_fields = {
        "cognitive_blueprint_id": blueprint_id,
        "cognitive_question_family_id": question_family_id,
        "cognitive_validator_version": validator_version,
        "diagnostic_validation_id": str(
            payload.get("diagnostic_validation_id") or ""
        ).strip(),
    }
    if any(
        str(_private_question_value(question, key) or "").strip() != expected
        for key, expected in expected_question_fields.items()
    ):
        raise _PreparationBlocked("transfer_not_certified")
    return payload, fact


def _reject_active_control(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    hypothesis_code: str,
    now: datetime,
) -> None:
    control = conn.execute(
        """
        SELECT action, expires_at FROM cognitive_user_controls
        WHERE topic_id = ? AND hypothesis_code = ?
        ORDER BY root_fact_seq DESC, rowid DESC LIMIT 1
        """,
        (topic_id, hypothesis_code),
    ).fetchone()
    if control is None:
        return
    action = str(control["action"] or "")
    active = action in {"dismiss", "delete"}
    if action == "suppress":
        expires_at = str(control["expires_at"] or "").strip()
        active = bool(expires_at) and _utc(expires_at) > now
    if active:
        raise _PreparationBlocked("control_active")


def _episode_context(
    conn: sqlite3.Connection,
    *,
    hypothesis_id: str,
    topic_id: str,
    hypothesis_code: str,
    model_version: str,
    source_attempt_id: str,
    transfer_at: datetime,
    now: datetime,
) -> tuple[str, str, str, str, str, bool, bool]:
    episode_id = _stable_id(
        "cognitive-episode", hypothesis_id, model_version, source_attempt_id
    )
    obligation_id = _stable_id("cognitive-obligation", episode_id, "retention", "1")
    claim = conn.execute(
        """
        SELECT 1 FROM cognitive_obligation_claims claims
        JOIN cognitive_learning_obligations obligations
          ON obligations.obligation_id = claims.obligation_id
        WHERE obligations.episode_id = ? AND claims.status = 'active'
        LIMIT 1
        """,
        (episode_id,),
    ).fetchone()
    if claim is not None:
        raise _PreparationBlocked("claim_active")
    active = conn.execute(
        """
        SELECT * FROM cognitive_monitoring_episodes
        WHERE hypothesis_id = ? AND model_version = ?
          AND status IN ('open', 'paused')
        """,
        (hypothesis_id, model_version),
    ).fetchall()
    if len(active) > 1 or (
        active and str(active[0]["episode_id"] or "") != episode_id
    ):
        raise _PreparationBlocked("episode_conflict")
    episode = conn.execute(
        "SELECT * FROM cognitive_monitoring_episodes WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    existing_episode = episode is not None
    if episode is None:
        original_not_before = _iso(transfer_at + timedelta(hours=24))
        due_by = _iso(transfer_at + timedelta(hours=72))
        eligibility_until = _iso(transfer_at + RETENTION_ELIGIBILITY)
        existing_override = False
    else:
        if (
            str(episode["status"] or "") != "open"
            or str(episode["hypothesis_id"] or "") != hypothesis_id
            or str(episode["topic_id"] or "") != topic_id
            or str(episode["hypothesis_code"] or "") != hypothesis_code
            or str(episode["model_version"] or "") != model_version
            or str(episode["source_attempt_id"] or "") != source_attempt_id
        ):
            raise _PreparationBlocked("episode_conflict")
        obligations = conn.execute(
            """
            SELECT * FROM cognitive_learning_obligations
            WHERE episode_id = ? AND obligation_type = 'retention'
            ORDER BY generation, obligation_id
            """,
            (episode_id,),
        ).fetchall()
        if len(obligations) != 1:
            raise _PreparationBlocked("episode_conflict")
        obligation = obligations[0]
        if (
            str(obligation["obligation_id"] or "") != obligation_id
            or int(obligation["generation"] or 0) != 1
            or str(obligation["status"] or "") != "pending"
            or str(obligation["current_claim_id"] or "")
            or str(obligation["not_before"] or "")
            != str(episode["not_before"] or "")
            or str(obligation["due_by"] or "") != str(episode["due_by"] or "")
            or str(obligation["eligibility_until"] or "")
            != str(episode["eligibility_until"] or "")
        ):
            raise _PreparationBlocked("episode_conflict")
        original_not_before = str(episode["not_before"] or "")
        due_by = str(episode["due_by"] or "")
        eligibility_until = str(episode["eligibility_until"] or "")
        existing_override = (
            str(obligation["reason"] or "") == _DEVELOPMENT_REASON
        )
    if _utc(eligibility_until) <= now:
        raise _PreparationBlocked("window_expired")
    return (
        episode_id,
        obligation_id,
        original_not_before,
        due_by,
        eligibility_until,
        existing_episode,
        existing_override,
    )


def _inspect(
    store: Any,
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    hypothesis_code: str,
    expected_source_attempt_id: str,
    now: datetime,
) -> _PreparedContext:
    configured_model_version = _require_active_retention_config(store, conn)
    current = _current_hypothesis(
        conn,
        topic_id=topic_id,
        hypothesis_code=hypothesis_code,
        expected_source_attempt_id=expected_source_attempt_id,
        model_version=configured_model_version,
    )
    hypothesis_id = str(current["hypothesis_id"])
    model_version = str(current["model_version"])
    source_attempt_id = str(current["source_attempt_id"] or "").strip()
    if not source_attempt_id:
        raise _PreparationBlocked("transfer_not_certified")
    payload, fact = _completed_transfer_payload(
        store,
        conn,
        attempt_id=source_attempt_id,
        topic_id=topic_id,
        hypothesis_code=hypothesis_code,
        hypothesis_id=hypothesis_id,
        model_version=model_version,
    )
    _reject_active_control(
        conn,
        topic_id=topic_id,
        hypothesis_code=hypothesis_code,
        now=now,
    )
    transfer_at = _utc(fact["submitted_at"])
    if transfer_at > now:
        raise _PreparationBlocked("transfer_not_certified")
    (
        episode_id,
        obligation_id,
        original_not_before,
        due_by,
        eligibility_until,
        existing_episode,
        existing_override,
    ) = _episode_context(
        conn,
        hypothesis_id=hypothesis_id,
        topic_id=topic_id,
        hypothesis_code=hypothesis_code,
        model_version=model_version,
        source_attempt_id=source_attempt_id,
        transfer_at=transfer_at,
        now=now,
    )
    cloned_payload = dict(payload)
    cloned_payload["retention_episode_requested"] = True
    return _PreparedContext(
        payload=cloned_payload,
        hypothesis_id=hypothesis_id,
        model_version=model_version,
        source_attempt_id=source_attempt_id,
        episode_id=episode_id,
        obligation_id=obligation_id,
        original_not_before=original_not_before,
        due_by=due_by,
        eligibility_until=eligibility_until,
        existing_episode=existing_episode,
        existing_override=existing_override,
    )


def _advance_retention_window(
    conn: sqlite3.Connection,
    *,
    context: _PreparedContext,
    now: datetime,
) -> bool:
    if _utc(context.original_not_before) <= now:
        return context.existing_override
    timestamp = _iso(_attempt_storage_now(now))
    episode_update = conn.execute(
        """
        UPDATE cognitive_monitoring_episodes
        SET not_before = ?, updated_at = ?
        WHERE episode_id = ? AND status = 'open' AND not_before = ?
        """,
        (
            timestamp,
            timestamp,
            context.episode_id,
            context.original_not_before,
        ),
    )
    obligation_update = conn.execute(
        """
        UPDATE cognitive_learning_obligations
        SET not_before = ?, reason = ?, updated_at = ?
        WHERE obligation_id = ? AND status = 'pending'
          AND current_claim_id = '' AND not_before = ?
        """,
        (
            timestamp,
            _DEVELOPMENT_REASON,
            timestamp,
            context.obligation_id,
            context.original_not_before,
        ),
    )
    if episode_update.rowcount != 1 or obligation_update.rowcount != 1:
        raise RuntimeError("development retention window update lost ownership")
    return True


def _success_result(
    context: _PreparedContext,
    *,
    status: str,
    development_override: bool,
    not_before: str,
) -> dict[str, Any]:
    result = _base_result(
        enabled=True,
        status=status,
        topic_id=_DEVELOPMENT_TOPIC_ID,
        hypothesis_code=_DEVELOPMENT_HYPOTHESIS_CODE,
    )
    result.update(
        {
            "development_override": development_override,
            "source_attempt_id": context.source_attempt_id,
            "episode_id": context.episode_id,
            "obligation_id": context.obligation_id,
            "not_before": not_before,
            "due_by": context.due_by,
            "eligibility_until": context.eligibility_until,
        }
    )
    return result


def prepare_cognitive_retention_for_development(
    store: Any,
    *,
    topic_id: str,
    hypothesis_code: str,
    expected_source_attempt_id: str,
    apply: bool,
) -> dict[str, Any]:
    """Validate and optionally prepare one real transfer for immediate retention.

    Business fences return stable reason codes. Unexpected storage failures are
    deliberately allowed to propagate so callers cannot mistake them for a
    domain rejection and the transaction always rolls back.
    """

    topic_key = str(topic_id or "").strip()
    hypothesis_key = str(hypothesis_code or "").strip()
    attempt_key = str(expected_source_attempt_id or "").strip()
    if os.environ.get(_DEV_ENVIRONMENT_VARIABLE) != "1":
        return _blocked_result(
            "dev_tools_disabled",
            topic_id=topic_key,
            hypothesis_code=hypothesis_key,
        )
    if (
        topic_key != _DEVELOPMENT_TOPIC_ID
        or hypothesis_key != _DEVELOPMENT_HYPOTHESIS_CODE
        or hypothesis_key != ACTIVE_RETENTION_HYPOTHESIS
    ):
        return _blocked_result(
            "unsupported_target",
            topic_id=topic_key,
            hypothesis_code=hypothesis_key,
        )
    if apply is True and not attempt_key:
        return _blocked_result(
            "source_attempt_mismatch",
            topic_id=topic_key,
            hypothesis_code=hypothesis_key,
        )
    if not isinstance(apply, bool):
        return _blocked_result(
            "unsupported_target",
            topic_id=topic_key,
            hypothesis_code=hypothesis_key,
        )
    now = _utc_now()
    try:
        with store._lock:
            conn = store._require_conn()
            preview = _inspect(
                store,
                conn,
                topic_id=topic_key,
                hypothesis_code=hypothesis_key,
                expected_source_attempt_id=attempt_key,
                now=now,
            )
        if not apply:
            ready_not_before = (
                _iso(_attempt_storage_now(now))
                if _utc(preview.original_not_before) > now
                else preview.original_not_before
            )
            return _success_result(
                preview,
                status="ready",
                development_override=(
                    preview.existing_override
                    or _utc(preview.original_not_before) > now
                ),
                not_before=ready_not_before,
            )
        with store._lock:
            conn = store._require_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                context = _inspect(
                    store,
                    conn,
                    topic_id=topic_key,
                    hypothesis_code=hypothesis_key,
                    expected_source_attempt_id=attempt_key,
                    now=now,
                )
                if not context.existing_episode:
                    try:
                        _insert_requested_transfer_episode(
                            store, conn, context.payload
                        )
                    except ValueError as exc:
                        raise _PreparationBlocked(
                            "transfer_not_certified"
                        ) from exc
                development_override = _advance_retention_window(
                    conn,
                    context=context,
                    now=now,
                )
                stored = conn.execute(
                    """
                    SELECT episodes.not_before, episodes.due_by,
                           episodes.eligibility_until,
                           obligations.reason
                    FROM cognitive_monitoring_episodes episodes
                    JOIN cognitive_learning_obligations obligations
                      ON obligations.episode_id = episodes.episode_id
                    WHERE episodes.episode_id = ?
                      AND obligations.obligation_id = ?
                    """,
                    (context.episode_id, context.obligation_id),
                ).fetchone()
                if stored is None:
                    raise RuntimeError(
                        "development retention preparation was not persisted"
                    )
                status = (
                    "already_prepared"
                    if context.existing_episode
                    and (
                        context.existing_override
                        or _utc(context.original_not_before) <= now
                    )
                    else "prepared"
                )
                result = _success_result(
                    context,
                    status=status,
                    development_override=(
                        development_override
                        or str(stored["reason"] or "") == _DEVELOPMENT_REASON
                    ),
                    not_before=str(stored["not_before"] or ""),
                )
                result["due_by"] = str(stored["due_by"] or "")
                result["eligibility_until"] = str(
                    stored["eligibility_until"] or ""
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return result
    except _PreparationBlocked as exc:
        return _blocked_result(
            exc.reason_code,
            topic_id=topic_key,
            hypothesis_code=hypothesis_key,
        )


__all__ = ["prepare_cognitive_retention_for_development"]
