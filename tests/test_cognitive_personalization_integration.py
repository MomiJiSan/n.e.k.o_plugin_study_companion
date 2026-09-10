from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# isort: off
import pytest
from adaptive_learning.cognitive_personalization import VERSION_SET
from adaptive_learning.cognitive_strategy_report import build_cognitive_strategy_report
from store_cognitive_strategy import insert_cognitive_strategy_exposure, insert_cognitive_strategy_fact
# isort: on

# isort: split
from test_cognitive_active_question_entry import _active_subject, _generate
from test_cognitive_answer_event_integration import _load_runtime, _Logger, _open_store, _UnusedExtractor
from test_cognitive_personalization import evidence
from test_cognitive_settings import _load_models, _load_status_entries
from test_cognitive_strategy_rotation import _decision
from test_cognitive_strategy_store import _exposure, _fact


def _config(**changes):
    return SimpleNamespace(
        **{
            "projection_enabled": True,
            "read_mode": "active",
            "intent_policy": "on",
            "strategy_shadow_enabled": True,
            "version_set": VERSION_SET,
            "strategy_personalization_enabled": True,
            "strategy_personalization_exploration_enabled": False,
            "strategy_personalization_stopped": False,
            **changes,
        }
    )


def _seed(store, now):
    """Synthetic evidence only, inserted through the production V3 ledger API."""
    conn = store._require_conn()
    for index, row in enumerate(evidence()["exposures"]):
        alternate = index >= 20
        occurred = now - timedelta(days=3, minutes=index)
        cursor = conn.execute(
            "INSERT INTO cognitive_fact_roots(fact_type,source_id,effective_at) VALUES('synthetic',?,?)",
            (f"seed-{index}", occurred.isoformat()),
        )
        exposure = insert_cognitive_strategy_exposure(
            store,
            conn,
            _exposure(
                f"seed-question-{index}",
                root_fact_seq=cursor.lastrowid,
                hypothesis_id="hypothesis-active",
                version_set_id=VERSION_SET,
                strategy_id=row["strategy_id"],
                strategy_version="v1",
                learner_id="local",
                catalog_version="cognitive-strategy-catalog-v1",
                difficulty_bucket="3",
                strategy_family="minimal_change" if alternate else "complete_steps",
                blueprint_id="chain.omit-inner.minimal-change.v1" if alternate else "chain.omit-inner.fill-factor.v1",
                question_family_id="chain.sin-power.minimal-change" if alternate else "chain.cos-cube.fill-factor",
                baseline=not alternate,
                occurred_at=occurred.isoformat(),
                answer_window_expires_at=(occurred + timedelta(days=1)).isoformat(),
            ),
        )
        for offset, kind in enumerate(("attempt", "transfer", "episode", "retention"), 1):
            at = occurred + timedelta(hours=26 if kind == "retention" else offset)
            seq = conn.execute(
                "INSERT INTO cognitive_fact_roots(fact_type,source_id,effective_at) VALUES('synthetic',?,?)",
                (f"seed-{index}-{kind}", at.isoformat()),
            ).lastrowid
            insert_cognitive_strategy_fact(
                store,
                conn,
                _fact(
                    exposure["exposure_id"],
                    kind,
                    seq,
                    source_id=f"seed-{index}-{kind}",
                    occurred_at=at.isoformat(),
                    episode_id=f"seed-episode-{index}",
                    outcome="correct" if index < 2 or alternate else "wrong",
                    status="observed",
                    certified=True,
                    provenance_complete=True,
                    used_hint=False,
                    interval_hours=26 if kind == "retention" else None,
                    independent_family=True,
                    obligation_completed=True,
                    independence_known=True,
                    answer_disclosed=False,
                    development_time_override=False,
                    evaluator_type="deterministic",
                    evaluator_version="synthetic-v1",
                    evaluator_confidence=1.0,
                ),
            )
    conn.commit()


