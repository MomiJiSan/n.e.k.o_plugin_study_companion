"""Application-service boundary for atomic learning write-back.

The service intentionally owns no SQL, retry, idempotency, or transaction
logic.  Those guarantees remain the responsibility of the supplied store
adapter, so adding this module cannot alter the existing answer-write path.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .contracts import INTERNAL_SCHEMA_VERSION, EvaluatedAttempt


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    """Opaque result from a successful store-owned commit operation."""

    attempt_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    adapter_name: str = ""
    schema_version: int = INTERNAL_SCHEMA_VERSION

    def as_payload(self) -> dict[str, Any]:
        """Provide a copy for a future public-entry adapter."""

        return dict(self.payload)


CommitResult = CommitOutcome | Mapping[str, Any]
MaybeAwaitableCommitResult = CommitResult | Awaitable[CommitResult]


class CommitPort(Protocol):
    """Persistence boundary which owns the all-or-nothing transaction."""

    def commit_evaluated_attempt(
        self, attempt: EvaluatedAttempt
    ) -> MaybeAwaitableCommitResult: ...


class DelegatingCommitPort:
    """Adapt an existing commit callable without changing its transaction."""

    def __init__(
        self, delegate: Callable[[EvaluatedAttempt], MaybeAwaitableCommitResult]
    ) -> None:
        self._delegate = delegate

    def commit_evaluated_attempt(
        self, attempt: EvaluatedAttempt
    ) -> MaybeAwaitableCommitResult:
        return self._delegate(attempt)


class LearningCommitService:
    """Delegate an evaluated attempt to one atomic persistence adapter.

    ``commit`` performs only result-shape normalization.  It does not split a
    write into tables, retry a failed write, or infer idempotency; adapters must
    preserve those existing storage semantics themselves.
    """

    def __init__(self, port: CommitPort) -> None:
        self._port = port

    async def commit(self, attempt: EvaluatedAttempt) -> CommitOutcome:
        result = self._port.commit_evaluated_attempt(attempt)
        if inspect.isawaitable(result):
            result = await result
        return self._normalize_outcome(attempt, result)

    @staticmethod
    def _normalize_outcome(
        attempt: EvaluatedAttempt, result: CommitResult
    ) -> CommitOutcome:
        if isinstance(result, CommitOutcome):
            if result.attempt_id != attempt.attempt_id:
                raise ValueError("commit outcome attempt_id does not match request")
            return result
        if not isinstance(result, Mapping):
            raise TypeError("commit port must return CommitOutcome or a mapping")
        return CommitOutcome(attempt_id=attempt.attempt_id, payload=dict(result))
