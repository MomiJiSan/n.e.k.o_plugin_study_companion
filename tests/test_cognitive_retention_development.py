from __future__ import annotations

import asyncio
import importlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "college_chain_rule"
HYPOTHESIS_CODE = "omit_inner_derivative"
HYPOTHESIS_ID = f"{TOPIC}:{HYPOTHESIS_CODE}"
MODEL = "cognitive-v2.1-1"
EXTRACTOR = "cognitive-extractor-v2"
TRANSFER_BLUEPRINT = "chain.omit-inner.cross-form-transfer.v1"
TRANSFER_FAMILY = "chain.polynomial-power.cross-form-transfer"
QUESTION_VALIDATOR_VERSION = "cognitive-question-validator-v2.1-1"
CONFIRMATION = "ADVANCE_RETENTION_WINDOW_FOR_DEVELOPMENT"
DEV_ENV = "STUDY_COMPANION_COGNITIVE_DEV_TOOLS"


class _Logger:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


class _UnusedExtractor:
    async def extract(self, _input: object) -> object:
        raise AssertionError("development preparation must not invoke extraction")


@dataclass
class _Runtime:
    package_name: str
    models: Any
    store_module: Any
    tracker_module: Any
    development: Any
    retention: Any
    contracts: Any


@dataclass
class _Fixture:
    runtime: _Runtime
    store: Any
    source_attempt_id: str
    source_event_id: str


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _runtime(monkeypatch: pytest.MonkeyPatch, name: str) -> _Runtime:
    package_name = f"_cognitive_retention_development_{name}_{id(monkeypatch)}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    setattr(mode_manager, "normalize_mode", lambda value: str(value or "companion"))
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return _Runtime(
        package_name=package_name,
        models=importlib.import_module(f"{package_name}.models"),
        store_module=importlib.import_module(f"{package_name}.store"),
        tracker_module=importlib.import_module(f"{package_name}.knowledge_tracker"),
        development=importlib.import_module(f"{package_name}.store_cognitive_development"),
        retention=importlib.import_module(f"{package_name}.adaptive_learning.cognitive_retention"),
        contracts=importlib.import_module(f"{package_name}.adaptive_learning.contracts"),
    )


def _active_config(runtime: _Runtime, *, retention_enabled: bool = True) -> Any:
    return runtime.models.StudyConfig(
        cognitive=runtime.models.CognitiveConfig(
            projection_enabled=True,
            read_mode="active",
            intent_policy="on",
            retention_enabled=retention_enabled,
            version_set=MODEL,
            supported_topics=(TOPIC,),
        )
    )


def _open_store(runtime: _Runtime, tmp_path: Path) -> Any:
    store = runtime.store_module.StudyStore(tmp_path / "study.db", tmp_path / "missing-seed.json", _Logger())
    store.open()
    store.ensure_topic(topic_id=TOPIC, name="Chain rule")
    store.save_config(_active_config(runtime))
    return store


def _ordinary_answer(store: Any, attempt_id: str) -> None:
    store.batch_write_answer_data(
        session_id="development-retention-source",
        mode="companion",
        topic_id=TOPIC,
        question={
            "question_id": f"question-{attempt_id}",
            "question": "Differentiate (x^2 + 1)^3.",
            "answer": "6*x*(x^2+1)^2",
            "question_type": "math_reasoning",
            "difficulty": 3,
        },
        user_answer="6*x*(x^2+1)^2",
        eval_result={"verdict": "correct", "score": 100},
        response_time_ms=100,
        attempt_id=attempt_id,
    )


