"""Versioned trial retention math, independent of scheduling and wall-clock IO.

This is a trial heuristic, not FSRS or a trained HLR model. Values are fractions;
rounding is a presentation concern and must never decide card destruction.
"""

from __future__ import annotations

import math

MODEL_VERSION = "mastery-retention-trial-1"
BASELINE_VERSION = "mastery-retention-evidence-1"
INITIAL_HALF_LIFE = 7.0
ZERO_THRESHOLD = 0.001
ACQUIRE_THRESHOLD = 0.01


def finite_fraction(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a mastery fraction")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError("expected a finite fraction in [0, 1]")
    return result


def current_mastery(baseline: float, half_life: float, elapsed_days: float) -> float:
    baseline = finite_fraction(baseline)
    if not math.isfinite(half_life) or half_life <= 0:
        raise ValueError("invalid half life")
    if not math.isfinite(elapsed_days) or elapsed_days < 0:
        raise ValueError("invalid elapsed time")
    value = baseline * math.exp2(-elapsed_days / half_life)
    return 0.0 if value < ZERO_THRESHOLD else value


def feedback_half_life(
    half_life: float, elapsed_days: float, verdict: str,
    confidence: float | None, used_hint: bool | None,
) -> float:
    current_mastery(1.0, half_life, elapsed_days)
    if confidence is None or used_hint is None:
        return half_life
    confidence = finite_fraction(confidence)
    weight = -math.expm1(-math.log(2) * elapsed_days / half_life)
    if verdict == "correct" and used_hint is False:
        result = half_life * (1 + confidence * weight)
    elif verdict in {"wrong", "dont_know"}:
        result = half_life * (1 - 0.5 * confidence * weight)
    else:
        result = half_life
    if not math.isfinite(result) or result <= 0:
        raise ValueError("half life exceeded numeric range")
    return result


def evidence_baseline(scores: list[float]) -> float:
    """V1-style consistency estimate over independent recent evidence groups.

    No calendar weighting is applied here: retention applies time exactly once.
    Prompted scores are discounted by the evidence collector, not this function.
    """
    values = [finite_fraction(score) for score in scores[-10:]]
    if not values:
        raise ValueError("missing mastery evidence")
    accuracy = sum(values) / len(values)
    variance = sum((score - accuracy) ** 2 for score in values) / len(values)
    consistency = max(0.0, 1 - 2 * math.sqrt(variance))
    support = 1 - math.exp(-len(values) / 5)
    return min(1.0, accuracy * (0.6 + 0.4 * consistency) * (0.55 + 0.45 * support))
