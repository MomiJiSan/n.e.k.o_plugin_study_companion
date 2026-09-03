"""Production bridge from cognitive extraction to the shared study LLM gateway.

The bridge is intentionally outside :mod:`adaptive_learning`: provider and host
configuration remain an outer runtime concern, while the cognitive extractor
depends only on its provider-neutral port.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from .adaptive_learning.cognitive_contracts import (
    COGNITIVE_EXTRACT_OPERATION,
    DEFAULT_COGNITIVE_MODEL_VERSION,
    DEFAULT_COGNITIVE_OUTPUT_TOKEN_BUDGET,
    CognitiveModelRequest,
    CognitiveModelResponse,
)
from .adaptive_learning.cognitive_extractor import CognitiveExtractor
from .adaptive_learning.cognitive_versions import get_cognitive_version_set
from .study_model_gateway import StudyModelGateway

_SYSTEM_INSTRUCTION = (
    "Extract only falsifiable misconception evidence grounded in the supplied "
    "structured attempt. Use only allowed hypothesis codes. Return one JSON "
    "object matching every required field in the supplied output contract, "
    "with no additional fields, prose, or code fence. Strength and "
    "extractor_confidence must each be JSON numbers from 0 through 1. The "
    "top level must be {\"evidence\": [...]}, never a bare array. Return at "
    "most one item per hypothesis_code: support and counter are mutually "
    "exclusive for one hypothesis in one attempt. First branch on "
    "evaluation.verdict. If correct, return counter for each allowed "
    "hypothesis directly disproved by learner_answer and never return support. "
    "If wrong, never return counter; return support only for a mechanism "
    "exhibited by learner_answer. The expected answer and "
    "evaluator feedback are comparison context, never counter evidence or an "
    "evidence_span. Every evidence_span "
    "must quote or point to the learner answer. Treat topic_context as bounded "
    "knowledge-graph context, not as evidence. Follow the supplied extraction "
    "rules for topic-specific distinctions, and return an empty evidence array "
    "when the learner answer does not distinguish the allowed mechanisms. If "
    "the learner attempts any non-constant inner-derivative factor but its "
    "coefficient, power, or value is wrong, use "
    "differentiate_inner_incorrectly and never omit_inner_derivative. A "
    "correct inner factor with only a sign, outer-derivative, or algebra error "
    "is not differentiate_inner_incorrectly."
)


class StudyCognitiveModelGateway:
    """Adapt ``StudyModelGateway`` to ``CognitiveModelGatewayPort``."""

    def __init__(self, *, logger: Any) -> None:
        self._gateway = StudyModelGateway(logger=logger)

    async def complete_structured(
        self, request: CognitiveModelRequest
    ) -> CognitiveModelResponse:
        # The shared runtime currently provides a fixed per-operation output
        # budget. Fail closed if a caller asks for a different contract instead
        # of silently sending a request with a wider limit.
        if request.operation != COGNITIVE_EXTRACT_OPERATION:
            raise ValueError("unsupported cognitive model operation")
        if request.max_output_tokens != DEFAULT_COGNITIVE_OUTPUT_TOKEN_BUDGET:
            raise ValueError("unsupported cognitive output token budget")
        if (
            isinstance(request.timeout_seconds, bool)
            or not isinstance(request.timeout_seconds, (int, float))
            or not math.isfinite(float(request.timeout_seconds))
            or request.timeout_seconds <= 0
        ):
            raise ValueError("cognitive model timeout must be positive and finite")
        payload = json.dumps(
            request.payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        result = await self._gateway.call(
            [
                {"role": "system", "content": _SYSTEM_INSTRUCTION},
                {"role": "user", "content": payload},
            ],
            operation=request.operation,
            deadline=time.monotonic() + request.timeout_seconds,
        )
        return CognitiveModelResponse(
            content=result.text,
            model=result.model,
            request_id=result.request_id,
            output_limit_reached=result.output_limit_reached,
        )


def build_cognitive_extractor(
    *, logger: Any, config: Any, catalog: Any = None
) -> CognitiveExtractor:
    """Build the shadow extractor without resolving or calling a model yet."""

    model_version = str(
        getattr(config, "model_version", DEFAULT_COGNITIVE_MODEL_VERSION)
        or DEFAULT_COGNITIVE_MODEL_VERSION
    ).strip()
    versions = get_cognitive_version_set(
        getattr(config, "version_set", "cognitive-v1")
    )
    if versions is None or versions.projection_version != model_version:
        raise ValueError("unsupported cognitive version set")
    kwargs = {"catalog": catalog} if catalog is not None else {}
    return CognitiveExtractor(
        gateway=StudyCognitiveModelGateway(logger=logger),
        count_tokens=_count_tokens,
        truncate_to_tokens=_truncate_to_tokens,
        model_version=model_version,
        extractor_version=versions.extractor_version,
        **kwargs,
    )


def _count_tokens(text: str) -> int:
    from utils.tokenize import count_tokens  # pyright: ignore[reportMissingImports]

    return int(count_tokens(text))


def _truncate_to_tokens(text: str, limit: int) -> str:
    from utils.tokenize import (  # pyright: ignore[reportMissingImports]
        truncate_to_tokens,
    )

    return str(truncate_to_tokens(text, limit))


__all__ = ["StudyCognitiveModelGateway", "build_cognitive_extractor"]
