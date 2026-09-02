from __future__ import annotations

import asyncio
import importlib
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def debug(self, *_args, **_kwargs):
        return None

    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None

    def error(self, *_args, **_kwargs):
        return None

    def exception(self, *_args, **_kwargs):
        return None


def _package(monkeypatch: pytest.MonkeyPatch, name: str) -> str:
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    return name


def _load_store_runtime(monkeypatch: pytest.MonkeyPatch, name: str):
    package = _package(monkeypatch, name)
    mode_manager = ModuleType(f"{package}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"{package}.mode_manager", mode_manager)
    store_module = importlib.import_module(f"{package}.store")
    tracker_module = importlib.import_module(f"{package}.knowledge_tracker")
    models_module = importlib.import_module(f"{package}.models")
    return store_module.StudyStore, tracker_module.KnowledgeTracker, models_module


@pytest.mark.parametrize(
    ("payload", "answer", "expected_error"),
    [
        (
            {"verdict": "correct", "score": 79, "final_answer_correct": True},
            "x",
            "correct_score_mismatch",
        ),
        (
            {"verdict": "partial", "score": 39, "final_answer_correct": False},
            "x",
            "partial_score_mismatch",
        ),
        (
            {"verdict": "wrong", "score": 40, "final_answer_correct": False},
            "x",
            "incorrect_score_mismatch",
        ),
        (
            {"verdict": "correct", "score": 90, "final_answer_correct": False},
            "x",
            "final_answer_correct_mismatch",
        ),
        (
            {"verdict": "wrong", "score": 10, "final_answer_correct": True},
            "x",
            "final_answer_correct_mismatch",
        ),
        (
            {"verdict": "partial", "score": 60, "final_answer_correct": False},
            "",
            "empty_answer_verdict_mismatch",
        ),
    ],
)
def test_evaluation_contract_rejects_inconsistent_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
    answer: str,
    expected_error: str,
) -> None:
    package = _package(monkeypatch, f"_evaluation_contract_{expected_error}")
    contract = importlib.import_module(f"{package}.evaluation_contract")
    result = contract.validate_evaluation(payload, learner_answer=answer)
    assert not result.valid
    assert expected_error in result.errors


@pytest.mark.parametrize("final_answer_correct", [True, False])
def test_evaluation_contract_allows_partial_with_either_final_answer_status(
    monkeypatch: pytest.MonkeyPatch, final_answer_correct: bool
) -> None:
    package = _package(monkeypatch, f"_evaluation_contract_partial_{final_answer_correct}")
    contract = importlib.import_module(f"{package}.evaluation_contract")

    result = contract.validate_evaluation(
        {
            "verdict": "partial",
            "score": 60,
            "final_answer_correct": final_answer_correct,
        },
        learner_answer="worked answer",
    )

    assert result.valid


@pytest.mark.parametrize(
    ("payload", "expected_final_answer_correct"),
    [
        ({"verdict": "partial", "final_answer_correct": True}, True),
        ({"verdict": "partial", "final_answer_correct": False}, False),
        ({"verdict": "correct"}, True),
        ({"verdict": "wrong"}, False),
    ],
)
def test_canonicalize_evaluation_preserves_valid_final_answer_status(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
    expected_final_answer_correct: bool,
) -> None:
    package = _package(monkeypatch, "_evaluation_contract_canonicalize")
    contract = importlib.import_module(f"{package}.evaluation_contract")

    canonical = contract.canonicalize_evaluation(payload)

    assert canonical["final_answer_correct"] is expected_final_answer_correct


def _make_store(tmp_path: Path, Store):
    store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    store.ensure_topic(topic_id="topic-a", name="Topic A")
    store.ensure_topic(topic_id="topic-b", name="Topic B")
    return store


def _write_attempt(store, *, origin_id: str, verdict: str, difficulty: int = 3):
    return store.batch_write_answer_data(
        session_id=f"session-{verdict}-{origin_id}",
        mode="companion",
        topic_id="topic-a",
        question={"question": "variant", "answer": "answer", "difficulty": difficulty},
        user_answer="learner",
        eval_result={"verdict": verdict, "score": 90 if verdict == "correct" else 10},
        response_time_ms=None,
        wrong_question_attempt_data={
            "question_id": origin_id,
            "topic_id": "topic-a",
            "verdict": verdict,
            "difficulty": difficulty,
        },
    )