def _subject(monkeypatch, tmp_path, name):
    subject, _ = _active_subject(monkeypatch, name)
    store_module, tracker_module = _load_runtime(monkeypatch, name + "_store")
    store = _open_store(tmp_path, store_module.StudyStore)
    store.ensure_topic(topic_id="college_chain_rule", name="Chain rule")
    conn = store._require_conn()
    # Adapt the existing synthetic current-state fixture to the supported V2.1 set.
    conn.execute(
        "UPDATE cognitive_hypothesis_current SET topic_id='college_chain_rule', hypothesis_id='hypothesis-active', model_version=?, source_snapshot_id='snapshot-active', projected_generation=9",
        (VERSION_SET,),
    )
    conn.execute(
        "UPDATE cognitive_topic_projection_queue SET topic_id='college_chain_rule', model_version=?, requested_generation=9, claimed_generation=9, projected_generation=9",
        (VERSION_SET,),
    )
    conn.commit()
    _seed(store, datetime.now(timezone.utc))
    subject._store = store
    subject._cfg = SimpleNamespace(cognitive=_config())
    original = subject._knowledge_tracker.propose_cognitive_intent

    def propose(plan):
        decision = original(plan)
        hypothesis = replace(decision.selected_hypothesis, model_version=VERSION_SET)
        proposed = replace(decision.proposed_plan, hypothesis_target=hypothesis)
        return replace(decision, selected_hypothesis=hypothesis, proposed_plan=proposed, effective_plan=proposed)

    subject._knowledge_tracker.propose_cognitive_intent = propose
    subject._knowledge_tracker.cognitive_strategy_shadow_enabled = True
    subject._knowledge_tracker.cognitive_strategy_rotation_enabled = True
    subject._knowledge_tracker._cognitive_version_set_id = VERSION_SET
    return subject, store, tracker_module


def test_real_entry_delivery_answer_and_next_report_use_the_personalized_strategy(monkeypatch, tmp_path):
    subject, store, tracker_module = _subject(monkeypatch, tmp_path, "_personalization_e2e")
    try:
        before = store.build_cognitive_strategy_report_snapshot()
        payload = asyncio.run(_generate(subject))
        assert "Change only the" in payload["question"], payload
        events = store.list_cognitive_intervention_events()
        assert [e["event_type"] for e in events] == ["intent_proposed", "question_committed"]
        committed = events[-1]
        audit = committed["metadata"]["strategy_personalization"]
        assert audit["reason"] == "supported_alternate"
        assert events[0]["metadata"]["strategy_personalization"] == audit
        assert len(store.list_cognitive_strategy_exposures()) == 41
        # Exact transaction replay is idempotent; a concurrent new question
        # cannot reuse the pre-delivery history decision.
        store.record_cognitive_intervention_event(committed)
        stale = {**committed, "event_id": "stale-new-event", "question_id": "stale-new-question"}
        with pytest.raises(ValueError, match="history|future delivery"):
            store.record_cognitive_intervention_event(stale)
        assert len(store.list_cognitive_strategy_exposures()) == 41
        before_answer = store.build_cognitive_strategy_report_snapshot()
        assert len(before_answer["exposures"]) == len(before["exposures"]) + 1
        tracker = tracker_module.KnowledgeTracker(
            store,
            logger=_Logger(),
            cognitive_config={
                "projection_enabled": True,
                "read_mode": "active",
                "intent_policy": "on",
                "version_set": VERSION_SET,
                "model_version": VERSION_SET,
                "supported_topics": ["college_chain_rule"],
            },
            cognitive_extractor=_UnusedExtractor(),
        )
        question = dict(subject.private_payload)
        question["question_id"] = committed["question_id"]
        question["target_binding"] = {
            **question.get("target_binding", {}),
            "target_topic_id": "college_chain_rule",
            "validation_status": "passed",
            "cognitive_learning_intent": "misconception_repair",
            "cognitive_decision_id": committed["decision_id"],
            "diagnostic_validation_id": committed["diagnostic_validation_id"],
        }
        tracker.on_answer(
            topic_id="college_chain_rule",
            question=question,
            user_answer="synthetic answer",
            eval_result={
                "verdict": "wrong",
                "score": 0,
                "evaluator_type": "deterministic",
                "evaluator_version": "synthetic-v1",
                "confidence": 1.0,
            },
            mode="companion",
            session_id="synthetic-personalization",
            response_time_ms=1000,
            used_hint=False,
            require_existing_topic=True,
            attempt_id="personalized-answer",
        )
        after = store.build_cognitive_strategy_report_snapshot()
        record = next(row for row in after["exposures"] if row["source_id"] == committed["question_id"])
        assert record["outcomes"]["immediate"]["success"] is False
        assert (
            build_cognitive_strategy_report(after)["content_sha256"]
            != build_cognitive_strategy_report(before_answer)["content_sha256"]
        )
        # The production history reader observes the newly committed wrong answer.
        persistence = importlib.import_module(f"{tracker_module.__package__}.store_cognitive_personalization")
        history = persistence.personalization_history(
            store._require_conn(), "hypothesis-active", datetime.now(timezone.utc)
        )
        assert history["alternate_n"] == 1
        assert history["consecutive_failures"] == 1
        runtime = importlib.import_module("_personalization_e2e.cognitive_personalization_runtime")
        status = runtime.personalization_status(subject)
        assert status["status"] == "active_alternate"
        assert status["switches"] == {
            "strategy_personalization_enabled": True,
            "strategy_personalization_exploration_enabled": False,
            "strategy_personalization_stopped": False,
        }
        assert status["current_strategy"] == "alternate"
        assert status["decision_reason"] == "supported_alternate"
        assert status["alternate_deliveries_7d"] == 1
        assert status["consecutive_alternate_failures"] == 1
        assert status["stopped"] is False
        subject._cfg.cognitive.strategy_personalization_stopped = True
        stopped = runtime.personalization_status(subject)
        assert stopped["status"] == "stopped"
        assert stopped["decision_reason"] == "user_stopped"
        assert stopped["last_decision_reason"] == "supported_alternate"
        assert stopped["current_strategy"] == "baseline"
        assert stopped["last_delivered_strategy"] == "alternate"
        assert stopped["stopped"] is True
        subject._cfg.cognitive.strategy_personalization_stopped = False
        _, updated_audit = runtime.personalize_candidate(subject, _decision())
        assert updated_audit["report_sha256"] != audit["report_sha256"]
        store.close()
        store.open()
        restarted = persistence.personalization_history(
            store._require_conn(), "hypothesis-active", datetime.now(timezone.utc)
        )
        assert restarted["alternate_n"] == 1
        assert restarted["consecutive_failures"] == 1
    finally:
        store.close()


