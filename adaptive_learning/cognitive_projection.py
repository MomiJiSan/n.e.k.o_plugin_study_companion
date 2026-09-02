"""Asynchronous, rebuildable projection for cognitive evidence.

The worker owns no lifecycle thread and never enters answer submission. Every
store call is dispatched through :func:`asyncio.to_thread` because the
production store is synchronous SQLite. Queue completion and failure updates
remain lease-fenced by the store.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Protocol, cast

from .cognitive_catalog import COGNITIVE_CATALOG_V1, CognitiveCatalog
from .cognitive_contracts import (
    DEFAULT_COGNITIVE_EXTRACTOR_VERSION,
    DEFAULT_COGNITIVE_MODEL_VERSION,
    CognitiveEvidenceDraft,
    CognitiveExtractionInput,
    CognitiveExtractionOutcome,
)

HypothesisStatus = Literal[
    "hypothesized",
    "supported",
    "contradicted",
    "dismissed",
    "remediating",
    "provisionally_resolved",
    "monitored",
    "resolved",
]

_SUPPRESSING_CONTROLS = frozenset({"dismiss", "suppress", "delete"})
_DIAGNOSTIC_SOURCES = frozenset({"misconception_probe", "diagnostic"})
_REPAIR_SOURCES = frozenset({"misconception_repair", "repair"})
_TRANSFER_SOURCES = frozenset({"transfer_check", "transfer"})
_RETENTION_SOURCES = frozenset({"retention_check", "retention"})
_SOURCE_DIAGNOSTICITY = {
    "practice": 0.60,
    "structured_attempt": 0.60,
    "readiness_probe": 0.50,
    "misconception_probe": 1.00,
    "misconception_repair": 0.80,
    "transfer_check": 0.85,
    "retention_check": 1.00,
}


class CognitiveProjectionStore(Protocol):
    """Synchronous SQLite boundary used only through ``asyncio.to_thread``."""

    def claim_cognitive_projections(self, *, limit: int = 1) -> list[dict[str, Any]]: ...

    def get_cognitive_projection_input(self, attempt_id: str) -> dict[str, Any] | None: ...

    def complete_cognitive_projection(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        evidence: Sequence[dict[str, Any]] = (),
        snapshots: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]: ...

    def mark_cognitive_projection_failed(self, *, attempt_id: str, lease_token: str, error: str) -> bool: ...

    def list_cognitive_evidence(
        self,
        *,
        topic_id: str | None = None,
        hypothesis_code: str | None = None,
        extractor_version: str | None = None,
        through_attempt_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def replace_cognitive_hypothesis_snapshots(
        self,
        *,
        topic_id: str,
        model_version: str,
        snapshots: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...

    def list_cognitive_hypothesis_snapshots(
        self,
        *,
        topic_id: str | None = None,
        hypothesis_code: str | None = None,
        model_version: str | None = None,
        latest_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_cognitive_user_controls(
        self,
        *,
        topic_id: str | None = None,
        hypothesis_code: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def list_cognitive_intervention_events(
        self,
        *,
        topic_id: str | None = None,
        hypothesis_code: str | None = None,
        decision_id: str | None = None,
        model_version: str | None = None,
        event_types: Sequence[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...

    def claim_cognitive_topic_projections(
        self, *, limit: int = 1, model_version: str | None = None
    ) -> list[dict[str, Any]]: ...

    def complete_cognitive_topic_projection(
        self,
        *,
        topic_id: str,
        model_version: str,
        lease_token: str,
        claimed_generation: int,
        snapshots: Sequence[dict[str, Any]],
    ) -> dict[str, Any]: ...

    def mark_cognitive_topic_projection_failed(
        self,
        *,
        topic_id: str,
        model_version: str,
        lease_token: str,
        claimed_generation: int,
        error: str,
    ) -> bool: ...


class CognitiveEvidenceExtractorPort(Protocol):
    async def extract(self, extraction_input: CognitiveExtractionInput) -> CognitiveExtractionOutcome: ...


@dataclass(frozen=True, slots=True)
class CognitiveProjectionPolicy:
    """Auditable thresholds for the V1 log-odds projection."""

    prior_probability: float = 0.55
    supported_probability: float = 0.75
    contradicted_probability: float = 0.25
    diagnosticity_by_source: Mapping[str, float] | None = None
    high_quality_diagnostic_weight: float = 0.75
    high_quality_diagnostic_strength: float = 0.80
    high_quality_diagnostic_confidence: float = 0.80
    probability_rounding_digits: int = 12

    def __post_init__(self) -> None:
        if not 0.0 < self.prior_probability < 1.0:
            raise ValueError("prior_probability must be between zero and one")
        if not 0.0 < self.contradicted_probability < self.supported_probability < 1.0:
            raise ValueError("cognitive probability thresholds are invalid")
        for value in (
            self.high_quality_diagnostic_weight,
            self.high_quality_diagnostic_strength,
            self.high_quality_diagnostic_confidence,
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError("diagnostic thresholds must be between zero and one")
        if self.probability_rounding_digits < 0:
            raise ValueError("probability_rounding_digits must be non-negative")

    def diagnosticity(self, source_kind: str) -> float:
        configured = self.diagnosticity_by_source or _SOURCE_DIAGNOSTICITY
        value = configured.get(source_kind, configured.get("practice", 0.60))
        number = float(value)
        if not math.isfinite(number):
            return 0.0
        return min(1.0, max(0.0, number))


DEFAULT_COGNITIVE_PROJECTION_POLICY = CognitiveProjectionPolicy()


@dataclass(frozen=True, slots=True)
class CognitiveProjectionFailure:
    attempt_id: str
    error: str


@dataclass(frozen=True, slots=True)
class CognitiveProjectionRunSummary:
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    failures: tuple[CognitiveProjectionFailure, ...] = ()

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "claimed": self.claimed,
            "completed": self.completed,
            "failed": self.failed,
            "failures": [{"attempt_id": item.attempt_id, "error": item.error} for item in self.failures],
        }


@dataclass(frozen=True, slots=True)
class CognitiveRebuildSummary:
    requested: int = 0
    rebuilt: int = 0
    skipped: int = 0
    failed: int = 0
    failures: tuple[CognitiveProjectionFailure, ...] = ()

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "rebuilt": self.rebuilt,
            "skipped": self.skipped,
            "failed": self.failed,
            "failures": [{"attempt_id": item.attempt_id, "error": item.error} for item in self.failures],
        }


@dataclass(frozen=True, slots=True)
class _ProjectionEvidence:
    attempt_id: str
    topic_id: str
    hypothesis_code: str
    direction: Literal["support", "counter"]
    strength: float
    extractor_confidence: float
    diagnosticity: float
    source_kind: str
    evidence_family_id: str
    session_id: str
    diagnostic_validation_id: str

    @property
    def unsigned_weight(self) -> float:
        return self.strength * self.extractor_confidence * self.diagnosticity

    @property
    def signed_weight(self) -> float:
        return self.unsigned_weight if self.direction == "support" else -self.unsigned_weight


class CognitiveProjector:
    """Extract queued evidence and project reconstructable hypothesis states."""

    def __init__(
        self,
        store: CognitiveProjectionStore,
        extractor: CognitiveEvidenceExtractorPort,
        *,
        catalog: CognitiveCatalog = COGNITIVE_CATALOG_V1,
        extractor_version: str = DEFAULT_COGNITIVE_EXTRACTOR_VERSION,
        model_version: str = DEFAULT_COGNITIVE_MODEL_VERSION,
        policy: CognitiveProjectionPolicy = DEFAULT_COGNITIVE_PROJECTION_POLICY,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._catalog = catalog
        self._extractor_version = str(extractor_version or "").strip()
        self._model_version = str(model_version or "").strip()
        self._policy = policy
        self._clock = clock or _utc_now
        if not self._extractor_version or not self._model_version:
            raise ValueError("cognitive projector versions are required")

    async def process_pending(
        self,
        *,
        limit: int = 100,
        as_of: datetime | str | None = None,
    ) -> CognitiveProjectionRunSummary:
        """Process one bounded queue pass without leaking item failures."""

        try:
            projection_time = self._projection_time(as_of)
            claimed = await asyncio.to_thread(
                self._store.claim_cognitive_projections,
                limit=max(1, int(limit)),
                **(
                    {"extractor_version": self._extractor_version}
                    if self._uses_topic_queue
                    else {}
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = CognitiveProjectionFailure(attempt_id="", error=_error_text(exc))
            return CognitiveProjectionRunSummary(failed=1, failures=(failure,))

        completed = 0
        failures: list[CognitiveProjectionFailure] = []
        for item in claimed:
            attempt_id = str(item.get("attempt_id") or "").strip()
            lease_token = str(item.get("lease_token") or "").strip()
            try:
                await self._process_claimed(
                    item,
                    attempt_id=attempt_id,
                    lease_token=lease_token,
                    projection_time=projection_time,
                )
                completed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = CognitiveProjectionFailure(
                    attempt_id=attempt_id,
                    error=_error_text(exc),
                )
                failures.append(failure)
                await self._mark_failed_without_raising(failure, lease_token=lease_token)
        if self._uses_topic_queue:
            topic_summary = await self.process_dirty_topics(
                limit=max(1, int(limit)),
                as_of=projection_time,
            )
            failures.extend(topic_summary.failures)
        return CognitiveProjectionRunSummary(
            claimed=len(claimed),
            completed=completed,
            failed=len(failures),
            failures=tuple(failures),
        )

    @property
    def _uses_topic_queue(self) -> bool:
        return all(
            callable(getattr(self._store, name, None))
            for name in (
                "claim_cognitive_topic_projections",
                "complete_cognitive_topic_projection",
                "mark_cognitive_topic_projection_failed",
            )
        )

    async def process_dirty_topics(
        self,
        *,
        limit: int = 100,
        as_of: datetime | str | None = None,
    ) -> CognitiveRebuildSummary:
        """Fold each claimed topic from all version-matching evidence."""

        if not self._uses_topic_queue:
            return CognitiveRebuildSummary()
        projection_time = self._projection_time(as_of)
        try:
            claimed = await asyncio.to_thread(
                self._store.claim_cognitive_topic_projections,
                limit=max(1, int(limit)),
                model_version=self._model_version,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = CognitiveProjectionFailure(attempt_id="", error=_error_text(exc))
            return CognitiveRebuildSummary(failed=1, failures=(failure,))
        rebuilt = 0
        skipped = 0
        failures: list[CognitiveProjectionFailure] = []
        for item in claimed:
            topic_id = str(item.get("topic_id") or "").strip()
            lease_token = str(item.get("lease_token") or "").strip()
            generation = int(item.get("claimed_generation") or 0)
            try:
                evidence_rows = await asyncio.to_thread(
                    self._store.list_cognitive_evidence,
                    topic_id=topic_id,
                    extractor_version=self._extractor_version,
                )
                intervention_events = await asyncio.to_thread(
                    self._store.list_cognitive_intervention_events,
                    topic_id=topic_id,
                    model_version=self._model_version,
                    limit=10_000,
                )
                snapshots = await self._rebuild_snapshot_history(
                    topic_id,
                    evidence_rows,
                    intervention_events=intervention_events,
                    projection_time=projection_time,
                )
                await asyncio.to_thread(
                    self._store.complete_cognitive_topic_projection,
                    topic_id=topic_id,
                    model_version=self._model_version,
                    lease_token=lease_token,
                    claimed_generation=generation,
                    snapshots=snapshots,
                )
                if snapshots:
                    rebuilt += 1
                else:
                    skipped += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = CognitiveProjectionFailure(
                    attempt_id=f"topic:{topic_id}", error=_error_text(exc)
                )
                failures.append(failure)
                try:
                    await asyncio.to_thread(
                        self._store.mark_cognitive_topic_projection_failed,
                        topic_id=topic_id,
                        model_version=self._model_version,
                        lease_token=lease_token,
                        claimed_generation=generation,
                        error=failure.error,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
        return CognitiveRebuildSummary(
            requested=len(claimed),
            rebuilt=rebuilt,
            skipped=skipped,
            failed=len(failures),
            failures=tuple(failures),
        )

    async def rebuild_topics(
        self,
        topic_ids: Iterable[str],
        *,
        as_of: datetime | str | None = None,
    ) -> CognitiveRebuildSummary:
        """Atomically replace each requested topic projection from evidence."""

        requested = tuple(
            topic_id for topic_id in dict.fromkeys(str(candidate or "").strip() for candidate in topic_ids) if topic_id
        )
        try:
            projection_time = self._projection_time(as_of)
        except Exception as exc:
            failure = CognitiveProjectionFailure(attempt_id="", error=_error_text(exc))
            return CognitiveRebuildSummary(requested=len(requested), failed=1, failures=(failure,))

        rebuilt = 0
        skipped = 0
        failures: list[CognitiveProjectionFailure] = []
        for topic_id in requested:
            try:
                evidence_rows = await asyncio.to_thread(
                    self._store.list_cognitive_evidence,
                    topic_id=topic_id,
                    extractor_version=self._extractor_version,
                )
                intervention_events = (
                    await asyncio.to_thread(
                        self._store.list_cognitive_intervention_events,
                        topic_id=topic_id,
                        model_version=self._model_version,
                        limit=10_000,
                    )
                    if callable(
                        getattr(self._store, "list_cognitive_intervention_events", None)
                    )
                    else []
                )
                snapshots = await self._rebuild_snapshot_history(
                    topic_id,
                    evidence_rows,
                    intervention_events=intervention_events,
                    projection_time=projection_time,
                )
                await asyncio.to_thread(
                    self._store.replace_cognitive_hypothesis_snapshots,
                    topic_id=topic_id,
                    model_version=self._model_version,
                    snapshots=snapshots,
                )
                if snapshots:
                    rebuilt += 1
                else:
                    skipped += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures.append(CognitiveProjectionFailure(attempt_id=f"topic:{topic_id}", error=_error_text(exc)))
        return CognitiveRebuildSummary(
            requested=len(requested),
            rebuilt=rebuilt,
            skipped=skipped,
            failed=len(failures),
            failures=tuple(failures),
        )

    async def rebuild_all(
        self,
        *,
        as_of: datetime | str | None = None,
    ) -> CognitiveRebuildSummary:
        """Rebuild every topic present in evidence or an existing projection."""

        try:
            # The production store may reuse one SQLite read connection; keep
            # these calls off-loop but sequential rather than sharing that
            # connection concurrently across worker threads.
            evidence_rows = await asyncio.to_thread(
                self._store.list_cognitive_evidence,
                extractor_version=self._extractor_version,
            )
            snapshot_rows = await asyncio.to_thread(
                self._store.list_cognitive_hypothesis_snapshots,
                model_version=self._model_version,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = CognitiveProjectionFailure(attempt_id="", error=_error_text(exc))
            return CognitiveRebuildSummary(failed=1, failures=(failure,))
        topic_ids = tuple(
            dict.fromkeys(
                str(row.get("topic_id") or "").strip()
                for row in (*evidence_rows, *snapshot_rows)
                if str(row.get("topic_id") or "").strip()
            )
        )
        return await self.rebuild_topics(topic_ids, as_of=as_of)

    async def _process_claimed(
        self,
        item: Mapping[str, Any],
        *,
        attempt_id: str,
        lease_token: str,
        projection_time: datetime,
    ) -> None:
        if not attempt_id:
            raise ValueError("claimed cognitive projection has no attempt_id")
        if not lease_token:
            raise ValueError("claimed cognitive projection has no lease_token")
        queue_version = str(item.get("extractor_version") or "").strip()
        if queue_version != self._extractor_version:
            raise ValueError("claimed cognitive projection uses another extractor version")
        projection_input = await asyncio.to_thread(
            self._store.get_cognitive_projection_input,
            attempt_id,
            **(
                {"extractor_version": self._extractor_version}
                if self._uses_topic_queue
                else {}
            ),
        )
        if projection_input is None:
            raise ValueError("cognitive projection input is unavailable")
        topic_id = str(projection_input.get("topic_id") or "").strip()
        if not topic_id:
            raise ValueError("cognitive projection topic_id is required")
        source_kind = _source_kind(projection_input)
        extraction_input = _extraction_input(
            projection_input,
            allowed_hypotheses=self._catalog.allowed_codes(topic_id),
        )
        outcome = await self._extractor.extract(extraction_input)
        if not outcome.succeeded:
            raise RuntimeError(f"cognitive extraction failed: {outcome.failure_reason or 'unknown'}")
        self._validate_outcome_version(outcome)
        evidence = [
            self._evidence_record(
                attempt_id,
                draft,
                source_kind=source_kind,
                extractor_version=outcome.extractor_version,
                projection_input=projection_input,
            )
            for draft in outcome.evidence
        ]
        snapshots: list[dict[str, Any]] = []
        if not self._uses_topic_queue:
            for record in evidence:
                existing = await asyncio.to_thread(
                    self._store.list_cognitive_evidence,
                    topic_id=record["topic_id"],
                    hypothesis_code=record["hypothesis_code"],
                    extractor_version=self._extractor_version,
                    through_attempt_id=attempt_id,
                )
                control = await self._latest_control_action(record["topic_id"], record["hypothesis_code"])
                snapshot = project_cognitive_hypothesis(
                    (*existing, record),
                    model_version=self._model_version,
                    source_attempt_id=attempt_id,
                    computed_at=_iso_utc(projection_time),
                    control_action=control,
                    policy=self._policy,
                )
                snapshots.append(snapshot)
        await asyncio.to_thread(
            self._store.complete_cognitive_projection,
            attempt_id=attempt_id,
            lease_token=lease_token,
            evidence=evidence,
            snapshots=snapshots,
            **(
                {
                    "extractor_version": self._extractor_version,
                    "model_version": self._model_version,
                }
                if self._uses_topic_queue
                else {}
            ),
        )

    async def _rebuild_snapshot_history(
        self,
        topic_id: str,
        evidence_rows: Sequence[Mapping[str, Any]],
        *,
        intervention_events: Sequence[Mapping[str, Any]] = (),
        projection_time: datetime,
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for row in evidence_rows:
            code = str(row.get("hypothesis_code") or "").strip()
            row_topic = str(row.get("topic_id") or "").strip()
            if row_topic != topic_id or not code:
                raise ValueError("cognitive rebuild evidence has inconsistent identity")
            grouped.setdefault(code, []).append(row)
        snapshots: list[dict[str, Any]] = []
        computed_at = _iso_utc(projection_time)
        for code, rows in grouped.items():
            control = await self._latest_control_action(topic_id, code)
            for index, row in enumerate(rows, start=1):
                attempt_id = str(row.get("attempt_id") or "").strip()
                snapshots.append(
                    project_cognitive_hypothesis(
                        rows[:index],
                        model_version=self._model_version,
                        source_attempt_id=attempt_id,
                        computed_at=computed_at,
                        control_action=control,
                        policy=self._policy,
                    )
                )
            matching_events = [
                event
                for event in intervention_events
                if str(event.get("topic_id") or _nested_topic(event)).strip()
                == topic_id
                and str(
                    event.get("hypothesis_code") or _nested_code(event)
                ).strip()
                == code
            ]
            if matching_events:
                snapshots[-1] = project_cognitive_intervention_events(
                    snapshots[-1], matching_events, evidence_rows=rows
                )
        return snapshots

    async def _latest_control_action(self, topic_id: str, code: str) -> str:
        controls = await asyncio.to_thread(
            self._store.list_cognitive_user_controls,
            topic_id=topic_id,
            hypothesis_code=code,
            limit=1,
        )
        if not controls:
            return ""
        action = str(controls[0].get("action") or "").strip()
        expires_at = str(controls[0].get("expires_at") or "").strip()
        if action == "suppress" and expires_at:
            try:
                if _utc_datetime(expires_at) <= self._projection_time(None):
                    return ""
            except ValueError:
                return ""
        return action

    def _evidence_record(
        self,
        attempt_id: str,
        draft: CognitiveEvidenceDraft,
        *,
        source_kind: str,
        extractor_version: str,
        projection_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        question = projection_input.get("question")
        question_payload = question if isinstance(question, Mapping) else {}
        target_binding = question_payload.get("target_binding")
        binding_payload = target_binding if isinstance(target_binding, Mapping) else {}
        diagnostic_validation_id = str(
            question_payload.get("diagnostic_validation_id")
            or projection_input.get("diagnostic_validation_id")
            or ""
        ).strip()
        diagnosticity = self._policy.diagnosticity(source_kind)
        if source_kind in _DIAGNOSTIC_SOURCES and not diagnostic_validation_id:
            diagnosticity = self._policy.diagnosticity("practice")
        reviewed_family_id = str(
            question_payload.get("cognitive_question_family_id")
            or binding_payload.get("cognitive_question_family_id")
            or ""
        ).strip()
        template_id = str(
            question_payload.get("template_id")
            or question_payload.get("question_template_id")
            or ""
        ).strip()
        question_id = str(projection_input.get("question_id") or "").strip()
        source_question_id = str(
            projection_input.get("source_question_id") or ""
        ).strip()
        session_id = str(projection_input.get("session_id") or "").strip()
        family_source = (
            f"reviewed-family:{reviewed_family_id}"
            if reviewed_family_id
            else f"template:{template_id}"
            if template_id
            else f"question:{source_question_id or question_id}"
            if source_question_id or question_id
            else f"session:{session_id or attempt_id}"
        )
        return {
            "attempt_id": attempt_id,
            "topic_id": draft.topic_id,
            "hypothesis_code": draft.hypothesis_code,
            "direction": draft.direction,
            "strength": draft.strength,
            "extractor_confidence": draft.extractor_confidence,
            "diagnosticity": diagnosticity,
            "source_kind": source_kind,
            "evidence_span": draft.evidence_span,
            "extractor_version": extractor_version,
            "evidence_family_id": f"{family_source}:{draft.hypothesis_code}",
            "question_id": question_id,
            "session_id": session_id,
            "diagnostic_validation_id": diagnostic_validation_id,
        }

    def _validate_outcome_version(self, outcome: CognitiveExtractionOutcome) -> None:
        if outcome.extractor_version != self._extractor_version:
            raise ValueError("cognitive extractor version does not match queue version")
        if outcome.model_version != self._model_version:
            raise ValueError("cognitive model version does not match projector version")

    def _projection_time(self, as_of: datetime | str | None) -> datetime:
        return _utc_datetime(self._clock() if as_of is None else as_of)

    async def _mark_failed_without_raising(
        self,
        failure: CognitiveProjectionFailure,
        *,
        lease_token: str,
    ) -> None:
        if not failure.attempt_id or not lease_token:
            return
        try:
            await asyncio.to_thread(
                self._store.mark_cognitive_projection_failed,
                attempt_id=failure.attempt_id,
                lease_token=lease_token,
                error=failure.error,
                **(
                    {"extractor_version": self._extractor_version}
                    if self._uses_topic_queue
                    else {}
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return


def project_cognitive_hypothesis(
    evidence_rows: Sequence[Mapping[str, Any]],
    *,
    model_version: str,
    source_attempt_id: str,
    computed_at: str,
    control_action: str = "",
    policy: CognitiveProjectionPolicy = DEFAULT_COGNITIVE_PROJECTION_POLICY,
) -> dict[str, Any]:
    """Pure deterministic fold used by both incremental and full rebuilds."""

    evidence = tuple(_evidence_from_mapping(row) for row in evidence_rows)
    if not evidence:
        raise ValueError("cognitive evidence is required")
    topic_id = evidence[0].topic_id
    code = evidence[0].hypothesis_code
    if any(item.topic_id != topic_id or item.hypothesis_code != code for item in evidence):
        raise ValueError("cognitive evidence must belong to one hypothesis")
    if len({item.attempt_id for item in evidence}) != len(evidence):
        raise ValueError("cognitive evidence attempts must be independent")
    if source_attempt_id != evidence[-1].attempt_id:
        raise ValueError("snapshot source attempt must be the latest folded evidence")

    prior_logit = math.log(policy.prior_probability / (1.0 - policy.prior_probability))
    log_odds = prior_logit
    support_count = 0
    counter_count = 0
    diagnostic_support_count = 0
    ordinary_support_count = 0
    high_quality_diagnostic_count = 0
    relapse_count = 0
    status: HypothesisStatus = "hypothesized"

    seen_families: set[str] = set()
    ordinary_support_sessions: set[str] = set()
    for item in evidence:
        if item.evidence_family_id in seen_families:
            continue
        if (
            item.direction == "support"
            and item.source_kind not in _DIAGNOSTIC_SOURCES
            and item.session_id
            and item.session_id in ordinary_support_sessions
        ):
            continue
        seen_families.add(item.evidence_family_id)
        if (
            item.direction == "support"
            and item.source_kind not in _DIAGNOSTIC_SOURCES
            and item.session_id
        ):
            ordinary_support_sessions.add(item.session_id)
        log_odds += item.signed_weight
        probability = _sigmoid(log_odds)
        if item.direction == "support":
            support_count += 1
            if item.source_kind in _DIAGNOSTIC_SOURCES and item.diagnostic_validation_id:
                diagnostic_support_count += 1
                if (
                    item.unsigned_weight >= policy.high_quality_diagnostic_weight
                    and item.strength >= policy.high_quality_diagnostic_strength
                    and item.extractor_confidence >= policy.high_quality_diagnostic_confidence
                ):
                    high_quality_diagnostic_count += 1
            else:
                ordinary_support_count += 1
        else:
            counter_count += 1

        if item.direction == "support" and status in {"monitored", "resolved"}:
            relapse_count += 1
            status = "supported"
            continue
        if item.direction == "support" and status == "provisionally_resolved":
            status = "supported"
            continue
        if item.source_kind in _REPAIR_SOURCES and status in {
            "supported",
            "remediating",
        }:
            status = "provisionally_resolved" if item.direction == "counter" else "supported"
            continue
        if item.source_kind in _TRANSFER_SOURCES and status == "provisionally_resolved":
            status = "monitored" if item.direction == "counter" else "supported"
            continue
        if item.source_kind in _RETENTION_SOURCES and status == "monitored":
            # V2 deliberately stops at monitored. Delayed retention may add
            # ``resolved`` in V2.1, but counter-evidence from such a future
            # source cannot advance today's state machine early.
            continue
        if status in {"provisionally_resolved", "monitored", "resolved"}:
            continue
        if status == "supported":
            if probability < policy.contradicted_probability:
                status = "contradicted"
            continue
        support_is_independent = ordinary_support_count >= 2
        support_is_diagnostic = high_quality_diagnostic_count >= 1
        if probability >= policy.supported_probability and (support_is_independent or support_is_diagnostic):
            status = "supported"
        elif probability < policy.contradicted_probability:
            status = "contradicted"
        else:
            status = "hypothesized"

    probability = round(_sigmoid(log_odds), policy.probability_rounding_digits)
    control = str(control_action or "").strip()
    evidence_status = (
        "contradicted"
        if status == "contradicted"
        else "supported"
        if status
        in {
            "supported",
            "remediating",
            "provisionally_resolved",
            "monitored",
            "resolved",
        }
        else "hypothesized"
    )
    intervention_stage = (
        status
        if status
        in {"remediating", "provisionally_resolved", "monitored", "resolved"}
        else "idle"
    )
    user_override = {
        "dismiss": "dismissed",
        "suppress": "suppressed",
        "delete": "deleted",
    }.get(control, "")
    if control in _SUPPRESSING_CONTROLS:
        status = "dismissed"
    return {
        "hypothesis_id": f"{topic_id}:{code}",
        "topic_id": topic_id,
        "hypothesis_code": code,
        "status": status,
        "probability": probability,
        "support_count": support_count,
        "counter_count": counter_count,
        "diagnostic_support_count": diagnostic_support_count,
        "relapse_count": relapse_count,
        "source_attempt_id": source_attempt_id,
        "model_version": str(model_version or "").strip(),
        "computed_at": computed_at,
        "evidence_status": evidence_status,
        "intervention_stage": intervention_stage,
        "user_override": user_override,
        "last_intent": "",
        "last_outcome": "",
        "consecutive_repair_failures": 0,
    }


def project_cognitive_intervention_events(
    snapshot: Mapping[str, Any],
    intervention_events: Sequence[Mapping[str, Any]],
    *,
    evidence_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Deterministically fold committed teaching facts over evidence state."""

    result = dict(snapshot)
    evidence_status = str(result.get("evidence_status") or "").strip()
    if evidence_status != "supported":
        return result
    stage = str(result.get("intervention_stage") or "idle").strip() or "idle"
    last_intent = str(result.get("last_intent") or "").strip()
    last_outcome = str(result.get("last_outcome") or "").strip()
    failures = int(result.get("consecutive_repair_failures") or 0)
    committed: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    last_repair_failure_key: tuple[str, str] | None = None
    events = sorted(
        intervention_events,
        key=lambda item: (
            str(item.get("created_at") or item.get("occurred_at") or ""),
            int(item.get("event_seq") or 0),
            str(item.get("event_id") or ""),
        ),
    )
    abandoned_exact = {
        (
            str(item.get("decision_id") or "").strip(),
            str(item.get("question_id") or "").strip(),
            str(item.get("learning_intent") or "").strip(),
        )
        for item in events
        if str(item.get("event_type") or "").strip()
        == "intervention_abandoned"
        and str(item.get("question_id") or "").strip()
    }
    abandoned_decisions = {
        (
            str(item.get("decision_id") or "").strip(),
            str(item.get("learning_intent") or "").strip(),
        )
        for item in events
        if str(item.get("event_type") or "").strip()
        == "intervention_abandoned"
        and not str(item.get("question_id") or "").strip()
    }
    for event in events:
        target = event.get("hypothesis_target")
        hypothesis = target if isinstance(target, Mapping) else event
        if (
            str(hypothesis.get("hypothesis_id") or "").strip()
            != str(result.get("hypothesis_id") or "").strip()
            or str(hypothesis.get("model_version") or "").strip()
            != str(result.get("model_version") or "").strip()
        ):
            continue
        event_type = str(event.get("event_type") or "").strip()
        intent = str(event.get("learning_intent") or "").strip()
        decision_id = str(event.get("decision_id") or "").strip()
        question_id = str(event.get("question_id") or "").strip()
        key = (decision_id, question_id, intent)
        if event_type == "intent_proposed":
            continue
        if event_type == "question_committed":
            committed[key] = event
            last_intent = intent
            last_outcome = ""
            if intent == "misconception_probe":
                stage = "probing"
            elif intent == "misconception_repair":
                stage = "remediating"
            continue
        if event_type == "intervention_abandoned":
            matching_committed = (
                key in committed
                if question_id
                else any(
                    candidate[0] == decision_id and candidate[2] == intent
                    for candidate in committed
                )
            )
            if not matching_committed:
                continue
            last_intent = intent
            last_outcome = "abandoned"
            if intent == "transfer_check" and stage == "provisionally_resolved":
                continue
            stage = "idle"
            result["status"] = "supported"
            continue
        if (
            event_type != "attempt_committed"
            or key not in committed
            or key in abandoned_exact
            or (decision_id, intent) in abandoned_decisions
        ):
            continue
        verdict = str(event.get("evaluation_verdict") or "").strip()
        last_intent = intent
        last_outcome = verdict
        passed = verdict == "correct"
        if intent == "misconception_probe":
            result["status"] = "supported"
            if _has_authenticated_probe_support(event, evidence_rows):
                stage = "probing"
                last_outcome = "confirmed"
            else:
                stage = "idle"
                last_outcome = "not_confirmed"
        elif intent == "misconception_repair":
            if passed:
                stage = "provisionally_resolved"
                result["status"] = "provisionally_resolved"
                failures = 0
                last_repair_failure_key = None
            else:
                stage = "remediating"
                result["status"] = "supported"
                failure_key = (
                    str(event.get("session_id") or "").strip(),
                    str(event.get("repair_strategy") or "").strip(),
                )
                failures = failures + 1 if failure_key == last_repair_failure_key else 1
                last_repair_failure_key = failure_key
        elif intent == "transfer_check":
            if passed:
                stage = "monitored"
                result["status"] = "monitored"
            else:
                stage = "idle"
                result["status"] = "supported"
    result["intervention_stage"] = stage
    result["last_intent"] = last_intent
    result["last_outcome"] = last_outcome
    result["consecutive_repair_failures"] = failures
    if str(result.get("user_override") or "").strip():
        result["status"] = "dismissed"
    return result


