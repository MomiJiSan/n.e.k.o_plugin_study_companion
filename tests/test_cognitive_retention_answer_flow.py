from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "calculus.chain_rule"
HYPOTHESIS = "calculus.chain_rule:omit_inner_derivative"
HYPOTHESIS_CODE = "omit_inner_derivative"
MODEL = "cognitive-v2.1-1"
EXTRACTOR = "cognitive-extractor-v1"
STRATEGY = "independent_delayed_retention"
BLUEPRINT = "chain.omit-inner.retention-exp-affine.v1"
QUESTION_FAMILY = "chain.exp-affine.retention"
INDEPENDENCE_GROUP = "chain.exponential-affine"
BLUEPRINT_VERSION = "cognitive-retention-blueprints-v1"
VALIDATOR_VERSION = "cognitive-retention-validator-v1"
QUESTION_VALIDATOR_VERSION = "cognitive-question-validator-v2.1-1"
TRANSFER_BLUEPRINT = "chain.omit-inner.cross-form-transfer.v1"
TRANSFER_FAMILY = "chain.polynomial-power.cross-form-transfer"


class _UnusedExtractor:
    async def extract(self, _input: object) -> object:
        raise AssertionError("answer commit must not synchronously extract")


class _TargetExtractor:
    def __init__(self, contracts: Any) -> None:
        self._contracts = contracts

    async def extract(self, extraction_input: Any) -> Any:
        return self._contracts.CognitiveExtractionOutcome(
            status="success",
            evidence=(
                self._contracts.CognitiveEvidenceDraft(
                    topic_id=extraction_input.topic_id,
                    hypothesis_code=HYPOTHESIS_CODE,
                    direction="support",
                    strength=0.95,
                    extractor_confidence=0.95,
                    evidence_span="target mechanism supported",
                ),
            ),
            extractor_version=EXTRACTOR,
            model_version=MODEL,
        )


class _Logger:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _runtime(monkeypatch: pytest.MonkeyPatch, package_name: str):
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    setattr(mode_manager, "normalize_mode", lambda value: str(value or "companion"))
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    store_module = importlib.import_module(f"{package_name}.store")
    outbox_module = importlib.import_module(f"{package_name}.store_cognitive_outbox")
    return store_module, outbox_module


def _store(tmp_path: Path, Store):
    store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    store.ensure_topic(topic_id=TOPIC, name="Chain rule")
    return store


def _ordinary_answer(store: Any, attempt_id: str) -> None:
    store.batch_write_answer_data(
        session_id="retention-source",
        mode="companion",
        topic_id=TOPIC,
        question={
            "question_id": f"question-{attempt_id}",
            "question": "Differentiate (x^2 + 1)^3.",
            "answer": "6x(x^2+1)^2",
            "question_type": "math_reasoning",
            "difficulty": 3,
        },
        user_answer="6x(x^2+1)^2",
        eval_result={"verdict": "correct", "score": 100},
        response_time_ms=100,
        attempt_id=attempt_id,
    )


def _claimed_retention(
    store: Any,
    *,
    transfer_at: datetime | None = None,
    claim_at: datetime | None = None,
    lease_seconds: int = 3600,
) -> tuple[dict[str, Any], dict[str, Any]]:
    now = datetime.now(timezone.utc)
    opened = transfer_at or now - timedelta(hours=25)
    claim_time = claim_at or now
    source_attempt = f"transfer-{opened.timestamp()}-{id(store)}"
    prior_support_attempt = f"support-{source_attempt}"
    _ordinary_answer(store, prior_support_attempt)
    _finish_extraction(
        store,
        prior_support_attempt,
        hypothesis_code=HYPOTHESIS_CODE,
    )
    _ordinary_answer(store, source_attempt)
    _finish_extraction(store, source_attempt, hypothesis_code=HYPOTHESIS_CODE)
    created = store.record_certified_transfer_success(
        {
            "hypothesis_id": HYPOTHESIS,
            "topic_id": TOPIC,
            "hypothesis_code": HYPOTHESIS_CODE,
            "model_version": MODEL,
            "source_attempt_id": source_attempt,
            "source_event_id": f"event-{source_attempt}",
            "question_family_id": "chain.polynomial.transfer",
            "evaluation_verdict": "correct",
            "certified": True,
            "used_hint": False,
            "occurred_at": _iso(opened),
        }
    )
    claims = store.claim_cognitive_obligations(
        worker_id="retention-question-worker",
        lease_seconds=lease_seconds,
        as_of=_iso(claim_time),
        obligation_types=("retention",),
        obligation_ids=(str(created["obligation"]["obligation_id"]),),
    )
    assert len(claims) == 1
    return created, claims[0]