def _question_event() -> dict[str, Any]:
    snapshot_id = f"{TOPIC}:{HYPOTHESIS_CODE}:{MODEL}:generation-7"
    return {
        "event_id": "event-transfer-question",
        "event_type": "question_committed",
        "decision_id": "decision-transfer",
        "hypothesis_target": {
            "hypothesis_id": HYPOTHESIS_ID,
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


def _prepare_retention_off_transfer(runtime: _Runtime, store: Any) -> _Fixture:
    source_attempt_id = "attempt-transfer-retention-off"
    _ordinary_answer(store, "attempt-transfer-hypothesis")
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
        ) VALUES (?, ?, ?, 'supported', 'provisionally_resolved', '',
                  'provisionally_resolved', 0.9, 2, 0, 1, 0,
                  'attempt-transfer-hypothesis', ?, ?, 7)
        """,
        (
            HYPOTHESIS_ID,
            TOPIC,
            HYPOTHESIS_CODE,
            f"{HYPOTHESIS_ID}:{MODEL}:generation-7",
            MODEL,
        ),
    )
    conn.commit()
    store.record_cognitive_intervention_event(_question_event())
    tracker = runtime.tracker_module.KnowledgeTracker(
        store,
        logger=_Logger(),
        cognitive_config={
            "projection_enabled": True,
            "read_mode": "active",
            "intent_policy": "on",
            "retention_enabled": False,
            "version_set": MODEL,
            "model_version": MODEL,
            "supported_topics": [TOPIC],
        },
        cognitive_extractor=_UnusedExtractor(),
    )
    result = tracker.on_answer(
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
        eval_result={
            "verdict": "correct",
            "score": 100,
            "evaluator_type": "deterministic_math",
            "evaluator_version": "math-evaluator-v2",
            "confidence": 1.0,
        },
        mode="companion",
        session_id="session-transfer",
        response_time_ms=150,
        used_hint=False,
        require_existing_topic=True,
        attempt_id=source_attempt_id,
    )
    assert result["topic_id"] == TOPIC
    attempt_event = store.list_cognitive_intervention_events(event_types=("attempt_committed",))[0]
    assert attempt_event["attempt_id"] == source_attempt_id
    source_event_id = str(attempt_event["event_id"])
    outbox = _transfer_outbox(store, source_attempt_id)
    assert outbox["status"] == "done"
    assert outbox["payload"].get("retention_episode_requested") is not True
    assert store.list_cognitive_monitoring_episodes() == []

    queue = store.get_cognitive_topic_projection_state(topic_id=TOPIC, model_version=MODEL)
    generation = int(queue["requested_generation"])
    conn.execute(
        """
        UPDATE cognitive_topic_projection_queue
        SET status = 'done', claimed_generation = ?, projected_generation = ?,
            lease_token = '', last_error = NULL
        WHERE topic_id = ? AND model_version = ?
        """,
        (generation, generation, TOPIC, MODEL),
    )
    conn.execute(
        """
        UPDATE cognitive_hypothesis_current
        SET evidence_status = 'supported', intervention_stage = 'monitored',
            user_override = '', status = 'monitored', source_attempt_id = ?,
            source_snapshot_id = ?, projected_generation = ?,
            last_intent = 'transfer_check', last_outcome = 'correct'
        WHERE topic_id = ? AND hypothesis_code = ? AND model_version = ?
        """,
        (
            source_attempt_id,
            f"{HYPOTHESIS_ID}:{MODEL}:generation-{generation}",
            generation,
            TOPIC,
            HYPOTHESIS_CODE,
            MODEL,
        ),
    )
    conn.commit()
    return _Fixture(runtime, store, source_attempt_id, source_event_id)


def _transfer_outbox(store: Any, attempt_id: str) -> dict[str, Any]:
    return next(
        row
        for row in store.list_cognitive_outbox()
        if row["operation"] == "intervention_event" and row["payload"].get("attempt_id") == attempt_id
    )


def _logical_dump(store: Any) -> tuple[str, ...]:
    return tuple(store._require_conn().iterdump())


def _call(
    fixture: _Fixture,
    *,
    apply: bool,
    expected_source_attempt_id: str | None = None,
) -> dict[str, Any]:
    return fixture.runtime.development.prepare_cognitive_retention_for_development(
        fixture.store,
        topic_id=TOPIC,
        hypothesis_code=HYPOTHESIS_CODE,
        expected_source_attempt_id=(
            fixture.source_attempt_id
            if expected_source_attempt_id is None and apply
            else str(expected_source_attempt_id or "")
        ),
        apply=apply,
    )


@pytest.fixture
def prepared_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest):
    runtime = _runtime(monkeypatch, str(request.node.name))
    store = _open_store(runtime, tmp_path)
    fixture = _prepare_retention_off_transfer(runtime, store)
    try:
        yield fixture
    finally:
        store.close()


def test_default_gate_and_preview_are_zero_write(monkeypatch: pytest.MonkeyPatch, prepared_fixture: _Fixture) -> None:
    monkeypatch.delenv(DEV_ENV, raising=False)
    before = _logical_dump(prepared_fixture.store)
    disabled = _call(prepared_fixture, apply=False)
    assert disabled["status"] == "blocked"
    assert disabled["reason_code"] == "dev_tools_disabled"
    assert _logical_dump(prepared_fixture.store) == before

    monkeypatch.setenv(DEV_ENV, "1")
    ready = _call(
        prepared_fixture,
        apply=False,
        expected_source_attempt_id="",
    )
    assert ready["status"] == "ready"
    assert ready["source_attempt_id"] == prepared_fixture.source_attempt_id
    assert _logical_dump(prepared_fixture.store) == before


def test_persisted_retention_gate_and_frozen_target_fail_closed(
    monkeypatch: pytest.MonkeyPatch, prepared_fixture: _Fixture
) -> None:
    monkeypatch.setenv(DEV_ENV, "1")
    prepared_fixture.store.save_config(_active_config(prepared_fixture.runtime, retention_enabled=False))
    disabled_baseline = _logical_dump(prepared_fixture.store)
    disabled = _call(prepared_fixture, apply=False)
    assert disabled["reason_code"] == "retention_disabled"
    assert _logical_dump(prepared_fixture.store) == disabled_baseline

    prepared_fixture.store.save_config(_active_config(prepared_fixture.runtime))
    unsupported_baseline = _logical_dump(prepared_fixture.store)
    unsupported = prepared_fixture.runtime.development.prepare_cognitive_retention_for_development(
        prepared_fixture.store,
        topic_id="calculus.chain_rule",
        hypothesis_code=HYPOTHESIS_CODE,
        expected_source_attempt_id="",
        apply=False,
    )
    assert unsupported["reason_code"] == "unsupported_target"
    assert _logical_dump(prepared_fixture.store) == unsupported_baseline


def test_retention_off_transfer_is_certified_backfilled_and_windowed_now(
    monkeypatch: pytest.MonkeyPatch, prepared_fixture: _Fixture
) -> None:
    monkeypatch.setenv(DEV_ENV, "1")
    transfer = prepared_fixture.store.get_attempt_fact(prepared_fixture.source_attempt_id)
    assert transfer is not None
    transfer_at = _utc(str(transfer["submitted_at"]))
    original_not_before = transfer_at + timedelta(hours=24)
    result = _call(prepared_fixture, apply=True)

    assert result["status"] == "prepared"
    assert result["enabled"] is True
    assert result["development_override"] is True
    assert result["topic_id"] == TOPIC
    assert result["hypothesis_code"] == HYPOTHESIS_CODE
    assert result["source_attempt_id"] == prepared_fixture.source_attempt_id
    episodes = prepared_fixture.store.list_cognitive_monitoring_episodes()
    obligations = prepared_fixture.store.list_cognitive_learning_obligations()
    assert len(episodes) == len(obligations) == 1
    episode, obligation = episodes[0], obligations[0]
    assert episode["source_event_id"] == prepared_fixture.source_event_id
    assert episode["source_attempt_id"] == prepared_fixture.source_attempt_id
    assert obligation["reason"] == "development_time_override"
    assert _utc(episode["not_before"]) == _utc(obligation["not_before"])
    assert _utc(episode["not_before"]) < original_not_before
    assert _utc(episode["due_by"]) == transfer_at + timedelta(hours=72)
    assert _utc(obligation["due_by"]) == transfer_at + timedelta(hours=72)
    assert _utc(episode["eligibility_until"]) == transfer_at + timedelta(days=7)
    assert _utc(obligation["eligibility_until"]) == transfer_at + timedelta(days=7)
    outbox = _transfer_outbox(prepared_fixture.store, prepared_fixture.source_attempt_id)
    assert outbox["payload"].get("retention_episode_requested") is not True


def test_prepare_is_idempotent_and_does_not_dirty_projection(
    monkeypatch: pytest.MonkeyPatch, prepared_fixture: _Fixture
) -> None:
    monkeypatch.setenv(DEV_ENV, "1")
    before_generation = prepared_fixture.store.get_cognitive_topic_projection_state(
        topic_id=TOPIC, model_version=MODEL
    )["requested_generation"]
    first = _call(prepared_fixture, apply=True)
    first_dump = _logical_dump(prepared_fixture.store)
    second = _call(prepared_fixture, apply=True)

    assert first["status"] == "prepared"
    assert second["status"] == "already_prepared"
    assert second["episode_id"] == first["episode_id"]
    assert second["obligation_id"] == first["obligation_id"]
    assert _logical_dump(prepared_fixture.store) == first_dump
    assert len(prepared_fixture.store.list_cognitive_monitoring_episodes()) == 1
    assert len(prepared_fixture.store.list_cognitive_learning_obligations()) == 1
    assert (
        prepared_fixture.store.get_cognitive_topic_projection_state(topic_id=TOPIC, model_version=MODEL)[
            "requested_generation"
        ]
        == before_generation
    )


def test_source_mismatch_and_stale_projection_fail_closed(
    monkeypatch: pytest.MonkeyPatch, prepared_fixture: _Fixture
) -> None:
    monkeypatch.setenv(DEV_ENV, "1")
    before = _logical_dump(prepared_fixture.store)
    mismatch = prepared_fixture.runtime.development.prepare_cognitive_retention_for_development(
        prepared_fixture.store,
        topic_id=TOPIC,
        hypothesis_code=HYPOTHESIS_CODE,
        expected_source_attempt_id="different-attempt",
        apply=True,
    )
    assert mismatch["reason_code"] == "source_attempt_mismatch"
    assert _logical_dump(prepared_fixture.store) == before

    conn = prepared_fixture.store._require_conn()
    conn.execute(
        """UPDATE cognitive_topic_projection_queue
        SET requested_generation = requested_generation + 1, status = 'pending'
        WHERE topic_id = ? AND model_version = ?""",
        (TOPIC, MODEL),
    )
    conn.commit()
    stale_baseline = _logical_dump(prepared_fixture.store)
    stale = _call(prepared_fixture, apply=True)
    assert stale["reason_code"] == "projection_stale"
    assert _logical_dump(prepared_fixture.store) == stale_baseline


@pytest.mark.parametrize("mutation", ["hint", "verdict", "version", "blueprint"])
def test_canonical_transfer_provenance_tampering_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    prepared_fixture: _Fixture,
    mutation: str,
) -> None:
    monkeypatch.setenv(DEV_ENV, "1")
    store = prepared_fixture.store
    conn = store._require_conn()
    attempt_id = prepared_fixture.source_attempt_id
    if mutation == "hint":
        conn.execute("UPDATE attempts SET used_hint = 1 WHERE attempt_id = ?", (attempt_id,))
    elif mutation == "verdict":
        row = conn.execute("SELECT evaluation_json FROM evaluations WHERE attempt_id = ?", (attempt_id,)).fetchone()
        payload = json.loads(row["evaluation_json"])
        payload["verdict"] = "wrong"
        conn.execute(
            "UPDATE evaluations SET evaluation_json = ? WHERE attempt_id = ?",
            (json.dumps(payload, sort_keys=True), attempt_id),
        )
    else:
        outbox = _transfer_outbox(store, attempt_id)
        payload = dict(outbox["payload"])
        if mutation == "version":
            payload["hypothesis_target"] = {
                **payload["hypothesis_target"],
                "model_version": "unknown-version",
            }
        else:
            payload["blueprint_id"] = "unknown-blueprint"
        conn.execute(
            "UPDATE cognitive_outbox SET payload_json = ? WHERE outbox_id = ?",
            (json.dumps(payload, sort_keys=True), outbox["outbox_id"]),
        )
    conn.commit()
    before = _logical_dump(store)

    result = _call(prepared_fixture, apply=True)

    assert result["status"] == "blocked"
    assert result["reason_code"] == "transfer_not_certified"
    assert store.list_cognitive_monitoring_episodes() == []
    assert store.list_cognitive_learning_obligations() == []
    assert _logical_dump(store) == before


@pytest.mark.parametrize("action", ["dismiss", "suppress", "delete"])
def test_active_user_controls_block_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    prepared_fixture: _Fixture,
    action: str,
) -> None:
    monkeypatch.setenv(DEV_ENV, "1")
    expires_at = _iso(datetime.now(timezone.utc) + timedelta(hours=1)) if action == "suppress" else ""
    conn = prepared_fixture.store._require_conn()
    conn.execute(
        """
        INSERT INTO cognitive_user_controls (
            control_id, topic_id, hypothesis_code, action, expires_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (f"control-{action}", TOPIC, HYPOTHESIS_CODE, action, expires_at),
    )
    conn.commit()
    before = _logical_dump(prepared_fixture.store)

    result = _call(prepared_fixture, apply=True)

    assert result["status"] == "blocked"
    assert result["reason_code"] == "control_active"
    assert _logical_dump(prepared_fixture.store) == before


def _certified_episode_for(fixture: _Fixture, *, attempt_id: str, source_event_id: str) -> dict[str, Any]:
    if attempt_id != fixture.source_attempt_id:
        _ordinary_answer(fixture.store, attempt_id)
    fact = fixture.store.get_attempt_fact(attempt_id)
    assert fact is not None
    return fixture.store.record_certified_transfer_success(
        {
            "hypothesis_id": HYPOTHESIS_ID,
            "topic_id": TOPIC,
            "hypothesis_code": HYPOTHESIS_CODE,
            "model_version": MODEL,
            "source_attempt_id": attempt_id,
            "source_event_id": source_event_id,
            "question_family_id": TRANSFER_FAMILY,
            "evaluation_verdict": "correct",
            "certified": True,
            "used_hint": False,
            "occurred_at": str(fact["submitted_at"]),
        }
    )


@pytest.mark.parametrize("state", ["claim", "conflict", "terminal"])
def test_claim_conflict_and_terminal_episode_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    prepared_fixture: _Fixture,
    state: str,
) -> None:
    monkeypatch.setenv(DEV_ENV, "1")
    if state == "conflict":
        created = _certified_episode_for(
            prepared_fixture,
            attempt_id="other-certified-transfer",
            source_event_id="event-other-certified-transfer",
        )
    else:
        created = _certified_episode_for(
            prepared_fixture,
            attempt_id=prepared_fixture.source_attempt_id,
            source_event_id=prepared_fixture.source_event_id,
        )
    if state == "claim":
        obligation = created["obligation"]
        prepared_fixture.store.claim_cognitive_obligations(
            worker_id="development-test-worker",
            lease_seconds=3600,
            as_of=obligation["not_before"],
            obligation_types=("retention",),
            obligation_ids=(obligation["obligation_id"],),
        )
    elif state == "terminal":
        conn = prepared_fixture.store._require_conn()
        conn.execute("UPDATE cognitive_monitoring_episodes SET status = 'resolved'")
        conn.execute("UPDATE cognitive_learning_obligations SET status = 'completed'")
        conn.commit()
    before = _logical_dump(prepared_fixture.store)

    result = _call(prepared_fixture, apply=True)

    expected = "claim_active" if state == "claim" else "episode_conflict"
    assert result["status"] == "blocked"
    assert result["reason_code"] == expected
    assert _logical_dump(prepared_fixture.store) == before


