"""Strict, whole-batch validation for cognitive extraction output."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from .cognitive_catalog import CognitiveCatalog
from .cognitive_contracts import CognitiveEvidenceDraft, CognitiveExtractionInput

MAX_EVIDENCE_PER_ATTEMPT = 3
MAX_EVIDENCE_SPAN_CHARACTERS = 1_000

_TOP_LEVEL_FIELDS = frozenset({"evidence"})
_EVIDENCE_FIELDS = frozenset(
    {
        "hypothesis_code",
        "direction",
        "strength",
        "extractor_confidence",
        "evidence_span",
    }
)


class CognitiveValidationError(ValueError):
    """A fail-closed input or output contract violation."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_allowed_hypotheses(
    extraction_input: CognitiveExtractionInput,
    catalog: CognitiveCatalog,
) -> tuple[str, ...]:
    """Validate the extraction scope and return a unique closed-world list."""

    topic_id = str(extraction_input.topic_id or "").strip()
    if not catalog.supports_topic(topic_id):
        raise CognitiveValidationError(
            "unsupported cognitive topic", code="unsupported_topic"
        )
    catalog_codes = catalog.allowed_codes(topic_id)
    requested = extraction_input.allowed_hypotheses or catalog_codes
    normalized: list[str] = []
    for value in requested:
        if not isinstance(value, str) or not value.strip():
            raise CognitiveValidationError(
                "invalid allowed hypothesis", code="invalid_input"
            )
        code = value.strip()
        if code not in catalog_codes:
            raise CognitiveValidationError(
                "allowed hypothesis is outside the topic catalog",
                code="invalid_input",
            )
        if code in normalized:
            raise CognitiveValidationError(
                "duplicate allowed hypothesis", code="invalid_input"
            )
        normalized.append(code)
    if not normalized or len(normalized) > MAX_EVIDENCE_PER_ATTEMPT:
        raise CognitiveValidationError(
            "allowed hypothesis count must be between one and three",
            code="invalid_input",
        )
    if not isinstance(extraction_input.evaluation, Mapping):
        raise CognitiveValidationError(
            "evaluation must be a mapping", code="invalid_input"
        )
    return tuple(normalized)


def validate_extraction_output(
    raw_output: Mapping[str, Any] | str,
    *,
    topic_id: str,
    allowed_hypotheses: tuple[str, ...],
) -> tuple[CognitiveEvidenceDraft, ...]:
    """Validate every output item before returning any evidence.

    This function intentionally constructs the result only after the complete
    batch has passed validation, so callers cannot accidentally persist a
    valid prefix followed by an invalid or out-of-scope item.
    """

    payload = _parse_payload(raw_output)
    if set(payload) != _TOP_LEVEL_FIELDS:
        raise CognitiveValidationError(
            "model output must contain only an evidence field",
            code="invalid_model_output",
        )
    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, list):
        raise CognitiveValidationError(
            "evidence must be a list", code="invalid_model_output"
        )
    if len(raw_evidence) > MAX_EVIDENCE_PER_ATTEMPT:
        raise CognitiveValidationError(
            "model returned too many evidence items",
            code="invalid_model_output",
        )

    parsed: list[CognitiveEvidenceDraft] = []
    seen_codes: set[str] = set()
    for item in raw_evidence:
        if not isinstance(item, Mapping) or set(item) != _EVIDENCE_FIELDS:
            raise CognitiveValidationError(
                "evidence item has an invalid structure",
                code="invalid_model_output",
            )
        hypothesis_code = _required_string(item, "hypothesis_code")
        if hypothesis_code not in allowed_hypotheses:
            raise CognitiveValidationError(
                "model invented or exceeded an allowed hypothesis",
                code="invalid_model_output",
            )
        if hypothesis_code in seen_codes:
            raise CognitiveValidationError(
                "an attempt may contribute only one aggregate item per hypothesis",
                code="invalid_model_output",
            )
        direction = _required_string(item, "direction")
        if direction not in {"support", "counter"}:
            raise CognitiveValidationError(
                "invalid evidence direction", code="invalid_model_output"
            )
        evidence_span = _required_string(item, "evidence_span")
        if len(evidence_span) > MAX_EVIDENCE_SPAN_CHARACTERS:
            raise CognitiveValidationError(
                "evidence span is too long", code="invalid_model_output"
            )
        parsed.append(
            CognitiveEvidenceDraft(
                topic_id=topic_id,
                hypothesis_code=hypothesis_code,
                direction=direction,
                strength=_unit_interval(item, "strength"),
                extractor_confidence=_unit_interval(
                    item, "extractor_confidence"
                ),
                evidence_span=evidence_span,
            )
        )
        seen_codes.add(hypothesis_code)
    return tuple(parsed)


def _parse_payload(raw_output: Mapping[str, Any] | str) -> Mapping[str, Any]:
    if isinstance(raw_output, str):
        try:
            parsed = json.loads(raw_output)
        except (TypeError, ValueError) as exc:
            raise CognitiveValidationError(
                "model output is not valid JSON", code="invalid_model_output"
            ) from exc
    else:
        parsed = raw_output
    if not isinstance(parsed, Mapping):
        raise CognitiveValidationError(
            "model output must be an object", code="invalid_model_output"
        )
    return parsed


def _required_string(item: Mapping[str, Any], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CognitiveValidationError(
            f"{field} must be a non-empty string", code="invalid_model_output"
        )
    return value.strip()


def _unit_interval(item: Mapping[str, Any], field: str) -> float:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CognitiveValidationError(
            f"{field} must be numeric", code="invalid_model_output"
        )
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise CognitiveValidationError(
            f"{field} must be between zero and one",
            code="invalid_model_output",
        )
    return number


__all__ = [
    "MAX_EVIDENCE_PER_ATTEMPT",
    "MAX_EVIDENCE_SPAN_CHARACTERS",
    "CognitiveValidationError",
    "resolve_allowed_hypotheses",
    "validate_extraction_output",
]
