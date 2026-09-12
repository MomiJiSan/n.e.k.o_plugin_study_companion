from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    name = "_retention_trial_test"
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, name, package)
    mode = ModuleType(name + ".mode_manager")
    mode.normalize_mode = lambda value: str(value or "companion")
    monkeypatch.setitem(sys.modules, mode.__name__, mode)
    module = importlib.import_module(name + ".store_mastery_retention")
    clock = [1_800_000_000.0]
    monkeypatch.setattr(module, "utc_timestamp", lambda: clock[0])
    logger = type("Logger", (), {key: lambda *args, **kwargs: None for key in
                                 ("debug", "info", "warning", "error", "exception")})()
    store = importlib.import_module(name + ".store").StudyStore(tmp_path / "data.db", tmp_path / "seed.json", logger)
    store.open()
    store.ensure_topic(topic_id="topic", name="Topic", subject="math")
    yield store, clock, module
    store.close()


def answer(store, attempt="a", **changes):
    kwargs = dict(session_id="s", mode="companion", topic_id="topic",
                  question={"question_id": attempt, "question": "2 + 2?"},
                  user_answer="4", eval_result=dict(verdict="correct", score=100,
                  evaluator_type="llm_rubric", confidence=1.0), response_time_ms=None,
                  attempt_id=attempt, used_hint=False)
    kwargs.update(changes)
    return store.batch_write_answer_data(**kwargs)


def topic(store):
    return next(row for row in store.get_learning_card_snapshot()["topics"] if row["topic_id"] == "topic")


def test_expiry_before_answer_reacquires_new_generation_and_retry_is_original(runtime):
    store, clock, _ = runtime
    first = answer(store)["retention_mastery"]
    assert first["owned"] and first["generation"] == 1
    clock[0] += 100 * 86400
    second = answer(store, "b")["retention_mastery"]
    assert second["generation"] == 2 and second["owned"]
    before_retry = store.get_learning_card_snapshot()
    assert answer(store, "b")["duplicate_attempt"]
    assert store.get_learning_card_snapshot() == before_retry
    events = store._require_conn().execute("SELECT event,generation FROM learning_card_events").fetchall()
    assert [tuple(row) for row in events] == [("acquired", 1), ("destroyed", 1), ("acquired", 2)]


def test_immediate_identical_exercises_do_not_inflate_mastery_or_half_life(runtime):
    store, _, _ = runtime
    first = answer(store)["retention_mastery"]
    for i in range(20):
        latest = answer(store, f"new-{i}", session_id=f"reset-{i}")["retention_mastery"]
        assert latest["half_life"] == 7
        assert latest["mastery"] == first["mastery"]
        assert latest["attempts"] == 1


def test_decay_once_reads_and_clock_rollback_do_not_refresh_anchor(runtime):
    store, clock, _ = runtime
    first = answer(store)["retention_mastery"]
    clock[0] += 7 * 86400
    half = topic(store)
    assert half["mastery"] == pytest.approx(first["mastery"] / 2)
    assert topic(store) == half
    clock[0] -= 3 * 86400
    assert topic(store) == half
    clock[0] += 10 * 86400
    assert topic(store)["mastery"] == pytest.approx(first["mastery"] / 4)


@pytest.mark.parametrize("verdict,score,hint,confidence", [
    ("wrong", 0, False, 1), ("dont_know", 0, False, 1),
    ("partial", 10, False, 1), ("correct", 10, True, 1),
])
def test_low_quality_answer_does_not_restore_old_high_baseline(runtime, verdict, score, hint, confidence):
    store, clock, _ = runtime
    for i in range(10):
        answer(store, str(i), question={"question_id": str(i), "question": f"different {i}"})
    clock[0] += 35 * 86400
    before = topic(store)["mastery"]
    result = answer(store, "new", eval_result=dict(verdict=verdict, score=score,
                    evaluator_type="llm_rubric", confidence=confidence), used_hint=hint)["retention_mastery"]
    if verdict in {"wrong", "dont_know"}:
        assert result["mastery"] <= before
    else:
        assert result["mastery"] < .1


@pytest.mark.parametrize("bad", [
    {"verdict": "correct", "score": 100},
    {"verdict": "correct", "score": 100, "evaluator_type": "self_report"},
    {"verdict": "wrong", "score": 0, "evaluator_type": "llm_rubric", "fallback_reason": "timeout"},
    {"verdict": "correct", "score": float("nan"), "evaluator_type": "llm_rubric"},
])
def test_invalid_evidence_cannot_leak_through_legacy_migration(runtime, bad):
    store, _, _ = runtime
    answer(store, eval_result=bad, mastery_snapshot={"mastery": .9})
    assert topic(store)["mastery"] is None
    assert not topic(store)["owned"]


def test_missing_confidence_and_hint_do_not_change_half_life(runtime):
    store, clock, _ = runtime
    answer(store)
    clock[0] += 7 * 86400
    payload = dict(verdict="correct", score=100, evaluator_type="llm_rubric")
    assert answer(store, "b", eval_result=payload)["retention_mastery"]["half_life"] == 7
    clock[0] += 7 * 86400
    payload.update(verdict="wrong", score=0, confidence=1)
    assert answer(store, "c", eval_result=payload, used_hint=None)["retention_mastery"]["half_life"] == 7