def _question(created: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
    episode = created["episode"]
    return {
        "question_id": f"question-retention-{claim['claim_id']}",
        "question": "Differentiate exp(5x - 2).",
        "answer": "5*exp(5*x-2)",
        "question_type": "math_reasoning",
        "difficulty": 3,
        "learning_intent": "retention_check",
        "cognitive_strategy": STRATEGY,
        "obligation_refs": [claim["obligation_id"]],
        "cognitive_episode_id": episode["episode_id"],
        "cognitive_obligation_id": claim["obligation_id"],
        "cognitive_claim_id": claim["claim_id"],
        "cognitive_claim_token": claim["claim_token"],
        "cognitive_claim_worker_id": claim["worker_id"],
        "cognitive_claim_lease_expires_at": claim["lease_expires_at"],
        "cognitive_blueprint_id": BLUEPRINT,
        "cognitive_question_family_id": QUESTION_FAMILY,
        "cognitive_independence_group": INDEPENDENCE_GROUP,
        "cognitive_transfer_question_family_id": episode[
            "transfer_question_family_id"
        ],
        "retention_blueprint_version": BLUEPRINT_VERSION,
        "retention_validator_version": VALIDATOR_VERSION,
    }


def _answer_retention(
    store: Any,
    created: dict[str, Any],
    claim: dict[str, Any],
    *,
    attempt_id: str,
    verdict: str,
    used_hint: bool | None = False,
    question_changes: dict[str, Any] | None = None,
    enqueue_projection: bool = True,
) -> dict[str, Any]:
    question = _question(created, claim)
    question.update(question_changes or {})
    return store.batch_write_answer_data(
        session_id="retention-answer",
        mode="companion",
        topic_id=TOPIC,
        question=question,
        user_answer="private learner answer",
        eval_result={
            "verdict": verdict,
            "score": 100 if verdict == "correct" else 0,
            "evaluator_type": "deterministic_math",
            "evaluator_version": "math-evaluator-v2",
            "confidence": 0.99,
        },
        response_time_ms=120,
        used_hint=used_hint,
        attempt_id=attempt_id,
        enqueue_cognitive_projection=enqueue_projection,
        cognitive_extractor_version=EXTRACTOR,
        cognitive_model_version=MODEL,
    )


def _finish_extraction(
    store: Any,
    attempt_id: str,
    *,
    hypothesis_code: str = "",
) -> None:
    with store.transaction() as conn:
        conn.execute(
            """UPDATE cognitive_extraction_queue SET status = 'done'
            WHERE attempt_id = ?""",
            (attempt_id,),
        )
        if hypothesis_code:
            conn.execute(
                """
                INSERT INTO cognitive_evidence (
                    evidence_id, attempt_id, topic_id, hypothesis_code,
                    direction, strength, extractor_confidence, diagnosticity,
                    source_kind, evidence_span, extractor_version,
                    evidence_family_id, question_id, session_id,
                    diagnostic_validation_id
                ) VALUES (?, ?, ?, ?, 'support', 0.9, 0.95, 0.9,
                          'answer', 'server-derived evidence', ?, '', ?, '', '')
                """,
                (
                    f"evidence-{attempt_id}",
                    attempt_id,
                    TOPIC,
                    hypothesis_code,
                    EXTRACTOR,
                    f"question-retention-{attempt_id}",
                ),
            )


def _prepare_transfer_intervention(store: Any) -> dict[str, Any]:
    _ordinary_answer(store, "attempt-transfer-hypothesis")
    snapshot_id = f"{TOPIC}:{HYPOTHESIS_CODE}:{MODEL}:generation-7"
    conn = store._require_conn()
    conn.execute(
        """
        INSERT INTO cognitive_topic_projection_queue (
            topic_id, model_version, status, requested_generation,
            claimed_generation, projected_generation
        ) VALUES (?, ?, 'done', 7, 7, 7)
        """,
        (TOPIC, MODEL),
    )
    conn.execute(
        """
        INSERT INTO cognitive_hypothesis_current (
            hypothesis_id, topic_id, hypothesis_code, evidence_status,
            intervention_stage, user_override, status, probability,
            support_count, counter_count, diagnostic_support_count,
            relapse_count, source_attempt_id, source_snapshot_id,
            model_version, projected_generation
        ) VALUES (?, ?, ?, 'supported', 'remediating', '', 'supported', 0.9,
                  2, 0, 1, 0, 'attempt-transfer-hypothesis', ?, ?, 7)
        """,
        (HYPOTHESIS, TOPIC, HYPOTHESIS_CODE, snapshot_id, MODEL),
    )
    conn.commit()
    event = {
        "event_id": "event-transfer-question",
        "event_type": "question_committed",
        "decision_id": "decision-transfer",
        "hypothesis_target": {
            "hypothesis_id": HYPOTHESIS,
            "topic_id": TOPIC,
            "code": HYPOTHESIS_CODE,
            "status": "supported",
            "probability": 0.9,
            "model_version": MODEL,
            "source_snapshot_id": snapshot_id,
            "source_attempt_id": "attempt-transfer-hypothesis",
            "projection_generation": 7,
        },
        "learning_intent": "transfer_check",
        "repair_strategy": "cross_form_transfer",
        "binding": {
            "plan_id": "plan-transfer",
            "topic_id": TOPIC,
            "selection_reason": "recommended",
            "eligible_topic_ids": [TOPIC],
            "learning_plan_id": "",
            "learning_plan_revision": 0,
            "scope_key": "",
            "scope_revision": 0,
            "origin_wrong_question_id": "",
            "source_question_id": "",
            "target_binding": {},
        },
        "question_id": "question-transfer",
        "attempt_id": "",
        "blueprint_id": TRANSFER_BLUEPRINT,
        "question_family_id": TRANSFER_FAMILY,
        "diagnostic_validation_id": "validation-transfer",
        "evaluation_verdict": "",
        "policy_version": "cognitive-intent-policy-v2",
        "validator_version": QUESTION_VALIDATOR_VERSION,
        "schema_version": 1,
        "metadata": {"reviewed": True},
        "created_at": _iso(datetime.now(timezone.utc)),
    }
    store.record_cognitive_intervention_event(event)
    return event


def _submit_transfer(
    store: Any,
    Tracker: type[Any],
    *,
    retention_enabled: bool,
    attempt_id: str,
    eval_overrides: dict[str, object] | None = None,
) -> dict[str, Any]:
    tracker = Tracker(
        store,
        logger=_Logger(),
        cognitive_config={
            "projection_enabled": True,
            "read_mode": "active",
            "intent_policy": "on",
            "retention_enabled": retention_enabled,
            "version_set": MODEL,
            "model_version": MODEL,
            "supported_topics": [TOPIC],
        },
        cognitive_extractor=_UnusedExtractor(),
    )
    eval_result: dict[str, object] = {
        "verdict": "correct",
        "score": 100,
        "evaluator_type": "deterministic_math",
        "evaluator_version": "math-evaluator-v2",
        "confidence": 1.0,
    }
    eval_result.update(eval_overrides or {})
    return tracker.on_answer(
        topic_id=TOPIC,
        question={
            "question_id": "question-transfer",
            "question": "Differentiate (x^2 + 1)^4.",
            "answer": "8*x*(x^2+1)^3",
            "question_type": "math_reasoning",
            "difficulty": 3,
            "learning_intent": "transfer_check",
            "cognitive_blueprint_id": TRANSFER_BLUEPRINT,
            "cognitive_question_family_id": TRANSFER_FAMILY,
            "cognitive_validator_version": QUESTION_VALIDATOR_VERSION,
            "diagnostic_validation_id": "validation-transfer",
            "target_binding": {
                "target_topic_id": TOPIC,
                "validation_status": "passed",
                "cognitive_learning_intent": "transfer_check",
                "cognitive_decision_id": "decision-transfer",
                "diagnostic_validation_id": "validation-transfer",
            },
        },
        user_answer="8*x*(x^2+1)^3",
        eval_result=eval_result,
        mode="companion",
        session_id="session-transfer",
        response_time_ms=150,
        used_hint=False,
        require_existing_topic=True,
        attempt_id=attempt_id,
    )


def test_correct_retention_answer_resolves_without_copying_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, _ = _runtime(monkeypatch, "_retention_answer_resolved")
    store = _store(tmp_path, store_module.StudyStore)
    try:
        created, claim = _claimed_retention(store)
        result = _answer_retention(
            store,
            created,
            claim,
            attempt_id="retention-correct",
            verdict="correct",
            enqueue_projection=False,
        )

        assert result == {
            "ok": True,
            "wrong_question_id": "",
            "wrong_question_attempt": {},
        }
        assert store.list_cognitive_monitoring_episodes()[0]["status"] == "resolved"
        rows = store.list_cognitive_outbox()
        retention = next(row for row in rows if row["operation"] == "retention_disposition")
        assert retention["status"] == "done"
        assert "answer" not in retention["payload"]
        assert "user_answer" not in retention["payload"]
        assert "outbox_id" not in result
        assert "claim_token" not in result
        queue = store.get_cognitive_topic_projection_state(
            topic_id=TOPIC,
            model_version=MODEL,
        )
        assert queue["status"] == "pending"
        assert queue["requested_generation"] == 1
    finally:
        store.close()


@pytest.mark.parametrize("retention_enabled", [True, False])
def test_transfer_opens_episode_only_when_the_full_retention_gate_is_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retention_enabled: bool,
) -> None:
    store_module, _ = _runtime(
        monkeypatch, f"_retention_transfer_gate_{retention_enabled}"
    )
    tracker_module = importlib.import_module(
        f"{store_module.__package__}.knowledge_tracker"
    )
    store = _store(tmp_path, store_module.StudyStore)
    try:
        _prepare_transfer_intervention(store)
        result = _submit_transfer(
            store,
            tracker_module.KnowledgeTracker,
            retention_enabled=retention_enabled,
            attempt_id=f"attempt-transfer-{retention_enabled}",
        )

        assert result["topic_id"] == TOPIC
        episodes = store.list_cognitive_monitoring_episodes()
        assert len(episodes) == (1 if retention_enabled else 0)
        if retention_enabled:
            assert episodes[0]["source_attempt_id"] == "attempt-transfer-True"
            obligations = store.list_cognitive_learning_obligations()
            assert len(obligations) == 1
            assert obligations[0]["obligation_type"] == "retention"
        attempt_event = store.list_cognitive_intervention_events(
            event_types=("attempt_committed",)
        )[0]
        outbox = next(
            row
            for row in store.list_cognitive_outbox(status="done")
            if row["operation"] == "intervention_event"
        )
        assert (
            outbox["payload"].get("retention_episode_requested") is True
        ) is retention_enabled
        assert attempt_event["evaluation_verdict"] == "correct"
    finally:
        store.close()


