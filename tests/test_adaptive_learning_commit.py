from __future__ import annotations

import asyncio

import pytest

# isort: split

from adaptive_learning import (
    EvaluatedAttempt,
    EvaluationResult,
    QuestionInstance,
    TopicRef,
)
from adaptive_learning.learning_commit import (
    CommitOutcome,
    DelegatingCommitPort,
    LearningCommitService,
)


def _attempt(attempt_id: str = "attempt-1") -> EvaluatedAttempt:
    topic = TopicRef(id="topic-1", name="Topic")
    question = QuestionInstance(
        question_id="question-1",
        plan_id="plan-1",
        target_topic=topic,
        question_type="short_answer",
        difficulty=2,
        public_payload={"question": "Q"},
    )
    return EvaluatedAttempt(
        attempt_id=attempt_id,
        question=question,
        learner_answer="A",
        evaluation=EvaluationResult(
            verdict="correct",
            score=100,
            final_answer_correct=True,
        ),
    )


def test_service_delegates_sync_write_once_and_preserves_mapping_payload() -> None:
    received: list[EvaluatedAttempt] = []

    class Port:
        def commit_evaluated_attempt(self, attempt: EvaluatedAttempt):
            received.append(attempt)
            return {"qa_record_id": "qa-1", "mastery": {"mastery": 0.8}}

    attempt = _attempt()
    outcome = asyncio.run(LearningCommitService(Port()).commit(attempt))

    assert received == [attempt]
    assert outcome.attempt_id == attempt.attempt_id
    assert outcome.as_payload() == {
        "qa_record_id": "qa-1",
        "mastery": {"mastery": 0.8},
    }


def test_service_delegates_async_port_without_introducing_retry_policy() -> None:
    received: list[EvaluatedAttempt] = []

    class Port:
        async def commit_evaluated_attempt(self, attempt: EvaluatedAttempt):
            received.append(attempt)
            return CommitOutcome(
                attempt_id=attempt.attempt_id,
                payload={"status": "committed"},
                adapter_name="test-store",
            )

    attempt = _attempt()
    outcome = asyncio.run(LearningCommitService(Port()).commit(attempt))

    assert received == [attempt]
    assert outcome.adapter_name == "test-store"
    assert outcome.as_payload() == {"status": "committed"}


def test_delegating_port_preserves_existing_store_callable_inputs_and_result() -> None:
    received: list[EvaluatedAttempt] = []
    expected = {"knowledge_tracking_status": "qa_only"}

    def existing_store_adapter(attempt: EvaluatedAttempt):
        received.append(attempt)
        return expected

    attempt = _attempt()
    outcome = asyncio.run(
        LearningCommitService(DelegatingCommitPort(existing_store_adapter)).commit(attempt)
    )

    assert received == [attempt]
    assert outcome.attempt_id == attempt.attempt_id
    assert outcome.as_payload() == expected


def test_service_propagates_port_errors_unchanged() -> None:
    class Port:
        def commit_evaluated_attempt(self, _attempt: EvaluatedAttempt):
            raise RuntimeError("transaction rolled back")

    with pytest.raises(RuntimeError, match="transaction rolled back"):
        asyncio.run(LearningCommitService(Port()).commit(_attempt()))


def test_service_rejects_invalid_or_mismatched_port_result() -> None:
    class InvalidPort:
        def commit_evaluated_attempt(self, _attempt: EvaluatedAttempt):
            return "not a commit result"

    with pytest.raises(TypeError, match="CommitOutcome or a mapping"):
        asyncio.run(LearningCommitService(InvalidPort()).commit(_attempt()))

    class MismatchedPort:
        def commit_evaluated_attempt(self, _attempt: EvaluatedAttempt):
            return CommitOutcome(attempt_id="other", payload={})

    with pytest.raises(ValueError, match="attempt_id does not match"):
        asyncio.run(LearningCommitService(MismatchedPort()).commit(_attempt()))