def test_expired_transfer_is_not_revived(monkeypatch: pytest.MonkeyPatch, prepared_fixture: _Fixture) -> None:
    monkeypatch.setenv(DEV_ENV, "1")
    attempt = prepared_fixture.store.get_attempt_fact(prepared_fixture.source_attempt_id)
    assert attempt is not None
    monkeypatch.setattr(
        prepared_fixture.runtime.development,
        "_utc_now",
        lambda: _utc(str(attempt["submitted_at"])) + timedelta(days=8),
    )
    before = _logical_dump(prepared_fixture.store)

    result = _call(prepared_fixture, apply=True)

    assert result["status"] == "blocked"
    assert result["reason_code"] == "window_expired"
    assert _logical_dump(prepared_fixture.store) == before


def test_failure_after_certification_rolls_back_everything(
    monkeypatch: pytest.MonkeyPatch, prepared_fixture: _Fixture
) -> None:
    monkeypatch.setenv(DEV_ENV, "1")

    def fail_after_episode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("private injected failure")

    monkeypatch.setattr(
        prepared_fixture.runtime.development,
        "_advance_retention_window",
        fail_after_episode,
    )
    before = _logical_dump(prepared_fixture.store)

    with pytest.raises(RuntimeError, match="private injected failure"):
        _call(prepared_fixture, apply=True)

    assert store_rows(prepared_fixture.store, "cognitive_monitoring_episodes") == []
    assert store_rows(prepared_fixture.store, "cognitive_learning_obligations") == []
    assert _logical_dump(prepared_fixture.store) == before