@pytest.mark.parametrize(
    "invalid_provenance",
    [
        {"evaluator_type": ""},
        {"evaluator_version": ""},
        {"confidence": None},
        {"confidence": -0.01},
        {"confidence": 1.01},
    ],
)
def test_transfer_without_valid_evaluator_provenance_keeps_ordinary_answer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_provenance: dict[str, object],
) -> None:
    store_module, _ = _runtime(monkeypatch, "_retention_transfer_provenance")
    tracker_module = importlib.import_module(
        f"{store_module.__package__}.knowledge_tracker"
    )
    store = _store(tmp_path, store_module.StudyStore)
    try:
        _prepare_transfer_intervention(store)
        result = _submit_transfer(
            store,
            tracker_module.KnowledgeTracker,
            retention_enabled=True,
            attempt_id="attempt-transfer-invalid-provenance",
            eval_overrides=invalid_provenance,
        )

        assert result["topic_id"] == TOPIC
        assert store.get_attempt_fact("attempt-transfer-invalid-provenance") is not None
        assert store.list_cognitive_monitoring_episodes() == []
        assert store.list_cognitive_intervention_events(
            event_types=("attempt_committed",)
        ) == []
        outbox = next(
            row
            for row in store.list_cognitive_outbox(status="failed")
            if row["operation"] == "intervention_event"
        )
        assert "retention transfer evaluator" in outbox["last_error"]
    finally:
        store.close()


