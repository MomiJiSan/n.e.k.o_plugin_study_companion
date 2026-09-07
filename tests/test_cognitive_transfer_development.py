from __future__ import annotations

import asyncio
import importlib
import json
import threading

import pytest
from test_cognitive_active_question_entry import _active_subject, _targeted_context
from test_cognitive_retention_development import (
    HYPOTHESIS_CODE,
    TOPIC,
    _active_config,
    _logical_dump,
    _open_store,
    _prepare_retention_off_transfer,
    _runtime,
)


@pytest.fixture
def legacy(monkeypatch, tmp_path, request):
    runtime = _runtime(monkeypatch, request.node.name)
    store = _open_store(runtime, tmp_path)
    fixture = _prepare_retention_off_transfer(runtime, store)
    conn = store._require_conn()
    conn.execute(
        """UPDATE evaluations SET evaluation_json = ?, evaluator_type = 'llm_rubric',
           evaluator_version = 'legacy-v1', confidence = NULL WHERE attempt_id = ?""",
        (json.dumps({"verdict": "correct", "score": 100}), fixture.source_attempt_id),
    )
    # The scenario starts after a real repair and an old transfer. Add that
    # earlier repair to this synthetic fixture's ledger for blueprint validation.
    row = dict(conn.execute(
        "SELECT * FROM cognitive_intervention_events WHERE event_type = 'attempt_committed'"
    ).fetchone())
    row.pop("event_seq")
    row.update(event_id="prior-repair", decision_id="prior-repair-decision",
               attempt_id="attempt-transfer-hypothesis", learning_intent="misconception_repair",
               question_family_id="chain.cos-power.complete-inner",
               hypothesis_source_attempt_id="earlier-support")
    conn.execute(f"INSERT INTO cognitive_intervention_events ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
    conn.commit()
    monkeypatch.setenv("STUDY_COMPANION_COGNITIVE_DEV_TOOLS", "1")
    try:
        yield fixture
    finally:
        store.close()


def _modules(legacy):
    name = legacy.runtime.package_name
    return (importlib.import_module(f"{name}.store_cognitive_transfer_development"),
            importlib.import_module(f"{name}.entry_cognitive_transfer_development"))


def _subject(monkeypatch, legacy, **kwargs):
    subject, _ = _active_subject(monkeypatch, legacy.runtime.package_name, **kwargs)
    subject._store = legacy.store
    subject._cfg = _active_config(legacy.runtime)
    subject._state.current_question = {"question_id": "old-question", "attempt_evaluated": True}
    context = _targeted_context()
    context["selection_reason"] = "recommended"
    context["question_params"].pop("retry_wrong_question")
    subject._build_targeted_question_context = lambda: context
    return subject


async def _invoke(handler, subject, legacy, **kwargs):
    return await handler.retest_transfer(subject, **{
        "topic_id": TOPIC, "hypothesis_code": HYPOTHESIS_CODE,
        "expected_source_attempt_id": legacy.source_attempt_id, "apply": True,
        "confirmation": "RETEST_TRANSFER_FOR_DEVELOPMENT", **kwargs,
    })


def test_preview_is_read_only_and_keeps_legacy_evaluation(legacy):
    store_module, _ = _modules(legacy)
    before = _logical_dump(legacy.store)
    ready = store_module.preview_transfer_retest(legacy.store)
    assert ready["status"] == "ready"
    assert ready["source_attempt_id"] == legacy.source_attempt_id
    assert _logical_dump(legacy.store) == before
    assert legacy.runtime.development.prepare_cognitive_retention_for_development(
        legacy.store, topic_id=TOPIC, hypothesis_code=HYPOTHESIS_CODE,
        expected_source_attempt_id=legacy.source_attempt_id, apply=False,
    )["reason_code"] == "transfer_not_certified"


@pytest.mark.parametrize("failure", ["gate", "retention", "stale", "source", "certified", "outbox", "control"])
def test_preflight_rejects_missing_or_changed_authority(monkeypatch, legacy, failure):
    module, _ = _modules(legacy)
    conn = legacy.store._require_conn()
    source = legacy.source_attempt_id
    if failure == "gate":
        monkeypatch.delenv(module.DEV_ENV)
    elif failure == "retention":
        legacy.store.save_config(_active_config(legacy.runtime, retention_enabled=False))
    elif failure == "stale":
        conn.execute("UPDATE cognitive_topic_projection_queue SET requested_generation=requested_generation+1")
    elif failure == "source":
        source = "stale-request"
    elif failure == "certified":
        conn.execute("UPDATE evaluations SET confidence=0.9")
    elif failure == "outbox":
        conn.execute("UPDATE cognitive_outbox SET status='failed'")
    elif failure == "control":
        legacy.store.record_cognitive_user_control(topic_id=TOPIC, hypothesis_code=HYPOTHESIS_CODE, action="dismiss")
    conn.commit()
    before = _logical_dump(legacy.store)
    result = module.preview_transfer_retest(legacy.store, source)
    assert result["status"] == "blocked"
    assert _logical_dump(legacy.store) == before


@pytest.mark.asyncio
async def test_formal_delivery_audits_source_and_new_answer_opens_retention(monkeypatch, legacy):
    subject = _subject(monkeypatch, legacy)
    _, handler = _modules(legacy)
    before = legacy.store._require_conn().execute("SELECT * FROM evaluations ORDER BY attempt_id").fetchall()
    result = await _invoke(handler, subject, legacy)
    assert result["status"] == "generated", result
    assert result["question_id"] != "question-transfer"
    assert subject.private_payload["question"] == "Differentiate (x^2 + 3)^5."
    assert subject._agent.generated == 0
    event = legacy.store.list_cognitive_intervention_events(event_types=("question_committed",))[-1]
    assert event["metadata"]["legacy_source_attempt_id"] == legacy.source_attempt_id
    assert event["metadata"]["development_transfer_retest"] is True
    assert "answer" not in result
    assert legacy.store._require_conn().execute("SELECT * FROM evaluations ORDER BY attempt_id").fetchall() == before
    again = await _invoke(handler, subject, legacy)
    assert again["status"] == "blocked"
    assert len(legacy.store.list_cognitive_intervention_events(event_types=("question_committed",))) == 2
    tracker = legacy.runtime.tracker_module.KnowledgeTracker(
        legacy.store, cognitive_config={"projection_enabled": True, "read_mode": "active",
        "intent_policy": "on", "retention_enabled": True, "version_set": "cognitive-v2.1-1",
        "model_version": "cognitive-v2.1-1", "supported_topics": [TOPIC]},
    )
    tracker.on_answer(
        topic_id=TOPIC, question=subject.private_payload, user_answer="10*x*(x^2+3)^4",
        eval_result={"verdict": "correct", "score": 100, "evaluator_type": "llm_rubric",
                     "evaluator_version": "llm-rubric-v2", "confidence": 0.95},
        mode="companion", session_id="fresh-human-fixture", used_hint=False,
        attempt_id=result["attempt_id"], require_existing_topic=True,
    )
    episodes = legacy.store.list_cognitive_monitoring_episodes()
    assert len(episodes) == 1
    assert episodes[0]["source_attempt_id"] == result["attempt_id"]
    assert len(legacy.store.list_cognitive_learning_obligations()) == 1
    assert not legacy.store.list_cognitive_outbox(status="failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["gate", "config", "source"])
async def test_transaction_rechecks_after_proposal(monkeypatch, legacy, change):
    subject = _subject(monkeypatch, legacy)
    module, handler = _modules(legacy)
    original = legacy.store.record_cognitive_intervention_event

    def changed(event):
        if event["event_type"] == "question_committed":
            if change == "gate":
                monkeypatch.delenv(module.DEV_ENV)
            elif change == "config":
                legacy.store.save_config(_active_config(legacy.runtime, retention_enabled=False))
            else:
                legacy.store._require_conn().execute("UPDATE cognitive_hypothesis_current SET source_attempt_id='replacement'")
                legacy.store._require_conn().commit()
        return original(event)

    monkeypatch.setattr(legacy.store, "record_cognitive_intervention_event", changed)
    result = await _invoke(handler, subject, legacy)
    assert result["status"] == "blocked"
    assert subject.private_payload is None
    assert subject._agent.generated == 0
    assert len(legacy.store.list_cognitive_intervention_events(event_types=("question_committed",))) == 1


@pytest.mark.asyncio
async def test_confirmation_busy_and_cancellation_keep_lifecycle_owned(monkeypatch, legacy):
    subject = _subject(monkeypatch, legacy)
    _, handler = _modules(legacy)
    missing = await _invoke(handler, subject, legacy, confirmation="")
    assert missing["reason_code"] == "confirmation_required"
    started, finish = asyncio.Event(), asyncio.Event()

    async def paused(*args, **kwargs):
        started.set()
        await finish.wait()

    monkeypatch.setattr(subject, "_build_learning_context", paused)
    task = asyncio.create_task(_invoke(handler, subject, legacy))
    await asyncio.wait_for(started.wait(), 2)
    busy = await _invoke(handler, subject, legacy)
    assert busy["reason_code"] == "operation_busy"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not subject._tutor_question_lifecycle_operation
    assert len(legacy.store.list_cognitive_intervention_events(event_types=("question_committed",))) == 1


@pytest.mark.asyncio
async def test_generation_failure_never_falls_back_to_ordinary_question(monkeypatch, legacy):
    subject = _subject(monkeypatch, legacy, finalize_failure=RuntimeError("delivery failed"))
    _, handler = _modules(legacy)
    result = await _invoke(handler, subject, legacy)
    assert result["status"] == "blocked"
    assert subject._agent.generated == 0
    assert legacy.store.list_cognitive_intervention_events(event_types=("intervention_abandoned",))


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [("question", "question_active"), ("selection", "selection_conflict")])
async def test_retest_preserves_existing_question_and_coach_selection(monkeypatch, legacy, change, reason):
    subject = _subject(monkeypatch, legacy)
    _, handler = _modules(legacy)
    if change == "question":
        subject._state.current_question = {"question_id": "in-progress"}
    else:
        subject._build_targeted_question_context = lambda: {"selected_topic_id": "another-topic"}
    before = _logical_dump(legacy.store)
    assert (await _invoke(handler, subject, legacy))["reason_code"] == reason
    assert _logical_dump(legacy.store) == before


@pytest.mark.asyncio
async def test_two_owners_cannot_commit_two_retests_for_one_source(monkeypatch, legacy):
    first = _subject(monkeypatch, legacy)
    second = _subject(monkeypatch, legacy)
    _, handler = _modules(legacy)
    original = legacy.store.record_cognitive_intervention_event
    barrier = threading.Barrier(2)

    def simultaneous(event):
        if event["event_type"] == "question_committed":
            barrier.wait(timeout=5)
        return original(event)

    monkeypatch.setattr(legacy.store, "record_cognitive_intervention_event", simultaneous)
    results = await asyncio.gather(_invoke(handler, first, legacy), _invoke(handler, second, legacy))
    assert sorted(result["status"] for result in results) == ["blocked", "generated"]
    assert len(legacy.store.list_cognitive_intervention_events(event_types=("question_committed",))) == 2


@pytest.mark.asyncio
async def test_cancelled_delivery_abandons_new_question_and_releases_owner(monkeypatch, legacy):
    subject = _subject(monkeypatch, legacy, finalize_failure=asyncio.CancelledError())
    _, handler = _modules(legacy)
    with pytest.raises(asyncio.CancelledError):
        await _invoke(handler, subject, legacy)
    assert not subject._tutor_question_lifecycle_operation
    assert legacy.store.list_cognitive_intervention_events(event_types=("intervention_abandoned",))


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,reason", [
    ({"topic_id": "another"}, "unsupported_target"),
    ({"expected_source_attempt_id": ""}, "source_attempt_mismatch"),
    ({"apply": "true"}, "unsupported_target"),
])
async def test_entry_rejects_invalid_arguments_before_writes(monkeypatch, legacy, arguments, reason):
    subject = _subject(monkeypatch, legacy)
    _, handler = _modules(legacy)
    before = _logical_dump(legacy.store)
    assert (await _invoke(handler, subject, legacy, **arguments))["reason_code"] == reason
    assert before == _logical_dump(legacy.store)