def store_rows(store: Any, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in store._require_read_conn().execute(f"SELECT * FROM {table}")]


def test_all_public_results_are_reference_only_and_redacted(
    monkeypatch: pytest.MonkeyPatch, prepared_fixture: _Fixture
) -> None:
    monkeypatch.setenv(DEV_ENV, "1")
    results = [_call(prepared_fixture, apply=False), _call(prepared_fixture, apply=True)]
    encoded = json.dumps(results, sort_keys=True).lower()
    for forbidden in (
        "user_answer",
        "reference_answer",
        "accepted_answers",
        "evidence_span",
        "prompt",
        "claim_token",
        "worker_id",
        "8*x*(x^2+1)^3",
    ):
        assert forbidden not in encoded
    assert set(results[-1]) <= {
        "enabled",
        "status",
        "development_override",
        "topic_id",
        "hypothesis_code",
        "source_attempt_id",
        "episode_id",
        "obligation_id",
        "not_before",
        "due_by",
        "eligibility_until",
        "reason_code",
    }


def _retention_plan(runtime: _Runtime, proposal: Any) -> Any:
    return runtime.contracts.QuestionPlan(
        plan_id="development-retention-plan",
        selection=runtime.contracts.PracticeSelection(
            reason="recommended",
            target_topic=runtime.contracts.TopicRef(TOPIC, "Chain rule"),
            eligible_topic_ids=(TOPIC,),
        ),
        difficulty=3,
        question_type="math_reasoning",
        learning_intent="retention_check",
        obligation_refs=(proposal.obligation_id,),
        cognitive_strategy=runtime.retention.RETENTION_COGNITIVE_STRATEGY,
    )