def test_wrong_waits_for_extraction_then_relapses_on_target_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, _ = _runtime(monkeypatch, "_retention_answer_relapse")
    store = _store(tmp_path, store_module.StudyStore)
    try:
        created, claim = _claimed_retention(store)
        _answer_retention(
            store,
            created,
            claim,
            attempt_id="retention-relapse",
            verdict="wrong",
        )
        failed = store.list_cognitive_outbox(status="failed")
        assert len(failed) == 1
        assert "evidence is not ready" in failed[0]["last_error"]
        assert store.list_cognitive_monitoring_episodes()[0]["status"] == "open"

        tracker_module = importlib.import_module(
            f"{store_module.__package__}.knowledge_tracker"
        )
        contracts = importlib.import_module(
            f"{store_module.__package__}.adaptive_learning.cognitive_contracts"
        )
        tracker = tracker_module.KnowledgeTracker(
            store,
            logger=_Logger(),
            cognitive_config={
                "projection_enabled": True,
                "read_mode": "active",
                "intent_policy": "on",
                "retention_enabled": True,
                "version_set": MODEL,
                "model_version": MODEL,
                "supported_topics": [TOPIC],
            },
            cognitive_extractor=_TargetExtractor(contracts),
        )
        projection = asyncio.run(tracker.project_cognitive_pending(limit=10))
        assert projection["completed"] == 1
        retention_rows = [
            row
            for row in store.list_cognitive_outbox(status="done")
            if row["operation"] == "retention_disposition"
        ]
        assert len(retention_rows) == 1
        episode = store.list_cognitive_monitoring_episodes()[0]
        assert episode["status"] == "relapsed"
        assert episode["relapse_count"] == 1
        queue = store.get_cognitive_topic_projection_state(
            topic_id=TOPIC,
            model_version=MODEL,
        )
        assert queue["requested_generation"] == 3
        assert queue["projected_generation"] == 3
        current = store.list_cognitive_hypothesis_current(
            topic_id=TOPIC,
            hypothesis_code=HYPOTHESIS_CODE,
            model_version=MODEL,
        )[0]
        assert current["status"] == "supported"
        assert current["evidence_status"] == "supported"
        assert current["intervention_stage"] == "idle"
        assert current["last_outcome"] == "relapse"
    finally:
        store.close()


