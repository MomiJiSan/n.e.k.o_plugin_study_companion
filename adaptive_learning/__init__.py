"""Internal contracts and ports for the adaptive-learning application layer.

This package intentionally has no dependency on plugin entry points or SQLite
implementations.  New application services can depend on these stable types
while public entry payloads remain backward compatible.
"""

from .contracts import (
    DEFAULT_ASSESSMENT_POLICY_VERSION,
    DEFAULT_PRACTICE_POLICY_VERSION,
    DEFAULT_QUESTION_POLICY_VERSION,
    INTERNAL_SCHEMA_VERSION,
    EvaluatedAttempt,
    EvaluationResult,
    MapPage,
    MapQuery,
    PracticeSelection,
    QuestionInstance,
    QuestionPlan,
    TopicRef,
)

__all__ = [
    "DEFAULT_ASSESSMENT_POLICY_VERSION",
    "DEFAULT_PRACTICE_POLICY_VERSION",
    "DEFAULT_QUESTION_POLICY_VERSION",
    "INTERNAL_SCHEMA_VERSION",
    "EvaluatedAttempt",
    "EvaluationResult",
    "MapPage",
    "MapQuery",
    "PracticeSelection",
    "QuestionInstance",
    "QuestionPlan",
    "TopicRef",
]