@pytest.mark.parametrize("case", ["stopped", "disabled_evidence", "fault", "coach_rejected", "gate_changed"])
def test_runtime_fallback_and_coach_veto(monkeypatch, tmp_path, case):
    name = "_personalization_fallback_" + case
    subject, store, _ = _subject(monkeypatch, tmp_path, name)
    runtime = importlib.import_module(name + ".cognitive_personalization_runtime")
    try:
        if case == "stopped":
            subject._cfg.cognitive.strategy_personalization_stopped = True
        elif case == "disabled_evidence":
            subject._cfg.cognitive.strategy_shadow_enabled = False
        elif case == "fault":

            def fail(*args, **kwargs):
                raise RuntimeError("synthetic read failure")

            monkeypatch.setattr(runtime, "decide_from_store", fail)
        elif case == "coach_rejected":
            monkeypatch.setitem(
                subject._generate_question_payload_impl.__func__.__globals__,
                "merge_learning_action_candidates",
                lambda original, *args, **kwargs: original,
            )
        elif case == "gate_changed":
            original = store.record_cognitive_intervention_event

            def stop_after_proposal(event):
                result = original(event)
                if event["event_type"] == "intent_proposed":
                    subject._cfg.cognitive.strategy_personalization_stopped = True
                return result

            monkeypatch.setattr(store, "record_cognitive_intervention_event", stop_after_proposal)
        payload = asyncio.run(_generate(subject))
        assert "Change only the" not in payload["question"]
        committed = [e for e in store.list_cognitive_intervention_events() if e["event_type"] == "question_committed"]
        assert all(e["repair_strategy"] != "minimal_change" for e in committed)
        if case == "coach_rejected":
            assert committed == []
            assert len(store.list_cognitive_strategy_exposures()) == 40
    finally:
        store.close()