def _answer_retention(
    fixture: _Fixture,
    proposal: Any,
    claim: dict[str, Any],
    *,
    attempt_id: str,
    verdict: str,
) -> None:
    prepared = fixture.runtime.retention.prepare_retention_question(
        _retention_plan(fixture.runtime, proposal), proposal, claim
    )
    assert prepared is not None
    question = fixture.runtime.retention.retention_question_payload(prepared, topic_id=TOPIC)
    question.update(
        {
            "question_id": f"question-{attempt_id}",
            "learning_intent": "retention_check",
            "cognitive_strategy": fixture.runtime.retention.RETENTION_COGNITIVE_STRATEGY,
            "obligation_refs": [claim["obligation_id"]],
            "cognitive_episode_id": proposal.episode_id,
            "cognitive_obligation_id": claim["obligation_id"],
            "cognitive_claim_id": claim["claim_id"],
            "cognitive_claim_token": claim["claim_token"],
            "cognitive_claim_worker_id": claim["worker_id"],
            "cognitive_claim_lease_expires_at": claim["lease_expires_at"],
            "cognitive_transfer_question_family_id": proposal.transfer_question_family_id,
            "retention_blueprint_version": fixture.runtime.retention.RETENTION_BLUEPRINT_VERSION,
            "retention_validator_version": fixture.runtime.retention.RETENTION_VALIDATOR_VERSION,
        }
    )
    fixture.store.batch_write_answer_data(
        session_id="development-retention-answer",
        mode="companion",
        topic_id=TOPIC,
        question=question,
        user_answer="private learner answer",
        eval_result={
            "verdict": verdict,
            "score": 100 if verdict == "correct" else 0,
            "evaluator_type": "deterministic_math",
            "evaluator_version": "math-evaluator-v2",
            "confidence": 1.0,
        },
        response_time_ms=120,
        used_hint=False,
        attempt_id=attempt_id,
        enqueue_cognitive_projection=False,
        cognitive_extractor_version=EXTRACTOR,
        cognitive_model_version=MODEL,
    )