def test_wrong_question_attempt_updates_only_bound_id_without_schema_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, _, _ = _load_store_runtime(monkeypatch, "_wrong_attempt_runtime")
    store = _make_store(tmp_path, Store)
    try:
        first = store.add_wrong_question(
            topic_id="topic-a",
            question={"question": "first"},
            user_answer="bad",
            expected_answer="good",
            error_type="misconception",
            verdict="wrong",
        )
        second = store.add_wrong_question(
            topic_id="topic-a",
            question={"question": "second"},
            user_answer="bad",
            expected_answer="good",
            error_type="misconception",
            verdict="wrong",
        )

        _write_attempt(store, origin_id=first, verdict="wrong")
        assert len(store.list_wrong_questions(limit=20)) == 2
        assert store.get_wrong_question(first)["retry_count"] == 1
        assert store.get_wrong_question(first)["consecutive_correct"] == 0
        assert store.get_wrong_question(second)["retry_count"] == 0

        stale = _write_attempt(store, origin_id="missing-id", verdict="wrong")
        assert stale["wrong_question_attempt"]["status"] == "stale"
        assert len(store.list_wrong_questions(limit=20)) == 2

        cross_topic = store.add_wrong_question(
            topic_id="topic-b",
            question={"question": "cross topic"},
            user_answer="bad",
            expected_answer="good",
            error_type="misconception",
            verdict="wrong",
        )
        cross_result = _write_attempt(store, origin_id=cross_topic, verdict="correct")
        assert cross_result["wrong_question_attempt"]["status"] == "stale"
        assert store.get_wrong_question(cross_topic)["retry_count"] == 0

        store.batch_write_answer_data(
            session_id="ordinary-correct",
            mode="companion",
            topic_id="topic-a",
            question={"question": "ordinary", "answer": "answer"},
            user_answer="answer",
            eval_result={"verdict": "correct", "score": 90},
            response_time_ms=None,
        )
        assert store.get_wrong_question(second)["retry_count"] == 0

        first_correct = _write_attempt(
            store, origin_id=first, verdict="correct", difficulty=3
        )
        assert first_correct["wrong_question_attempt"]["status"] == "retrying"
        assert store.get_wrong_question(first)["consecutive_correct"] == 1
        assert store.get_wrong_question(second)["status"] == "active"
    finally:
        store.close()


def test_auto_retry_candidates_apply_cooldowns_and_oldest_retry_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, _, _ = _load_store_runtime(monkeypatch, "_auto_retry_candidates_runtime")
    store = _make_store(tmp_path, Store)
    try:
        def add(label: str, *, topic_id: str = "topic-a") -> str:
            return store.add_wrong_question(
                topic_id=topic_id,
                question={"question": label},
                user_answer="bad",
                expected_answer="good",
                error_type="misconception",
                verdict="wrong",
            )

        active = add("active")
        cooling_wrong = add("cooling wrong")
        oldest_ready = add("oldest ready")
        newer_ready = add("newer ready")
        cooling_correct = add("cooling correct")
        day_old_correct = add("day-old correct")
        outside_scope = add("outside scope", topic_id="topic-b")
        with store._lock:
            conn = store._require_conn()
            conn.execute(
                """
                UPDATE wrong_questions
                SET status = 'retrying', last_retry_at = datetime('now', '-10 minutes')
                WHERE id = ?
                """,
                (cooling_wrong,),
            )
            conn.execute(
                """
                UPDATE wrong_questions
                SET status = 'retrying', last_retry_at = datetime('now', '-90 minutes')
                WHERE id = ?
                """,
                (oldest_ready,),
            )
            conn.execute(
                """
                UPDATE wrong_questions
                SET status = 'retrying', last_retry_at = datetime('now', '-31 minutes')
                WHERE id = ?
                """,
                (newer_ready,),
            )
            conn.execute(
                """
                UPDATE wrong_questions
                SET status = 'retrying', consecutive_correct = 1,
                    last_error_at = datetime('now', '-12 hours'),
                    last_retry_at = datetime('now', '-2 hours')
                WHERE id = ?
                """,
                (cooling_correct,),
            )
            conn.execute(
                """
                UPDATE wrong_questions
                SET status = 'retrying', consecutive_correct = 1,
                    last_error_at = datetime('now', '-25 hours'),
                    last_retry_at = datetime('now', '-1 minute')
                WHERE id = ?
                """,
                (day_old_correct,),
            )
            conn.commit()

        candidates = store.list_auto_retry_candidates(
            limit=None, topic_ids={"topic-a"}
        )
        assert [item["id"] for item in candidates] == [
            active,
            oldest_ready,
            newer_ready,
        ]
        assert cooling_wrong not in {item["id"] for item in candidates}
        assert cooling_correct not in {item["id"] for item in candidates}
        assert outside_scope not in {item["id"] for item in candidates}

        visible = store.list_wrong_questions(
            limit=None, topic_ids={"topic-a"}, statuses=("active", "retrying")
        )
        assert {item["id"] for item in visible} == {
            active,
            cooling_wrong,
            oldest_ready,
            newer_ready,
            cooling_correct,
            day_old_correct,
        }
    finally:
        store.close()


