"""Pure, rebuildable mastery V2 shadow projection.

The model in this module is intentionally detached from entries, stores, and
the V1 mastery tracker.  Callers provide immutable attempt facts and an
explicit projection time; the same facts therefore produce the same snapshot
whether they are supplied in one rebuild or accumulated incrementally.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Literal

MASTERY_V2_MODEL_VERSION = "mastery-v2-shadow-1"

Verdict = Literal["correct", "partial", "wrong", "dont_know"]


@dataclass(frozen=True, slots=True)
class MasteryV2Policy:
    """All versioned coefficients used by the shadow model.

    Keeping the coefficients on one immutable object makes a historical
    rebuild auditable and prevents entries, stores, or UIs from silently
    changing model behaviour.
    """

    model_version: str = MASTERY_V2_MODEL_VERSION
    correct_default_score: float = 1.0
    partial_default_score: float = 0.5
    wrong_default_score: float = 0.0
    difficulty_modifier_min: float = 0.9
    difficulty_modifier_max: float = 1.1
    hint_used_modifier: float = 0.85
    hint_not_used_modifier: float = 1.0
    hint_unknown_modifier: float = 1.0
    evaluator_confidence_default: float = 0.75
    response_time_min_reliability: float = 0.6
    response_time_suspicious_ms: int = 1_000
    response_time_full_reliability_ms: int = 5_000
    time_decay_half_life_days: float = 60.0
    consistency_floor: float = 0.7
    consistency_span: float = 0.3
    confidence_evidence_scale: float = 4.0
    confidence_floor: float = 0.5
    confidence_span: float = 0.5
    mastered_threshold: float = 0.8
    unresolved_wrong_mastery_cap: float = 0.79
    rounding_digits: int = 6

    def __post_init__(self) -> None:
        unit_interval_fields = (
            self.correct_default_score,
            self.partial_default_score,
            self.wrong_default_score,
            self.hint_used_modifier,
            self.hint_not_used_modifier,
            self.hint_unknown_modifier,
            self.evaluator_confidence_default,
            self.response_time_min_reliability,
            self.consistency_floor,
            self.consistency_span,
            self.confidence_floor,
            self.confidence_span,
            self.mastered_threshold,
            self.unresolved_wrong_mastery_cap,
        )
        if not self.model_version.strip():
            raise ValueError("model_version is required")
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in unit_interval_fields):
            raise ValueError("mastery policy probabilities must be finite values in [0, 1]")
        if (
            not math.isfinite(self.difficulty_modifier_min)
            or not math.isfinite(self.difficulty_modifier_max)
            or not 0.0 <= self.difficulty_modifier_min <= self.difficulty_modifier_max
        ):
            raise ValueError("difficulty modifiers must be ordered and non-negative")
        if self.response_time_suspicious_ms < 0:
            raise ValueError("response_time_suspicious_ms must be non-negative")
        if self.response_time_full_reliability_ms <= self.response_time_suspicious_ms:
            raise ValueError("full response-time threshold must exceed the suspicious threshold")
        if not math.isfinite(self.time_decay_half_life_days) or self.time_decay_half_life_days <= 0.0:
            raise ValueError("time_decay_half_life_days must be positive")
        if not math.isfinite(self.confidence_evidence_scale) or self.confidence_evidence_scale <= 0.0:
            raise ValueError("confidence_evidence_scale must be positive")
        if self.consistency_floor + self.consistency_span > 1.0:
            raise ValueError("consistency factor must not exceed 1")
        if self.confidence_floor + self.confidence_span > 1.0:
            raise ValueError("confidence factor must not exceed 1")
        if self.unresolved_wrong_mastery_cap >= self.mastered_threshold:
            raise ValueError("unresolved wrong mastery cap must be below the mastered threshold")
        if self.rounding_digits < 0:
            raise ValueError("rounding_digits must be non-negative")


DEFAULT_MASTERY_V2_POLICY = MasteryV2Policy()


@dataclass(frozen=True, slots=True)
class MasteryEvidence:
    """One immutable assessment fact consumed by the shadow model."""

    attempt_id: str
    verdict: str
    score: float | int | None
    difficulty: float | int | None
    used_hint: bool | None
    response_time_ms: int | None
    evaluator_confidence: float | None
    submitted_at: datetime | str

    def __post_init__(self) -> None:
        if not self.attempt_id.strip():
            raise ValueError("attempt_id is required")
        if self.used_hint is not None and not isinstance(self.used_hint, bool):
            raise TypeError("used_hint must be bool or None")


@dataclass(frozen=True, slots=True)
class MasteryV2Snapshot:
    """Versioned output ready for the V2 snapshot read model."""

    topic_id: str
    mastery: float
    accuracy: float
    recency: float
    consistency: float
    confidence: float
    evidence_count: int
    unresolved_wrong_count: int
    mastery_model_version: str
    source_attempt_id: str
    computed_at: str
    mastered: bool
    flags: tuple[str, ...] = ()

    def to_record(self) -> dict[str, object]:
        """Return only fields defined by ``mastery_snapshots_v2``."""

        return {
            "topic_id": self.topic_id,
            "mastery": self.mastery,
            "accuracy": self.accuracy,
            "recency": self.recency,
            "consistency": self.consistency,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "unresolved_wrong_count": self.unresolved_wrong_count,
            "mastery_model_version": self.mastery_model_version,
            "source_attempt_id": self.source_attempt_id,
            "computed_at": self.computed_at,
        }


@dataclass(frozen=True, slots=True)
class MasteryV2Accumulator:
    """Immutable in-memory fold used to verify incremental/rebuild parity.

    Durable consumers may rebuild one topic from its fact rows when a queued
    attempt arrives.  This small accumulator provides the equivalent
    incremental API without coupling the model to a persistence strategy.
    """

    topic_id: str
    policy: MasteryV2Policy = DEFAULT_MASTERY_V2_POLICY
    evidence: tuple[MasteryEvidence, ...] = field(default_factory=tuple)

    def add(self, item: MasteryEvidence) -> MasteryV2Accumulator:
        return MasteryV2Accumulator(
            topic_id=self.topic_id,
            policy=self.policy,
            evidence=(*self.evidence, item),
        )

    def extend(self, items: Iterable[MasteryEvidence]) -> MasteryV2Accumulator:
        state = self
        for item in items:
            state = state.add(item)
        return state

    def snapshot(
        self,
        *,
        unresolved_wrong_count: int,
        as_of: datetime | str,
    ) -> MasteryV2Snapshot:
        return calculate_mastery_v2(
            self.topic_id,
            self.evidence,
            unresolved_wrong_count=unresolved_wrong_count,
            as_of=as_of,
            policy=self.policy,
        )


@dataclass(frozen=True, slots=True)
class _PreparedEvidence:
    source: MasteryEvidence
    submitted_at: datetime
    normalized_score: float
    attempt_quality: float
    evidence_weight: float
    time_decay: float


def calculate_mastery_v2(
    topic_id: str,
    evidence: Iterable[MasteryEvidence],
    *,
    unresolved_wrong_count: int,
    as_of: datetime | str,
    policy: MasteryV2Policy = DEFAULT_MASTERY_V2_POLICY,
) -> MasteryV2Snapshot:
    """Project one topic from immutable facts using ``policy``.

    ``as_of`` is required rather than reading the clock, so a rebuild is
    deterministic.  Duplicate deliveries with the same attempt ID are ignored
    when their facts match; conflicting duplicates are rejected.
    """

    resolved_topic_id = str(topic_id or "").strip()
    if not resolved_topic_id:
        raise ValueError("topic_id is required")
    if isinstance(unresolved_wrong_count, bool):
        raise TypeError("unresolved_wrong_count must be an integer")
    resolved_wrong_count = max(0, int(unresolved_wrong_count))
    projection_time = _coerce_datetime(as_of)
    unique_evidence = _deduplicate_evidence(evidence)
    prepared = [
        _prepare_evidence(item, as_of=projection_time, policy=policy)
        for item in unique_evidence
    ]
    prepared.sort(key=lambda item: (item.submitted_at, item.source.attempt_id))

    evidence_count = len(prepared)
    source_attempt_id = prepared[-1].source.attempt_id if prepared else ""
    flags: list[str] = []
    if not prepared:
        flags.append("no_evidence")
        return MasteryV2Snapshot(
            topic_id=resolved_topic_id,
            mastery=0.0,
            accuracy=0.0,
            recency=0.0,
            consistency=0.0,
            confidence=0.0,
            evidence_count=0,
            unresolved_wrong_count=resolved_wrong_count,
            mastery_model_version=policy.model_version,
            source_attempt_id="",
            computed_at=_format_datetime(projection_time),
            mastered=False,
            flags=tuple(flags),
        )

    total_weight = sum(item.evidence_weight for item in prepared)
    if total_weight > 0.0:
        accuracy = sum(
            item.normalized_score * item.evidence_weight for item in prepared
        ) / total_weight
        weighted_quality = sum(
            item.attempt_quality * item.evidence_weight for item in prepared
        ) / total_weight
        variance = sum(
            item.evidence_weight * (item.normalized_score - accuracy) ** 2
            for item in prepared
        ) / total_weight
    else:
        accuracy = 0.0
        weighted_quality = 0.0
        variance = 0.25
        flags.append("zero_confidence_evidence")

    consistency = _clamp(1.0 - 2.0 * math.sqrt(max(0.0, variance)))
    recency = sum(item.time_decay for item in prepared) / evidence_count
    confidence = 1.0 - math.exp(-total_weight / policy.confidence_evidence_scale)
    consistency_factor = policy.consistency_floor + policy.consistency_span * consistency
    confidence_factor = policy.confidence_floor + policy.confidence_span * confidence
    mastery = _clamp(weighted_quality * consistency_factor * confidence_factor)

    if resolved_wrong_count > 0:
        mastery = min(mastery, policy.unresolved_wrong_mastery_cap)
        flags.append("unresolved_wrong_cap")

    rounded_mastery = _rounded_unit(mastery, policy)
    mastered = resolved_wrong_count == 0 and rounded_mastery >= policy.mastered_threshold
    return MasteryV2Snapshot(
        topic_id=resolved_topic_id,
        mastery=rounded_mastery,
        accuracy=_rounded_unit(accuracy, policy),
        recency=_rounded_unit(recency, policy),
        consistency=_rounded_unit(consistency, policy),
        confidence=_rounded_unit(confidence, policy),
        evidence_count=evidence_count,
        unresolved_wrong_count=resolved_wrong_count,
        mastery_model_version=policy.model_version,
        source_attempt_id=source_attempt_id,
        computed_at=_format_datetime(projection_time),
        mastered=mastered,
        flags=tuple(flags),
    )


def _deduplicate_evidence(evidence: Iterable[MasteryEvidence]) -> tuple[MasteryEvidence, ...]:
    unique: dict[str, MasteryEvidence] = {}
    for item in evidence:
        if not isinstance(item, MasteryEvidence):
            raise TypeError("evidence items must be MasteryEvidence")
        previous = unique.get(item.attempt_id)
        if previous is not None and previous != item:
            raise ValueError(f"conflicting facts for attempt_id: {item.attempt_id}")
        unique[item.attempt_id] = item
    return tuple(unique.values())


def _prepare_evidence(
    item: MasteryEvidence,
    *,
    as_of: datetime,
    policy: MasteryV2Policy,
) -> _PreparedEvidence:
    submitted_at = _coerce_datetime(item.submitted_at, default=as_of)
    normalized_score = _normalized_score(item.verdict, item.score, policy)
    difficulty_modifier = _difficulty_modifier(item.difficulty, policy)
    hint_modifier = _hint_modifier(item.used_hint, policy)
    attempt_quality = _clamp(normalized_score * difficulty_modifier * hint_modifier)
    evaluator_confidence = _finite_unit(
        item.evaluator_confidence,
        default=policy.evaluator_confidence_default,
    )
    response_reliability = _response_time_reliability(item.response_time_ms, policy)
    time_decay = _time_decay(submitted_at, as_of=as_of, policy=policy)
    evidence_weight = _clamp(evaluator_confidence * response_reliability * time_decay)
    return _PreparedEvidence(
        source=item,
        submitted_at=submitted_at,
        normalized_score=normalized_score,
        attempt_quality=attempt_quality,
        evidence_weight=evidence_weight,
        time_decay=time_decay,
    )


def _normalized_score(
    verdict: str,
    score: float | int | None,
    policy: MasteryV2Policy,
) -> float:
    normalized_verdict = str(verdict or "").strip().lower()
    fallback = {
        "correct": policy.correct_default_score,
        "partial": policy.partial_default_score,
        "wrong": policy.wrong_default_score,
        "dont_know": policy.wrong_default_score,
    }.get(normalized_verdict, policy.wrong_default_score)
    if normalized_verdict in {"wrong", "dont_know"}:
        return policy.wrong_default_score
    if isinstance(score, bool) or score is None:
        return fallback
    try:
        numeric_score = float(score)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if not math.isfinite(numeric_score):
        return fallback
    return _clamp(numeric_score / 100.0)


def _difficulty_modifier(
    difficulty: float | int | None,
    policy: MasteryV2Policy,
) -> float:
    if isinstance(difficulty, bool) or difficulty is None:
        normalized = 0.5
    else:
        try:
            numeric = float(difficulty)
        except (TypeError, ValueError, OverflowError):
            numeric = 3.0
        if not math.isfinite(numeric):
            numeric = 3.0
        # Generated questions use the server-owned 1..5 scale.
        normalized = _clamp((numeric - 1.0) / 4.0)
    return policy.difficulty_modifier_min + (
        policy.difficulty_modifier_max - policy.difficulty_modifier_min
    ) * normalized


def _hint_modifier(used_hint: bool | None, policy: MasteryV2Policy) -> float:
    if used_hint is None:
        return policy.hint_unknown_modifier
    if used_hint:
        return policy.hint_used_modifier
    return policy.hint_not_used_modifier


def _response_time_reliability(
    response_time_ms: int | None,
    policy: MasteryV2Policy,
) -> float:
    if isinstance(response_time_ms, bool) or response_time_ms is None or response_time_ms < 0:
        return 1.0
    if response_time_ms <= policy.response_time_suspicious_ms:
        return policy.response_time_min_reliability
    if response_time_ms >= policy.response_time_full_reliability_ms:
        return 1.0
    span = policy.response_time_full_reliability_ms - policy.response_time_suspicious_ms
    progress = (response_time_ms - policy.response_time_suspicious_ms) / span
    return policy.response_time_min_reliability + (
        1.0 - policy.response_time_min_reliability
    ) * progress


def _time_decay(
    submitted_at: datetime,
    *,
    as_of: datetime,
    policy: MasteryV2Policy,
) -> float:
    age_seconds = max(0.0, (as_of - submitted_at).total_seconds())
    age_days = age_seconds / 86_400.0
    return _clamp(math.exp(-math.log(2.0) * age_days / policy.time_decay_half_life_days))


def _finite_unit(value: float | int | None, *, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return _clamp(default)
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return _clamp(default)
    if not math.isfinite(numeric):
        return _clamp(default)
    return _clamp(numeric)


def _clamp(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, value))


def _rounded_unit(value: float, policy: MasteryV2Policy) -> float:
    return round(_clamp(value), policy.rounding_digits)


def _coerce_datetime(
    value: datetime | str,
    *,
    default: datetime | None = None,
) -> datetime:
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            if default is None:
                raise ValueError("datetime value is required")
            return default
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            if default is None:
                raise
            return default
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