def _has_authenticated_probe_support(
    event: Mapping[str, Any], evidence_rows: Sequence[Mapping[str, Any]]
) -> bool:
    attempt_id = str(event.get("attempt_id") or "").strip()
    validation_id = str(event.get("diagnostic_validation_id") or "").strip()
    if not attempt_id or not validation_id:
        return False
    return any(
        str(row.get("attempt_id") or "").strip() == attempt_id
        and str(row.get("direction") or "").strip() == "support"
        and str(row.get("diagnostic_validation_id") or "").strip()
        == validation_id
        for row in evidence_rows
    )


def _nested_topic(event: Mapping[str, Any]) -> str:
    target = event.get("hypothesis_target")
    return str(target.get("topic_id") or "") if isinstance(target, Mapping) else ""


def _nested_code(event: Mapping[str, Any]) -> str:
    target = event.get("hypothesis_target")
    return str(target.get("code") or "") if isinstance(target, Mapping) else ""


def _evidence_from_mapping(payload: Mapping[str, Any]) -> _ProjectionEvidence:
    direction = str(payload.get("direction") or "").strip()
    if direction not in {"support", "counter"}:
        raise ValueError("cognitive evidence direction is invalid")
    return _ProjectionEvidence(
        attempt_id=_required_text(payload.get("attempt_id"), "attempt_id"),
        topic_id=_required_text(payload.get("topic_id"), "topic_id"),
        hypothesis_code=_required_text(payload.get("hypothesis_code"), "hypothesis_code"),
        direction=cast(Literal["support", "counter"], direction),
        strength=_unit_float(payload.get("strength"), "strength"),
        extractor_confidence=_unit_float(payload.get("extractor_confidence"), "extractor_confidence"),
        diagnosticity=_unit_float(payload.get("diagnosticity"), "diagnosticity"),
        source_kind=_required_text(payload.get("source_kind") or "structured_attempt", "source_kind"),
        evidence_family_id=_required_text(
            payload.get("evidence_family_id") or f"attempt:{payload.get('attempt_id')}",
            "evidence_family_id",
        ),
        session_id=str(payload.get("session_id") or "").strip(),
        diagnostic_validation_id=str(
            payload.get("diagnostic_validation_id") or ""
        ).strip(),
    )