def test_correct_retry_reenters_automatic_candidates_one_day_after_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, _, _ = _load_store_runtime(monkeypatch, "_correct_retry_delay_runtime")
    store = _make_store(tmp_path, Store)
    try:
        origin = store.add_wrong_question(
            topic_id="topic-a",
            question={"question": "original"},
            user_answer="bad",
            expected_answer="good",
            error_type="misconception",
            verdict="wrong",
        )
        _write_attempt(store, origin_id=origin, verdict="correct")
        assert store.get_wrong_question(origin)["consecutive_correct"] == 1
        assert store.list_auto_retry_candidates(limit=None) == []

        with store._lock:
            store._require_conn().execute(
                """
                UPDATE wrong_questions
                SET last_error_at = datetime('now', '-25 hours'),
                    last_retry_at = datetime('now', '-25 hours')
                WHERE id = ?
                """,
                (origin,),
            )
            store._require_conn().commit()
        assert [
            item["id"] for item in store.list_auto_retry_candidates(limit=None)
        ] == [origin]
    finally:
        store.close()


def test_correct_retries_require_one_day_between_each_counted_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, _, _ = _load_store_runtime(monkeypatch, "_correct_retry_spacing_runtime")
    store = _make_store(tmp_path, Store)
    try:
        origin = store.add_wrong_question(
            topic_id="topic-a",
            question={"question": "original"},
            user_answer="bad",
            expected_answer="good",
            error_type="misconception",
            verdict="wrong",
        )

        first = _write_attempt(store, origin_id=origin, verdict="correct")
        assert first["wrong_question_attempt"]["status"] == "retrying"
        assert store.get_wrong_question(origin)["consecutive_correct"] == 1

        second_immediate = _write_attempt(store, origin_id=origin, verdict="correct")
        third_immediate = _write_attempt(store, origin_id=origin, verdict="correct")
        assert second_immediate["wrong_question_attempt"]["status"] == "cooling"
        assert third_immediate["wrong_question_attempt"]["status"] == "cooling"
        assert store.get_wrong_question(origin)["status"] == "retrying"
        assert store.get_wrong_question(origin)["consecutive_correct"] == 1

        with store._lock:
            conn = store._require_conn()
            conn.execute(
                """
                UPDATE wrong_questions
                SET last_error_at = datetime('now', '-2 days'),
                    last_retry_at = datetime('now', '-25 hours')
                WHERE id = ?
                """,
                (origin,),
            )
            conn.commit()
        second = _write_attempt(store, origin_id=origin, verdict="correct")
        assert second["wrong_question_attempt"]["status"] == "retrying"
        assert store.get_wrong_question(origin)["consecutive_correct"] == 2

        immediate_after_second = _write_attempt(
            store, origin_id=origin, verdict="correct"
        )
        assert immediate_after_second["wrong_question_attempt"]["status"] == "cooling"
        assert store.get_wrong_question(origin)["consecutive_correct"] == 2

        with store._lock:
            conn = store._require_conn()
            conn.execute(
                """
                UPDATE wrong_questions
                SET last_retry_at = datetime('now', '-25 hours')
                WHERE id = ?
                """,
                (origin,),
            )
            conn.commit()
        third = _write_attempt(store, origin_id=origin, verdict="correct")
        assert third["wrong_question_attempt"]["status"] == "resolved"
        assert store.get_wrong_question(origin)["consecutive_correct"] == 3
    finally:
        store.close()


def test_wrong_question_attempt_rolls_back_with_answer_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, _, _ = _load_store_runtime(monkeypatch, "_wrong_attempt_rollback")
    store = _make_store(tmp_path, Store)
    try:
        origin = store.add_wrong_question(
            topic_id="topic-a",
            question={"question": "first"},
            user_answer="bad",
            expected_answer="good",
            error_type="misconception",
            verdict="wrong",
        )
        qa_before = len(store.list_qa_records(limit=100))

        def fail_after_wrong_attempt(*_args, **_kwargs):
            raise RuntimeError("injected fsrs failure")

        monkeypatch.setattr(store, "_batch_write_fsrs_card", fail_after_wrong_attempt)
        with pytest.raises(RuntimeError, match="injected fsrs failure"):
            store.batch_write_answer_data(
                session_id="rollback",
                mode="companion",
                topic_id="topic-a",
                question={"question": "variant", "answer": "answer"},
                user_answer="learner",
                eval_result={"verdict": "correct", "score": 90},
                response_time_ms=None,
                wrong_question_attempt_data={
                    "question_id": origin,
                    "topic_id": "topic-a",
                    "verdict": "correct",
                    "difficulty": 3,
                },
                fsrs_card={"topic_id": "topic-a"},
            )
        assert len(store.list_qa_records(limit=100)) == qa_before
        assert store.get_wrong_question(origin)["retry_count"] == 0
        assert store.get_wrong_question(origin)["consecutive_correct"] == 0
    finally:
        store.close()