@pytest.mark.parametrize("verdict", ["partial", "dont_know"])
def test_non_target_incomplete_result_reschedules_after_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verdict: str
) -> None:
    store_module, _ = _runtime(monkeypatch, f"_retention_answer_{verdict}")
    store = _store(tmp_path, store_module.StudyStore)
    try:
        created, claim = _claimed_retention(store)
        attempt_id = f"retention-{verdict}"
        _answer_retention(
            store,
            created,
            claim,
            attempt_id=attempt_id,
            verdict=verdict,
        )
        _finish_extraction(store, attempt_id)
        store.process_cognitive_outbox(include_retention=True)

        assert store.list_cognitive_monitoring_episodes()[0]["status"] == "open"
        obligation = store.list_cognitive_learning_obligations()[0]
        assert obligation["status"] == "pending"
        satisfaction = store._require_read_conn().execute(
            "SELECT disposition FROM cognitive_obligation_satisfactions"
        ).fetchone()
        assert satisfaction["disposition"] == "reschedule"
        queue = store.get_cognitive_topic_projection_state(
            topic_id=TOPIC,
            model_version=MODEL,
        )
        assert queue["requested_generation"] == 2
    finally:
        store.close()


@pytest.mark.parametrize(
    ("used_hint", "question_changes"),
    [
        (True, {}),
        (False, {"retention_blueprint_version": "unknown"}),
        (False, {"retention_validator_version": "unknown"}),
        (False, {"cognitive_question_family_id": "chain.polynomial.transfer"}),
    ],
)
def test_uncertified_retention_is_only_ordinary_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    used_hint: bool,
    question_changes: dict[str, Any],
) -> None:
    store_module, _ = _runtime(
        monkeypatch, f"_retention_uncertified_{len(question_changes)}_{used_hint}"
    )
    store = _store(tmp_path, store_module.StudyStore)
    try:
        created, claim = _claimed_retention(store)
        _answer_retention(
            store,
            created,
            claim,
            attempt_id="retention-ordinary",
            verdict="correct",
            used_hint=used_hint,
            question_changes=question_changes,
            enqueue_projection=False,
        )

        assert store.list_cognitive_monitoring_episodes()[0]["status"] == "open"
        satisfaction = store._require_read_conn().execute(
            "SELECT disposition, metadata_json FROM cognitive_obligation_satisfactions"
        ).fetchone()
        assert satisfaction["disposition"] == "ordinary_evidence"
        assert "certified" in satisfaction["metadata_json"]
        queue = store.get_cognitive_topic_projection_state(
            topic_id=TOPIC,
            model_version=MODEL,
        )
        assert queue["requested_generation"] == 1
    finally:
        store.close()


