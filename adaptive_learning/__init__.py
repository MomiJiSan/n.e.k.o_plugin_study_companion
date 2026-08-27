"""Internal contracts and ports for the adaptive-learning application layer.

This package intentionally has no dependency on plugin entry points or SQLite
implementations.  New application services can depend on these stable types
while public entry payloads remain backward compatible.
"""

from .answer_application import AnswerAssessmentService, AssessmentValidationResult
from .contracts import (
    DEFAULT_ASSESSMENT_POLICY_VERSION,
    DEFAULT_PRACTICE_POLICY_VERSION,
    DEFAULT_QUESTION_POLICY_VERSION,
    INTERNAL_SCHEMA_VERSION,
    AssessmentResult,
    EvaluatedAttempt,
    EvaluationResult,
    LearningCommitContext,
    MapPage,
    MapQuery,
    PracticeSelection,
    QuestionGenerationResult,
    QuestionInstance,
    QuestionPlan,
    TopicRef,
)
from .learning_commit import CommitOutcome, LearningCommitService
from .production_adapters import StudyTrackerCommitAdapter
from .question_application import QuestionApplicationService, QuestionGenerationFailure

__all__ = [
    "DEFAULT_ASSESSMENT_POLICY_VERSION",
    "DEFAULT_PRACTICE_POLICY_VERSION",
    "DEFAULT_QUESTION_POLICY_VERSION",
    "INTERNAL_SCHEMA_VERSION",
    "AssessmentResult",
    "EvaluatedAttempt",
    "EvaluationResult",
    "LearningCommitContext",
    "MapPage",
    "MapQuery",
    "PracticeSelection",
    "QuestionInstance",
    "QuestionGenerationResult",
    "QuestionPlan",
    "TopicRef",
    "AnswerAssessmentService",
    "AssessmentValidationResult",
    "CommitOutcome",
    "LearningCommitService",
    "QuestionApplicationService",
    "QuestionGenerationFailure",
    "StudyTrackerCommitAdapter",
]
