"""Production-shaped adapters with no dependency on the plugin owner object."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable
from typing import Any, Protocol

from .contracts import EvaluatedAttempt
from .learning_commit import CommitResult


class TrackerAnswerPort(Protocol):
    """The narrow subset of ``KnowledgeTracker`` used for answer write-back."""

    def on_answer(self, **kwargs: Any) -> CommitResult | Awaitable[CommitResult]: ...


class StudyTrackerCommitAdapter:
    """Map one typed attempt to one existing tracker-owned atomic write call."""

    def __init__(self, tracker: TrackerAnswerPort) -> None:
        self._tracker = tracker

    async def commit_evaluated_attempt(self, attempt: EvaluatedAttempt) -> CommitResult:
        kwargs = self.to_tracker_kwargs(attempt)
        # ``KnowledgeTracker.on_answer`` owns the existing SQLite transaction
        # and is synchronous.  Keep it in a worker so cancellation still
        # drains an already-started atomic write in the entry layer.
        result = await asyncio.to_thread(self._tracker.on_answer, **kwargs)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def to_tracker_kwargs(attempt: EvaluatedAttempt) -> dict[str, Any]:
        """Create all tracker inputs from ``attempt`` without ambient state."""

        question = dict(attempt.question.public_payload)
        question.update(dict(attempt.question.private_payload))
        context = attempt.commit_context
        target_binding = dict(attempt.question.target_binding)
        target_binding.update(dict(context.target_binding))
        if target_binding:
            question["target_binding"] = target_binding
        source_question_id = context.source_question_id or attempt.question.source_question_id
        if source_question_id:
            question["source_question_id"] = source_question_id
        if context.scope_key:
            question["scope_key"] = context.scope_key
            question["scope_revision"] = context.scope_revision
        elif attempt.question.scope_key:
            question["scope_key"] = attempt.question.scope_key
            question["scope_revision"] = attempt.question.scope_revision
        evaluation = dict(attempt.evaluation.details)
        # Entry adapters place the exact canonical evaluator payload in
        # ``details``.  Do not synthesize absent fields here: persistence must
        # retain its historical input shape as well as transaction semantics.
        for key, value in (
            ("verdict", attempt.evaluation.verdict),
            ("score", attempt.evaluation.score),
            ("feedback", attempt.evaluation.feedback),
            ("error_type", attempt.evaluation.error_type),
            ("final_answer_correct", attempt.evaluation.final_answer_correct),
        ):
            if key in evaluation:
                evaluation[key] = value
        return {
            "topic_id": attempt.question.target_topic.id,
            "question": question,
            "user_answer": attempt.learner_answer,
            "eval_result": evaluation,
            "mode": context.mode or attempt.question.mode,
            "session_id": attempt.session_id or "default",
            "response_time_ms": attempt.response_time_ms,
            "used_hint": attempt.used_hint,
            "allow_knowledge_update": context.allow_knowledge_update,
            "require_existing_topic": context.require_existing_topic,
            "origin_wrong_question_id": context.origin_wrong_question_id,
            "attempt_id": attempt.attempt_id,
        }