def test_old_claim_answer_is_discarded_after_takeover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, _ = _runtime(monkeypatch, "_retention_stale_takeover")
    store = _store(tmp_path, store_module.StudyStore)
    try:
        now = datetime.now(timezone.utc)
        created, old_claim = _claimed_retention(
            store,
            claim_at=now,
            lease_seconds=1,
        )
        replacement = store.claim_cognitive_obligations(
            worker_id="replacement-worker",
            lease_seconds=3600,
            as_of=_iso(now + timedelta(seconds=2)),
            obligation_types=("retention",),
            obligation_ids=(str(old_claim["obligation_id"]),),
        )[0]

        result = _answer_retention(
            store,
            created,
            old_claim,
            attempt_id="retention-stale-claim",
            verdict="correct",
        )

        assert result["ok"] is True
        discarded = store.list_cognitive_outbox(status="discarded")
        assert len(discarded) == 1
        assert "stale retention claim" in discarded[0]["last_error"]
        assert store.list_cognitive_monitoring_episodes()[0]["status"] == "open"
        obligation = store.list_cognitive_learning_obligations()[0]
        assert obligation["current_claim_id"] == replacement["claim_id"]
    finally:
        store.close()


@pytest.mark.parametrize("action", ["dismiss", "delete"])
def test_user_control_discards_retention_effect_but_keeps_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str
) -> None:
    store_module, _ = _runtime(monkeypatch, f"_retention_stale_{action}")
    store = _store(tmp_path, store_module.StudyStore)
    try:
        created, claim = _claimed_retention(store)
        store.record_cognitive_user_control(
            topic_id=TOPIC,
            hypothesis_code=HYPOTHESIS_CODE,
            action=action,
        )

        result = _answer_retention(
            store,
            created,
            claim,
            attempt_id=f"retention-{action}",
            verdict="correct",
        )

        assert result["ok"] is True
        assert store.get_attempt_fact(f"retention-{action}") is not None
        assert len(store.list_cognitive_outbox(status="discarded")) == 1
    finally:
        store.close()


