from __future__ import annotations

import asyncio
import importlib
import math
import sys
import threading
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# isort: split
from adaptive_learning.cognitive_contracts import (
    CognitiveEvidenceDraft,
    CognitiveExtractionInput,
    CognitiveExtractionOutcome,
)
from adaptive_learning.cognitive_projection import (
    CognitiveProjector,
    project_cognitive_hypothesis,
    project_cognitive_intervention_events,
)

TOPIC = "calculus.chain_rule"
CODE = "omit_inner_derivative"
AS_OF = "2026-09-02T08:00:00Z"
ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


def _draft(
    direction: str = "support",
    *,
    strength: float = 1.0,
    confidence: float = 1.0,
) -> CognitiveEvidenceDraft:
    return CognitiveEvidenceDraft(
        topic_id=TOPIC,
        hypothesis_code=CODE,
        direction=direction,  # type: ignore[arg-type]
        strength=strength,
        extractor_confidence=confidence,
        evidence_span="missing 2x" if direction == "support" else "included 2x",
    )


def _outcome(
    *drafts: CognitiveEvidenceDraft,
    status: str = "success",
    failure_reason: str = "",
) -> CognitiveExtractionOutcome:
    return CognitiveExtractionOutcome(
        status=status,  # type: ignore[arg-type]
        evidence=tuple(drafts),
        failure_reason=failure_reason,
    )


def _input(attempt_id: str, *, intent: str = "practice") -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "topic_id": TOPIC,
        "question": {
            "learning_intent": intent,
        },
        "question_text": "Differentiate sin(x^2)",
        "expected_answer": "2x cos(x^2)",
        "learner_answer": attempt_id,
        "evaluation": {"verdict": "wrong"},
        "submitted_at": f"2026-09-02T00:00:{attempt_id[-1]}0Z",
    }


class _Extractor:
    def __init__(self, outcomes: Mapping[str, CognitiveExtractionOutcome]) -> None:
        self.outcomes = dict(outcomes)
        self.inputs: list[CognitiveExtractionInput] = []

    async def extract(self, extraction_input: CognitiveExtractionInput) -> CognitiveExtractionOutcome:
        self.inputs.append(extraction_input)
        return self.outcomes[extraction_input.learner_answer]


class _CancellingExtractor:
    async def extract(self, _extraction_input: CognitiveExtractionInput) -> CognitiveExtractionOutcome:
        raise asyncio.CancelledError