def test_prepared_window_flows_through_formal_proposal_claim_and_resolves(
    monkeypatch: pytest.MonkeyPatch, prepared_fixture: _Fixture
) -> None:
    monkeypatch.setenv(DEV_ENV, "1")
    result = _call(prepared_fixture, apply=True)
    tracker = prepared_fixture.runtime.tracker_module.KnowledgeTracker(
        prepared_fixture.store,
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
        cognitive_extractor=_UnusedExtractor(),
    )
    proposals = tracker.propose_cognitive_retention_actions(
        as_of=_utc(result["not_before"]) + timedelta(microseconds=1)
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    claims = prepared_fixture.store.claim_cognitive_obligations(
        worker_id="development-retention-worker",
        lease_seconds=3600,
        as_of=_utc(result["not_before"]) + timedelta(microseconds=1),
        obligation_types=("retention",),
        obligation_ids=(proposal.obligation_id,),
    )
    assert len(claims) == 1

    _answer_retention(
        prepared_fixture,
        proposal,
        claims[0],
        attempt_id="development-retention-resolved",
        verdict="correct",
    )

    episode = prepared_fixture.store.list_cognitive_monitoring_episodes()[0]
    obligation = prepared_fixture.store.list_cognitive_learning_obligations()[0]
    satisfaction = store_rows(prepared_fixture.store, "cognitive_obligation_satisfactions")[0]
    metadata = json.loads(satisfaction["metadata_json"])
    assert episode["status"] == "resolved"
    assert obligation["status"] == "completed"
    assert satisfaction["disposition"] == "resolved"
    assert metadata["certified"] is True


def test_relapse_uses_an_independent_fixture_and_never_reuses_resolved_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _runtime(monkeypatch, "independent_relapse")
    store = _open_store(runtime, tmp_path)
    fixture = _prepare_retention_off_transfer(runtime, store)
    monkeypatch.setenv(DEV_ENV, "1")
    try:
        result = _call(fixture, apply=True)
        obligation = store.list_cognitive_learning_obligations()[0]
        claims = store.claim_cognitive_obligations(
            worker_id="development-relapse-worker",
            lease_seconds=3600,
            as_of=_utc(result["not_before"]) + timedelta(microseconds=1),
            obligation_types=("retention",),
            obligation_ids=(obligation["obligation_id"],),
        )
        assert len(claims) == 1
        store.apply_cognitive_retention_disposition(
            obligation_id=obligation["obligation_id"],
            claim_token=claims[0]["claim_token"],
            worker_id=claims[0]["worker_id"],
            attempt_id="development-retention-relapse",
            disposition="relapse",
            occurred_at=_utc(result["not_before"]) + timedelta(seconds=1),
            metadata={"certified": True, "development_time_override": True},
        )

        episode = store.list_cognitive_monitoring_episodes()[0]
        assert episode["status"] == "relapsed"
        assert episode["relapse_count"] == 1
        assert all(row["status"] != "resolved" for row in store.list_cognitive_monitoring_episodes())
    finally:
        store.close()


def _load_entry(runtime: _Runtime, monkeypatch: pytest.MonkeyPatch) -> Any:
    common = ModuleType(f"{runtime.package_name}.entry_common")
    setattr(common, "Ok", lambda payload: payload)

    class SdkError(Exception):
        pass

    setattr(common, "SdkError", SdkError)
    setattr(
        common,
        "_entry_exception_error",
        lambda _owner, exc, **_kwargs: (_ for _ in ()).throw(exc),
    )
    setattr(common, "asyncio", asyncio)
    setattr(common, "plugin_entry", lambda **_kwargs: lambda function: function)
    setattr(common, "tr", lambda _key, *, default: default)

    class Ui:
        @staticmethod
        def action():
            def decorate(function: Any) -> Any:
                setattr(function, "_development_test_ui_action", True)
                return function

            return decorate

    setattr(common, "ui", Ui())
    monkeypatch.setitem(sys.modules, common.__name__, common)
    return importlib.import_module(f"{runtime.package_name}.entry_cognitive_entries")


def test_entry_requires_confirmation_and_respects_question_lifecycle(
    monkeypatch: pytest.MonkeyPatch, prepared_fixture: _Fixture
) -> None:
    monkeypatch.setenv(DEV_ENV, "1")
    entries = _load_entry(prepared_fixture.runtime, monkeypatch)

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _active_config(prepared_fixture.runtime)
        _store = prepared_fixture.store

    harness = Harness()
    assert not hasattr(
        entries._CognitiveEntriesMixin.study_cognitive_dev_prepare_retention,
        "_development_test_ui_action",
    )
    preview = asyncio.run(
        harness.study_cognitive_dev_prepare_retention(
            topic_id=TOPIC,
            hypothesis_code=HYPOTHESIS_CODE,
            apply=False,
        )
    )
    assert preview["status"] == "ready"
    assert preview["source_attempt_id"] == prepared_fixture.source_attempt_id
    missing_confirmation = asyncio.run(
        harness.study_cognitive_dev_prepare_retention(
            topic_id=TOPIC,
            hypothesis_code=HYPOTHESIS_CODE,
            expected_source_attempt_id=prepared_fixture.source_attempt_id,
            apply=True,
        )
    )
    assert missing_confirmation["reason_code"] == "confirmation_required"
    assert prepared_fixture.store.list_cognitive_monitoring_episodes() == []

    async def busy() -> dict[str, Any]:
        assert await entries.reserve_question_lifecycle(harness, "answer_evaluation") == ""
        try:
            return await harness.study_cognitive_dev_prepare_retention(
                topic_id=TOPIC,
                hypothesis_code=HYPOTHESIS_CODE,
                expected_source_attempt_id=prepared_fixture.source_attempt_id,
                apply=True,
                confirmation=CONFIRMATION,
            )
        finally:
            await entries.release_question_lifecycle(harness, "answer_evaluation")

    blocked = asyncio.run(busy())
    assert blocked["reason_code"] == "operation_busy"
    assert prepared_fixture.store.list_cognitive_monitoring_episodes() == []


@pytest.mark.parametrize("environment_value", [None, "true", " 1"])
def test_entry_environment_gate_requires_the_exact_value_one(
    monkeypatch: pytest.MonkeyPatch,
    prepared_fixture: _Fixture,
    environment_value: str | None,
) -> None:
    if environment_value is None:
        monkeypatch.delenv(DEV_ENV, raising=False)
    else:
        monkeypatch.setenv(DEV_ENV, environment_value)
    entries = _load_entry(prepared_fixture.runtime, monkeypatch)

    def must_not_call(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("disabled entry must not call the store helper")

    monkeypatch.setattr(
        prepared_fixture.runtime.development,
        "prepare_cognitive_retention_for_development",
        must_not_call,
    )

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _active_config(prepared_fixture.runtime)
        _store = prepared_fixture.store

    before = _logical_dump(prepared_fixture.store)
    result = asyncio.run(
        Harness().study_cognitive_dev_prepare_retention(
            topic_id=TOPIC,
            hypothesis_code=HYPOTHESIS_CODE,
            apply=False,
        )
    )
    assert result["status"] == "failed"
    assert result["reason_code"] == "dev_tools_disabled"
    assert result["enabled"] is False
    assert _logical_dump(prepared_fixture.store) == before