@pytest.mark.asyncio
async def test_entry_preview_and_disabled_gate_do_not_write(monkeypatch, legacy):
    subject = _subject(monkeypatch, legacy)
    module, handler = _modules(legacy)
    before = _logical_dump(legacy.store)
    assert (await _invoke(handler, subject, legacy, apply=False))["status"] == "ready"
    monkeypatch.delenv(module.DEV_ENV)
    assert (await _invoke(handler, subject, legacy))["reason_code"] == "dev_tools_disabled"
    assert before == _logical_dump(legacy.store)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["retry", "due_review"])
async def test_same_topic_coach_work_can_merge_without_losing_ownership(monkeypatch, legacy, reason):
    subject = _subject(monkeypatch, legacy)
    _, handler = _modules(legacy)
    context = _targeted_context()
    context["selection_reason"] = reason
    subject._build_targeted_question_context = lambda: context
    result = await _invoke(handler, subject, legacy)
    assert result["status"] == "generated", result
    event = legacy.store.list_cognitive_intervention_events(event_types=("question_committed",))[-1]
    assert event["binding"]["topic_id"] == TOPIC
    assert event["binding"]["selection_reason"] == ("wrong_retry" if reason == "retry" else reason)
    assert event["binding"]["scope_key"] == context["scope_key"]
    assert event["binding"]["learning_plan_id"] == context["learning_plan_id"]
    if reason == "retry":
        assert event["binding"]["origin_wrong_question_id"] == "wrong-a"
