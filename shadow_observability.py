"""Read-only shadow-model comparison reports.

This module is intentionally detached from entries and storage writes.  It
turns already-collected V1/V2 snapshots, projection-queue rows, assessment
decisions, and retrieval contexts into a stable local report.  In particular,
building a report never enables a feature flag, drains a queue, or selects V2
as the learner-state read model.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

REPORT_VERSION = 1
SHADOW_DEVELOPMENT_CONFIG = {
    "mastery": {
        "v2_shadow_enabled": True,
        "read_model": "v1",
        "model_version": "mastery-v2-shadow-1",
    },
    "assessment": {
        "exact_short_answer_enabled": False,
        "numeric_tolerance_enabled": False,
        "math_expression_enabled": False,
    },
}
_QUEUE_STATUSES = ("pending", "processing", "done", "failed")
_SNAPSHOT_FIELDS = (
    "topic_id",
    "mastery",
    "accuracy",
    "recency",
    "consistency",
    "confidence",
    "evidence_count",
    "unresolved_wrong_count",
    "mastery_model_version",
    "source_attempt_id",
    "computed_at",
)
_DEFAULT_MAX_BACKLOG_AGE_SECONDS = 900.0


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(values: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    return [_mapping(value) for value in values or () if isinstance(value, Mapping)]


def _text(value: object) -> str:
    return str(value or "").strip()


def _unit_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if 0.0 <= result <= 1.0 else None


def _parse_time(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        # SQLite's default format is accepted by datetime.fromisoformat, but
        # retain a conservative fail-closed path for invalid operational data.
        return None


def _as_of(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        resolved = value
    else:
        resolved = _parse_time(value) if value is not None else datetime.now(timezone.utc)
    if resolved is None:
        raise ValueError("as_of must be an ISO-8601 timestamp")
    return resolved if resolved.tzinfo else resolved.replace(tzinfo=timezone.utc)


def _mastery_by_topic(rows: Iterable[Mapping[str, Any]] | None) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in _rows(rows):
        topic_id = _text(row.get("topic_id"))
        mastery = _unit_float(row.get("mastery"))
        if topic_id and mastery is not None:
            result[topic_id] = mastery
    return result


def compare_mastery_rows(
    v1_rows: Iterable[Mapping[str, Any]] | None,
    v2_rows: Iterable[Mapping[str, Any]] | None,
    *,
    high_delta_threshold: float = 0.25,
) -> dict[str, Any]:
    """Compare latest V1/V2 mastery values by topic without changing either."""

    if not 0.0 <= float(high_delta_threshold) <= 1.0:
        raise ValueError("high_delta_threshold must be in [0, 1]")
    v1 = _mastery_by_topic(v1_rows)
    v2 = _mastery_by_topic(v2_rows)
    shared = sorted(set(v1) & set(v2))
    deltas = {topic_id: round(v2[topic_id] - v1[topic_id], 6) for topic_id in shared}
    absolute = {topic_id: abs(delta) for topic_id, delta in deltas.items()}
    bins = {
        "exact": sum(delta == 0.0 for delta in absolute.values()),
        "within_0_05": sum(0.0 < delta <= 0.05 for delta in absolute.values()),
        "within_0_15": sum(0.05 < delta <= 0.15 for delta in absolute.values()),
        "above_0_15": sum(delta > 0.15 for delta in absolute.values()),
    }
    high_delta_topics = [
        {"topic_id": topic_id, "v1_mastery": v1[topic_id], "v2_mastery": v2[topic_id], "delta": deltas[topic_id]}
        for topic_id in shared
        if absolute[topic_id] >= high_delta_threshold
    ]
    return {
        "v1_topic_count": len(v1),
        "v2_topic_count": len(v2),
        "compared_topic_count": len(shared),
        "v1_only_topic_count": len(set(v1) - set(v2)),
        "v2_only_topic_count": len(set(v2) - set(v1)),
        "absolute_delta_distribution": bins,
        "high_delta_threshold": high_delta_threshold,
        "high_delta_topics": high_delta_topics,
    }


def summarize_projection_queue(
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """Summarize queue health from read-only queue rows."""

    now = _as_of(as_of)
    queue_rows = _rows(rows)
    statuses = Counter(_text(row.get("status")).lower() for row in queue_rows)
    status_counts = {status: statuses.get(status, 0) for status in _QUEUE_STATUSES}
    unknown_status_count = sum(count for status, count in statuses.items() if status not in _QUEUE_STATUSES)
    ages: list[float] = []
    backlog_ages: list[float] = []
    missing_time_count = 0
    failure_rows: list[dict[str, Any]] = []
    for row in queue_rows:
        status = _text(row.get("status")).lower()
        created_at = _parse_time(row.get("created_at"))
        if created_at is None:
            missing_time_count += 1
            continue
        age = max(0.0, (now - created_at).total_seconds())
        ages.append(age)
        if status in {"pending", "processing", "failed"}:
            backlog_ages.append(age)
        if status == "failed":
            failure_rows.append(
                {
                    "attempt_id": _text(row.get("attempt_id")),
                    "retry_count": int(row.get("retry_count") or 0),
                    "last_error": _text(row.get("last_error"))[:200],
                    "age_seconds": round(age, 3),
                }
            )
    terminal = status_counts["done"] + status_counts["failed"]
    return {
        "queue_item_count": len(queue_rows),
        "status_counts": status_counts,
        "unknown_status_count": unknown_status_count,
        "projection_success_rate": (status_counts["done"] / terminal) if terminal else None,
        "backlog_count": sum(status_counts[name] for name in ("pending", "processing", "failed")),
        "max_backlog_age_seconds": round(max(backlog_ages), 3) if backlog_ages else 0.0,
        "max_queue_age_seconds": round(max(ages), 3) if ages else 0.0,
        "missing_created_at_count": missing_time_count,
        "failed_items": failure_rows,
    }


def compare_projection_snapshots(
    incremental_rows: Iterable[Mapping[str, Any]] | None,
    rebuild_rows: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Require complete field equality for comparable incremental/rebuild rows."""

    def indexed(rows: Iterable[Mapping[str, Any]] | None) -> dict[tuple[str, str, str], dict[str, Any]]:
        result: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in _rows(rows):
            key = (
                _text(row.get("topic_id")),
                _text(row.get("mastery_model_version")),
                _text(row.get("source_attempt_id")),
            )
            if all(key):
                result[key] = {field: row.get(field) for field in _SNAPSHOT_FIELDS}
        return result

    incremental = indexed(incremental_rows)
    rebuild = indexed(rebuild_rows)
    keys = sorted(set(incremental) | set(rebuild))
    mismatches: list[dict[str, Any]] = []
    for key in keys:
        before = incremental.get(key)
        after = rebuild.get(key)
        differing_fields = [
            field for field in _SNAPSHOT_FIELDS if (before or {}).get(field) != (after or {}).get(field)
        ]
        if differing_fields:
            mismatches.append(
                {
                    "topic_id": key[0],
                    "mastery_model_version": key[1],
                    "source_attempt_id": key[2],
                    "differing_fields": differing_fields,
                }
            )
    return {
        "incremental_snapshot_count": len(incremental),
        "rebuild_snapshot_count": len(rebuild),
        "comparable_snapshot_count": len(set(incremental) & set(rebuild)),
        "fully_consistent": bool(keys) and not mismatches,
        "mismatches": mismatches,
    }


