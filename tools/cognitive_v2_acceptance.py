"""Deterministic, local-only acceptance runner for the Cognitive V2 loop.

The runner exercises production projection, retention, and outbox contracts against
throw-away SQLite databases.  It never opens a user database, calls a model, sleeps,
or writes raw learner content to its reports.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOPIC = "calculus.chain_rule"
HYPOTHESIS_CODE = "omit_inner_derivative"
HYPOTHESIS_ID = f"{TOPIC}:{HYPOTHESIS_CODE}"
VERSION_SET = "cognitive-v2.1-1"
EXTRACTOR_VERSION = "cognitive-extractor-v1"
FIXED_START = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
REPORT_JSON = "cognitive-v2-acceptance.json"
REPORT_MARKDOWN = "cognitive-v2-acceptance.md"
OUTBOX_RETRY_CEILING = 5

_RUNTIME_PACKAGE = "_cognitive_v2_acceptance_runtime"
_PRIVATE_KEYS = frozenset(
    {
        "answer",
        "learner_answer",
        "user_answer",
        "raw_answer",
        "expected_answer",
        "prompt",
        "evidence_span",
        "claim_token",
        "lease_token",
    }
)


class _Logger:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


@dataclass
class FixedClock:
    """Small injectable clock; acceptance scenarios never wait on wall time."""

    current: datetime = FIXED_START

    def now(self) -> datetime:
        return self.current

    def iso(self) -> str:
        return _iso(self.current)

    def advance(self, **delta: float) -> datetime:
        self.current += timedelta(**delta)
        return self.current


class DeterministicExtractor:
    """Identity-only fake extractor used to feed the real projection fold."""

    def support(
        self,
        attempt_id: str,
        *,
        source_kind: str = "practice",
        session_id: str | None = None,
        diagnostic_validation_id: str = "",
    ) -> dict[str, object]:
        return {
            "attempt_id": attempt_id,
            "topic_id": TOPIC,
            "hypothesis_code": HYPOTHESIS_CODE,
            "direction": "support",
            "strength": 1.0,
            "extractor_confidence": 1.0,
            "diagnosticity": 0.8,
            "source_kind": source_kind,
            "extractor_version": EXTRACTOR_VERSION,
            "evidence_span": "deterministic-fixture",
            "session_id": session_id or f"session-{attempt_id}",
            "evidence_family_id": f"family-{attempt_id}",
            "diagnostic_validation_id": diagnostic_validation_id,
        }


class DeterministicEvaluator:
    """Maps reviewed synthetic outcomes to retention dispositions."""

    _DISPOSITIONS = {
        "correct_without_help": "resolved",
        "same_hypothesis": "relapse",
        "ordinary_error": "ordinary_evidence",
        "partial": "reschedule",
        "dont_know": "reschedule",
    }

    def disposition(self, outcome: str) -> str:
        try:
            return self._DISPOSITIONS[outcome]
        except KeyError as exc:
            raise ValueError("unsupported deterministic outcome") from exc


@dataclass
class ScenarioRecorder:
    name: str
    steps: list[dict[str, object]] = field(default_factory=list)

    def check(
        self,
        step: str,
        *,
        expected: object,
        actual: object,
        details: Mapping[str, object] | None = None,
        input_event: Mapping[str, object] | None = None,
        generated_facts: Sequence[Mapping[str, object]] = (),
    ) -> bool:
        passed = actual == expected
        item: dict[str, object] = {
            "step": step,
            "status": "PASS" if passed else "FAIL",
            "input_event": dict(input_event or {"operation": step}),
            "generated_facts": [dict(fact) for fact in generated_facts],
            "expected_state": expected,
            "actual_state": actual,
            "invariant_result": "PASS" if passed else "FAIL",
            "failure_diff": (
                {} if passed else {"expected": expected, "actual": actual}
            ),
        }
        if details:
            item["details"] = dict(details)
        self.steps.append(_sanitize(item))
        return passed

    def result(self) -> dict[str, Any]:
        passed = all(item["status"] == "PASS" for item in self.steps)
        return {
            "name": self.name,
            "status": "PASS" if passed else "FAIL",
            "steps": self.steps,
        }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _sanitize(value: Any) -> Any:
    """Remove private fields from every report boundary."""

    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _PRIVATE_KEYS or normalized.endswith("_token"):
                continue
            clean[str(key)] = _sanitize(child)
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Path):
        return value.name
    return value


def _load_runtime() -> tuple[type[Any], ModuleType, ModuleType]:
    """Load package-relative runtime modules without importing test helpers."""

    package = sys.modules.get(_RUNTIME_PACKAGE)
    if package is None:
        package = ModuleType(_RUNTIME_PACKAGE)
        package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
        sys.modules[_RUNTIME_PACKAGE] = package
        mode_manager = ModuleType(f"{_RUNTIME_PACKAGE}.mode_manager")
        mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
        sys.modules[mode_manager.__name__] = mode_manager
    store_module = importlib.import_module(f"{_RUNTIME_PACKAGE}.store")
    projection_module = importlib.import_module(
        f"{_RUNTIME_PACKAGE}.adaptive_learning.cognitive_projection"
    )
    outbox_module = importlib.import_module(
        f"{_RUNTIME_PACKAGE}.store_cognitive_outbox"
    )
    return store_module.StudyStore, projection_module, outbox_module


def _open_store(Store: type[Any], root: Path, name: str) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / f"{name}.db", root / f"{name}-seed.json", _Logger())
    store.open()
    store.ensure_topic(topic_id=TOPIC, name="Chain rule")
    return store


def _ordinary_answer(
    store: Any,
    attempt_id: str,
    *,
    cognitive: bool = False,
    valid_projection: bool = True,
) -> dict[str, object]:
    return store.batch_write_answer_data(
        session_id="acceptance-session",
        mode="companion",
        topic_id=TOPIC,
        question={
            "question_id": f"question-{attempt_id}",
            "question": "Synthetic chain-rule item",
            "answer": "synthetic-reference",
            "question_type": "math_reasoning",
            "difficulty": 3,
        },
        user_answer="synthetic-response",
        eval_result={
            "verdict": "correct",
            "score": 100,
            "evaluator_type": "deterministic_math",
            "evaluator_version": "acceptance-evaluator-v1",
            "confidence": 1.0,
        },
        response_time_ms=100,
        attempt_id=attempt_id,
        enqueue_cognitive_projection=cognitive,
        cognitive_extractor_version=(
            EXTRACTOR_VERSION if valid_projection else ""
        ),
        cognitive_model_version=VERSION_SET,
    )


def _transfer(store: Any, attempt_id: str, occurred_at: datetime) -> dict[str, object]:
    _ordinary_answer(store, attempt_id)
    return {
        "hypothesis_id": HYPOTHESIS_ID,
        "topic_id": TOPIC,
        "hypothesis_code": HYPOTHESIS_CODE,
        "model_version": VERSION_SET,
        "source_attempt_id": attempt_id,
        "source_event_id": f"transfer-event:{attempt_id}",
        "question_family_id": f"transfer-family:{attempt_id}",
        "evaluation_verdict": "correct",
        "certified": True,
        "used_hint": False,
        "occurred_at": _iso(occurred_at),
    }


def _open_episode(store: Any, attempt_id: str, clock: FixedClock) -> tuple[dict[str, Any], dict[str, object]]:
    transfer = _transfer(store, attempt_id, clock.now())
    created = store.record_certified_transfer_success(transfer)
    return created, transfer


def _claim_retention(
    store: Any,
    created: Mapping[str, Any],
    clock: FixedClock,
    *,
    worker_id: str = "acceptance-worker",
    lease_seconds: int = 300,
) -> dict[str, Any]:
    claims = store.claim_cognitive_obligations(
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        as_of=clock.iso(),
        obligation_types=("retention",),
        obligation_ids=(str(created["obligation"]["obligation_id"]),),
    )
    if len(claims) != 1:
        raise RuntimeError("expected exactly one retention claim")
    return claims[0]


def _event(
    event_type: str,
    intent: str,
    decision: str,
    sequence: int,
    *,
    verdict: str = "",
    validation_id: str = "",
) -> dict[str, object]:
    return {
        "event_id": f"{decision}:{event_type}",
        "event_seq": sequence,
        "event_type": event_type,
        "decision_id": decision,
        "hypothesis_target": {
            "hypothesis_id": HYPOTHESIS_ID,
            "topic_id": TOPIC,
            "code": HYPOTHESIS_CODE,
            "model_version": VERSION_SET,
        },
        "learning_intent": intent,
        "repair_strategy": "complete_inner_derivative",
        "question_id": f"question-{decision}",
        "attempt_id": (
            f"attempt-{decision}" if event_type == "attempt_committed" else ""
        ),
        "diagnostic_validation_id": validation_id,
        "evaluation_verdict": verdict,
        "session_id": "acceptance-session",
        "created_at": f"2026-09-01T08:31:{sequence:02d}Z",
    }


def _monitored_projection(projection: ModuleType) -> tuple[dict[str, Any], dict[str, Any]]:
    extractor = DeterministicExtractor()
    evidence = [
        extractor.support("support-1", session_id="support-session-1"),
        extractor.support("support-2", session_id="support-session-2"),
    ]
    supported = projection.project_cognitive_hypothesis(
        evidence,
        model_version=VERSION_SET,
        source_attempt_id="support-2",
        computed_at=_iso(FIXED_START),
    )
    events = [
        _event(
            "question_committed",
            "misconception_probe",
            "probe",
            1,
            validation_id="validation-probe",
        ),
        _event(
            "attempt_committed",
            "misconception_probe",
            "probe",
            2,
            verdict="wrong",
            validation_id="validation-probe",
        ),
        _event("question_committed", "misconception_repair", "repair", 3),
        _event(
            "attempt_committed",
            "misconception_repair",
            "repair",
            4,
            verdict="correct",
        ),
        _event("question_committed", "transfer_check", "transfer", 5),
        _event(
            "attempt_committed",
            "transfer_check",
            "transfer",
            6,
            verdict="correct",
        ),
    ]
    probe_evidence = extractor.support(
        "attempt-probe",
        source_kind="misconception_probe",
        diagnostic_validation_id="validation-probe",
    )
    monitored = projection.project_cognitive_intervention_events(
        supported, events, evidence_rows=(probe_evidence,)
    )
    return supported, monitored


def _scenario_happy_path(
    root: Path, Store: type[Any], projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("happy_path_resolved")
    clock = FixedClock()
    store = _open_store(Store, root, "happy")
    try:
        supported, monitored = _monitored_projection(projection)
        recorder.check(
            "independent_support_reaches_supported",
            expected="supported",
            actual=supported["status"],
        )
        recorder.check(
            "probe_repair_transfer_reaches_monitored",
            expected="monitored",
            actual=monitored["intervention_stage"],
        )
        created, _ = _open_episode(store, "transfer-happy", clock)
        recorder.check(
            "certified_transfer_opens_episode",
            expected=("open", "pending"),
            actual=(
                created["episode"]["status"],
                created["obligation"]["status"],
            ),
        )
        clock.advance(hours=24)
        claim = _claim_retention(store, created, clock)
        disposition = DeterministicEvaluator().disposition("correct_without_help")
        result = store.apply_cognitive_retention_disposition(
            obligation_id=str(claim["obligation_id"]),
            claim_token=str(claim["claim_token"]),
            worker_id=str(claim["worker_id"]),
            attempt_id="retention-happy",
            disposition=disposition,
            occurred_at=clock.iso(),
            metadata={"evaluator_version": "acceptance-evaluator-v1"},
        )
        recorder.check(
            "delayed_retention_resolves",
            expected=("resolved", "completed"),
            actual=(result["episode"]["status"], result["obligation"]["status"]),
        )
    finally:
        store.close()
    return recorder.result()


def _scenario_relapse(
    root: Path, Store: type[Any], _projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("same_hypothesis_relapse")
    clock = FixedClock()
    store = _open_store(Store, root, "relapse")
    try:
        created, _ = _open_episode(store, "transfer-relapse", clock)
        clock.advance(hours=24, minutes=5)
        claim = _claim_retention(store, created, clock)
        extracted = DeterministicExtractor().support(
            "retention-relapse", source_kind="retention_check"
        )
        same_hypothesis = extracted["hypothesis_code"] == HYPOTHESIS_CODE
        disposition = DeterministicEvaluator().disposition(
            "same_hypothesis" if same_hypothesis else "ordinary_error"
        )
        result = store.apply_cognitive_retention_disposition(
            obligation_id=str(claim["obligation_id"]),
            claim_token=str(claim["claim_token"]),
            worker_id=str(claim["worker_id"]),
            attempt_id="retention-relapse",
            disposition=disposition,
            occurred_at=clock.iso(),
        )
        recorder.check(
            "same_hypothesis_closes_as_relapse",
            expected=("relapsed", 1, "transfer_check"),
            actual=(
                result["episode"]["status"],
                result["episode"]["relapse_count"],
                result["next_obligation"]["obligation_type"],
            ),
        )
    finally:
        store.close()
    return recorder.result()


def _scenario_ordinary_reschedule(
    root: Path, Store: type[Any], _projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("ordinary_error_reschedule")
    clock = FixedClock()
    store = _open_store(Store, root, "reschedule")
    evaluator = DeterministicEvaluator()
    try:
        created, _ = _open_episode(store, "transfer-reschedule", clock)
        clock.advance(hours=24)
        first = _claim_retention(store, created, clock)
        ordinary = store.apply_cognitive_retention_disposition(
            obligation_id=str(first["obligation_id"]),
            claim_token=str(first["claim_token"]),
            worker_id=str(first["worker_id"]),
            attempt_id="retention-ordinary-error",
            disposition=evaluator.disposition("ordinary_error"),
            occurred_at=clock.iso(),
            metadata={
                "cognitive_question_family_id": "family-retention-a",
                "cognitive_independence_group": "independence-a",
            },
        )
        recorder.check(
            "ordinary_error_keeps_episode_open",
            expected=("open", "pending", 0),
            actual=(
                ordinary["episode"]["status"],
                ordinary["obligation"]["status"],
                ordinary["episode"]["relapse_count"],
            ),
        )
        clock.advance(hours=24, minutes=1)
        second = _claim_retention(store, created, clock)
        partial = store.apply_cognitive_retention_disposition(
            obligation_id=str(second["obligation_id"]),
            claim_token=str(second["claim_token"]),
            worker_id=str(second["worker_id"]),
            attempt_id="retention-partial",
            disposition=evaluator.disposition("partial"),
            occurred_at=clock.iso(),
        )
        recorder.check(
            "partial_reschedules_without_relapse",
            expected=("open", "pending", 0),
            actual=(
                partial["episode"]["status"],
                partial["obligation"]["status"],
                partial["episode"]["relapse_count"],
            ),
        )
        clock.advance(hours=24, minutes=1)
        third = _claim_retention(store, created, clock)
        dont_know = store.apply_cognitive_retention_disposition(
            obligation_id=str(third["obligation_id"]),
            claim_token=str(third["claim_token"]),
            worker_id=str(third["worker_id"]),
            attempt_id="retention-dont-know",
            disposition=evaluator.disposition("dont_know"),
            occurred_at=clock.iso(),
        )
        recorder.check(
            "dont_know_reschedules_without_relapse",
            expected=("open", "pending", 0),
            actual=(
                dont_know["episode"]["status"],
                dont_know["obligation"]["status"],
                dont_know["episode"]["relapse_count"],
            ),
        )
    finally:
        store.close()
    return recorder.result()


def _control_case(
    Store: type[Any], root: Path, action: str
) -> tuple[str, str, int]:
    clock = FixedClock()
    store = _open_store(Store, root, f"control-{action}")
    try:
        created, _ = _open_episode(store, f"transfer-control-{action}", clock)
        clock.advance(hours=24)
        if action == "suppress":
            changed = store.record_cognitive_obligation_control(
                topic_id=TOPIC,
                hypothesis_code=HYPOTHESIS_CODE,
                action="suppress",
                occurred_at=clock.iso(),
            )
            blocked = store.claim_cognitive_obligations(
                worker_id="blocked-worker",
                as_of=clock.iso(),
                obligation_types=("retention",),
            )
            store.record_cognitive_obligation_control(
                topic_id=TOPIC,
                hypothesis_code=HYPOTHESIS_CODE,
                action="restore",
                occurred_at=_iso(clock.advance(minutes=1)),
            )
            restored = _claim_retention(store, created, clock)
            statuses = (
                store.list_cognitive_monitoring_episodes()[0]["status"],
                store.list_cognitive_learning_obligations()[0]["status"],
            )
            return statuses[0], statuses[1], len(blocked) + int(bool(restored)) + changed["claims"]
        claim = _claim_retention(store, created, clock)
        changed = store.record_cognitive_obligation_control(
            topic_id=TOPIC,
            hypothesis_code=HYPOTHESIS_CODE,
            action=action,
            occurred_at=_iso(clock.advance(minutes=1)),
        )
        episode = store.list_cognitive_monitoring_episodes()[0]
        obligation = store.list_cognitive_learning_obligations()[0]
        stale_fenced = False
        try:
            store.release_cognitive_obligation_claim(
                obligation_id=str(claim["obligation_id"]),
                claim_token=str(claim["claim_token"]),
                worker_id=str(claim["worker_id"]),
                released_at=clock.iso(),
            )
        except ValueError:
            stale_fenced = True
        return episode["status"], obligation["status"], changed["claims"] + int(stale_fenced)
    finally:
        store.close()


def _scenario_controls(
    root: Path, Store: type[Any], _projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("control_interruptions")
    suppressed = _control_case(Store, root, "suppress")
    recorder.check(
        "suppress_blocks_then_restore_reclaims",
        expected=("open", "claimed", 1),
        actual=suppressed,
    )
    dismissed = _control_case(Store, root, "dismiss")
    recorder.check(
        "dismiss_cancels_and_fences_claim",
        expected=("cancelled", "cancelled", 2),
        actual=dismissed,
    )
    deleted = _control_case(Store, root, "delete")
    recorder.check(
        "delete_cancels_and_fences_claim",
        expected=("cancelled", "cancelled", 2),
        actual=deleted,
    )
    return recorder.result()


def _scenario_outbox_failure(
    root: Path, Store: type[Any], _projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("outbox_failure_isolation")
    store = _open_store(Store, root, "outbox")
    try:
        _ordinary_answer(
            store,
            "outbox-failure",
            cognitive=True,
            valid_projection=False,
        )
        for _ in range(OUTBOX_RETRY_CEILING - 1):
            store.process_cognitive_outbox(limit=1)
        discarded = store.list_cognitive_outbox(status="discarded")
        attempt_preserved = store.get_attempt_fact("outbox-failure") is not None
        recorder.check(
            "failure_reaches_retry_ceiling",
            expected=(1, OUTBOX_RETRY_CEILING, 0),
            actual=(
                len(discarded),
                discarded[0]["retry_count"] if discarded else 0,
                len(store.claim_cognitive_outbox(limit=1)),
            ),
        )
        recorder.check(
            "ordinary_attempt_survives_cognitive_failure",
            expected=True,
            actual=attempt_preserved,
        )
    finally:
        store.close()
    return recorder.result()


def _scenario_restart_rebuild(
    root: Path, Store: type[Any], _projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("restart_and_rebuild")
    clock = FixedClock()
    store = _open_store(Store, root, "restart")
    created, transfer = _open_episode(store, "transfer-restart", clock)
    original_episode = str(created["episode"]["episode_id"])
    original_obligation = str(created["obligation"]["obligation_id"])
    store.close()

    reopened = _open_store(Store, root, "restart")
    try:
        before = (
            reopened.list_cognitive_monitoring_episodes(),
            reopened.list_cognitive_learning_obligations(),
        )
        rebuilt = reopened.rebuild_cognitive_retention_from_transfers([transfer])
        after = (
            reopened.list_cognitive_monitoring_episodes(),
            reopened.list_cognitive_learning_obligations(),
        )
        recorder.check(
            "restart_preserves_episode_and_obligation",
            expected=(1, 1, original_episode, original_obligation),
            actual=(
                len(before[0]),
                len(before[1]),
                str(before[0][0]["episode_id"]),
                str(before[1][0]["obligation_id"]),
            ),
        )
        recorder.check(
            "rebuild_is_idempotent",
            expected=(1, 1, original_episode, original_obligation),
            actual=(
                len(after[0]),
                len(after[1]),
                str(rebuilt[0]["episode"]["episode_id"]),
                str(rebuilt[0]["obligation"]["obligation_id"]),
            ),
        )
    finally:
        reopened.close()
    return recorder.result()


def _scenario_lease_takeover(
    root: Path, Store: type[Any], _projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("lease_takeover_fencing")
    clock = FixedClock()
    store = _open_store(Store, root, "lease")
    try:
        created, _ = _open_episode(store, "transfer-lease", clock)
        clock.advance(hours=24)
        old = _claim_retention(
            store, created, clock, worker_id="old-worker", lease_seconds=30
        )
        clock.advance(seconds=31)
        new = _claim_retention(
            store, created, clock, worker_id="new-worker", lease_seconds=300
        )
        old_fenced = False
        try:
            store.release_cognitive_obligation_claim(
                obligation_id=str(old["obligation_id"]),
                claim_token=str(old["claim_token"]),
                worker_id="old-worker",
                released_at=clock.iso(),
            )
        except ValueError:
            old_fenced = True
        recorder.check(
            "expired_lease_is_taken_over_and_old_worker_fenced",
            expected=("new-worker", True),
            actual=(new["worker_id"], old_fenced),
        )
    finally:
        store.close()
    return recorder.result()


def _normalize_private_comparison(value: Any) -> Any:
    """Normalize volatile storage timestamps for an in-memory A/B comparison."""

    volatile = {
        "created_at",
        "updated_at",
        "submitted_at",
        "evaluated_at",
        "last_review_at",
    }
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_private_comparison(child)
            for key, child in value.items()
            if str(key) not in volatile
        }
    if isinstance(value, (list, tuple)):
        normalized = [_normalize_private_comparison(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    return value


def _protected_snapshot(store: Any, attempt_id: str) -> dict[str, Any]:
    """Read protected domains for comparison; the result never enters reports."""

    return _normalize_private_comparison(
        {
            "attempt": store.get_attempt_fact(attempt_id),
            "mastery": store.get_latest_mastery(TOPIC),
            "fsrs": store.list_fsrs_cards(topic_ids=(TOPIC,)),
            "wrong_questions": store.list_wrong_questions(topic_id=TOPIC),
            "learning_plan": store.get_active_learning_plan(),
        }
    )


def _scenario_off_on_equivalence(
    root: Path, Store: type[Any], _projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("cognitive_off_on_equivalence")
    off = _open_store(Store, root, "off")
    on = _open_store(Store, root, "on")
    try:
        attempt_id = "ab-ordinary-attempt"
        _ordinary_answer(off, attempt_id, cognitive=False)
        _ordinary_answer(on, attempt_id, cognitive=True)
        equivalent = _protected_snapshot(off, attempt_id) == _protected_snapshot(
            on, attempt_id
        )
        cognitive_rows = len(on.list_cognitive_outbox())
        recorder.check(
            "protected_learning_domains_are_equivalent",
            expected=True,
            actual=equivalent,
            details={"domains_compared": 5},
        )
        recorder.check(
            "only_enabled_arm_has_cognitive_work",
            expected=(0, True),
            actual=(len(off.list_cognitive_outbox()), cognitive_rows > 0),
        )
    finally:
        on.close()
        off.close()
    return recorder.result()


def _timeline_evidence(attempt_id: str) -> dict[str, object]:
    return {
        "evidence_id": f"evidence-{attempt_id}",
        "attempt_id": attempt_id,
        "topic_id": TOPIC,
        "hypothesis_code": HYPOTHESIS_CODE,
        "direction": "support",
        "strength": 1.0,
        "extractor_confidence": 1.0,
        "diagnosticity": 0.8,
        "source_kind": "practice",
        "extractor_version": EXTRACTOR_VERSION,
        "evidence_span": "deterministic-fixture",
        "evidence_family_id": f"family-{attempt_id}",
        "session_id": f"session-{attempt_id}",
    }


def _timeline_event(
    event_type: str,
    intent: str,
    decision: str,
    *,
    question_id: str,
    attempt_id: str = "",
    verdict: str = "",
) -> dict[str, object]:
    return {
        "event_id": f"{decision}-{event_type}",
        "event_type": event_type,
        "decision_id": decision,
        "hypothesis_id": HYPOTHESIS_ID,
        "topic_id": TOPIC,
        "hypothesis_code": HYPOTHESIS_CODE,
        "model_version": VERSION_SET,
        "learning_intent": intent,
        "repair_strategy": "complete_inner_derivative",
        "question_id": question_id,
        "attempt_id": attempt_id,
        "session_id": "same-second-session",
        "diagnostic_validation_id": "validation-same-second",
        "evaluation_verdict": verdict,
        "created_at": "2026-09-01T08:30:00.000000Z",
    }


def _timeline_fact(
    root: int, kind: str, payload: Mapping[str, object]
) -> dict[str, object]:
    return {
        "root_fact_seq": root,
        "effective_at": "2026-09-01T08:30:00.000000Z",
        "recorded_at": "2026-09-01T08:30:00.000000Z",
        "fact_order": {
            "evidence": 0,
            "intervention": 1,
            "obligation_satisfaction": 2,
        }[kind],
        "fact_kind": kind,
        "payload": dict(payload),
    }


def _same_second_facts() -> list[dict[str, object]]:
    relapse = _timeline_evidence("retention-relapse")
    relapse["source_kind"] = "retention_check"
    satisfaction = {
        "satisfaction_id": "satisfaction-retention-relapse",
        "obligation_id": "obligation-same-second",
        "episode_id": "episode-same-second",
        "claim_id": "claim-same-second",
        "attempt_id": "retention-relapse",
        "hypothesis_id": HYPOTHESIS_ID,
        "topic_id": TOPIC,
        "hypothesis_code": HYPOTHESIS_CODE,
        "model_version": VERSION_SET,
        "disposition": "relapse",
        "occurred_at": "2026-09-01T08:30:00.000000Z",
    }
    return [
        _timeline_fact(1, "evidence", _timeline_evidence("support-1")),
        _timeline_fact(2, "evidence", _timeline_evidence("support-2")),
        _timeline_fact(
            3,
            "intervention",
            _timeline_event(
                "question_committed",
                "misconception_repair",
                "repair",
                question_id="repair-question",
            ),
        ),
        _timeline_fact(
            4,
            "intervention",
            _timeline_event(
                "attempt_committed",
                "misconception_repair",
                "repair",
                question_id="repair-question",
                attempt_id="repair-attempt",
                verdict="correct",
            ),
        ),
        _timeline_fact(
            5,
            "intervention",
            _timeline_event(
                "question_committed",
                "transfer_check",
                "transfer",
                question_id="transfer-question",
            ),
        ),
        _timeline_fact(
            6,
            "intervention",
            _timeline_event(
                "attempt_committed",
                "transfer_check",
                "transfer",
                question_id="transfer-question",
                attempt_id="transfer-attempt",
                verdict="correct",
            ),
        ),
        _timeline_fact(7, "evidence", relapse),
        _timeline_fact(7, "obligation_satisfaction", satisfaction),
    ]


def _scenario_same_second_out_of_order(
    _root: Path, _Store: type[Any], projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("same_second_out_of_order_facts")
    ordered = _same_second_facts()
    expected = projection.project_cognitive_fact_timeline(
        ordered,
        topic_id=TOPIC,
        model_version=VERSION_SET,
        computed_at="2026-09-03T12:00:00.000000Z",
    )
    rebuilt = projection.project_cognitive_fact_timeline(
        list(reversed(ordered)),
        topic_id=TOPIC,
        model_version=VERSION_SET,
        computed_at="2026-09-03T12:00:00.000000Z",
    )
    final = rebuilt[-1]
    recorder.check(
        "same_second_reverse_delivery_matches_canonical_fold",
        expected=(True, "supported", 1, "relapse"),
        actual=(
            rebuilt == expected,
            final["status"],
            final["relapse_count"],
            final["last_outcome"],
        ),
        input_event={"operation": "reverse_fact_delivery", "fact_count": len(ordered)},
        generated_facts=({"kind": "canonical_projection"},),
    )
    return recorder.result()


class _NeverExtractor:
    async def extract(self, _input: object) -> object:
        raise AssertionError("unknown version must fail before extraction")


def _retention_entities(**obligation_changes: object) -> tuple[dict[str, object], dict[str, object]]:
    episode: dict[str, object] = {
        "episode_id": "episode-boundary",
        "hypothesis_id": HYPOTHESIS_ID,
        "topic_id": TOPIC,
        "hypothesis_code": HYPOTHESIS_CODE,
        "model_version": VERSION_SET,
        "source_attempt_id": "transfer-boundary",
        "source_event_id": "event-transfer-boundary",
        "transfer_question_family_id": "chain.polynomial-power.cross-form-transfer",
        "status": "open",
    }
    obligation: dict[str, object] = {
        "obligation_id": "obligation-boundary",
        "episode_id": "episode-boundary",
        "hypothesis_id": HYPOTHESIS_ID,
        "topic_id": TOPIC,
        "hypothesis_code": HYPOTHESIS_CODE,
        "obligation_type": "retention",
        "status": "pending",
        "not_before": "2026-09-02T08:30:00.000000Z",
        "due_by": "2026-09-04T08:30:00.000000Z",
        "eligibility_until": "2026-09-08T08:30:00.000000Z",
    }
    obligation.update(obligation_changes)
    return episode, obligation


def _scenario_unknown_version(
    root: Path, Store: type[Any], projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("unknown_version_set_fail_closed")
    rejected = False
    try:
        projection.CognitiveProjector(
            object(), _NeverExtractor(), version_set="unknown-version-set"
        )
    except ValueError:
        rejected = True
    retention = importlib.import_module(
        f"{_RUNTIME_PACKAGE}.adaptive_learning.cognitive_retention"
    )
    episode, obligation = _retention_entities()
    proposal = retention.build_retention_action_proposal(
        obligation,
        episode,
        version_set="unknown-version-set",
        projection_current=True,
        as_of=FIXED_START + timedelta(days=2),
    )
    models = importlib.import_module(f"{_RUNTIME_PACKAGE}.models")
    config = models.CognitiveConfig(
        projection_enabled=True,
        read_mode="active",
        intent_policy="on",
        ui_enabled=True,
        retention_enabled=True,
        version_set="unknown-version-set",
    )
    disabled_surfaces = (
        config.projection_enabled,
        config.read_mode,
        config.intent_policy,
        config.ui_enabled,
        config.retention_enabled,
        config.model_version,
    )
    recorder.check(
        "unknown_version_disables_projector_and_retention_candidate",
        expected=(True, None, (False, "off", "off", False, False, "")),
        actual=(rejected, proposal, disabled_surfaces),
        input_event={"operation": "configure_unknown_version"},
    )
    store = _open_store(Store, root, "unknown-version")
    try:
        conn = store._require_conn()
        conn.execute(
            """INSERT INTO cognitive_topic_projection_queue (
                topic_id, model_version, status, requested_generation,
                claimed_generation, projected_generation
            ) VALUES (?, 'unknown-v9', 'pending', 1, 0, 0)""",
            (TOPIC,),
        )
        conn.commit()
        listed = store.list_cognitive_topic_projection_queue(
            model_version="unknown-v9"
        )
        default_claims = store.claim_cognitive_topic_projections()
        explicit_claim_rejected = False
        dirty_rejected = False
        try:
            store.claim_cognitive_topic_projections(model_version="unknown-v9")
        except ValueError:
            explicit_claim_rejected = True
        try:
            store.mark_cognitive_topic_projection_dirty(
                topic_id=TOPIC, model_version="unknown-v9"
            )
        except ValueError:
            dirty_rejected = True
        unchanged = conn.execute(
            """SELECT status, requested_generation
            FROM cognitive_topic_projection_queue
            WHERE topic_id = ? AND model_version = 'unknown-v9'""",
            (TOPIC,),
        ).fetchone()
        recorder.check(
            "unknown_historical_projection_is_read_only_and_never_claimed",
            expected=("unknown-v9", 0, True, True, ("pending", 1)),
            actual=(
                listed[0]["model_version"],
                len(default_claims),
                explicit_claim_rejected,
                dirty_rejected,
                tuple(unchanged),
            ),
            input_event={"operation": "inspect_unknown_projection_row"},
        )
    finally:
        store.close()
    return recorder.result()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _downgrade_to_legacy_fixture(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        objects = connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchall()
        for object_type, name, sql in objects:
            if object_type == "trigger" and (
                str(name).startswith("trg_cognitive_root_")
                or name == "trg_cognitive_control_expiry_validate"
            ):
                connection.execute(f'DROP TRIGGER "{name}"')
            if object_type == "index" and "root_fact_seq" in str(sql):
                connection.execute(f'DROP INDEX "{name}"')
        for table in (
            "cognitive_monitoring_episode_facts",
            "cognitive_obligation_satisfactions",
            "cognitive_obligation_claims",
            "cognitive_learning_obligations",
            "cognitive_monitoring_episodes",
            "cognitive_outbox",
            "cognitive_delete_cutoffs",
            "cognitive_fact_roots",
        ):
            connection.execute(f'DROP TABLE "{table}"')
        for table in (
            "question_instances",
            "attempts",
            "cognitive_evidence",
            "cognitive_user_controls",
            "cognitive_intervention_events",
        ):
            connection.execute(f'ALTER TABLE "{table}" DROP COLUMN root_fact_seq')
        connection.commit()
    finally:
        connection.close()


def _scenario_legacy_copy_migration(
    root: Path, Store: type[Any], _projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("legacy_database_copy_migration")
    fixture_root = root / "fixture"
    fixture = _open_store(Store, fixture_root, "legacy-fixture")
    fixture_path = fixture_root / "legacy-fixture.db"
    _ordinary_answer(fixture, "legacy-attempt")
    fixture.close()
    _downgrade_to_legacy_fixture(fixture_path)
    fixture_hash = _file_hash(fixture_path)

    migrated_path = root / "migration-copy.db"
    shutil.copy2(fixture_path, migrated_path)
    copied = Store(migrated_path, root / "copy-seed.json", _Logger())
    copied.open()
    try:
        attempt_preserved = copied.get_attempt_fact("legacy-attempt") is not None
        tables = {
            str(row["name"])
            for row in copied._require_read_conn()
            .execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            .fetchall()
        }
        attempt_columns = {
            str(row["name"])
            for row in copied._require_read_conn()
            .execute("PRAGMA table_info(attempts)")
            .fetchall()
        }
        required_tables = {
            "cognitive_fact_roots",
            "cognitive_outbox",
            "cognitive_monitoring_episodes",
            "cognitive_learning_obligations",
            "cognitive_obligation_claims",
            "cognitive_obligation_satisfactions",
        }
        integrity = str(
            copied._require_read_conn().execute("PRAGMA integrity_check").fetchone()[0]
        )
        foreign_key_errors = copied._require_read_conn().execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        recorder.check(
            "copied_legacy_fixture_migrates_additively",
            expected=(True, True, True, True, "ok", 0),
            actual=(
                attempt_preserved,
                required_tables <= tables,
                "root_fact_seq" in attempt_columns,
                fixture_hash == _file_hash(fixture_path),
                integrity,
                len(foreign_key_errors),
            ),
            input_event={"operation": "open_copied_legacy_fixture"},
            generated_facts=({"kind": "schema_backfill", "source": "copy"},),
        )
    finally:
        copied.close()
    return recorder.result()


def _scenario_retention_family_rotation(
    root: Path, Store: type[Any], _projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("retention_question_family_rotation")
    retention = importlib.import_module(
        f"{_RUNTIME_PACKAGE}.adaptive_learning.cognitive_retention"
    )
    clock = FixedClock()
    store = _open_store(Store, root, "family-rotation")
    try:
        created, _ = _open_episode(store, "transfer-family-rotation", clock)
        clock.advance(hours=24)
        first_claim = _claim_retention(store, created, clock)
        store.apply_cognitive_retention_disposition(
            obligation_id=str(first_claim["obligation_id"]),
            claim_token=str(first_claim["claim_token"]),
            worker_id=str(first_claim["worker_id"]),
            attempt_id="retention-family-exp",
            disposition="reschedule",
            occurred_at=clock.iso(),
            metadata={
                "cognitive_question_family_id": "chain.exp-affine.retention",
                "cognitive_independence_group": "chain.exponential-affine",
            },
        )
        clock.advance(hours=24, minutes=1)
        episode = store.list_cognitive_monitoring_episodes()[0]
        obligation = store.list_cognitive_learning_obligations()[0]
        proposal = retention.build_retention_action_proposal(
            obligation,
            episode,
            version_set=VERSION_SET,
            projection_current=True,
            as_of=clock.now(),
        )
        recorder.check(
            "stored_history_rotates_to_unused_family",
            expected=(
                ("chain.exp-affine.retention",),
                ("chain.exponential-affine",),
                "chain.sin-affine.retention",
                "chain.trigonometric-affine",
            ),
            actual=(
                obligation["previous_question_family_ids"],
                obligation["previous_independence_groups"],
                getattr(
                    getattr(proposal, "blueprint", None),
                    "question_family_id",
                    "",
                ),
                getattr(
                    getattr(proposal, "blueprint", None),
                    "independence_group",
                    "",
                ),
            ),
            input_event={"operation": "reschedule_after_first_retention_family"},
            generated_facts=({"kind": "obligation_satisfaction"},),
        )

        second_claim = _claim_retention(store, created, clock)
        store.apply_cognitive_retention_disposition(
            obligation_id=str(second_claim["obligation_id"]),
            claim_token=str(second_claim["claim_token"]),
            worker_id=str(second_claim["worker_id"]),
            attempt_id="retention-family-trig",
            disposition="reschedule",
            occurred_at=clock.iso(),
            metadata={
                "cognitive_question_family_id": "chain.sin-affine.retention",
                "cognitive_independence_group": "chain.trigonometric-affine",
            },
        )
        clock.advance(hours=24, minutes=1)
        exhausted_episode = store.list_cognitive_monitoring_episodes()[0]
        exhausted_obligation = store.list_cognitive_learning_obligations()[0]
        exhausted = retention.build_retention_action_proposal(
            exhausted_obligation,
            exhausted_episode,
            version_set=VERSION_SET,
            projection_current=True,
            as_of=clock.now(),
        )
        recorder.check(
            "reviewed_family_exhaustion_fails_closed",
            expected=(
                (
                    "chain.exp-affine.retention",
                    "chain.sin-affine.retention",
                ),
                None,
            ),
            actual=(
                exhausted_obligation["previous_question_family_ids"],
                exhausted,
            ),
            input_event={"operation": "request_candidate_after_all_families_used"},
        )
    finally:
        store.close()
    return recorder.result()


def _retention_validation_input(retention: ModuleType, **changes: object) -> Any:
    blueprint = retention.CHAIN_RULE_RETENTION_BLUEPRINT
    values: dict[str, object] = {
        "episode_id": "episode-window",
        "obligation_id": "obligation-window",
        "hypothesis_code": HYPOTHESIS_CODE,
        "verdict": "correct",
        "used_hint": False,
        "evaluator_type": "deterministic_math",
        "evaluator_version": "acceptance-evaluator-v1",
        "evaluator_confidence": 1.0,
        "answered_at": "2026-09-02T09:00:00.000000Z",
        "not_before": "2026-09-02T08:30:00.000000Z",
        "eligibility_until": "2026-09-08T08:30:00.000000Z",
        "question_family_id": blueprint.question_family_id,
        "transfer_question_family_id": "chain.polynomial-power.cross-form-transfer",
        "independence_group": blueprint.independence_group,
        "blueprint_version": retention.RETENTION_BLUEPRINT_VERSION,
        "validator_version": retention.RETENTION_VALIDATOR_VERSION,
    }
    values.update(changes)
    return retention.RetentionValidationInput(**values)


def _scenario_retention_window_boundaries(
    _root: Path, _Store: type[Any], _projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("retention_early_hint_expired_window")
    retention = importlib.import_module(
        f"{_RUNTIME_PACKAGE}.adaptive_learning.cognitive_retention"
    )
    validator = retention.RetentionValidator()
    early = validator.validate(
        _retention_validation_input(
            retention, answered_at="2026-09-02T08:29:59.000000Z"
        )
    )
    hinted = validator.validate(
        _retention_validation_input(retention, used_hint=True)
    )
    expired = validator.validate(
        _retention_validation_input(
            retention, answered_at="2026-09-08T08:30:00.000001Z"
        )
    )
    recorder.check(
        "early_attempt_is_ordinary_evidence",
        expected=(False, "ordinary_evidence", True),
        actual=(
            early.certified,
            early.disposition,
            "retention_too_early" in early.reasons,
        ),
    )
    recorder.check(
        "hinted_attempt_is_ordinary_evidence",
        expected=(False, "ordinary_evidence", True),
        actual=(
            hinted.certified,
            hinted.disposition,
            "hint_used_or_unknown" in hinted.reasons,
        ),
    )
    recorder.check(
        "expired_attempt_is_ordinary_evidence",
        expected=(False, "ordinary_evidence", True),
        actual=(
            expired.certified,
            expired.disposition,
            "retention_window_expired" in expired.reasons,
        ),
    )
    return recorder.result()


def _scenario_all_features_disabled(
    root: Path, Store: type[Any], _projection: ModuleType, _outbox: ModuleType
) -> dict[str, Any]:
    recorder = ScenarioRecorder("all_cognitive_features_disabled_equivalence")
    models = importlib.import_module(f"{_RUNTIME_PACKAGE}.models")
    baseline_config = models.CognitiveConfig()
    disabled_config = models.CognitiveConfig(
        projection_enabled=False,
        read_mode="off",
        intent_policy="off",
        ui_enabled=False,
        retention_enabled=False,
        version_set=VERSION_SET,
    )
    baseline = _open_store(Store, root, "baseline")
    disabled = _open_store(Store, root, "all-disabled")
    try:
        attempt_id = "ordinary-disabled"
        baseline_result = _ordinary_answer(baseline, attempt_id, cognitive=False)
        disabled_result = _ordinary_answer(disabled, attempt_id, cognitive=False)
        tables = (
            "cognitive_projection_queue",
            "cognitive_extraction_queue",
            "cognitive_evidence",
            "cognitive_monitoring_episodes",
            "cognitive_learning_obligations",
            "cognitive_outbox",
        )
        baseline_counts = tuple(
            int(
                baseline._require_read_conn()
                .execute(f"SELECT COUNT(*) FROM {table}")
                .fetchone()[0]
            )
            for table in tables
        )
        disabled_counts = tuple(
            int(
                disabled._require_read_conn()
                .execute(f"SELECT COUNT(*) FROM {table}")
                .fetchone()[0]
            )
            for table in tables
        )
        flags = (
            disabled_config.projection_enabled,
            disabled_config.read_mode,
            disabled_config.intent_policy,
            disabled_config.ui_enabled,
            disabled_config.retention_enabled,
        )
        recorder.check(
            "default_config_keeps_every_cognitive_surface_off",
            expected=(True, (False, "off", "off", False, False)),
            actual=(baseline_config == disabled_config, flags),
        )
        equivalent = (
            _normalize_private_comparison(baseline_result)
            == _normalize_private_comparison(disabled_result)
            and _protected_snapshot(baseline, attempt_id)
            == _protected_snapshot(disabled, attempt_id)
        )
        recorder.check(
            "ordinary_answer_is_preserved_without_cognitive_writes",
            expected=(True, (0, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0)),
            actual=(equivalent, baseline_counts, disabled_counts),
            input_event={"operation": "commit_ordinary_attempt_with_all_gates_off"},
        )
    finally:
        disabled.close()
        baseline.close()
    return recorder.result()


Scenario = Callable[[Path, type[Any], ModuleType, ModuleType], dict[str, Any]]
SCENARIOS: tuple[Scenario, ...] = (
    _scenario_happy_path,
    _scenario_relapse,
    _scenario_ordinary_reschedule,
    _scenario_controls,
    _scenario_outbox_failure,
    _scenario_restart_rebuild,
    _scenario_lease_takeover,
    _scenario_off_on_equivalence,
    _scenario_same_second_out_of_order,
    _scenario_unknown_version,
    _scenario_legacy_copy_migration,
    _scenario_retention_family_rotation,
    _scenario_retention_window_boundaries,
    _scenario_all_features_disabled,
)


def _failed_scenario(name: str, exc: Exception) -> dict[str, Any]:
    return {
        "name": name,
        "status": "FAIL",
        "steps": [
            {
                "step": "scenario_execution",
                "status": "FAIL",
                "input_event": {"operation": "run_scenario"},
                "generated_facts": [],
                "expected_state": "completed",
                "actual_state": "scenario_error",
                "invariant_result": "FAIL",
                "failure_diff": {
                    "expected": "completed",
                    "actual": "scenario_error",
                },
                "details": {"error_type": type(exc).__name__},
            }
        ],
    }


def build_report(
    scenarios: Sequence[Mapping[str, object]], *, profile: str
) -> dict[str, Any]:
    passed = sum(item.get("status") == "PASS" for item in scenarios)
    failed = len(scenarios) - passed
    return _sanitize(
        {
            "schema_version": 1,
            "profile": profile,
            "environment": {
                "database": "temporary_sqlite_only",
                "clock": _iso(FIXED_START),
                "extractor": "deterministic_fake",
                "evaluator": "deterministic_fake",
                "network": "disabled",
            },
            "scope": {
                "version_set": VERSION_SET,
                "topic": TOPIC,
                "hypothesis_code": HYPOTHESIS_CODE,
                "real_user_evidence": "NOT_EVALUATED",
            },
            "coverage": {
                "projection_fold": "EVALUATED",
                "retention_persistence": "EVALUATED",
                "outbox_failure_isolation": "EVALUATED",
                "retention_candidate_rotation": "EVALUATED",
                "legacy_database_copy_migration": "EVALUATED",
                "planner_and_question_delivery": "PARTIAL",
                "real_model": "NOT_EVALUATED",
            },
            "remaining_scope": [
                {
                    "area": "planner_and_question_delivery",
                    "status": "PARTIAL",
                    "reason": "candidate_rotation_only",
                },
                {
                    "area": "real_model_and_real_user_evidence",
                    "status": "NOT_EVALUATED",
                    "reason": "network_and_user_data_out_of_scope",
                },
            ],
            "summary": {
                "status": "PASS" if failed == 0 else "FAIL",
                "scenario_count": len(scenarios),
                "passed": passed,
                "failed": failed,
            },
            "scenarios": list(scenarios),
        }
    )


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Cognitive V2 acceptance",
        "",
        f"- Status: **{summary['status']}**",
        f"- Scenarios: {summary['passed']} passed, {summary['failed']} failed",
        f"- Fixed clock: `{report['environment']['clock']}`",
        "- Database scope: temporary SQLite databases only",
        "- Real-user evidence: not evaluated",
        "- Planner/question delivery: candidate rotation only",
        "- Legacy migration: copied synthetic fixture evaluated",
        "",
    ]
    for scenario in report["scenarios"]:
        lines.extend((f"## {scenario['name']}: {scenario['status']}", ""))
        lines.extend(
            (
                f"- `{step['step']}` — {step['status']} "
                f"(expected `{json.dumps(step['expected_state'], ensure_ascii=False, sort_keys=True)}`, "
                f"actual `{json.dumps(step['actual_state'], ensure_ascii=False, sort_keys=True)}`)"
            )
            for step in scenario["steps"]
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_reports(report: Mapping[str, Any], report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / REPORT_JSON
    markdown_path = report_dir / REPORT_MARKDOWN
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def run_acceptance(*, report_dir: Path, profile: str = "local") -> dict[str, Any]:
    Store, projection, outbox = _load_runtime()
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="cognitive-v2-acceptance-") as temp:
        temp_root = Path(temp)
        for scenario in SCENARIOS:
            try:
                results.append(scenario(temp_root / scenario.__name__, Store, projection, outbox))
            except Exception as exc:  # A failed scenario must still produce a report.
                results.append(_failed_scenario(scenario.__name__.removeprefix("_scenario_"), exc))
    report = build_report(results, profile=profile)
    write_reports(report, report_dir)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("local", "ci"), default="local")
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("cognitive-acceptance-report"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        report = run_acceptance(report_dir=args.report_dir, profile=args.profile)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"cognitive acceptance tool error: {type(exc).__name__}", file=sys.stderr)
        return 2
    summary = report["summary"]
    print(
        f"Cognitive V2 acceptance: {summary['status']} "
        f"({summary['passed']}/{summary['scenario_count']} scenarios passed)"
    )
    print(f"Reports: {args.report_dir / REPORT_JSON}, {args.report_dir / REPORT_MARKDOWN}")
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
