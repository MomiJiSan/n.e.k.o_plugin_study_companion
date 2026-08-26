"""Fault-isolated orchestration for the mastery V2 shadow projection.

The projector deliberately owns no thread, timer, or answer-entry lifecycle.
It may be called by a background worker after the answer transaction commits,
and turns storage failures into summaries so they cannot escape into the
already-completed answer path.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .mastery_v2 import (
    DEFAULT_MASTERY_V2_POLICY,
    MasteryEvidence,
    MasteryV2Policy,
    MasteryV2Snapshot,
    calculate_mastery_v2,
)


class MasteryProjectionStore(Protocol):
    """Minimal storage port required by the shadow projector."""

    def claim_mastery_projections(self, *, limit: int = 1) -> list[dict[str, Any]]: ...

    def get_mastery_v2_projection_input(self, attempt_id: str) -> dict[str, Any] | None: ...

    def complete_mastery_projection(
        self,
        snapshot: dict[str, Any],
        *,
        lease_token: str,
    ) -> dict[str, Any]: ...

    def mark_mastery_projection_failed(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        error: str,
    ) -> bool: ...

    def upsert_mastery_snapshot_v2(self, snapshot: dict[str, Any]) -> dict[str, Any]: ...

    def list_mastery_v2_evidence(
        self,
        *,
        topic_id: str,
        through_attempt_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def count_active_wrong_questions(self, topic_id: str) -> int: ...

    def list_mastery_v2_attempt_ids(self, *, topic_id: str | None = None) -> list[str]: ...

    def list_topics(
        self,
        limit: int | None = 100,
        subject: str | None = None,
        stage: str | None = None,
        **scope: Any,
    ) -> list[dict[str, Any]]: ...

    def list_latest_mastery_for_topics(
        self,
        topic_ids: list[str] | set[str] | tuple[str, ...],
    ) -> list[dict[str, Any]]: ...

    def list_latest_mastery_v2_for_topics(
        self,
        topic_ids: list[str] | set[str] | tuple[str, ...],
        *,
        mastery_model_version: str,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class ProjectionFailure:
    attempt_id: str
    error: str


@dataclass(frozen=True, slots=True)
class ProjectionRunSummary:
    """Auditable, non-throwing result of one queue worker pass."""

    claimed: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    failures: tuple[ProjectionFailure, ...] = ()

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "claimed": self.claimed,
            "completed": self.completed,
            "failed": self.failed,
            "skipped": self.skipped,
            "failures": [
                {"attempt_id": item.attempt_id, "error": item.error}
                for item in self.failures
            ],
        }


@dataclass(frozen=True, slots=True)
class MasteryRebuildSummary:
    """Per-topic summary for an explicit full shadow-model rebuild."""

    requested: int = 0
    rebuilt: int = 0
    failed: int = 0
    skipped: int = 0
    failures: tuple[ProjectionFailure, ...] = ()

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "rebuilt": self.rebuilt,
            "failed": self.failed,
            "skipped": self.skipped,
            "failures": [
                {"attempt_id": item.attempt_id, "error": item.error}
                for item in self.failures
            ],
        }


class MasteryV2Projector:
    """Project queued or historical immutable facts into V2 snapshots."""

    def __init__(
        self,
        store: MasteryProjectionStore,
        *,
        policy: MasteryV2Policy = DEFAULT_MASTERY_V2_POLICY,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._clock = clock or _utc_now

    def process_pending(
        self,
        *,
        limit: int = 100,
        as_of: datetime | str | None = None,
    ) -> ProjectionRunSummary:
        """Process one bounded queue batch without propagating failures."""

        resolved_limit = max(1, int(limit))
        try:
            projection_time = self._projection_time(as_of)
            claimed_items = self._store.claim_mastery_projections(limit=resolved_limit)
        except Exception as exc:
            failure = ProjectionFailure(attempt_id="", error=_error_text(exc))
            return ProjectionRunSummary(failed=1, failures=(failure,))

        completed = 0
        failures: list[ProjectionFailure] = []
        for item in claimed_items:
            attempt_id = str(item.get("attempt_id") or "").strip()
            lease_token = str(item.get("lease_token") or "").strip()
            try:
                if not attempt_id:
                    raise ValueError("claimed mastery projection has no attempt_id")
                if not lease_token:
                    raise ValueError("claimed mastery projection has no lease_token")
                projection_input = self._store.get_mastery_v2_projection_input(attempt_id)
                if projection_input is None:
                    raise ValueError("mastery projection input is unavailable")
                snapshot = self._snapshot_from_projection_input(
                    projection_input,
                    expected_source_attempt_id=attempt_id,
                    as_of=projection_time,
                )
                self._store.complete_mastery_projection(
                    snapshot.to_record(), lease_token=lease_token
                )
                completed += 1
            except Exception as exc:
                failure = ProjectionFailure(
                    attempt_id=attempt_id,
                    error=_error_text(exc),
                )
                failures.append(failure)
                self._mark_failed_without_raising(
                    failure, lease_token=lease_token
                )

        return ProjectionRunSummary(
            claimed=len(claimed_items),
            completed=completed,
            failed=len(failures),
            failures=tuple(failures),
        )

    def rebuild_topics(
        self,
        topic_ids: Iterable[str],
        *,
        as_of: datetime | str | None = None,
    ) -> MasteryRebuildSummary:
        """Rebuild explicit topics from all stable attempt facts.

        No legacy QA row is synthesized.  Topics without PR-7 facts are
        reported as skipped and remain V1-only.
        """

        requested_topics = tuple(
            topic_id
            for topic_id in dict.fromkeys(
                str(candidate or "").strip() for candidate in topic_ids
            )
            if topic_id
        )
        try:
            projection_time = self._projection_time(as_of)
        except Exception as exc:
            return MasteryRebuildSummary(
                requested=len(requested_topics),
                failed=1,
                failures=(ProjectionFailure(attempt_id="", error=_error_text(exc)),),
            )
        rebuilt = 0
        skipped = 0
        failures: list[ProjectionFailure] = []
        for topic_id in requested_topics:
            try:
                raw_evidence = self._store.list_mastery_v2_evidence(
                    topic_id=topic_id,
                    through_attempt_id=None,
                )
                evidence = evidence_from_mappings(raw_evidence)
                if not evidence:
                    skipped += 1
                    continue
                source = _latest_evidence(evidence)
                snapshot = calculate_mastery_v2(
                    topic_id,
                    evidence,
                    unresolved_wrong_count=self._store.count_active_wrong_questions(topic_id),
                    as_of=projection_time,
                    policy=self._policy,
                )
                if snapshot.source_attempt_id != source.attempt_id:
                    raise ValueError("rebuilt snapshot source attempt is inconsistent")
                self._store.upsert_mastery_snapshot_v2(snapshot.to_record())
                rebuilt += 1
            except Exception as exc:
                failures.append(
                    ProjectionFailure(
                        attempt_id=f"topic:{topic_id}",
                        error=_error_text(exc),
                    )
                )

        return MasteryRebuildSummary(
            requested=len(requested_topics),
            rebuilt=rebuilt,
            failed=len(failures),
            skipped=skipped,
            failures=tuple(failures),
        )

    def rebuild_all(
        self,
        *,
        topic_id: str | None = None,
        as_of: datetime | str | None = None,
    ) -> MasteryRebuildSummary:
        """Rebuild every complete PR-7 attempt, optionally for one topic.

        The store deliberately lists only attempts with question and evaluation
        facts.  Legacy QA rows without stable attempt identity are never
        invented or included.
        """

        try:
            projection_time = self._projection_time(as_of)
            attempt_ids = self._store.list_mastery_v2_attempt_ids(topic_id=topic_id)
        except Exception as exc:
            return MasteryRebuildSummary(
                failed=1,
                failures=(ProjectionFailure(attempt_id="", error=_error_text(exc)),),
            )

        rebuilt = 0
        failures: list[ProjectionFailure] = []
        for attempt_id in attempt_ids:
            attempt_key = str(attempt_id or "").strip()
            try:
                if not attempt_key:
                    raise ValueError("rebuild attempt_id is required")
                projection_input = self._store.get_mastery_v2_projection_input(
                    attempt_key
                )
                if projection_input is None:
                    raise ValueError("mastery rebuild input is unavailable")
                snapshot = self._snapshot_from_projection_input(
                    projection_input,
                    expected_source_attempt_id=attempt_key,
                    as_of=projection_time,
                )
                self._store.upsert_mastery_snapshot_v2(snapshot.to_record())
                rebuilt += 1
            except Exception as exc:
                failures.append(
                    ProjectionFailure(
                        attempt_id=attempt_key,
                        error=_error_text(exc),
                    )
                )
        return MasteryRebuildSummary(
            requested=len(attempt_ids),
            rebuilt=rebuilt,
            failed=len(failures),
            failures=tuple(failures),
        )

    def difference_report(self) -> dict[str, object]:
        """Return a read-only V1/V2 comparison grouped by catalog metadata."""

        topics = self._store.list_topics(limit=None)
        topic_metadata = {
            topic_id: {
                "stage": str(topic.get("stage") or "").strip(),
                "subject": str(topic.get("subject") or "").strip(),
            }
            for topic in topics
            if (topic_id := str(topic.get("id") or "").strip())
        }
        topic_ids = list(topic_metadata)
        v1_rows = self._store.list_latest_mastery_for_topics(topic_ids)
        v2_rows = self._store.list_latest_mastery_v2_for_topics(
            topic_ids,
            mastery_model_version=self._policy.model_version,
        )
        v1_by_topic = _mastery_by_topic(v1_rows)
        v2_by_topic = _mastery_by_topic(v2_rows)
        topic_rows: list[dict[str, object]] = []
        for topic_id, metadata in topic_metadata.items():
            v1_mastery = v1_by_topic.get(topic_id)
            v2_mastery = v2_by_topic.get(topic_id)
            delta = (
                None
                if v1_mastery is None or v2_mastery is None
                else round(v2_mastery - v1_mastery, self._policy.rounding_digits)
            )
            topic_rows.append(
                {
                    "topic_id": topic_id,
                    "stage": metadata["stage"],
                    "subject": metadata["subject"],
                    "v1_mastery": v1_mastery,
                    "v2_mastery": v2_mastery,
                    "delta_v2_minus_v1": delta,
                }
            )
        return {
            "mastery_model_version": self._policy.model_version,
            "overall": _difference_summary(topic_rows, self._policy.rounding_digits),
            "by_stage": _grouped_difference_summary(
                topic_rows,
                key="stage",
                rounding_digits=self._policy.rounding_digits,
            ),
            "by_subject": _grouped_difference_summary(
                topic_rows,
                key="subject",
                rounding_digits=self._policy.rounding_digits,
            ),
            "topics": topic_rows,
        }

    def _snapshot_from_projection_input(
        self,
        projection_input: Mapping[str, Any],
        *,
        expected_source_attempt_id: str,
        as_of: datetime,
    ) -> MasteryV2Snapshot:
        topic_id = str(projection_input.get("topic_id") or "").strip()
        source_attempt_id = str(
            projection_input.get("source_attempt_id") or ""
        ).strip()
        if not topic_id:
            raise ValueError("mastery projection topic_id is required")
        if source_attempt_id != expected_source_attempt_id:
            raise ValueError("mastery projection source attempt does not match queue item")
        raw_evidence = projection_input.get("evidence")
        if not isinstance(raw_evidence, Sequence) or isinstance(
            raw_evidence, (str, bytes, bytearray)
        ):
            raise TypeError("mastery projection evidence must be a sequence")
        evidence = evidence_from_mappings(raw_evidence)
        source = next(
            (item for item in evidence if item.attempt_id == source_attempt_id),
            None,
        )
        if source is None:
            raise ValueError("source attempt is absent from mastery projection evidence")
        snapshot = calculate_mastery_v2(
            topic_id,
            evidence,
            unresolved_wrong_count=_non_negative_int(
                projection_input.get("unresolved_wrong_count", 0),
                "unresolved_wrong_count",
            ),
            as_of=as_of,
            policy=self._policy,
        )
        if snapshot.source_attempt_id != source_attempt_id:
            raise ValueError("snapshot contains facts newer than its source attempt")
        return snapshot

    def _projection_time(self, as_of: datetime | str | None) -> datetime:
        value = self._clock() if as_of is None else as_of
        return _utc_datetime(value)

    def _mark_failed_without_raising(
        self, failure: ProjectionFailure, *, lease_token: str
    ) -> None:
        if not failure.attempt_id:
            return
        try:
            self._store.mark_mastery_projection_failed(
                attempt_id=failure.attempt_id,
                lease_token=lease_token,
                error=failure.error,
            )
        except Exception:
            # The worker summary already records the original failure.  A
            # queue bookkeeping outage must not leak into the answer path.
            return


def evidence_from_mapping(payload: Mapping[str, Any]) -> MasteryEvidence:
    """Convert one stable store fact without manufacturing missing identity."""

    used_hint = payload.get("used_hint")
    if used_hint is not None and not isinstance(used_hint, bool):
        if used_hint in (0, 1):
            used_hint = bool(used_hint)
        else:
            raise TypeError("used_hint must be bool, 0, 1, or None")
    return MasteryEvidence(
        attempt_id=str(payload.get("attempt_id") or "").strip(),
        verdict=str(payload.get("verdict") or "").strip(),
        score=payload.get("score"),
        difficulty=payload.get("difficulty"),
        used_hint=used_hint,
        response_time_ms=_optional_int(payload.get("response_time_ms")),
        evaluator_confidence=_optional_float(payload.get("evaluator_confidence")),
        submitted_at=str(payload.get("submitted_at") or "").strip(),
    )


def evidence_from_mappings(
    payloads: Iterable[Mapping[str, Any]],
) -> tuple[MasteryEvidence, ...]:
    return tuple(evidence_from_mapping(payload) for payload in payloads)


def _latest_evidence(evidence: Sequence[MasteryEvidence]) -> MasteryEvidence:
    if not evidence:
        raise ValueError("mastery evidence is required")
    return max(
        evidence,
        key=lambda item: (_utc_datetime(item.submitted_at), item.attempt_id),
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("numeric fact must not be boolean")
    return int(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("numeric fact must not be boolean")
    return float(value)


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be an integer")
    result = int(value)
    if result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _error_text(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:2_000]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("projection as_of is required")
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mastery_by_topic(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in rows:
        topic_id = str(row.get("topic_id") or "").strip()
        if not topic_id:
            continue
        try:
            mastery = float(row.get("mastery"))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(mastery):
            result[topic_id] = min(1.0, max(0.0, mastery))
    return result


def _difference_summary(
    rows: Sequence[Mapping[str, object]],
    rounding_digits: int,
) -> dict[str, object]:
    deltas = [
        float(delta)
        for row in rows
        if (delta := row.get("delta_v2_minus_v1")) is not None
    ]
    v1_count = sum(row.get("v1_mastery") is not None for row in rows)
    v2_count = sum(row.get("v2_mastery") is not None for row in rows)
    compared_count = len(deltas)
    return {
        "topic_count": len(rows),
        "v1_count": v1_count,
        "v2_count": v2_count,
        "compared_count": compared_count,
        "v1_only_count": sum(
            row.get("v1_mastery") is not None and row.get("v2_mastery") is None
            for row in rows
        ),
        "v2_only_count": sum(
            row.get("v1_mastery") is None and row.get("v2_mastery") is not None
            for row in rows
        ),
        "mean_delta_v2_minus_v1": (
            round(sum(deltas) / compared_count, rounding_digits)
            if compared_count
            else None
        ),
        "mean_absolute_delta": (
            round(sum(abs(delta) for delta in deltas) / compared_count, rounding_digits)
            if compared_count
            else None
        ),
        "max_absolute_delta": (
            round(max(abs(delta) for delta in deltas), rounding_digits)
            if compared_count
            else None
        ),
    }


def _grouped_difference_summary(
    rows: Sequence[Mapping[str, object]],
    *,
    key: str,
    rounding_digits: int,
) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        group = str(row.get(key) or "unknown")
        grouped.setdefault(group, []).append(row)
    return {
        group: _difference_summary(group_rows, rounding_digits)
        for group, group_rows in sorted(grouped.items())
    }
