from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ATTEMPT_STATUSES = frozenset({"correct", "partial", "wrong", "dont_know"})
MASTERY_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
MASTERY_PROGRESSING = "progressing"
MASTERY_MASTERED = "mastered"
SCOPE_ACTIVE = "active"
SCOPE_REVIEWING = "reviewing"

MASTERY_THRESHOLD = 0.80
MASTERY_MIN_ATTEMPTS = 3


def _attempt_status(verdict: object) -> str:
    normalized = str(verdict or "").strip().lower()
    return normalized if normalized in ATTEMPT_STATUSES else "wrong"


def _mastery_status(
    snapshot: Mapping[str, Any] | None,
    *,
    has_active_wrong_question: bool,
) -> str:
    if not isinstance(snapshot, Mapping):
        return MASTERY_INSUFFICIENT_EVIDENCE
    try:
        attempts = max(0, int(snapshot.get("attempts") or 0))
        mastery = float(snapshot.get("mastery") or 0.0)
    except (TypeError, ValueError, OverflowError):
        return MASTERY_INSUFFICIENT_EVIDENCE
    flags = {
        str(flag or "").strip().lower()
        for flag in (
            snapshot.get("flags") if isinstance(snapshot.get("flags"), list) else []
        )
        if str(flag or "").strip()
    }
    if attempts < MASTERY_MIN_ATTEMPTS or "low_confidence" in flags:
        return MASTERY_INSUFFICIENT_EVIDENCE
    if mastery >= MASTERY_THRESHOLD and not has_active_wrong_question:
        return MASTERY_MASTERED
    return MASTERY_PROGRESSING


def build_practice_outcome(
    *,
    verdict: object,
    practice_scope: Mapping[str, Any] | None,
    active_scope_matches: bool,
    validated_target: bool,
    mastery_snapshot: Mapping[str, Any] | None = None,
    has_active_wrong_question: bool = False,
) -> dict[str, Any]:
    attempt_status = _attempt_status(verdict)
    mastery_status = (
        _mastery_status(
            mastery_snapshot,
            has_active_wrong_question=has_active_wrong_question,
        )
        if validated_target
        else MASTERY_INSUFFICIENT_EVIDENCE
    )
    if attempt_status != "correct" and mastery_status == MASTERY_MASTERED:
        mastery_status = MASTERY_PROGRESSING
    scope = dict(practice_scope or {})
    scope_status = (
        SCOPE_REVIEWING
        if attempt_status == "correct"
        and active_scope_matches
        and str(scope.get("mode") or "").strip().lower() == "explicit_topic"
        and mastery_status == MASTERY_MASTERED
        else SCOPE_ACTIVE
    )
    return {
        "attempt_status": attempt_status,
        "scope_status": scope_status,
        "mastery_status": mastery_status,
        # Compatibility for clients released before the three-state contract.
        # "completed" is now emitted only when the selected topic is mastered.
        "practice_scope_status": (
            "completed" if scope_status == SCOPE_REVIEWING else "active"
        ),
        "can_continue_review": scope_status == SCOPE_REVIEWING,
    }
