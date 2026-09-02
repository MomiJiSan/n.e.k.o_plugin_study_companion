"""Versioned, implementation-neutral contracts for adaptive learning.

The types in this module are internal contracts.  They deliberately preserve
room for the current dict-based public entry payloads while separating future
application services from entry and storage implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

INTERNAL_SCHEMA_VERSION = 1
DEFAULT_PRACTICE_POLICY_VERSION = "practice-planner-v1"
DEFAULT_QUESTION_POLICY_VERSION = "question-factory-v1"
DEFAULT_ASSESSMENT_POLICY_VERSION = "assessment-v1"

SelectionReason = Literal[
    "wrong_retry",
    "due_review",
    "weak_topic",
    "recommended",
    "default",
]
LearningIntent = Literal[
    "practice",
    "readiness_probe",
    "misconception_probe",
    "misconception_repair",
    "transfer_check",
    "retention_check",
]
RepairStrategy = Literal[
    "",
    "compare_steps",
    "complete_inner_derivative",
    "structure_classification",
    "minimal_change",
    "cross_form_transfer",
]
QuestionStatus = Literal["planned", "generated", "validated", "failed", "answered"]
EvaluationVerdict = Literal["correct", "partial", "wrong", "dont_know"]


@dataclass(frozen=True, slots=True)
class TopicRef:
    """A stable, compact reference to a catalog topic."""

    id: str
    name: str
    subject: str = ""
    stage: str = ""
    course_family: str = ""
    chapter: str = ""
    unit: str = ""
    depth: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PracticeSelection:
    """The auditable decision to practise one topic next."""

    reason: SelectionReason
    target_topic: TopicRef
    eligible_topic_ids: tuple[str, ...] = ()
    origin_wrong_question_id: str | None = None
    explanation: str = ""
    policy_version: str = DEFAULT_PRACTICE_POLICY_VERSION
    schema_version: int = INTERNAL_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class HypothesisRef:
    """Compact reference to one versioned cognitive hypothesis projection."""

    hypothesis_id: str
    topic_id: str
    code: str
    status: str
    probability: float
    model_version: str
    # Exact projection provenance is optional for legacy/shadow callers.  An
    # active cognitive validator requires a concrete snapshot and generation.
    source_snapshot_id: str = ""
    source_attempt_id: str = ""
    projection_generation: int = 0


@dataclass(frozen=True, slots=True)
class QuestionPlan:
    """Server-owned intent passed from the planner to the question factory."""

    plan_id: str
    selection: PracticeSelection
    difficulty: int
    question_type: str
    learning_objective: str = ""
    misconception_target: str = ""
    prerequisite_context: tuple[TopicRef, ...] = ()
    scope_key: str = ""
    scope_revision: int = 0
    mode: str = "companion"
    source_question_id: str = ""
    target_binding: Mapping[str, Any] = field(default_factory=dict)
    policy_version: str = DEFAULT_PRACTICE_POLICY_VERSION
    schema_version: int = INTERNAL_SCHEMA_VERSION
    # Appended after every V1 field so legacy positional construction remains
    # valid while later policy phases can decorate an already selected topic.
    learning_intent: LearningIntent = "practice"
    hypothesis_target: HypothesisRef | None = None
    repair_strategy: RepairStrategy = ""

    @property
    def target_topic(self) -> TopicRef:
        return self.selection.target_topic


@dataclass(frozen=True, slots=True)
class LearningActionCandidate:
    """Shadow-only proposal for the coach's future obligation merger.

    Cognitive policy may submit one of these candidates but never owns the
    final next-action decision.
    """

    source: str
    topic_id: str
    intent: LearningIntent
    urgency: float
    expected_learning_gain: float
    information_gain: float
    evidence_refs: tuple[str, ...]
    satisfies: tuple[str, ...]
    not_before: str = ""
    expires_at: str = ""


@dataclass(frozen=True, slots=True)
class QuestionInstance:
    """A generated question and its private answer material.

    ``private_payload`` must never be returned by public entry adapters.
    """

    question_id: str
    plan_id: str
    target_topic: TopicRef
    question_type: str
    difficulty: int
    public_payload: Mapping[str, Any]
    private_payload: Mapping[str, Any] = field(default_factory=dict)
    generator: str = ""
    model: str = ""
    prompt_version: str = ""
    generator_metadata: Mapping[str, Any] = field(default_factory=dict)
    mode: str = "companion"
    source_question_id: str = ""
    target_binding: Mapping[str, Any] = field(default_factory=dict)
    scope_key: str = ""
    scope_revision: int = 0
    validation_errors: tuple[str, ...] = ()
    status: QuestionStatus = "planned"
    created_at: str = ""
    policy_version: str = DEFAULT_QUESTION_POLICY_VERSION
    schema_version: int = INTERNAL_SCHEMA_VERSION
    # V2 fields are appended so every pre-cognitive positional constructor
    # remains valid.  A question carries at most one hypothesis by contract.
    learning_intent: LearningIntent = "practice"
    hypothesis_target: HypothesisRef | None = None
    repair_strategy: RepairStrategy = ""
    cognitive_decision_id: str = ""
    cognitive_validator_version: str = ""
    diagnostic_validation_id: str = ""


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """Canonical evaluation output, independent of its evaluator implementation."""

    verdict: EvaluationVerdict
    score: int
    feedback: str = ""
    error_type: str = ""
    final_answer_correct: bool = False
    confidence: float | None = None
    evaluator_type: str = "llm_rubric"
    evaluator_version: str = ""
    evaluator_metadata: Mapping[str, Any] = field(default_factory=dict)
    fallback_reason: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)
    policy_version: str = DEFAULT_ASSESSMENT_POLICY_VERSION
    schema_version: int = INTERNAL_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class QuestionGenerationResult:
    """A typed generator outcome before an application service chooses a retry.

    ``payload`` and ``raw_result`` preserve provider-specific information for
    the outer entry adapter.  They are internal-only and must not be exposed
    as a public question response without explicit filtering.
    """

    question: QuestionInstance | None
    payload: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    diagnostic: str = ""
    raw_result: Any = None
    schema_version: int = INTERNAL_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    """Typed assessment outcome with its internal evaluator payload retained."""

    evaluation: EvaluationResult
    payload: Mapping[str, Any] = field(default_factory=dict)
    deterministic: bool = False
    validation_errors: tuple[str, ...] = ()
    schema_version: int = INTERNAL_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class LearningCommitContext:
    """All non-answer facts required to commit one evaluated attempt.

    Keeping these facts on the attempt prevents a production commit adapter
    from recovering hidden values from an entry closure or plugin owner.
    """

    mode: str = ""
    source_question_id: str = ""
    target_binding: Mapping[str, Any] = field(default_factory=dict)
    scope_key: str = ""
    scope_revision: int = 0
    origin_wrong_question_id: str = ""
    allow_knowledge_update: bool = True
    require_existing_topic: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = INTERNAL_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class EvaluatedAttempt:
    """The immutable input to the atomic learning write-back operation."""

    attempt_id: str
    question: QuestionInstance
    learner_answer: str
    evaluation: EvaluationResult
    session_id: str | None = None
    response_time_ms: int | None = None
    submitted_at: str = ""
    used_hint: bool = False
    commit_context: LearningCommitContext = field(default_factory=LearningCommitContext)
    schema_version: int = INTERNAL_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class MapQuery:
    """A cursor-based request for a scoped knowledge-map page."""

    stage: str = ""
    subject: str = ""
    course_family: str = ""
    chapter: str = ""
    unit: str = ""
    page_size: int = 100
    cursor: str = ""
    include_boundary: bool = True
    schema_version: int = INTERNAL_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class MapPage:
    """A scoped map response with explicit pagination and boundary truncation."""

    nodes: tuple[Mapping[str, Any], ...]
    edges: tuple[Mapping[str, Any], ...]
    scope_total_count: int
    scope_returned_count: int
    has_more: bool
    next_cursor: str | None = None
    boundary_returned_count: int = 0
    boundary_truncated: bool = False
    catalog_revision: str = ""
    schema_version: int = INTERNAL_SCHEMA_VERSION
