"""Dependency protocols for future adaptive-learning application services."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Mapping, Protocol, Sequence

from .assessment import AssessmentDecision, AssessmentRequest
from .contracts import (
    EvaluatedAttempt,
    MapPage,
    MapQuery,
    QuestionGenerationResult,
    QuestionInstance,
    QuestionPlan,
    TopicRef,
)
from .learning_commit import CommitResult
from .question_factory import (
    QuestionGenerationRequest,
    QuestionValidationResult,
)


class CatalogPort(Protocol):
    """Read-only knowledge catalog access required by planners and map queries."""

    def get_topic(self, topic_id: str) -> Mapping[str, Any] | None: ...

    def list_topics(self, **filters: str) -> Sequence[Mapping[str, Any]]: ...

    def query_map(self, query: MapQuery) -> MapPage: ...


class LearnerStatePort(Protocol):
    """Read-only learner-state queries used to plan the next practice item."""

    def get_mastery(self, topic_ids: Sequence[str]) -> Mapping[str, Mapping[str, Any]]: ...

    def get_due_review_topics(self, topic_ids: Sequence[str]) -> Sequence[TopicRef]: ...

    def get_retry_topics(self, topic_ids: Sequence[str]) -> Sequence[TopicRef]: ...


class StudyStorePort(Protocol):
    """Atomic persistence boundary for a fully evaluated attempt."""

    def commit_evaluated_attempt(self, attempt: EvaluatedAttempt) -> Mapping[str, Any]: ...


class ModelGatewayPort(Protocol):
    """Model boundary used by the question factory and LLM rubric evaluator."""

    def generate_question(self, plan: QuestionPlan) -> QuestionInstance: ...

    def evaluate_answer(
        self,
        *,
        question: QuestionInstance,
        learner_answer: str,
    ) -> Mapping[str, Any]: ...


class TutorModelPort(Protocol):
    """Typed model boundary used by future question and answer adapters."""

    def generate_question(
        self, request: QuestionGenerationRequest
    ) -> QuestionGenerationResult | Awaitable[QuestionGenerationResult]: ...

    def evaluate_answer(
        self, request: AssessmentRequest
    ) -> AssessmentDecision | Awaitable[AssessmentDecision]: ...


class LearningContextPort(Protocol):
    """Build entry-owned generation context without exposing the plugin owner."""

    def build_question_context(self, plan: QuestionPlan) -> Mapping[str, Any]: ...


class QuestionValidationPort(Protocol):
    """Typed structural or semantic question validation boundary."""

    def validate_question(
        self,
        request: QuestionGenerationRequest,
        generation: QuestionGenerationResult,
    ) -> QuestionValidationResult | Awaitable[QuestionValidationResult]: ...


class LearningCommitPort(Protocol):
    """Submit exactly one complete evaluated attempt to an atomic adapter."""

    def commit_evaluated_attempt(
        self, attempt: EvaluatedAttempt
    ) -> CommitResult | Awaitable[CommitResult]: ...