def _extraction_input(
    projection_input: Mapping[str, Any],
    *,
    allowed_hypotheses: tuple[str, ...],
) -> CognitiveExtractionInput:
    question = projection_input.get("question")
    question_payload = question if isinstance(question, Mapping) else {}
    public = question_payload.get("public_payload")
    public_payload = public if isinstance(public, Mapping) else {}
    private = question_payload.get("private_payload")
    private_payload = private if isinstance(private, Mapping) else {}
    evaluation = projection_input.get("evaluation")
    if not isinstance(evaluation, Mapping):
        raise TypeError("cognitive projection evaluation must be a mapping")
    question_text = (
        str(projection_input.get("question_text") or "").strip()
        or _first_text(
            question_payload,
            ("question", "question_text", "prompt", "text"),
        )
        or _first_text(public_payload, ("question", "question_text", "prompt", "text"))
    )
    expected_answer = (
        str(projection_input.get("expected_answer") or "").strip()
        or _first_text(
            question_payload,
            ("expected_answer", "answer", "reference_answer"),
        )
        or _first_text(
            private_payload,
            ("expected_answer", "answer", "reference_answer"),
        )
    )
    return CognitiveExtractionInput(
        topic_id=_required_text(projection_input.get("topic_id"), "topic_id"),
        question=question_text,
        expected_answer=expected_answer,
        learner_answer=str(projection_input.get("learner_answer") or ""),
        evaluation=evaluation,
        allowed_hypotheses=allowed_hypotheses,
    )


