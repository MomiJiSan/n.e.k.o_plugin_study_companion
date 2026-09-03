"""Asynchronous, fail-closed cognitive evidence extraction."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Mapping
from typing import Any

from .cognitive_catalog import COGNITIVE_CATALOG_V1, CognitiveCatalog
from .cognitive_contracts import (
    COGNITIVE_EXTRACT_OPERATION,
    DEFAULT_COGNITIVE_EXTRACTOR_VERSION,
    DEFAULT_COGNITIVE_INPUT_TOKEN_BUDGET,
    DEFAULT_COGNITIVE_MODEL_VERSION,
    DEFAULT_COGNITIVE_OUTPUT_TOKEN_BUDGET,
    DEFAULT_COGNITIVE_TIMEOUT_SECONDS,
    CognitiveExtractionInput,
    CognitiveExtractionOutcome,
    CognitiveModelGatewayPort,
    CognitiveModelRequest,
    CognitiveModelResponse,
    TokenCounter,
    TokenTruncator,
)
from .cognitive_validation import (
    CognitiveValidationError,
    resolve_allowed_hypotheses,
    validate_extraction_output,
)

_EVALUATION_PROMPT_FIELDS = (
    "verdict",
    "score",
    "feedback",
    "error_type",
    "missing_points",
    "misconceptions",
    "step_feedback",
)


class CognitiveExtractor:
    """Extract bounded evidence without entering the synchronous answer path."""

    def __init__(
        self,
        *,
        gateway: CognitiveModelGatewayPort,
        count_tokens: TokenCounter,
        truncate_to_tokens: TokenTruncator,
        catalog: CognitiveCatalog = COGNITIVE_CATALOG_V1,
        extractor_version: str = DEFAULT_COGNITIVE_EXTRACTOR_VERSION,
        model_version: str = DEFAULT_COGNITIVE_MODEL_VERSION,
        max_input_tokens: int = DEFAULT_COGNITIVE_INPUT_TOKEN_BUDGET,
        max_output_tokens: int = DEFAULT_COGNITIVE_OUTPUT_TOKEN_BUDGET,
        timeout_seconds: float = DEFAULT_COGNITIVE_TIMEOUT_SECONDS,
    ) -> None:
        if max_input_tokens <= 0 or max_output_tokens <= 0:
            raise ValueError("cognitive token budgets must be positive")
        if timeout_seconds <= 0:
            raise ValueError("cognitive timeout must be positive")
        if not str(extractor_version or "").strip():
            raise ValueError("cognitive extractor version is required")
        if not str(model_version or "").strip():
            raise ValueError("cognitive model version is required")
        self._gateway = gateway
        self._count_tokens = count_tokens
        self._truncate_to_tokens = truncate_to_tokens
        self._catalog = catalog
        self._extractor_version = str(extractor_version or "").strip()
        self._model_version = str(model_version or "").strip()
        self._max_input_tokens = int(max_input_tokens)
        self._max_output_tokens = int(max_output_tokens)
        self._timeout_seconds = float(timeout_seconds)

    async def extract(
        self, extraction_input: CognitiveExtractionInput
    ) -> CognitiveExtractionOutcome:
        """Return evidence, an empty successful result, or a safe failure."""

        try:
            allowed = resolve_allowed_hypotheses(
                extraction_input, self._catalog
            )
            payload = self._build_bounded_payload(extraction_input, allowed)
        except CognitiveValidationError as exc:
            return self._failure(exc.code)

        request = CognitiveModelRequest(
            payload=payload,
            operation=COGNITIVE_EXTRACT_OPERATION,
            model_version=self._model_version,
            max_input_tokens=self._max_input_tokens,
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
        )
        try:
            pending = self._gateway.complete_structured(request)
            if inspect.isawaitable(pending):
                response = await asyncio.wait_for(
                    pending, timeout=self._timeout_seconds
                )
            else:
                response = pending
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            return self._failure("timeout")
        except Exception:
            return self._failure("model_unavailable")

        if not isinstance(response, CognitiveModelResponse):
            return self._failure("invalid_model_response")
        if response.output_limit_reached:
            return self._failure("output_truncated")
        try:
            evidence = validate_extraction_output(
                response.content,
                topic_id=extraction_input.topic_id.strip(),
                allowed_hypotheses=allowed,
            )
        except CognitiveValidationError as exc:
            return self._failure(exc.code)
        return CognitiveExtractionOutcome(
            status="success",
            evidence=evidence,
            extractor_version=self._extractor_version,
            model_version=self._model_version,
        )

    def _build_bounded_payload(
        self,
        extraction_input: CognitiveExtractionInput,
        allowed: tuple[str, ...],
    ) -> Mapping[str, Any]:
        hypotheses = [
            item.to_model_payload()
            for item in self._catalog.hypotheses(
                extraction_input.topic_id, allowed
            )
        ]
        evaluation = {
            field: _safe_prompt_value(extraction_input.evaluation[field])
            for field in _EVALUATION_PROMPT_FIELDS
            if field in extraction_input.evaluation
        }
        topic_context = _safe_prompt_value(extraction_input.topic_context)
        if "omit_inner_derivative" in allowed:
            extraction_rules = [
                "One item per allowed code; support/counter are exclusive.",
                "Correct verdict: counter disproved codes, never support. Wrong verdict: support shown mechanisms, never counter.",
                "Inner factor absent => omit; attempted and wrong => differentiate_inner_incorrectly; correct factor plus another error => neither.",
                "Product-rule structure on a composition => confuse_product_and_chain; otherwise return [] if insufficient.",
            ]
        else:
            extraction_rules = [
                "One item per allowed code; support/counter are exclusive.",
                "Correct verdict: counter disproved codes, never support. Wrong verdict: support shown mechanisms, never counter.",
                "Use topic_context only to interpret the concept and reviewed graph relationships; it is not answer evidence.",
                "Return [] unless the learner answer distinguishes an allowed mechanism.",
            ]
        raw_fields = {
            "question": str(extraction_input.question or ""),
            "expected_answer": str(extraction_input.expected_answer or ""),
            "learner_answer": str(extraction_input.learner_answer or ""),
        }

        def candidate(per_field_limit: int) -> dict[str, Any]:
            payload = {
                "topic_id": extraction_input.topic_id.strip(),
                "question": self._truncate(raw_fields["question"], per_field_limit),
                "expected_answer": self._truncate(
                    raw_fields["expected_answer"], per_field_limit
                ),
                "learner_answer": self._truncate(
                    raw_fields["learner_answer"], per_field_limit
                ),
                "evaluation": _truncate_prompt_value(
                    evaluation,
                    per_string_limit=per_field_limit,
                    truncate=self._truncate,
                ),
                "allowed_hypotheses": hypotheses,
                "extraction_rules": extraction_rules,
                "output_contract": {
                    "top_level": "object with only an evidence array",
                    "max_evidence": 3,
                    "unique_hypothesis_codes": True,
                    "directions": ["support", "counter"],
                    "empty_evidence_allowed": True,
                    "required_item_fields": [
                        "hypothesis_code",
                        "direction",
                        "strength",
                        "extractor_confidence",
                        "evidence_span",
                    ],
                    "no_other_fields": True,
                    "strength_and_confidence": "JSON numbers in [0,1]",
                    "evidence_span": "non-empty learner_answer excerpt",
                },
            }
            if topic_context:
                payload["topic_context"] = _truncate_prompt_value(
                    topic_context,
                    per_string_limit=per_field_limit,
                    truncate=self._truncate,
                )
            return payload

        # Binary-search the largest equal share for each learner-controlled
        # field.  The complete serialized payload is checked on every pass.
        low = 0
        high = self._max_input_tokens
        best: dict[str, Any] | None = None
        while low <= high:
            middle = (low + high) // 2
            current = candidate(middle)
            if self._payload_tokens(current) <= self._max_input_tokens:
                best = current
                low = middle + 1
            else:
                high = middle - 1
        if best is None:
            raise CognitiveValidationError(
                "required cognitive context exceeds input token budget",
                code="input_too_large",
            )
        return best

    def _truncate(self, value: str, limit: int) -> str:
        if limit <= 0 or not value:
            return ""
        if self._count(value) <= limit:
            return value
        try:
            truncated = str(self._truncate_to_tokens(value, limit) or "")
        except Exception as exc:
            raise CognitiveValidationError(
                "token truncator failed", code="input_budget_unavailable"
            ) from exc
        # A faulty or optimistic truncator must never bypass the final budget.
        while truncated and self._count(truncated) > limit:
            truncated = truncated[:-1]
        return truncated

    def _payload_tokens(self, payload: Mapping[str, Any]) -> int:
        return self._count(_render_json(payload))

    def _count(self, value: str) -> int:
        try:
            return max(0, int(self._count_tokens(value)))
        except Exception as exc:
            raise CognitiveValidationError(
                "token counter failed", code="input_budget_unavailable"
            ) from exc

    def _failure(self, reason: str) -> CognitiveExtractionOutcome:
        return CognitiveExtractionOutcome(
            status="failed",
            evidence=(),
            failure_reason=reason,
            extractor_version=self._extractor_version,
            model_version=self._model_version,
        )


def _safe_prompt_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return "...[maximum depth reached]"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _safe_prompt_value(item, depth=depth + 1)
            for key, item in list(value.items())[:32]
        }
    if isinstance(value, (list, tuple)):
        return [
            _safe_prompt_value(item, depth=depth + 1)
            for item in value[:32]
        ]
    return str(value)


def _truncate_prompt_value(
    value: Any,
    *,
    per_string_limit: int,
    truncate: TokenTruncator,
) -> Any:
    if isinstance(value, str):
        return truncate(value, per_string_limit)
    if isinstance(value, Mapping):
        return {
            str(key): _truncate_prompt_value(
                item,
                per_string_limit=per_string_limit,
                truncate=truncate,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _truncate_prompt_value(
                item,
                per_string_limit=per_string_limit,
                truncate=truncate,
            )
            for item in value
        ]
    return value


def _render_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = ["CognitiveExtractor"]