def test_transaction_rolls_back_facts_and_lifecycle_together(runtime, monkeypatch):
    store, _, module = runtime
    original = store._batch_write_fsrs_card
    def fail(*args, **kwargs):
        raise RuntimeError("write failed")
    monkeypatch.setattr(store, "_batch_write_fsrs_card", fail)
    with pytest.raises(RuntimeError, match="write failed"):
        answer(store)
    assert store._require_conn().execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    assert store._require_conn().execute("SELECT count(*) FROM learning_card_events").fetchone()[0] == 0
    monkeypatch.setattr(store, "_batch_write_fsrs_card", original)
    assert answer(store)["retention_mastery"]["generation"] == 1


def test_snapshot_all_topics_and_dataset_identity_survives_reopen(runtime):
    store, _, _ = runtime
    for i in range(900):
        store.ensure_topic(topic_id=f"topic-{i}", name=str(i), subject=f"subject-{i % 11}")
    first = store.get_learning_card_snapshot()
    assert len(first["topics"]) == 901
    store.close()
    store.open()
    assert store.get_learning_card_snapshot() == first


def test_invalid_persisted_state_is_error_not_destruction(runtime):
    store, _, _ = runtime
    answer(store)
    conn = store._require_conn()
    conn.execute("UPDATE mastery_retention_state SET half_life=-1")
    conn.commit()
    with pytest.raises(ValueError, match="half life"):
        store.get_learning_card_snapshot()
    assert conn.execute("SELECT owned FROM mastery_retention_state").fetchone()[0] == 1


def test_math_boundaries_and_elapsed_feedback(runtime):
    _, _, module = runtime
    pure = importlib.import_module(module.__package__ + ".adaptive_learning.mastery_retention")
    assert pure.current_mastery(.001, 7, 0) == .001
    assert pure.current_mastery(math.nextafter(.001, 0), 7, 0) == 0
    assert pure.feedback_half_life(7, 7, "correct", 1, False) == 10.5
    assert pure.feedback_half_life(7, 7, "wrong", 1, False) == 5.25


def test_purge_changes_dataset_identity_and_cannot_reuse_old_generation(runtime):
    store, _, _ = runtime
    answer(store)
    old = store.get_learning_card_snapshot()["dataset_id"]
    store.purge_all()
    new = store.get_learning_card_snapshot()
    assert new["dataset_id"] != old
    assert new["topics"] == []
    assert store._require_conn().execute("SELECT count(*) FROM learning_card_events").fetchone()[0] == 0


def test_concurrent_same_attempt_commits_one_lifecycle(runtime):
    from concurrent.futures import ThreadPoolExecutor
    store, _, _ = runtime
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: answer(store), range(8)))
    assert sum(bool(item.get("duplicate_attempt")) for item in results) == 7
    assert len({item["retention_mastery"]["mastery"] for item in results}) == 1
    assert store._require_conn().execute("SELECT count(*) FROM learning_card_events").fetchone()[0] == 1


def test_legacy_migration_uses_saved_anchor_only_once(runtime):
    store, clock, _ = runtime
    conn = store._require_conn()
    # Reconstruct a pre-feature database: identity established only at upgrade.
    conn.execute("DELETE FROM mastery_retention_identity")
    from datetime import datetime, timezone
    stamp = datetime.fromtimestamp(clock[0] - 14 * 86400, timezone.utc).isoformat()
    conn.execute("INSERT INTO mastery_snapshots(topic_id,mastery,updated_at,attempts) VALUES('topic',.8,?,5)", (stamp,))
    conn.commit()
    store.close()
    store.open()
    assert topic(store)["mastery"] == pytest.approx(.2)
    clock[0] += 7 * 86400
    assert topic(store)["mastery"] == pytest.approx(.1)


def test_actual_normalized_rubric_provenance_drives_retention(runtime, monkeypatch):
    from types import SimpleNamespace
    store, clock, module = runtime
    sdk_plugin = ModuleType("plugin.sdk.plugin")
    sdk_plugin.SdkError = RuntimeError
    monkeypatch.setitem(sys.modules, "plugin.sdk.plugin", sdk_plugin)
    prompts = ModuleType(module.__package__ + ".llm_prompts")
    prompts.build_concept_explain_messages = lambda *args, **kwargs: []
    prompts.build_operation_messages = lambda *args, **kwargs: []
    monkeypatch.setitem(sys.modules, prompts.__name__, prompts)
    mode = sys.modules[module.__package__ + ".mode_manager"]
    monkeypatch.setattr(mode, "build_transition_phrase", lambda *args, **kwargs: "", raising=False)
    monkeypatch.setattr(mode, "study_i18n_t", lambda *args, **kwargs: "", raising=False)
    evaluator = importlib.import_module(module.__package__ + ".tutor_llm_agent_answer_evaluate")
    harness = SimpleNamespace(_fallback_feedback=lambda *_: "ok", _fallback_next_action=lambda *_: "practice",
                              _screen_type_from_context=lambda *_: "text")
    payload = evaluator._normalize_evaluation(harness, {
        "verdict": "correct", "score": 100, "confidence": .8,
        "feedback": "ok", "evaluator_type": "forged", "evaluator_version": "forged",
    }, {"answer": "4", "expected_answer": "4"})
    assert payload["evaluator_type"] == "llm_rubric"
    assert payload["evaluator_version"] == "llm-rubric-v2"
    assert answer(store, eval_result=payload)["retention_mastery"]["owned"]
    clock[0] += 7 * 86400
    assert answer(store, "b", eval_result=payload)["retention_mastery"]["half_life"] == pytest.approx(9.8)
