"""Provider-neutral contracts for Cognitive Evidence Engine extraction.

The contracts in this module deliberately contain no persistence or coaching
policy.  Extraction produces immutable evidence drafts; a later projection
worker owns persistence, retries, and hypothesis-state updates.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias

COGNITIVE_EXTRACT_OPERATION = "cognitive_evidence_extract"
DEFAULT_COGNITIVE_EXTRACTOR_VERSION = "cognitive-extractor-v1"
DEFAULT_COGNITIVE_MODEL_VERSION = "cognitive-v1"
DEFAULT_COGNITIVE_INPUT_TOKEN_BUDGET = 2_048
DEFAULT_COGNITIVE_OUTPUT_TOKEN_BUDGET = 768
DEFAULT_COGNITIVE_TIMEOUT_SECONDS = 30.0

EvidenceDirection = Literal["support", "counter"]
ExtractionStatus = Literal["success", "failed"]


@dataclass(frozen=True, slots=True)
class CognitiveExtractionInput:
    """Server-verified facts available for one extraction attempt."""

    topic_id: str
    question: str
    expected_answer: str
    learner_answer: str
    evaluation: Mapping[str, Any] = field(default_factory=dict)
    allowed_hypotheses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CognitiveEvidenceDraft:
    """One aggregate evidence contribution for one hypothesis and attempt."""

    topic_id: str
    hypothesis_code: str
    direction: EvidenceDirection
    strength: float
    extractor_confidence: float
    evidence_span: str


@dataclass(frozen=True, slots=True)
class CognitiveModelRequest:
    """A bounded structured request for a provider-neutral model adapter.

    The payload has already been constrained to ``max_input_tokens`` by the
    extractor.  A production adapter may add its own static instructions, but
    must preserve these input/output limits and the timeout when calling the
    shared study-model gateway.
    """

    payload: Mapping[str, Any]
    operation: str = COGNITIVE_EXTRACT_OPERATION
    model_version: str = DEFAULT_COGNITIVE_MODEL_VERSION
    max_input_tokens: int = DEFAULT_COGNITIVE_INPUT_TOKEN_BUDGET
    max_output_tokens: int = DEFAULT_COGNITIVE_OUTPUT_TOKEN_BUDGET
    timeout_seconds: float = DEFAULT_COGNITIVE_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class CognitiveModelResponse:
    """Provider-neutral structured model output."""

    content: Mapping[str, Any] | str
    model: str = ""
    request_id: str = ""
    output_limit_reached: bool = False


@dataclass(frozen=True, slots=True)
class CognitiveExtractionOutcome:
    """Safe extraction result consumed by the asynchronous queue worker."""

    status: ExtractionStatus
    evidence: tuple[CognitiveEvidenceDraft, ...] = ()
    failure_reason: str = ""
    extractor_version: str = DEFAULT_COGNITIVE_EXTRACTOR_VERSION
    model_version: str = DEFAULT_COGNITIVE_MODEL_VERSION

    @property
    def succeeded(self) -> bool:
        return self.status == "success"


MaybeAwaitableModelResponse: TypeAlias = (
    CognitiveModelResponse | Awaitable[CognitiveModelResponse]
)


class CognitiveModelGatewayPort(Protocol):
    """Adapter seam over the existing provider-neutral study-model gateway."""

    def complete_structured(
        self, request: CognitiveModelRequest
    ) -> MaybeAwaitableModelResponse: ...


class CognitiveTokenBudgetPort(Protocol):
    """Host tokenization functions used before model-bound strings are built."""

    def count_tokens(self, text: str) -> int: ...

    def truncate_to_tokens(self, text: str, limit: int) -> str: ...


TokenCounter: TypeAlias = Callable[[str], int]
TokenTruncator: TypeAlias = Callable[[str, int], str]


__all__ = [
    "COGNITIVE_EXTRACT_OPERATION",
    "DEFAULT_COGNITIVE_EXTRACTOR_VERSION",
    "DEFAULT_COGNITIVE_INPUT_TOKEN_BUDGET",
    "DEFAULT_COGNITIVE_MODEL_VERSION",
    "DEFAULT_COGNITIVE_OUTPUT_TOKEN_BUDGET",
    "DEFAULT_COGNITIVE_TIMEOUT_SECONDS",
    "CognitiveEvidenceDraft",
    "CognitiveExtractionInput",
    "CognitiveExtractionOutcome",
    "CognitiveModelGatewayPort",
    "CognitiveModelRequest",
    "CognitiveModelResponse",
    "CognitiveTokenBudgetPort",
    "EvidenceDirection",
    "ExtractionStatus",
    "TokenCounter",
    "TokenTruncator",
]