def _source_kind(projection_input: Mapping[str, Any]) -> str:
    explicit = str(projection_input.get("source_kind") or "").strip()
    if explicit:
        return explicit
    question = projection_input.get("question")
    if isinstance(question, Mapping):
        intent = str(question.get("learning_intent") or "").strip()
        if intent:
            return intent
        plan = question.get("plan")
        if isinstance(plan, Mapping):
            intent = str(plan.get("learning_intent") or "").strip()
            if intent:
                return intent
    return str(projection_input.get("learning_intent") or "practice").strip() or "practice"


def _first_text(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _required_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _unit_float(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field} must be between zero and one")
    return number


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        factor = math.exp(-value)
        return 1.0 / (1.0 + factor)
    factor = math.exp(value)
    return factor / (1.0 + factor)


def _error_text(exc: Exception) -> str:
    return (str(exc).strip() or exc.__class__.__name__)[:2_000]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("cognitive projection as_of is required")
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_COGNITIVE_PROJECTION_POLICY",
    "CognitiveEvidenceExtractorPort",
    "CognitiveProjectionFailure",
    "CognitiveProjectionPolicy",
    "CognitiveProjectionRunSummary",
    "CognitiveProjectionStore",
    "CognitiveProjector",
    "CognitiveRebuildSummary",
    "HypothesisStatus",
    "project_cognitive_intervention_events",
    "project_cognitive_hypothesis",
]