def test_batch_attempt_id_is_atomic_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, Tracker, _ = _load_store_runtime(monkeypatch, "_attempt_idempotency")
    store = _make_store(tmp_path, Store)
    try:
        tracker = Tracker(store, logger=_Logger())
        question = {
            "question": "targeted",
            "answer": "answer",
            "difficulty": 3,
        }
        first = tracker.on_answer(
            topic_id="topic-a",
            question=question,
            user_answer="wrong",
            eval_result={"verdict": "wrong", "score": 10},
            mode="companion",
            session_id="idempotent",
            require_existing_topic=True,
            attempt_id="attempt-1",
        )

        def table_count(table: str) -> int:
            return int(
                store._require_read_conn()
                .execute(f"SELECT COUNT(*) FROM {table}")
                .fetchone()[0]
            )

        counts_after_first = {
            "qa": len(store.list_qa_records(limit=100)),
            "mastery": table_count("mastery_snapshots"),
            "review": table_count("review_log"),
            "wrong": len(store.list_wrong_questions(limit=100)),
        }
        second = tracker.on_answer(
            topic_id="topic-a",
            question=question,
            user_answer="answer",
            eval_result={"verdict": "correct", "score": 95},
            mode="companion",
            session_id="idempotent",
            require_existing_topic=True,
            attempt_id="attempt-1",
        )
        assert first["topic_id"] == "topic-a"
        assert second["knowledge_tracking_status"] == "duplicate_attempt"
        assert second["existing_eval_result"]["verdict"] == "wrong"
        assert {
            "qa": len(store.list_qa_records(limit=100)),
            "mastery": table_count("mastery_snapshots"),
            "review": table_count("review_log"),
            "wrong": len(store.list_wrong_questions(limit=100)),
        } == counts_after_first
    finally:
        store.close()


def test_validated_target_without_batch_capability_is_qa_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, Tracker, _ = _load_store_runtime(monkeypatch, "_legacy_target_qa_only")
    store = _make_store(tmp_path, Store)
    store._supports_batch_answer = False
    try:
        tracker = Tracker(store, logger=_Logger())
        result = tracker.on_answer(
            topic_id="topic-a",
            question={"question": "targeted", "answer": "answer"},
            user_answer="wrong",
            eval_result={"verdict": "wrong", "score": 10},
            mode="companion",
            session_id="legacy-qa-only",
            require_existing_topic=True,
            attempt_id="attempt-legacy",
        )
        assert result["knowledge_tracking_status"] == "qa_only"
        qa = store.list_qa_records(limit=10)[-1]
        assert qa["topic_id"] == ""
        assert store.get_latest_mastery("topic-a") is None
        assert store.get_fsrs_card("topic-a") is None
        assert store.list_wrong_questions(limit=10) == []
    finally:
        store.close()


def test_unvalidated_answer_is_topic_null_qa_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, Tracker, _ = _load_store_runtime(monkeypatch, "_qa_only_runtime")
    store = _make_store(tmp_path, Store)
    try:
        tracker = Tracker(store, logger=_Logger())
        result = tracker.on_answer(
            topic_id="topic-a",
            question={
                "question": "manual",
                "answer": "answer",
                "target_binding": {"validation_status": "passed"},
            },
            user_answer="learner",
            eval_result={"verdict": "wrong", "score": 10},
            mode="companion",
            session_id="qa-only",
            allow_knowledge_update=False,
            require_existing_topic=True,
        )
        assert result["knowledge_tracking_status"] == "qa_only"
        qa = store.list_qa_records(limit=10)[-1]
        assert qa["topic_id"] == ""
        assert "target_binding" not in qa["question"]
        assert store.get_latest_mastery("topic-a") is None
        assert store.get_fsrs_card("topic-a") is None
        assert store.list_wrong_questions(limit=10) == []
    finally:
        store.close()


def test_target_binding_is_private_and_survives_state_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, _, models = _load_store_runtime(monkeypatch, "_binding_state_runtime")
    store = _make_store(tmp_path, Store)
    binding = {
        "target_topic_id": "topic-a",
        "validation_status": "passed",
        "generated_at": "now",
        "origin_wrong_question_id": "wrong-1",
    }
    try:
        state = models.StudyState(
            current_question={
                "question": "question",
                "answer": "answer",
                "target_binding": binding,
            }
        )
        store.save_state(state)
        restored = store.load_state(models.StudyState())
        assert restored.current_question["target_binding"] == binding
        public = models.public_current_question_payload(restored.current_question)
        assert "target_binding" not in public
        assert "answer" not in public
    finally:
        store.close()


def test_target_binding_lookup_failure_degrades_to_qa_only_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(monkeypatch, "_binding_lookup_failure")
    target_binding = importlib.import_module(f"{package}.target_binding")
    warnings: list[str] = []

    class Logger:
        def warning(self, message, error):
            warnings.append(message.format(error))

    async def run():
        return await target_binding.resolve_existing_target_topic_id(
            {
                "source": "targeted_question",
                "target_binding": {
                    "target_topic_id": "topic-a",
                    "validation_status": "passed",
                    "generated_at": "now",
                },
            },
            {"selected_topic_id": "topic-a"},
            question_source="current_question",
            get_topic=lambda _topic_id: (_ for _ in ()).throw(RuntimeError("db down")),
            logger=Logger(),
        )

    assert asyncio.run(run()) == ""
    assert warnings and "recording QA only" in warnings[0]