def compare_assessment_decisions(
    llm_rows: Iterable[Mapping[str, Any]] | None,
    deterministic_rows: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Compare decision verdicts for the same local assessment identifiers."""

    def decisions(rows: Iterable[Mapping[str, Any]] | None) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in _rows(rows):
            attempt_id = _text(row.get("attempt_id") or row.get("id"))
            verdict = _text(row.get("verdict")).lower()
            if attempt_id and verdict:
                result[attempt_id] = verdict
        return result

    llm = decisions(llm_rows)
    deterministic = decisions(deterministic_rows)
    shared = sorted(set(llm) & set(deterministic))
    disagreements = [
        {"attempt_id": attempt_id, "llm_verdict": llm[attempt_id], "deterministic_verdict": deterministic[attempt_id]}
        for attempt_id in shared
        if llm[attempt_id] != deterministic[attempt_id]
    ]
    return {
        "llm_decision_count": len(llm),
        "deterministic_decision_count": len(deterministic),
        "compared_decision_count": len(shared),
        "agreement_rate": ((len(shared) - len(disagreements)) / len(shared)) if shared else None,
        "disagreements": disagreements,
    }


def summarize_math_equivalence_disagreements(rows: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Expose only declared equivalent-answer disagreement metadata."""

    disagreements: list[dict[str, Any]] = []
    total = 0
    for row in _rows(rows):
        total += 1
        llm = _text(row.get("llm_verdict")).lower()
        deterministic = _text(row.get("deterministic_verdict")).lower()
        equivalent = row.get("equivalent") is True
        if equivalent and llm and deterministic and llm != deterministic:
            disagreements.append(
                {
                    "case_id": _text(row.get("case_id") or row.get("attempt_id")),
                    "llm_verdict": llm,
                    "deterministic_verdict": deterministic,
                }
            )
    return {
        "declared_equivalence_case_count": total,
        "equivalent_answer_disagreement_count": len(disagreements),
        "equivalent_answer_disagreements": disagreements,
    }


def _fingerprint(value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compare_relationship_contexts(
    v1_rows: Iterable[Mapping[str, Any]] | None,
    v2_rows: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Compare V1/V2 context content through hashes, never retaining prompts."""

    def indexed(rows: Iterable[Mapping[str, Any]] | None) -> dict[str, str]:
        result: dict[str, str] = {}
        for index, row in enumerate(_rows(rows)):
            context_id = _text(row.get("context_id") or row.get("request_id") or row.get("id")) or str(index)
            result[context_id] = _fingerprint(row.get("context", row))
        return result

    v1 = indexed(v1_rows)
    v2 = indexed(v2_rows)
    shared = sorted(set(v1) & set(v2))
    changed = [context_id for context_id in shared if v1[context_id] != v2[context_id]]
    return {
        "v1_context_count": len(v1),
        "v2_context_count": len(v2),
        "compared_context_count": len(shared),
        "changed_context_count": len(changed),
        "changed_context_ids": changed,
        "v1_only_context_count": len(set(v1) - set(v2)),
        "v2_only_context_count": len(set(v2) - set(v1)),
    }


def _config_values(config: object) -> dict[str, Any]:
    value = _mapping(config)
    if value:
        return value
    mastery = getattr(config, "mastery", None)
    assessment = getattr(config, "assessment", None)
    return {
        "mastery": _mapping(getattr(mastery, "to_dict", lambda: {})()) or {
            "v2_shadow_enabled": getattr(mastery, "v2_shadow_enabled", False),
            "read_model": getattr(mastery, "read_model", "v1"),
        },
        "assessment": _mapping(getattr(assessment, "to_dict", lambda: {})()) or {
            "exact_short_answer_enabled": getattr(assessment, "exact_short_answer_enabled", False),
            "numeric_tolerance_enabled": getattr(assessment, "numeric_tolerance_enabled", False),
            "math_expression_enabled": getattr(assessment, "math_expression_enabled", False),
        },
    }


def shadow_gate_status(
    *,
    config: object,
    projection: Mapping[str, Any],
    parity: Mapping[str, Any],
    mastery: Mapping[str, Any],
    assessment: Mapping[str, Any],
    math_equivalence: Mapping[str, Any],
    relationship: Mapping[str, Any],
    high_delta_reviewed: bool = False,
    non_math_retrieval_covered: bool = False,
    max_backlog_age_seconds: float = _DEFAULT_MAX_BACKLOG_AGE_SECONDS,
) -> dict[str, Any]:
    """Report promotion prerequisites; this function never performs promotion."""

    values = _config_values(config)
    mastery_config = _mapping(values.get("mastery"))
    assessment_config = _mapping(values.get("assessment"))
    deterministic_off = all(
        assessment_config.get(flag) is False
        for flag in ("exact_short_answer_enabled", "numeric_tolerance_enabled", "math_expression_enabled")
    )
    success_rate = projection.get("projection_success_rate")
    high_delta_count = len(_rows(mastery.get("high_delta_topics")))
    checks = {
        "read_model_remains_v1": _text(mastery_config.get("read_model")).lower() == "v1",
        "deterministic_scoring_remains_disabled": deterministic_off,
        "projection_success_rate_at_least_99_9": isinstance(success_rate, (int, float)) and success_rate >= 0.999,
        "incremental_rebuild_fully_consistent": parity.get("fully_consistent") is True,
        "no_long_lived_projection_backlog": projection.get("backlog_count", 0) == 0
        or float(projection.get("max_backlog_age_seconds", 0.0)) <= max_backlog_age_seconds,
        "high_mastery_deltas_reviewed": high_delta_count == 0 or high_delta_reviewed,
        "no_equivalent_math_answer_disagreement": math_equivalence.get("equivalent_answer_disagreement_count", 0) == 0,
        "non_math_relationship_retrieval_covered": non_math_retrieval_covered,
        # This requires enough matched assessments to be meaningful; no data
        # is explicitly not interpreted as evidence of deterministic safety.
        "deterministic_assessment_has_no_observed_disagreement": assessment.get("compared_decision_count", 0) > 0
        and assessment.get("agreement_rate") == 1.0,
    }
    return {
        "shadow_enabled": mastery_config.get("v2_shadow_enabled") is True,
        "read_model": _text(mastery_config.get("read_model")) or "v1",
        "promotion_allowed": all(checks.values()),
        "checks": checks,
    }


def build_shadow_observability_report(
    *,
    config: object,
    v1_mastery_rows: Iterable[Mapping[str, Any]] | None = None,
    v2_mastery_rows: Iterable[Mapping[str, Any]] | None = None,
    projection_queue_rows: Iterable[Mapping[str, Any]] | None = None,
    incremental_snapshots: Iterable[Mapping[str, Any]] | None = None,
    rebuild_snapshots: Iterable[Mapping[str, Any]] | None = None,
    llm_assessments: Iterable[Mapping[str, Any]] | None = None,
    deterministic_assessments: Iterable[Mapping[str, Any]] | None = None,
    math_equivalence_cases: Iterable[Mapping[str, Any]] | None = None,
    relationship_v1_contexts: Iterable[Mapping[str, Any]] | None = None,
    relationship_v2_contexts: Iterable[Mapping[str, Any]] | None = None,
    high_delta_reviewed: bool = False,
    non_math_retrieval_covered: bool = False,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """Build a deterministic, non-mutating shadow-observability report."""

    mastery = compare_mastery_rows(v1_mastery_rows, v2_mastery_rows)
    projection = summarize_projection_queue(projection_queue_rows, as_of=as_of)
    parity = compare_projection_snapshots(incremental_snapshots, rebuild_snapshots)
    assessment = compare_assessment_decisions(llm_assessments, deterministic_assessments)
    math_equivalence = summarize_math_equivalence_disagreements(math_equivalence_cases)
    relationship = compare_relationship_contexts(relationship_v1_contexts, relationship_v2_contexts)
    gate = shadow_gate_status(
        config=config,
        projection=projection,
        parity=parity,
        mastery=mastery,
        assessment=assessment,
        math_equivalence=math_equivalence,
        relationship=relationship,
        high_delta_reviewed=high_delta_reviewed,
        non_math_retrieval_covered=non_math_retrieval_covered,
    )
    return {
        "report_version": REPORT_VERSION,
        "read_only": True,
        "mastery_difference": mastery,
        "projection_queue": projection,
        "incremental_rebuild_parity": parity,
        "assessment_agreement": assessment,
        "math_equivalence": math_equivalence,
        "relationship_context_difference": relationship,
        "shadow_gate": gate,
    }


def collect_shadow_observability(
    store: Any,
    *,
    config: object,
    relationship_v1_contexts: Iterable[Mapping[str, Any]] | None = None,
    relationship_v2_contexts: Iterable[Mapping[str, Any]] | None = None,
    **report_inputs: Any,
) -> dict[str, Any]:
    """Read current V1/V2/queue state from a store, then build a report.

    The store is used through existing list methods only.  Snapshot parity and
    assessment comparisons remain caller-provided because running a rebuild or
    a second evaluator here would violate this module's read-only boundary.
    """

    topics = store.list_topics(limit=5000)
    topic_ids = [_text(topic.get("id") or topic.get("topic_id")) for topic in topics if isinstance(topic, Mapping)]
    topic_ids = [topic_id for topic_id in topic_ids if topic_id]
    model_version = _text(_mapping(_config_values(config).get("mastery")).get("model_version")) or "mastery-v2-shadow-1"
    return build_shadow_observability_report(
        config=config,
        v1_mastery_rows=store.list_latest_mastery_for_topics(topic_ids),
        v2_mastery_rows=store.list_latest_mastery_v2_for_topics(topic_ids, mastery_model_version=model_version),
        projection_queue_rows=store.list_mastery_projection_queue(limit=5000),
        relationship_v1_contexts=relationship_v1_contexts,
        relationship_v2_contexts=relationship_v2_contexts,
        **report_inputs,
    )