def test_retention_sqlite_failure_rolls_back_the_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, outbox_module = _runtime(monkeypatch, "_retention_sqlite_failure")
    store = _store(tmp_path, store_module.StudyStore)
    try:
        created, claim = _claimed_retention(store)

        def fail_sqlite(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("injected retention disk failure")

        monkeypatch.setattr(
            outbox_module,
            "apply_cognitive_retention_disposition",
            fail_sqlite,
        )
        with pytest.raises(sqlite3.OperationalError, match="injected retention"):
            _answer_retention(
                store,
                created,
                claim,
                attempt_id="retention-sqlite-failure",
                verdict="correct",
            )

        assert store.get_attempt_fact("retention-sqlite-failure") is None
        assert all(
            row["attempt_id"] != "retention-sqlite-failure"
            for row in store.list_cognitive_outbox()
        )
        assert store.list_cognitive_monitoring_episodes()[0]["status"] == "open"
    finally:
        store.close()


def test_early_retention_answer_is_ordinary_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, _ = _runtime(monkeypatch, "_retention_answer_early")
    store = _store(tmp_path, store_module.StudyStore)
    try:
        now = datetime.now(timezone.utc)
        created, claim = _claimed_retention(
            store,
            transfer_at=now,
            claim_at=now + timedelta(hours=25),
        )
        _answer_retention(
            store,
            created,
            claim,
            attempt_id="retention-early",
            verdict="correct",
            enqueue_projection=False,
        )

        satisfaction = store._require_read_conn().execute(
            "SELECT disposition, metadata_json FROM cognitive_obligation_satisfactions"
        ).fetchone()
        assert satisfaction["disposition"] == "ordinary_evidence"
        assert "retention_too_early" in satisfaction["metadata_json"]
        assert store.list_cognitive_monitoring_episodes()[0]["status"] == "open"
    finally:
        store.close()


def test_episode_expiry_marks_projection_dirty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_module, _ = _runtime(monkeypatch, "_retention_expiry_dirty")
    store = _store(tmp_path, store_module.StudyStore)
    try:
        now = datetime.now(timezone.utc)
        source_attempt = "transfer-expired"
        _ordinary_answer(store, source_attempt)
        created = store.record_certified_transfer_success(
            {
                "hypothesis_id": HYPOTHESIS,
                "topic_id": TOPIC,
                "hypothesis_code": HYPOTHESIS_CODE,
                "model_version": MODEL,
                "source_attempt_id": source_attempt,
                "source_event_id": "event-transfer-expired",
                "question_family_id": "chain.polynomial.transfer",
                "evaluation_verdict": "correct",
                "certified": True,
                "used_hint": False,
                "occurred_at": _iso(now - timedelta(days=8)),
            }
        )
        assert created["episode"]["status"] == "open"
        store.expire_cognitive_monitoring_episodes(as_of=_iso(now))

        assert store.list_cognitive_monitoring_episodes()[0]["status"] == "expired"
        queue = store.get_cognitive_topic_projection_state(
            topic_id=TOPIC,
            model_version=MODEL,
        )
        assert queue["status"] == "pending"
        assert queue["requested_generation"] >= 1
    finally:
        store.close()