def _load_answer_entries(monkeypatch: pytest.MonkeyPatch, package: str):
    _package(monkeypatch, package)
    common = ModuleType(f"{package}.entry_common")

    class SdkError(Exception):
        def __init__(self, message: str, *, code: str = "") -> None:
            super().__init__(message)
            self.code = code

    class Ui:
        @staticmethod
        def action():
            return lambda value: value

    class Err:
        def __init__(self, error) -> None:
            self.error = error

    class Ok:
        def __init__(self, value) -> None:
            self.value = value

    common.LLM_OPERATION_ANSWER_EVALUATE = "answer_evaluate"
    common.Err = Err
    common.Ok = Ok
    common.SdkError = SdkError
    common._entry_exception_error = lambda *_args, **_kwargs: None
    common._validate_optional_vision_image_payload = lambda *_args, **_kwargs: ""
    common.asyncio = asyncio
    common.plugin_entry = lambda **_kwargs: lambda value: value
    common.tr = lambda *_args, **kwargs: kwargs.get("default", "")
    common.ui = Ui()
    monkeypatch.setitem(sys.modules, f"{package}.entry_common", common)
    models = ModuleType(f"{package}.models")
    models.public_current_question_payload = lambda value: dict(value or {})
    monkeypatch.setitem(sys.modules, f"{package}.models", models)
    entries = importlib.import_module(f"{package}.entry_tutor_answer_entries")
    return entries, SdkError, Err, Ok


def test_attempt_signal_values_accept_only_bounded_integer_and_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _, _, _ = _load_answer_entries(monkeypatch, "_attempt_signal_values")
    assert entries._attempt_signal_values({"response_time_ms": 1_234, "used_hint": True}) == (1_234, True)
    assert entries._attempt_signal_values({"response_time_ms": True, "used_hint": 1}) == (None, None)
    assert entries._attempt_signal_values({"response_time_ms": 86_400_001, "used_hint": False}) == (None, False)


class _EvaluationReply:
    def __init__(self, payload: dict, *, degraded: bool = False) -> None:
        self.payload = payload
        self.degraded = degraded
        self.diagnostic = ""
        self.input_text = "learner"
        self.reply = "evaluation"
        self.operation = "answer_evaluate"
        self.created_at = "now"


async def _run_inconsistent_entry_test(monkeypatch: pytest.MonkeyPatch) -> None:
    entries, SdkError, Err, _ = _load_answer_entries(
        monkeypatch, "_evaluation_entry_failure"
    )

    class Agent:
        calls = 0

        async def answer_evaluate(self, **_kwargs):
            self.calls += 1
            return _EvaluationReply(
                {
                    "verdict": "correct",
                    "score": 0,
                    "final_answer_correct": False,
                }
            )

    class Subject(entries._TutorAnswerEntriesMixin):
        _agent = Agent()
        _lock = asyncio.Lock()
        _state = SimpleNamespace(
            current_question={
                "question": "question",
                "answer": "expected",
                "question_id": "q1",
                "attempt_id": "a1",
            },
            active_mode="companion",
        )
        finalized = 0

        def _resolve_study_target_lanlan(self, _kwargs):
            return None

        def _resolve_current_run_id(self, _kwargs):
            return "run"

        async def _build_learning_context(self, _operation, *, input_text, extra):
            return {**extra, "input_text": input_text}

        async def _finalize_tutor_call(self, *_args, **_kwargs):
            self.finalized += 1
            return {}

        async def _persist_state(self):
            raise AssertionError("inconsistent evaluation must not persist state")

    subject = Subject()
    result = await subject.study_evaluate_answer(
        answer="learner",
        question_id="q1",
        attempt_id="a1",
    )
    assert isinstance(result, Err)
    assert isinstance(result.error, SdkError)
    assert result.error.code == "EVALUATION_INCONSISTENT"
    assert subject._agent.calls == 2
    assert subject.finalized == 0
    assert "attempt_evaluation_pending" not in subject._state.current_question
    assert not subject._state.current_question.get("attempt_evaluated")


def test_inconsistent_evaluation_repairs_once_and_never_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_run_inconsistent_entry_test(monkeypatch))