def test_config_stop_roundtrip_and_strict_flags(monkeypatch):
    models, name = _load_models(monkeypatch, "_personalization_config")
    entries = _load_status_entries(monkeypatch, name, models)
    assert models.CognitiveConfig().strategy_personalization_enabled is False
    assert models.CognitiveConfig(strategy_personalization_enabled="true").strategy_personalization_enabled is False
    assert models.CognitiveConfig(strategy_personalization_stopped="false").strategy_personalization_stopped is True
    assert (
        models.CognitiveConfig(
            version_set="unknown", strategy_personalization_enabled=True
        ).strategy_personalization_enabled
        is False
    )
    changed = entries._apply_settings_config(
        models.StudyConfig(),
        {
            "cognitive": {
                "strategy_personalization_enabled": True,
                "strategy_personalization_stopped": True,
            }
        },
    )
    encoded = json.loads(json.dumps(entries._settings_config_payload(changed)))
    assert models.build_config(encoded).cognitive.strategy_personalization_stopped is True


def test_new_flags_preserve_legacy_config_positional_arguments(monkeypatch):
    models, _ = _load_models(monkeypatch, "_personalization_config_positional")
    config = models.CognitiveConfig(
        False,
        "off",
        "off",
        False,
        False,
        False,
        False,
        True,
        VERSION_SET,
        "",
        ("calculus.chain_rule",),
    )
    assert config.knowledge_graph_enabled is True
    assert config.version_set == VERSION_SET
    assert config.supported_topics == ("calculus.chain_rule",)
    assert config.strategy_personalization_enabled is False


def test_user_stop_also_cancels_an_in_flight_legacy_rotation(monkeypatch):
    subject, _ = _active_subject(monkeypatch, "_personalization_stop_rotation")
    subject._cfg = SimpleNamespace(cognitive=_config(strategy_personalization_enabled=False))
    tracker = subject._knowledge_tracker
    original = tracker.propose_cognitive_intent

    def propose(plan):
        decision = original(plan)
        hypothesis = replace(decision.selected_hypothesis, source_attempt_id="attempt-a")
        proposed = replace(decision.proposed_plan, hypothesis_target=hypothesis)
        return replace(decision, selected_hypothesis=hypothesis, proposed_plan=proposed, effective_plan=proposed)

    tracker.propose_cognitive_intent = propose
    tracker.cognitive_strategy_shadow_enabled = True
    tracker.cognitive_strategy_rotation_enabled = True
    tracker._cognitive_version_set_id = VERSION_SET
    subject._store.list_cognitive_intervention_events = lambda **kwargs: []
    record = subject._store.record_cognitive_intervention_event

    def stop_after_proposal(event):
        result = record(event)
        if event["event_type"] == "intent_proposed":
            assert event["repair_strategy"] == "minimal_change"
            subject._cfg.cognitive.strategy_personalization_stopped = True
        return result

    subject._store.record_cognitive_intervention_event = stop_after_proposal
    result = asyncio.run(_generate(subject))
    assert "Change only the" not in result["question"]
    assert not any(e["event_type"] == "question_committed" for e in subject._store.events)


