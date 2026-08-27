"""Deterministic, bounded difficulty selection for targeted questions.

The policy deliberately consumes only server-owned, private selection data.
It does not persist a decision and it never exposes a new question payload
field, so it can be rolled back by removing its single entry-point call.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


def _unit(value: object, *, default: float = 0.0) -> float:
    """Return a finite value constrained to the unit interval."""

    try:
        resolved = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(resolved):
        return default
    return min(1.0, max(0.0, resolved))


def _seed_difficulty_unit(value: object) -> float:
    """Normalize either seed's 0..1 value or a legacy 1..5 level."""

    try:
        resolved = float(value)
    except (TypeError, ValueError):
        return 0.5
    if not math.isfinite(resolved):
        return 0.5
    if resolved > 1.0:
        return _unit((resolved - 1.0) / 4.0, default=0.5)
    return _unit(resolved, default=0.5)


def _level(value: float) -> int:
    if value < 0.35:
        return 2
    if value < 0.65:
        return 3
    return 4


def _recent_verdicts(value: object) -> tuple[str, ...]:
    """Accept the private tracker shapes without relying on persistence."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    verdicts: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            item = item.get("verdict") or item.get("result") or ""
        verdict = str(item or "").strip().casefold()
        if verdict:
            verdicts.append(verdict)
    return tuple(verdicts)


def _has_streak(verdicts: tuple[str, ...], accepted: frozenset[str]) -> bool:
    return len(verdicts) >= 2 and verdicts[-1] in accepted and verdicts[-2] in accepted


@dataclass(frozen=True)
class DifficultyPolicy:
    """Pure policy that only returns the staged range 2, 3, or 4."""

    low_evidence_attempts: int = 3
    low_confidence_threshold: float = 0.6

    def select(
        self,
        topic: Mapping[str, Any] | None,
        *,
        mastery: Mapping[str, Any] | None = None,
        blockers: Sequence[object] | None = None,
        selection_reason: str = "",
        retry_wrong_question: Mapping[str, Any] | None = None,
        recent_results: Sequence[object] | None = None,
    ) -> int:
        """Choose a reproducible question difficulty from server-owned inputs.

        Seed complexity and observed mastery are averaged before quantization.
        The safety constraints then win in this order: prerequisite blockers,
        retry decrement, streak adjustment, and the evidence confidence cap.
        """

        topic_payload = topic if isinstance(topic, Mapping) else {}
        mastery_payload = mastery if isinstance(mastery, Mapping) else {}
        combined = (
            _seed_difficulty_unit(topic_payload.get("difficulty"))
            + _unit(mastery_payload.get("mastery"), default=0.0)
        ) / 2.0
        difficulty = _level(combined)

        if any(True for _ in (blockers or ())):
            return 2

        normalized_reason = str(selection_reason or "").strip().casefold()
        retry = retry_wrong_question if isinstance(retry_wrong_question, Mapping) else {}
        is_retry = normalized_reason in {"retry", "wrong_retry"} or bool(retry)
        if is_retry:
            difficulty -= 1

        raw_recent = recent_results
        if raw_recent is None:
            raw_recent = (
                mastery_payload.get("recent_results")
                or mastery_payload.get("recent_verdicts")
                or mastery_payload.get("recent_outcomes")
                or ()
            )
        verdicts = _recent_verdicts(raw_recent)
        # A retry already has its one-step decrease.  Do not stack a recent
        # wrong streak onto it: a retry must never drop more than one level.
        if not is_retry:
            if _has_streak(verdicts, frozenset({"correct"})):
                difficulty += 1
            elif _has_streak(verdicts, frozenset({"wrong", "dont_know"})):
                difficulty -= 1

        try:
            attempts = max(0, int(mastery_payload.get("attempts") or 0))
        except (TypeError, ValueError):
            attempts = 0
        confidence = _unit(mastery_payload.get("confidence"), default=0.0)
        flags = {str(flag or "").strip().casefold() for flag in mastery_payload.get("flags") or ()}
        low_evidence = (
            attempts < self.low_evidence_attempts
            or confidence < self.low_confidence_threshold
            or "low_confidence" in flags
        )
        if low_evidence:
            difficulty = min(difficulty, 3)
        return min(4, max(2, difficulty))


DEFAULT_DIFFICULTY_POLICY = DifficultyPolicy()


def select_targeted_difficulty(
    topic: Mapping[str, Any] | None,
    *,
    mastery: Mapping[str, Any] | None = None,
    blockers: Sequence[object] | None = None,
    selection_reason: str = "",
    retry_wrong_question: Mapping[str, Any] | None = None,
    recent_results: Sequence[object] | None = None,
) -> int:
    """Select the bounded server-owned difficulty for a targeted question."""

    return DEFAULT_DIFFICULTY_POLICY.select(
        topic,
        mastery=mastery,
        blockers=blockers,
        selection_reason=selection_reason,
        retry_wrong_question=retry_wrong_question,
        recent_results=recent_results,
    )


__all__ = ["DEFAULT_DIFFICULTY_POLICY", "DifficultyPolicy", "select_targeted_difficulty"]