async def _run_successful_repair_entry_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _, _, Ok = _load_answer_entries(
        monkeypatch, "_evaluation_entry_successful_repair"
    )

    class Agent:
        calls = 0

        async def answer_evaluate(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return _EvaluationReply(
                    {
                        "verdict": "correct",
                        "score": 0,
                        "final_answer_correct": False,
                    }
                )
            return _EvaluationReply(
                {
                    "verdict": "correct",
                    "score": 90,
                    "final_answer_correct": True,
                    "feedback": "ok",
                }
            )

    class Subject(entries._TutorAnswerEntriesMixin):
        _agent = Agent()
        _lock = asyncio.Lock()
        _state = SimpleNamespace(
            current_question={
                "question": "question",
                "answer": "expected",
                "question_id": "q1",
                "attempt_id": "a1",
            },
            active_mode="companion",
        )
        finalized = 0
        persisted = 0
        logger = _Logger()

        def _resolve_study_target_lanlan(self, _kwargs):
            return None

        def _resolve_current_run_id(self, _kwargs):
            return "run"

        async def _build_learning_context(self, _operation, *, input_text, extra):
            return {**extra, "input_text": input_text}

        async def _finalize_tutor_call(self, _operation, reply, **_kwargs):
            self.finalized += 1
            return dict(reply.payload)

        async def _persist_state(self):
            self.persisted += 1

        async def _emit_answer_evaluated_event(self, **_kwargs):
            return None

    subject = Subject()
    result = await subject.study_evaluate_answer(
        answer="learner",
        question_id="q1",
        attempt_id="a1",
    )
    assert isinstance(result, Ok)
    assert result.value["verdict"] == "correct"
    assert result.value["score"] == 90
    assert result.value["final_answer_correct"] is True
    assert result.value["attempt_status"] == "correct"
    assert result.value["mastery_status"] == "insufficient_evidence"
    assert result.value["scope_status"] == "active"
    assert result.value["practice_scope_status"] == "active"
    assert result.value["can_continue_review"] is False
    assert subject._agent.calls == 2
    assert subject.finalized == 1
    assert subject.persisted == 1
    assert subject._state.current_question["attempt_evaluated"] is True
    assert subject._state.current_question["answer_evaluation_cache"][
        "attempt_status"
    ] == "correct"


def test_successful_semantic_repair_finalizes_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_run_successful_repair_entry_test(monkeypatch))


async def _run_deterministic_short_answer_entry_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _, _, Ok = _load_answer_entries(
        monkeypatch, "_evaluation_entry_deterministic_short_answer"
    )

    class Agent:
        calls = 0

        async def answer_evaluate(self, **_kwargs):
            self.calls += 1
            raise AssertionError("a deterministic exact match must not call the LLM")

    class Subject(entries._TutorAnswerEntriesMixin):
        _agent = Agent()
        _lock = asyncio.Lock()
        _cfg = SimpleNamespace(
            assessment=SimpleNamespace(
                exact_short_answer_enabled=True,
                numeric_tolerance_enabled=False,
                math_expression_enabled=False,
            )
        )
        _state = SimpleNamespace(
            current_question={
                "question": "What is the capital of France?",
                "answer": "Paris",
                "accepted_answers": ["Paris", "Paris, France"],
                "question_type": "short_answer",
                "question_id": "q-deterministic",
                "attempt_id": "a-deterministic",
            },
            active_mode="companion",
        )
        finalized = 0
        persisted = 0
        logger = _Logger()

        def _resolve_study_target_lanlan(self, _kwargs):
            return None

        def _resolve_current_run_id(self, _kwargs):
            return "run"

        async def _build_learning_context(self, _operation, *, input_text, extra):
            return {**extra, "input_text": input_text}

        async def _finalize_tutor_call(self, _operation, reply, **_kwargs):
            self.finalized += 1
            return dict(reply.payload)

        async def _persist_state(self):
            self.persisted += 1

        async def _emit_answer_evaluated_event(self, **_kwargs):
            return None

    subject = Subject()
    result = await subject.study_evaluate_answer(
        answer=" PARIS ",
        question_id="q-deterministic",
        attempt_id="a-deterministic",
    )

    assert isinstance(result, Ok)
    assert result.value["verdict"] == "correct"
    assert result.value["evaluator_type"] == "exact_short_answer"
    assert result.value["evaluator_version"] == "exact-short-answer-v1"
    assert result.value["confidence"] == 1.0
    assert result.value["fallback_reason"] == ""
    assert "accepted_answers" not in result.value
    assert "answer" not in result.value
    assert subject._agent.calls == 0
    assert subject.finalized == 1
    assert subject.persisted == 1


def test_deterministic_short_answer_skips_llm_and_keeps_private_answer_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_run_deterministic_short_answer_entry_test(monkeypatch))


async def _run_persistence_failure_entry_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, SdkError, Err, _ = _load_answer_entries(
        monkeypatch, "_evaluation_persistence_failure"
    )

    class Agent:
        async def answer_evaluate(self, **_kwargs):
            return _EvaluationReply(
                {
                    "verdict": "correct",
                    "score": 90,
                    "final_answer_correct": True,
                }
            )

    class Subject(entries._TutorAnswerEntriesMixin):
        _agent = Agent()
        _lock = asyncio.Lock()
        _state = SimpleNamespace(
            current_question={
                "question": "question",
                "answer": "expected",
                "question_id": "q-persist",
                "attempt_id": "a-persist",
            },
            active_mode="companion",
        )

        def _resolve_study_target_lanlan(self, _kwargs):
            return None

        def _resolve_current_run_id(self, _kwargs):
            return "run"

        async def _build_learning_context(self, _operation, *, input_text, extra):
            return {**extra, "input_text": input_text}

        async def _finalize_tutor_call(self, *_args, **_kwargs):
            raise SdkError(
                "answer persistence failed", code="ANSWER_PERSISTENCE_FAILED"
            )

        async def _persist_state(self):
            return None

    subject = Subject()
    result = await subject.study_evaluate_answer(
        answer="learner",
        question_id="q-persist",
        attempt_id="a-persist",
    )
    assert isinstance(result, Err)
    assert result.error.code == "ANSWER_PERSISTENCE_FAILED"
    assert "attempt_evaluation_pending" not in subject._state.current_question
    assert not subject._state.current_question.get("attempt_evaluated")