def test_consecutive_failures_follow_delivery_time_not_question_id(monkeypatch, tmp_path):
    name = "_personalization_failure_order"
    subject, store, _ = _subject(monkeypatch, tmp_path, name)
    persistence = importlib.import_module(name + ".store_cognitive_personalization")
    try:
        asyncio.run(_generate(subject))
        latest = store.list_cognitive_intervention_events()[-1]
        store.record_cognitive_intervention_event(
            {
                **latest,
                "event_id": "latest-correct-attempt-event",
                "event_type": "attempt_committed",
                "attempt_id": "latest-correct-attempt",
                "evaluation_verdict": "correct",
                "metadata": {},
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        conn = store._require_conn()
        conn.execute(
            "UPDATE cognitive_topic_projection_queue SET status='done', requested_generation=9, projected_generation=9"
        )
        conn.commit()
        older = {
            **latest,
            "event_id": "older-wrong-question-event",
            "question_id": "zzzz-lexically-last-but-older",
            "metadata": {},
            "created_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        }
        store.record_cognitive_intervention_event(older)
        store.record_cognitive_intervention_event(
            {
                **older,
                "event_id": "older-wrong-attempt-event",
                "event_type": "attempt_committed",
                "attempt_id": "older-wrong-attempt",
                "evaluation_verdict": "wrong",
                "created_at": (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
            }
        )

        history = persistence.personalization_history(
            conn, "hypothesis-active", datetime.now(timezone.utc)
        )

        assert history["alternate_n"] == 2
        assert history["consecutive_failures"] == 0
    finally:
        store.close()


def test_canonical_answers_stop_personalization_when_attempt_event_delivery_is_missing(
    monkeypatch, tmp_path
):
    name = "_personalization_missing_attempt_event"
    subject, store, _ = _subject(monkeypatch, tmp_path, name)
    runtime = importlib.import_module(name + ".cognitive_personalization_runtime")
    persistence = importlib.import_module(name + ".store_cognitive_personalization")
    try:
        for index in range(2):
            asyncio.run(_generate(subject))
            committed = store.list_cognitive_intervention_events()[-1]
            store.batch_write_answer_data(
                session_id="canonical-answer-fallback",
                mode="companion",
                topic_id="college_chain_rule",
                question={
                    "question_id": committed["question_id"],
                    "question": "Synthetic committed repair question",
                    "answer": "synthetic",
                    "question_type": "math_exact",
                    "difficulty": 3,
                },
                user_answer="synthetic wrong answer",
                eval_result={
                    "verdict": "wrong",
                    "score": 0,
                    "evaluator_type": "deterministic",
                    "evaluator_version": "synthetic-v1",
                    "confidence": 1.0,
                },
                response_time_ms=100,
                used_hint=False,
                attempt_id=f"canonical-only-attempt-{index}",
            )
            conn = store._require_conn()
            conn.execute(
                "UPDATE cognitive_topic_projection_queue SET status='done', requested_generation=9, projected_generation=9"
            )
            conn.commit()

        assert store.list_cognitive_intervention_events(
            event_types=("attempt_committed",)
        ) == []
        history = persistence.personalization_history(
            store._require_conn(), "hypothesis-active", datetime.now(timezone.utc)
        )
        assert history["consecutive_failures"] == 2
        selected, audit = runtime.personalize_candidate(subject, _decision())
        assert selected.repair_strategy == "complete_inner_derivative"
        assert audit["reason"] == "failure_stop"
    finally:
        store.close()


@pytest.mark.parametrize("failure_verdict", [None, "wrong", "partial", "dont_know"])
def test_persisted_limits_include_legacy_rotation_and_survive_restart(
    monkeypatch, tmp_path, failure_verdict
):
    name = "_personalization_limit_" + str(failure_verdict)
    subject, store, _ = _subject(monkeypatch, tmp_path, name)
    runtime = importlib.import_module(name + ".cognitive_personalization_runtime")
    try:
        asyncio.run(_generate(subject))
        template = store.list_cognitive_intervention_events()[-1]
        conn = store._require_conn()
        count = 2 if failure_verdict else 3
        for index in range(count):
            if index == 0:
                event = template
            else:
                # A synthetic previous rotation delivery uses the ordinary
                # canonical writer; it carries no personalization metadata.
                conn.execute(
                    "UPDATE cognitive_topic_projection_queue SET status='done', requested_generation=9, projected_generation=9"
                )
                conn.commit()
                event = {
                    **template,
                    "event_id": f"limit-question-event-{index}",
                    "question_id": f"limit-question-{index}",
                    "metadata": {},
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                store.record_cognitive_intervention_event(event)
            if failure_verdict:
                store.record_cognitive_intervention_event(
                    {
                        **event,
                        "event_id": f"limit-attempt-event-{index}",
                        "event_type": "attempt_committed",
                        "attempt_id": f"limit-attempt-{index}",
                        "evaluation_verdict": failure_verdict,
                        "metadata": {},
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
        store.close()
        store.open()
        selected, audit = runtime.personalize_candidate(subject, _decision())
        assert audit["reason"] == (
            "failure_stop" if failure_verdict else "exposure_limit"
        )
        assert selected.repair_strategy == "complete_inner_derivative"
        # Reopening or enabling exploration never resets the canonical budget.
        subject._cfg.cognitive.strategy_personalization_exploration_enabled = True
        assert runtime.personalize_candidate(subject, _decision())[1]["reason"] == audit["reason"]
    finally:
        store.close()
