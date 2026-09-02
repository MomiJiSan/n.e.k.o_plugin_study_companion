"""Fail-closed read model for current cognitive hypothesis state.

The reader deliberately exposes no mutation.  It accepts only a synchronous
store protocol and verifies the topic projection generation both before and
after reading current hypotheses.  A concurrent projection request therefore
cannot leak a stale hypothesis into coaching policy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Protocol, cast

from .cognitive_contracts import DEFAULT_COGNITIVE_MODEL_VERSION
from .contracts import HypothesisRef

CognitiveEvidenceStatus = Literal["hypothesized", "supported", "contradicted"]
CognitiveInterventionStage = Literal[
    "idle",
    "probing",
    "remediating",
    "provisionally_resolved",
    "monitored",
    "resolved",
]
CognitiveReadReason = Literal[
    "ready",
    "missing_projection",
    "projection_not_ready",
    "stale_projection",
    "version_mismatch",
    "concurrent_update",
    "read_failed",
]

_EVIDENCE_STATUSES = frozenset({"hypothesized", "supported", "contradicted"})
_INTERVENTION_STAGES = frozenset(
    {
        "idle",
        "probing",
        "remediating",
        "provisionally_resolved",
        "monitored",
        "resolved",
    }
)
_BLOCKING_OVERRIDES = frozenset({"dismiss", "dismissed", "suppress", "suppressed", "delete", "deleted"})


class CognitiveStateStorePort(Protocol):
    """Minimum synchronous persistence view used by :class:`CognitiveStateReader`."""

    def get_cognitive_topic_projection_state(
        self, *, topic_id: str, model_version: str
    ) -> Mapping[str, Any] | None: ...

    def list_cognitive_hypothesis_current(
        self,
        *,
        topic_id: str | None = None,
        hypothesis_code: str | None = None,
        model_version: str | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...

    def list_cognitive_user_controls(
        self,
        *,
        topic_id: str | None = None,
        hypothesis_code: str | None = None,
        limit: int = 100,
        active_only: bool = False,
        as_of: str | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class LearnerCognitiveHypothesis:
    """One usable current hypothesis plus its deterministic intervention state."""

    ref: HypothesisRef
    evidence_status: CognitiveEvidenceStatus
    intervention_stage: CognitiveInterventionStage = "idle"
    last_intent: str = ""
    last_outcome: str = ""
    support_count: int = 0
    counter_count: int = 0
    diagnostic_support_count: int = 0
    relapse_count: int = 0
    consecutive_repair_failures: int = 0
    computed_at: str = ""


@dataclass(frozen=True, slots=True)
class LearnerCognitiveStateView:
    """A fail-closed, generation-consistent topic view for coaching policy."""

    topic_id: str
    model_version: str
    requested_generation: int = 0
    projected_generation: int = 0
    hypotheses: tuple[LearnerCognitiveHypothesis, ...] = ()
    usable: bool = False
    reason: CognitiveReadReason = "missing_projection"

    @classmethod
    def empty(
        cls,
        topic_id: str,
        model_version: str,
        *,
        reason: CognitiveReadReason,
        requested_generation: int = 0,
        projected_generation: int = 0,
    ) -> LearnerCognitiveStateView:
        return cls(
            topic_id=topic_id,
            model_version=model_version,
            requested_generation=requested_generation,
            projected_generation=projected_generation,
            usable=False,
            reason=reason,
        )


class CognitiveStateReader:
    """Read current state without ever falling back to historical snapshots."""

    def __init__(
        self,
        store: CognitiveStateStorePort,
        *,
        model_version: str = DEFAULT_COGNITIVE_MODEL_VERSION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._model_version = _required_text(model_version, "model_version")
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def read_topic(self, topic_id: str) -> LearnerCognitiveStateView:
        topic_key = _required_text(topic_id, "topic_id")
        try:
            before = self._store.get_cognitive_topic_projection_state(
                topic_id=topic_key, model_version=self._model_version
            )
            checked = _check_projection(before, topic_key, self._model_version)
            if checked.reason != "ready":
                return checked

            as_of = _iso_utc(self._clock())
            rows = self._store.list_cognitive_hypothesis_current(
                topic_id=topic_key,
                model_version=self._model_version,
            )
            controls = self._store.list_cognitive_user_controls(
                topic_id=topic_key,
                active_only=True,
                as_of=as_of,
            )
            after = self._store.get_cognitive_topic_projection_state(
                topic_id=topic_key, model_version=self._model_version
            )
        except Exception:
            return LearnerCognitiveStateView.empty(
                topic_key, self._model_version, reason="read_failed"
            )

        after_checked = _check_projection(after, topic_key, self._model_version)
        if after_checked.reason != "ready":
            return after_checked
        if _generation_identity(before) != _generation_identity(after):
            return LearnerCognitiveStateView.empty(
                topic_key,
                self._model_version,
                reason="concurrent_update",
                requested_generation=after_checked.requested_generation,
                projected_generation=after_checked.projected_generation,
            )

        blocked_codes = _blocked_hypothesis_codes(controls)
        hypotheses: list[LearnerCognitiveHypothesis] = []
        for row in rows:
            parsed = _hypothesis_from_row(
                row,
                topic_id=topic_key,
                model_version=self._model_version,
                projection_generation=after_checked.projected_generation,
            )
            if parsed is None or parsed.ref.code in blocked_codes:
                continue
            hypotheses.append(parsed)
        hypotheses.sort(key=_hypothesis_sort_key)
        return LearnerCognitiveStateView(
            topic_id=topic_key,
            model_version=self._model_version,
            requested_generation=after_checked.requested_generation,
            projected_generation=after_checked.projected_generation,
            hypotheses=tuple(hypotheses),
            usable=True,
            reason="ready",
        )


def _check_projection(
    row: Mapping[str, Any] | None, topic_id: str, model_version: str
) -> LearnerCognitiveStateView:
    if not isinstance(row, Mapping):
        return LearnerCognitiveStateView.empty(
            topic_id, model_version, reason="missing_projection"
        )
    requested = _nonnegative_int(row.get("requested_generation"))
    projected = _nonnegative_int(row.get("projected_generation"))
    row_topic = str(row.get("topic_id") or "").strip()
    row_version = str(row.get("model_version") or "").strip()
    if row_topic != topic_id or row_version != model_version:
        return LearnerCognitiveStateView.empty(
            topic_id,
            model_version,
            reason="version_mismatch",
            requested_generation=requested,
            projected_generation=projected,
        )
    if str(row.get("status") or "").strip() != "done":
        return LearnerCognitiveStateView.empty(
            topic_id,
            model_version,
            reason="projection_not_ready",
            requested_generation=requested,
            projected_generation=projected,
        )
    if requested != projected:
        return LearnerCognitiveStateView.empty(
            topic_id,
            model_version,
            reason="stale_projection",
            requested_generation=requested,
            projected_generation=projected,
        )
    return LearnerCognitiveStateView(
        topic_id=topic_id,
        model_version=model_version,
        requested_generation=requested,
        projected_generation=projected,
        usable=True,
        reason="ready",
    )


def _hypothesis_from_row(
    row: Mapping[str, Any],
    *,
    topic_id: str,
    model_version: str,
    projection_generation: int,
) -> LearnerCognitiveHypothesis | None:
    row_topic = str(row.get("topic_id") or "").strip()
    row_version = str(row.get("model_version") or "").strip()
    row_generation = _nonnegative_int(row.get("projected_generation"))
    evidence_status = str(row.get("evidence_status") or "").strip()
    intervention_stage = str(row.get("intervention_stage") or "idle").strip()
    user_override = str(row.get("user_override") or "").strip().lower()
    combined_status = str(row.get("status") or "").strip().lower()
    if (
        row_topic != topic_id
        or row_version != model_version
        or row_generation != projection_generation
        or evidence_status not in _EVIDENCE_STATUSES
        or intervention_stage not in _INTERVENTION_STAGES
        or user_override in _BLOCKING_OVERRIDES
        or combined_status in _BLOCKING_OVERRIDES
    ):
        return None
    code = str(row.get("hypothesis_code") or "").strip()
    hypothesis_id = str(row.get("hypothesis_id") or "").strip()
    if not code or not hypothesis_id:
        return None
    probability = _unit_float(row.get("probability"))
    if probability is None:
        return None
    ref_values: dict[str, Any] = {
        "hypothesis_id": hypothesis_id,
        "topic_id": topic_id,
        "code": code,
        "status": evidence_status,
        "probability": probability,
        "model_version": model_version,
        "source_snapshot_id": str(row.get("source_snapshot_id") or ""),
        "source_attempt_id": str(row.get("source_attempt_id") or ""),
        "projection_generation": projection_generation,
    }
    accepted = {item.name for item in fields(HypothesisRef)}
    ref = HypothesisRef(**{key: value for key, value in ref_values.items() if key in accepted})
    return LearnerCognitiveHypothesis(
        ref=ref,
        evidence_status=cast(CognitiveEvidenceStatus, evidence_status),
        intervention_stage=cast(CognitiveInterventionStage, intervention_stage),
        last_intent=str(row.get("last_intent") or "").strip(),
        last_outcome=str(row.get("last_outcome") or "").strip(),
        support_count=_nonnegative_int(row.get("support_count")),
        counter_count=_nonnegative_int(row.get("counter_count")),
        diagnostic_support_count=_nonnegative_int(row.get("diagnostic_support_count")),
        relapse_count=_nonnegative_int(row.get("relapse_count")),
        consecutive_repair_failures=_nonnegative_int(
            row.get("consecutive_repair_failures")
        ),
        computed_at=str(row.get("computed_at") or ""),
    )


def _blocked_hypothesis_codes(rows: Sequence[Mapping[str, Any]]) -> set[str]:
    blocked: set[str] = set()
    latest: dict[str, str] = {}
    for row in rows:
        code = str(row.get("hypothesis_code") or "").strip()
        if not code or code in latest:
            continue
        latest[code] = str(row.get("action") or "").strip().lower()
    for code, action in latest.items():
        if action in _BLOCKING_OVERRIDES:
            blocked.add(code)
    return blocked


def _generation_identity(row: Mapping[str, Any] | None) -> tuple[object, ...]:
    if not isinstance(row, Mapping):
        return ()
    return (
        row.get("topic_id"),
        row.get("model_version"),
        row.get("status"),
        row.get("requested_generation"),
        row.get("projected_generation"),
    )


def _hypothesis_sort_key(item: LearnerCognitiveHypothesis) -> tuple[object, ...]:
    active_rank = 0 if item.intervention_stage != "idle" else 1
    evidence_rank = 0 if item.evidence_status == "supported" else 1
    return (active_rank, evidence_rank, -item.ref.probability, item.ref.code)


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _unit_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0.0 <= number <= 1.0:
        return None
    return number


def _required_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _iso_utc(value: object) -> str:
    if not isinstance(value, datetime):
        raise TypeError("cognitive state clock must return datetime")
    parsed = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "CognitiveEvidenceStatus",
    "CognitiveInterventionStage",
    "CognitiveReadReason",
    "CognitiveStateReader",
    "CognitiveStateStorePort",
    "LearnerCognitiveHypothesis",
    "LearnerCognitiveStateView",
]