class _Store:
    def __init__(
        self,
        inputs: Mapping[str, dict[str, Any]],
        *,
        controls: Mapping[tuple[str, str], str] | None = None,
        stale_on_complete: set[str] | None = None,
    ) -> None:
        self.owner_thread = threading.get_ident()
        self.inputs = {key: deepcopy(value) for key, value in inputs.items()}
        self.order = {key: index for index, key in enumerate(self.inputs)}
        self.queue = {
            key: {
                "attempt_id": key,
                "status": "pending",
                "lease_token": "",
                "extractor_version": "cognitive-extractor-v1",
            }
            for key in self.inputs
        }
        self.evidence: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.controls = dict(controls or {})
        self.stale_on_complete = set(stale_on_complete or ())
        self.store_threads: list[int] = []

    def _off_loop(self) -> None:
        current = threading.get_ident()
        self.store_threads.append(current)
        assert current != self.owner_thread

    def claim_cognitive_projections(self, *, limit: int = 1) -> list[dict[str, Any]]:
        self._off_loop()
        claimed: list[dict[str, Any]] = []
        for item in self.queue.values():
            if item["status"] != "pending":
                continue
            item["status"] = "processing"
            item["lease_token"] = f"lease-{item['attempt_id']}"
            claimed.append(deepcopy(item))
            if len(claimed) >= limit:
                break
        return claimed

    def get_cognitive_projection_input(self, attempt_id: str) -> dict[str, Any] | None:
        self._off_loop()
        value = self.inputs.get(attempt_id)
        return deepcopy(value) if value is not None else None

    def complete_cognitive_projection(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        evidence: Sequence[dict[str, Any]] = (),
        snapshots: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        self._off_loop()
        item = self.queue[attempt_id]
        if attempt_id in self.stale_on_complete:
            item["lease_token"] = "replacement-lease"
            raise ValueError("cognitive projection lease is no longer active")
        if item["status"] != "processing" or item["lease_token"] != lease_token:
            raise ValueError("cognitive projection lease is no longer active")
        self.evidence.extend(deepcopy(list(evidence)))
        self.snapshots.extend(deepcopy(list(snapshots)))
        item["status"] = "done"
        item["lease_token"] = ""
        return {"status": "done"}

    def mark_cognitive_projection_failed(self, *, attempt_id: str, lease_token: str, error: str) -> bool:
        self._off_loop()
        item = self.queue[attempt_id]
        if item["status"] != "processing" or item["lease_token"] != lease_token:
            return False
        item["status"] = "failed"
        item["lease_token"] = ""
        item["last_error"] = error
        return True

    def list_cognitive_evidence(
        self,
        *,
        topic_id: str | None = None,
        hypothesis_code: str | None = None,
        extractor_version: str | None = None,
        through_attempt_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self._off_loop()
        rows = [
            row
            for row in self.evidence
            if (topic_id is None or row["topic_id"] == topic_id)
            and (hypothesis_code is None or row["hypothesis_code"] == hypothesis_code)
            and (extractor_version is None or row["extractor_version"] == extractor_version)
            and (through_attempt_id is None or self.order[row["attempt_id"]] <= self.order[through_attempt_id])
        ]
        rows.sort(key=lambda row: self.order[row["attempt_id"]])
        if limit is not None:
            rows = rows[:limit]
        return deepcopy(rows)

    def replace_cognitive_hypothesis_snapshots(
        self,
        *,
        topic_id: str,
        model_version: str,
        snapshots: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        self._off_loop()
        self.snapshots = [
            row for row in self.snapshots if not (row["topic_id"] == topic_id and row["model_version"] == model_version)
        ]
        self.snapshots.extend(deepcopy(list(snapshots)))
        return deepcopy(list(snapshots))

    def list_cognitive_hypothesis_snapshots(
        self,
        *,
        topic_id: str | None = None,
        hypothesis_code: str | None = None,
        model_version: str | None = None,
        latest_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self._off_loop()
        rows = [
            row
            for row in self.snapshots
            if (topic_id is None or row["topic_id"] == topic_id)
            and (hypothesis_code is None or row["hypothesis_code"] == hypothesis_code)
            and (model_version is None or row["model_version"] == model_version)
        ]
        rows.sort(key=lambda row: self.order.get(row["source_attempt_id"], -1))
        if latest_only:
            latest: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows:
                latest[(row["hypothesis_id"], row["model_version"])] = row
            rows = list(latest.values())
        if limit is not None:
            rows = rows[:limit]
        return deepcopy(rows)

    def list_cognitive_user_controls(
        self,
        *,
        topic_id: str | None = None,
        hypothesis_code: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._off_loop()
        action = self.controls.get((str(topic_id), str(hypothesis_code)))
        return [] if action is None else [{"action": action}][:limit]


def _latest(store: _Store) -> dict[str, Any]:
    return max(store.snapshots, key=lambda row: store.order[row["source_attempt_id"]])


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _load_store(monkeypatch: pytest.MonkeyPatch, name: str):
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    mode_manager = ModuleType(f"{name}.mode_manager")
    setattr(mode_manager, "normalize_mode", lambda value: str(value or "companion"))
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{name}.store").StudyStore


def test_one_ordinary_support_is_only_hypothesized_but_two_can_support() -> None:
    store = _Store({"attempt-1": _input("attempt-1"), "attempt-2": _input("attempt-2")})
    extractor = _Extractor({"attempt-1": _outcome(_draft()), "attempt-2": _outcome(_draft())})
    projector = CognitiveProjector(
        store,  # type: ignore[arg-type]
        extractor,  # type: ignore[arg-type]
    )

    first = _run(projector.process_pending(limit=1, as_of=AS_OF))
    assert first.to_dict() == {
        "claimed": 1,
        "completed": 1,
        "failed": 0,
        "failures": [],
    }
    assert _latest(store)["status"] == "hypothesized"
    assert extractor.inputs[0].question == "Differentiate sin(x^2)"
    assert extractor.inputs[0].expected_answer == "2x cos(x^2)"

    second = _run(projector.process_pending(limit=1, as_of=AS_OF))
    assert second.completed == 1
    assert _latest(store)["status"] == "supported"
    assert _latest(store)["support_count"] == 2


def test_one_high_quality_diagnostic_support_can_reach_supported() -> None:
    diagnostic_input = _input("attempt-1", intent="misconception_probe")
    diagnostic_input["question"]["diagnostic_validation_id"] = "validator-v1:probe-a"
    store = _Store({"attempt-1": diagnostic_input})
    projector = CognitiveProjector(
        store,  # type: ignore[arg-type]
        _Extractor({"attempt-1": _outcome(_draft())}),  # type: ignore[arg-type]
    )

    summary = _run(projector.process_pending(as_of=AS_OF))

    assert summary.ok
    assert _latest(store)["status"] == "supported"
    assert _latest(store)["diagnostic_support_count"] == 1
    assert _latest(store)["probability"] > 0.75


def test_reviewed_question_family_is_the_independence_key() -> None:
    first = _input("attempt-1", intent="misconception_probe")
    second = _input("attempt-2", intent="misconception_probe")
    for payload in (first, second):
        payload["question"]["diagnostic_validation_id"] = "validator-v2"
    first["question"]["cognitive_question_family_id"] = (
        "chain.sin-square.compare-steps"
    )
    second["question"]["target_binding"] = {
        "cognitive_question_family_id": "chain.sin-square.compare-steps"
    }
    store = _Store({"attempt-1": first, "attempt-2": second})
    projector = CognitiveProjector(
        store,  # type: ignore[arg-type]
        _Extractor(
            {
                "attempt-1": _outcome(_draft()),
                "attempt-2": _outcome(_draft()),
            }
        ),  # type: ignore[arg-type]
    )

    summary = _run(projector.process_pending(limit=2, as_of=AS_OF))

    assert summary.completed == 2
    assert {item["evidence_family_id"] for item in store.evidence} == {
        "reviewed-family:chain.sin-square.compare-steps:omit_inner_derivative"
    }


def test_empty_evidence_marks_queue_done_without_snapshot() -> None:
    store = _Store({"attempt-1": _input("attempt-1")})
    projector = CognitiveProjector(
        store,  # type: ignore[arg-type]
        _Extractor({"attempt-1": _outcome()}),  # type: ignore[arg-type]
    )

    summary = _run(projector.process_pending(as_of=AS_OF))
    repeated = _run(projector.process_pending(as_of=AS_OF))

    assert summary.completed == 1
    assert repeated.claimed == 0
    assert repeated.completed == 0
    assert store.queue["attempt-1"]["status"] == "done"
    assert store.evidence == []
    assert store.snapshots == []


def test_repair_and_transfer_stop_at_monitored_until_v2_1() -> None:
    rows = [
        _evidence("attempt-1", "support", "practice", 0.6),
        _evidence("attempt-2", "support", "practice", 0.6),
        _evidence("attempt-3", "counter", "misconception_repair", 0.8),
    ]
    provisional = project_cognitive_hypothesis(
        rows,
        model_version="cognitive-v1",
        source_attempt_id="attempt-3",
        computed_at=AS_OF,
    )
    assert provisional["status"] == "provisionally_resolved"

    rows.append(_evidence("attempt-4", "counter", "transfer_check", 0.85))
    monitored = project_cognitive_hypothesis(
        rows,
        model_version="cognitive-v1",
        source_attempt_id="attempt-4",
        computed_at=AS_OF,
    )
    assert monitored["status"] == "monitored"

    rows.append(_evidence("attempt-5", "counter", "retention_check", 1.0))
    retained = project_cognitive_hypothesis(
        rows,
        model_version="cognitive-v1",
        source_attempt_id="attempt-5",
        computed_at=AS_OF,
    )
    assert retained["status"] == "monitored"

    rows.append(_evidence("attempt-6", "support", "practice", 0.6))
    relapsed = project_cognitive_hypothesis(
        rows,
        model_version="cognitive-v1",
        source_attempt_id="attempt-6",
        computed_at=AS_OF,
    )
    assert relapsed["status"] == "supported"
    assert relapsed["relapse_count"] == 1


def test_single_ordinary_counter_cannot_resolve_a_supported_hypothesis() -> None:
    rows = [
        _evidence("attempt-1", "support", "practice", 0.6),
        _evidence("attempt-2", "support", "practice", 0.6),
        _evidence("attempt-3", "counter", "practice", 0.6),
    ]

    snapshot = project_cognitive_hypothesis(
        rows,
        model_version="cognitive-v1",
        source_attempt_id="attempt-3",
        computed_at=AS_OF,
    )

    assert snapshot["status"] == "supported"
    assert snapshot["counter_count"] == 1


def test_probability_uses_signed_strength_confidence_diagnosticity_product() -> None:
    row = _evidence("attempt-1", "support", "practice", 0.6)
    row["strength"] = 0.8
    row["extractor_confidence"] = 0.5

    snapshot = project_cognitive_hypothesis(
        [row],
        model_version="cognitive-v1",
        source_attempt_id="attempt-1",
        computed_at=AS_OF,
    )
    prior_logit = math.log(0.55 / 0.45)
    expected = 1.0 / (1.0 + math.exp(-(prior_logit + 0.8 * 0.5 * 0.6)))

    assert snapshot["probability"] == round(expected, 12)


def test_duplicate_attempts_cannot_satisfy_independent_evidence_gate() -> None:
    duplicate = _evidence("attempt-1", "support", "practice", 0.6)

    with pytest.raises(ValueError, match="independent"):
        project_cognitive_hypothesis(
            [duplicate, duplicate],
            model_version="cognitive-v1",
            source_attempt_id="attempt-1",
            computed_at=AS_OF,
        )


def test_distinct_attempts_in_one_evidence_family_count_only_once() -> None:
    first = _evidence("attempt-1", "support", "practice", 0.6)
    second = _evidence("attempt-2", "support", "practice", 0.6)
    first["evidence_family_id"] = "template:chain-a"
    second["evidence_family_id"] = "template:chain-a"

    snapshot = project_cognitive_hypothesis(
        [first, second],
        model_version="cognitive-v1",
        source_attempt_id="attempt-2",
        computed_at=AS_OF,
    )

    assert snapshot["support_count"] == 1
    assert snapshot["status"] == "hypothesized"


def test_ordinary_support_is_capped_to_one_per_session() -> None:
    first = _evidence("attempt-1", "support", "practice", 0.6)
    second = _evidence("attempt-2", "support", "practice", 0.6)
    first.update(evidence_family_id="question:a", session_id="session-a")
    second.update(evidence_family_id="question:b", session_id="session-a")

    snapshot = project_cognitive_hypothesis(
        [first, second],
        model_version="cognitive-v1",
        source_attempt_id="attempt-2",
        computed_at=AS_OF,
    )

    assert snapshot["support_count"] == 1
    assert snapshot["status"] == "hypothesized"


@pytest.mark.parametrize("action", ["dismiss", "suppress", "delete"])
def test_user_suppression_overrides_model_state(action: str) -> None:
    snapshot = project_cognitive_hypothesis(
        [
            _evidence("attempt-1", "support", "practice", 0.6),
            _evidence("attempt-2", "support", "practice", 0.6),
        ],
        model_version="cognitive-v1",
        source_attempt_id="attempt-2",
        computed_at=AS_OF,
        control_action=action,
    )

    assert snapshot["status"] == "dismissed"


def test_bad_attempt_and_stale_lease_are_isolated_from_other_items() -> None:
    store = _Store(
        {
            "attempt-1": _input("attempt-1"),
            "attempt-2": _input("attempt-2"),
            "attempt-3": _input("attempt-3"),
        },
        stale_on_complete={"attempt-2"},
    )
    extractor = _Extractor(
        {
            "attempt-1": _outcome(status="failed", failure_reason="model_unavailable"),
            "attempt-2": _outcome(_draft()),
            "attempt-3": _outcome(),
        }
    )
    projector = CognitiveProjector(store, extractor)

    summary = _run(projector.process_pending(limit=3, as_of=AS_OF))

    assert summary.claimed == 3
    assert summary.completed == 1
    assert summary.failed == 2
    assert store.queue["attempt-1"]["status"] == "failed"
    assert store.queue["attempt-2"]["lease_token"] == "replacement-lease"
    assert store.queue["attempt-3"]["status"] == "done"


def test_incremental_and_topic_rebuild_latest_state_are_identical() -> None:
    store = _Store({"attempt-1": _input("attempt-1"), "attempt-2": _input("attempt-2")})
    projector = CognitiveProjector(
        store,  # type: ignore[arg-type]
        _Extractor({"attempt-1": _outcome(_draft()), "attempt-2": _outcome(_draft())}),  # type: ignore[arg-type]
    )
    _run(projector.process_pending(limit=2, as_of=AS_OF))
    incremental = deepcopy(_latest(store))

    summary = _run(projector.rebuild_topics([TOPIC, TOPIC], as_of=AS_OF))
    rebuilt = deepcopy(_latest(store))

    assert summary.requested == 1
    assert summary.rebuilt == 1
    assert summary.failed == 0
    assert rebuilt == incremental
    assert len(store.snapshots) == 2


def test_full_rebuild_clears_stale_topic_and_is_idempotent() -> None:
    store = _Store({"attempt-1": _input("attempt-1")})
    projector = CognitiveProjector(
        store,  # type: ignore[arg-type]
        _Extractor({"attempt-1": _outcome(_draft())}),  # type: ignore[arg-type]
    )
    _run(projector.process_pending(as_of=AS_OF))
    store.snapshots.append(_snapshot_record("stale-topic", "attempt-1"))

    first = _run(projector.rebuild_all(as_of=AS_OF))
    after_first = deepcopy(store.snapshots)
    second = _run(projector.rebuild_all(as_of=AS_OF))

    assert first.requested == 2
    assert first.rebuilt == 1
    assert first.skipped == 1
    assert second.requested == 1
    assert second.rebuilt == 1
    assert second.failed == 0
    assert store.snapshots == after_first
    assert all(row["topic_id"] != "stale-topic" for row in store.snapshots)


def test_every_store_call_runs_off_the_event_loop_thread() -> None:
    store = _Store({"attempt-1": _input("attempt-1")})
    projector = CognitiveProjector(
        store,  # type: ignore[arg-type]
        _Extractor({"attempt-1": _outcome()}),  # type: ignore[arg-type]
    )

    _run(projector.process_pending(as_of=AS_OF))
    _run(projector.rebuild_all(as_of=AS_OF))

    assert store.store_threads
    assert all(thread_id != store.owner_thread for thread_id in store.store_threads)


def test_cancellation_propagates_and_leaves_lease_for_expiry_recovery() -> None:
    store = _Store({"attempt-1": _input("attempt-1")})
    projector = CognitiveProjector(store, _CancellingExtractor())

    with pytest.raises(asyncio.CancelledError):
        _run(projector.process_pending(as_of=AS_OF))

    assert store.queue["attempt-1"]["status"] == "processing"
    assert store.queue["attempt-1"]["lease_token"] == "lease-attempt-1"


def test_topic_queue_claim_failure_is_reported_without_raising() -> None:
    class _TopicClaimFailureStore(_Store):
        def claim_cognitive_topic_projections(self, **_kwargs):
            self._off_loop()
            raise RuntimeError("topic queue unavailable")

        def complete_cognitive_topic_projection(self, **_kwargs):
            raise AssertionError("nothing was claimed")

        def mark_cognitive_topic_projection_failed(self, **_kwargs):
            raise AssertionError("nothing was claimed")

    store = _TopicClaimFailureStore({})
    projector = CognitiveProjector(store, _Extractor({}))  # type: ignore[arg-type]

    summary = _run(projector.process_dirty_topics(as_of=AS_OF))

    assert summary.failed == 1
    assert summary.failures[0].error == "topic queue unavailable"


def _intervention_event(
    event_type: str,
    intent: str,
    decision: str,
    *,
    verdict: str = "",
    session_id: str = "session-a",
    repair_strategy: str = "complete_inner_derivative",
) -> dict[str, Any]:
    return {
        "event_id": f"{decision}:{event_type}",
        "event_seq": 1 if event_type == "question_committed" else 2,
        "event_type": event_type,
        "decision_id": decision,
        "hypothesis_target": {
            "hypothesis_id": f"{TOPIC}:{CODE}",
            "topic_id": TOPIC,
            "code": CODE,
            "model_version": "cognitive-v1",
        },
        "learning_intent": intent,
        "repair_strategy": repair_strategy,
        "question_id": f"question-{decision}",
        "attempt_id": f"attempt-{decision}" if event_type == "attempt_committed" else "",
        "diagnostic_validation_id": f"validation-{decision}",
        "evaluation_verdict": verdict,
        "session_id": session_id,
        "created_at": f"2026-09-02T08:00:{len(decision):02d}Z",
    }


def _supported_snapshot() -> dict[str, Any]:
    return project_cognitive_hypothesis(
        [
            _evidence("attempt-1", "support", "practice", 0.6),
            _evidence("attempt-2", "support", "practice", 0.6),
        ],
        model_version="cognitive-v1",
        source_attempt_id="attempt-2",
        computed_at=AS_OF,
    )


def test_probe_requires_authenticated_support_evidence_not_verdict() -> None:
    question = _intervention_event(
        "question_committed", "misconception_probe", "probe"
    )
    attempt = _intervention_event(
        "attempt_committed", "misconception_probe", "probe", verdict="correct"
    )
    unsupported = project_cognitive_intervention_events(
        _supported_snapshot(), [question, attempt]
    )
    support = {
        **_evidence("attempt-probe", "support", "misconception_probe", 1.0),
        "diagnostic_validation_id": "validation-probe",
    }
    confirmed = project_cognitive_intervention_events(
        _supported_snapshot(), [question, attempt], evidence_rows=[support]
    )

    assert unsupported["intervention_stage"] == "idle"
    assert unsupported["last_outcome"] == "not_confirmed"
    assert confirmed["intervention_stage"] == "probing"
    assert confirmed["last_outcome"] == "confirmed"


def test_repair_failures_are_consecutive_only_within_session_and_strategy() -> None:
    events: list[dict[str, Any]] = []
    for decision, session, strategy in (
        ("repair-a", "session-a", "complete_inner_derivative"),
        ("repair-b", "session-a", "complete_inner_derivative"),
        ("repair-c", "session-a", "compare_steps"),
        ("repair-d", "session-b", "compare_steps"),
    ):
        events.extend(
            (
                _intervention_event(
                    "question_committed",
                    "misconception_repair",
                    decision,
                    session_id=session,
                    repair_strategy=strategy,
                ),
                _intervention_event(
                    "attempt_committed",
                    "misconception_repair",
                    decision,
                    verdict="wrong",
                    session_id=session,
                    repair_strategy=strategy,
                ),
            )
        )

    two_failures = project_cognitive_intervention_events(
        _supported_snapshot(), events[:4]
    )
    changed_strategy = project_cognitive_intervention_events(
        _supported_snapshot(), events[:6]
    )
    changed_session = project_cognitive_intervention_events(
        _supported_snapshot(), events
    )

    assert two_failures["consecutive_repair_failures"] == 2
    assert changed_strategy["consecutive_repair_failures"] == 1
    assert changed_session["consecutive_repair_failures"] == 1
    assert changed_session["intervention_stage"] == "remediating"


def test_repair_then_transfer_success_reaches_monitored_and_failure_returns_supported() -> None:
    repair = [
        _intervention_event(
            "question_committed", "misconception_repair", "repair"
        ),
        _intervention_event(
            "attempt_committed",
            "misconception_repair",
            "repair",
            verdict="correct",
        ),
    ]
    transfer_question = _intervention_event(
        "question_committed", "transfer_check", "transfer"
    )
    passed = _intervention_event(
        "attempt_committed", "transfer_check", "transfer", verdict="correct"
    )
    failed = {**passed, "event_id": "transfer:attempt-failed", "evaluation_verdict": "wrong"}

    provisional = project_cognitive_intervention_events(_supported_snapshot(), repair)
    monitored = project_cognitive_intervention_events(
        _supported_snapshot(), [*repair, transfer_question, passed]
    )
    returned = project_cognitive_intervention_events(
        _supported_snapshot(), [*repair, transfer_question, failed]
    )

    assert provisional["status"] == "provisionally_resolved"
    assert provisional["intervention_stage"] == "provisionally_resolved"
    assert monitored["status"] == "monitored"
    assert monitored["intervention_stage"] == "monitored"
    assert returned["status"] == "supported"
    assert returned["intervention_stage"] == "idle"


def test_abandoned_committed_question_never_counts_as_failure_or_attempt() -> None:
    question = _intervention_event(
        "question_committed", "misconception_repair", "abandoned"
    )
    abandoned = {
        **_intervention_event(
            "intervention_abandoned", "misconception_repair", "abandoned"
        ),
        "abandonment_reason": "scope_revision_changed",
    }
    late_attempt = _intervention_event(
        "attempt_committed",
        "misconception_repair",
        "abandoned",
        verdict="wrong",
    )

    result = project_cognitive_intervention_events(
        _supported_snapshot(), [question, abandoned, late_attempt]
    )

    assert result["status"] == "supported"
    assert result["intervention_stage"] == "idle"
    assert result["last_outcome"] == "abandoned"
    assert result["consecutive_repair_failures"] == 0


def test_real_sqlite_incremental_projection_matches_topic_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store = _load_store(monkeypatch, "_cognitive_projection_real_store")
    store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    try:
        store.ensure_topic(topic_id=TOPIC, name="Chain rule")
        for index in (1, 2):
            attempt_id = f"attempt-{index}"
            store.batch_write_answer_data(
                session_id=f"session-{index}",
                mode="companion",
                topic_id=TOPIC,
                question={
                    "question_id": f"question-{index}",
                    "question": "Differentiate sin(x^2)",
                    "answer": "2x cos(x^2)",
                    "question_type": "math_exact",
                    "difficulty": 3,
                },
                user_answer=attempt_id,
                eval_result={"verdict": "wrong", "score": 0},
                response_time_ms=100,
                attempt_id=attempt_id,
                enqueue_cognitive_projection=True,
                cognitive_extractor_version="cognitive-extractor-v1",
            )
        projector = CognitiveProjector(
            store,
            _Extractor({"attempt-1": _outcome(_draft()), "attempt-2": _outcome(_draft())}),
        )

        processed = _run(projector.process_pending(limit=2, as_of=AS_OF))
        incremental = store.list_cognitive_hypothesis_snapshots(
            topic_id=TOPIC,
            model_version="cognitive-v1",
            latest_only=True,
        )[0]
        store._require_conn().execute(
            "DELETE FROM cognitive_hypothesis_current WHERE topic_id = ?",
            (TOPIC,),
        )
        store._require_conn().commit()
        rebuilt_summary = _run(projector.rebuild_topics([TOPIC], as_of=AS_OF))
        rebuilt = store.list_cognitive_hypothesis_snapshots(
            topic_id=TOPIC,
            model_version="cognitive-v1",
            latest_only=True,
        )[0]

        assert processed.completed == 2
        assert incremental["status"] == "supported"
        assert rebuilt_summary.rebuilt == 1
        assert {key: value for key, value in rebuilt.items() if key != "snapshot_id"} == {
            key: value for key, value in incremental.items() if key != "snapshot_id"
        }
        current = store.list_cognitive_hypothesis_current(
            topic_id=TOPIC,
            model_version="cognitive-v1",
        )[0]
        assert current["status"] == rebuilt["status"]
        assert current["support_count"] == rebuilt["support_count"]
    finally:
        store.close()


def _evidence(
    attempt_id: str,
    direction: str,
    source_kind: str,
    diagnosticity: float,
) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "topic_id": TOPIC,
        "hypothesis_code": CODE,
        "direction": direction,
        "strength": 1.0,
        "extractor_confidence": 1.0,
        "diagnosticity": diagnosticity,
        "source_kind": source_kind,
        "extractor_version": "cognitive-extractor-v1",
        "evidence_span": "evidence",
    }


def _snapshot_record(topic_id: str, attempt_id: str) -> dict[str, Any]:
    return {
        "hypothesis_id": f"{topic_id}:{CODE}",
        "topic_id": topic_id,
        "hypothesis_code": CODE,
        "status": "supported",
        "probability": 0.8,
        "support_count": 2,
        "counter_count": 0,
        "diagnostic_support_count": 0,
        "relapse_count": 0,
        "source_attempt_id": attempt_id,
        "model_version": "cognitive-v1",
        "computed_at": AS_OF,
    }