def test_persistence_failure_clears_reservation_without_consuming_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_run_persistence_failure_entry_test(monkeypatch))


def _practice_question_payload() -> dict:
    return {
        "source": "targeted_question",
        "selected_topic_id": "topic-a",
        "practice_scope": {
            "mode": "explicit_topic",
            "topic_id": "topic-a",
            "scope_key": "scope-a",
            "scope_revision": 7,
        },
        "scope_key": "scope-a",
        "scope_revision": 7,
        "target_binding": {
            "target_topic_id": "topic-a",
            "validation_status": "passed",
            "generated_at": "2026-08-24T00:00:00Z",
        },
    }


async def _run_practice_outcome_enrichment_test(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_read: bool,
    active_scope_key: str = "scope-a",
    active_scope_revision: int = 7,
    question_scope_revision: int = 7,
    verdict: str = "correct",
) -> dict:
    entries, _, _, _ = _load_answer_entries(
        monkeypatch, f"_practice_outcome_enrichment_{fail_read}"
    )

    class Store:
        def get_latest_mastery(self, topic_id: str):
            assert topic_id == "topic-a"
            if fail_read:
                raise RuntimeError("forced read failure")
            return {"mastery": 0.8, "attempts": 3, "flags": []}

        def list_wrong_questions(self, **kwargs):
            assert kwargs == {
                "limit": 1,
                "topic_id": "topic-a",
                "statuses": ("active", "retrying"),
            }
            return []

    class Subject(entries._TutorAnswerEntriesMixin):
        logger = _Logger()
        _knowledge_tracker = SimpleNamespace(store=Store())

        def __init__(self) -> None:
            self._lock = asyncio.Lock()
            self._scope_lock = asyncio.Lock()
            self._state = SimpleNamespace(
                active_practice_scope={"scope_key": active_scope_key},
                practice_scope_revision=active_scope_revision,
            )

        def _practice_scope_write_lock(self):
            return self._scope_lock

    question = _practice_question_payload()
    question["scope_revision"] = question_scope_revision
    return await Subject()._build_practice_outcome_payload(
        payload={"verdict": verdict},
        question_payload=question,
        current_question=question,
        question_source="current_question",
    )


def test_practice_outcome_reads_mastery_and_active_wrong_questions_after_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = asyncio.run(
        _run_practice_outcome_enrichment_test(monkeypatch, fail_read=False)
    )
    assert result["attempt_status"] == "correct"
    assert result["mastery_status"] == "mastered"
    assert result["scope_status"] == "reviewing"
    assert result["practice_scope_status"] == "completed"


def test_practice_outcome_read_failure_is_conservative_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = asyncio.run(
        _run_practice_outcome_enrichment_test(monkeypatch, fail_read=True)
    )
    assert result["attempt_status"] == "correct"
    assert result["mastery_status"] == "insufficient_evidence"
    assert result["scope_status"] == "active"
    assert result["practice_scope_status"] == "active"


def test_next_step_preview_reuses_context_without_calling_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _, _, _ = _load_answer_entries(
        monkeypatch, "_next_step_preview_without_llm"
    )

    class Subject(entries._TutorAnswerEntriesMixin):
        logger = _Logger()
        _cfg = SimpleNamespace()
        _agent = SimpleNamespace(
            question_generate=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("next-step preview must not call the LLM")
            )
        )

        def _build_targeted_question_context(self):
            return {
                "selection_context_id": "scq-next",
                "expires_at": 1234.0,
                "selection_reason": "retry",
                "selected_topic_id": "topic-a",
                "selected_topic_name": "Topic A",
                "difficulty": 2,
                "selection_domain": "global",
            }

    learning_update, next_step = asyncio.run(
        Subject()._build_adaptive_next_step(
            question_payload={},
            learning_update={"status": "updated", "topic_id": "topic-a"},
            validated_target=True,
            knowledge_tracking_status="",
        )
    )

    assert learning_update["status"] == "updated"
    assert next_step == {
        "status": "ready",
        "action": "generate_question",
        "selection_context_id": "scq-next",
        "expires_at": 1234.0,
        "reason": "retry",
        "topic_id": "topic-a",
        "topic_name": "Topic A",
        "difficulty": 2,
        "available_now": True,
        "selection_domain": "global",
        "learning_plan_id": "",
        "learning_plan_revision": 0,
        "plan_progress": {},
    }


def test_qa_only_answer_has_no_adaptive_next_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _, _, _ = _load_answer_entries(
        monkeypatch, "_next_step_qa_only"
    )

    learning_update, next_step = asyncio.run(
        entries._TutorAnswerEntriesMixin()._build_adaptive_next_step(
            question_payload={},
            learning_update={"status": "updated"},
            validated_target=False,
            knowledge_tracking_status="qa_only",
        )
    )

    assert learning_update == {"status": "not_applicable"}
    assert next_step["status"] == "not_applicable"
    assert next_step["available_now"] is False


def test_completed_learning_plan_returns_summary_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _, _, _ = _load_answer_entries(
        monkeypatch, "_next_step_completed_plan"
    )
    reconciled = []

    class Service:
        def reconcile(self, plan_id):
            reconciled.append(plan_id)
            return {
                "id": plan_id,
                "status": "completed",
                "progress": {"total": 2, "mastered": 2},
            }

    class Subject(entries._TutorAnswerEntriesMixin):
        logger = _Logger()
        _cfg = SimpleNamespace()

        def _learning_plan_service(self):
            return Service()

        def _build_targeted_question_context(self):
            raise AssertionError("a completed plan must exit before selecting another topic")

    learning_update, next_step = asyncio.run(
        Subject()._build_adaptive_next_step(
            question_payload={"learning_plan_id": "lp-1"},
            learning_update={"status": "updated"},
            validated_target=True,
            knowledge_tracking_status="",
        )
    )

    assert reconciled == ["lp-1"]
    assert learning_update["plan_progress"] == {"total": 2, "mastered": 2}
    assert next_step["action"] == "summarize_plan"


@pytest.mark.parametrize(
    ("active_scope_key", "active_revision", "question_revision"),
    [
        ("scope-b", 7, 7),
        ("scope-a", 8, 7),
    ],
)
def test_stale_question_scope_cannot_become_reviewing_but_keeps_mastery(
    monkeypatch: pytest.MonkeyPatch,
    active_scope_key: str,
    active_revision: int,
    question_revision: int,
) -> None:
    result = asyncio.run(
        _run_practice_outcome_enrichment_test(
            monkeypatch,
            fail_read=False,
            active_scope_key=active_scope_key,
            active_scope_revision=active_revision,
            question_scope_revision=question_revision,
        )
    )
    assert result["mastery_status"] == "mastered"
    assert result["scope_status"] == "active"
    assert result["practice_scope_status"] == "active"
    assert result["can_continue_review"] is False


@pytest.mark.parametrize("verdict", ["wrong", "partial", "dont_know"])
def test_non_correct_attempt_cannot_enter_reviewing_after_mastery_read(
    monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    result = asyncio.run(
        _run_practice_outcome_enrichment_test(
            monkeypatch,
            fail_read=False,
            verdict=verdict,
        )
    )
    assert result["attempt_status"] == verdict
    assert result["mastery_status"] == "progressing"
    assert result["scope_status"] == "active"
    assert result["practice_scope_status"] == "active"
    assert result["can_continue_review"] is False


async def _run_scope_switch_race_test(
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    entries, _, _, _ = _load_answer_entries(
        monkeypatch, "_practice_outcome_scope_switch_race"
    )
    evidence_loaded = threading.Event()

    class Subject(entries._TutorAnswerEntriesMixin):
        logger = _Logger()

        def __init__(self) -> None:
            self._lock = asyncio.Lock()
            self._scope_lock = asyncio.Lock()
            self._state = SimpleNamespace(
                active_practice_scope={"scope_key": "scope-a"},
                practice_scope_revision=7,
            )

        def _practice_scope_write_lock(self):
            return self._scope_lock

        def _load_practice_mastery_evidence(self, topic_id: str):
            assert topic_id == "topic-a"
            evidence_loaded.set()
            return {"mastery": 0.95, "attempts": 8, "flags": []}, False

    subject = Subject()
    question = _practice_question_payload()
    await subject._scope_lock.acquire()
    outcome_task = asyncio.create_task(
        subject._build_practice_outcome_payload(
            payload={"verdict": "correct"},
            question_payload=question,
            current_question=question,
            question_source="current_question",
        )
    )
    try:
        assert await asyncio.to_thread(evidence_loaded.wait, 2)
        async with subject._lock:
            subject._state.active_practice_scope = {"scope_key": "scope-b"}
            subject._state.practice_scope_revision = 8
    finally:
        subject._scope_lock.release()
    return await asyncio.wait_for(outcome_task, timeout=2)


def test_scope_switch_is_observed_atomically_before_reviewing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = asyncio.run(_run_scope_switch_race_test(monkeypatch))
    assert result["mastery_status"] == "mastered"
    assert result["scope_status"] == "active"
    assert result["practice_scope_status"] == "active"
    assert result["can_continue_review"] is False
