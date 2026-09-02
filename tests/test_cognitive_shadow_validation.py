from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from adaptive_learning.cognitive_projection import (
    project_cognitive_hypothesis,
    project_cognitive_intervention_events,
)
from tools.cognitive_shadow_validation import (
    DEFAULT_FIXTURE,
    EXPECTED_CODES,
    _load_store,
    _Logger,
    _make_extractor,
    _ReferenceGateway,
    benchmark_answer_commit,
    build_report,
    evaluate_samples,
    load_fixture,
    verify_fail_closed,
)


def test_reviewed_fixture_is_strict_and_covers_three_hypothesis_boundaries() -> None:
    fixture = load_fixture(DEFAULT_FIXTURE)
    samples = fixture["samples"]

    assert len(samples) == 22
    assert {item["kind"] for item in samples} == {"standard", "adversarial"}
    assert {
        evidence["hypothesis_code"]
        for sample in samples
        for evidence in sample["expected_evidence"]
    } == set(EXPECTED_CODES)
    assert any(not sample["expected_evidence"] for sample in samples)


def test_reference_labels_pass_through_the_real_extractor_contract() -> None:
    fixture = load_fixture(DEFAULT_FIXTURE)
    samples = fixture["samples"]

    result = asyncio.run(
        evaluate_samples(_make_extractor(_ReferenceGateway(samples)), samples)
    )

    assert result["sample_count"] == 22
    assert result["exact_match_count"] == 22
    assert result["exact_match_rate"] == 1.0
    assert result["expected_item_recall"] == 1.0
    assert result["unexpected_item_rate"] == 0.0
    assert result["non_empty_span_rate"] == 1.0
    assert all(
        item["precision"] == item["recall"] == 1.0
        for item in result["hypothesis_metrics"].values()
    )
    assert result["adversarial"] == {
        "sample_count": 7,
        "exact_match_count": 7,
        "unexpected_evidence_count": 0,
        "safe_failure_count": 0,
    }


def test_model_unavailable_is_recorded_as_fail_closed_without_evidence() -> None:
    sample = load_fixture(DEFAULT_FIXTURE)["samples"][0]

    result = asyncio.run(verify_fail_closed(sample))

    assert result == {
        "passed": True,
        "status": "failed",
        "failure_reason": "model_unavailable",
        "evidence_count": 0,
    }


def test_local_answer_commit_benchmark_compares_real_atomic_enqueue_path() -> None:
    result = benchmark_answer_commit(iterations=20, warmup=2)

    assert result["environment"] == "synthetic_local_sqlite_provisional"
    assert result["iterations_per_arm"] == 20
    assert result["off"]["p95_ms"] >= 0
    assert result["on"]["p95_ms"] >= 0
    assert isinstance(result["gate_passed"], bool)


def test_two_sqlite_workers_cannot_claim_the_same_extraction(tmp_path: Path) -> None:
    Store = _load_store()
    db_path = tmp_path / "shared.db"
    first = Store(db_path, tmp_path / "seed-a.json", _Logger())
    second = Store(db_path, tmp_path / "seed-b.json", _Logger())
    first.open()
    second.open()
    try:
        first.ensure_topic(topic_id="calculus.chain_rule", name="Chain rule")
        first.batch_write_answer_data(
            session_id="concurrent",
            mode="companion",
            topic_id="calculus.chain_rule",
            question={
                "question_id": "question-concurrent",
                "question": "Differentiate sin(x^2).",
                "answer": "2*x*cos(x^2)",
                "question_type": "math_exact",
                "difficulty": 3,
            },
            user_answer="cos(x^2)",
            eval_result={"verdict": "wrong", "score": 0},
            response_time_ms=100,
            attempt_id="attempt-concurrent",
            enqueue_cognitive_projection=True,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(
                pool.map(
                    lambda store: store.claim_cognitive_projections(limit=1),
                    (first, second),
                )
            )
        claimed_ids = [item["attempt_id"] for group in claims for item in group]

        assert claimed_ids == ["attempt-concurrent"]
        assert first.get_latest_mastery("calculus.chain_rule") is None
        assert first.list_fsrs_cards(topic_ids=["calculus.chain_rule"]) == []
        assert first.list_wrong_questions(topic_id="calculus.chain_rule") == []
        assert first.get_active_learning_plan() is None
    finally:
        second.close()
        first.close()


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
            "hypothesis_id": "calculus.chain_rule:omit_inner_derivative",
            "topic_id": "calculus.chain_rule",
            "code": "omit_inner_derivative",
            "model_version": "cognitive-v1",
        },
        "learning_intent": intent,
        "repair_strategy": "complete_inner_derivative",
        "question_id": f"question-{decision}",
        "attempt_id": f"attempt-{decision}" if event_type == "attempt_committed" else "",
        "diagnostic_validation_id": validation_id,
        "evaluation_verdict": verdict,
        "session_id": "session-loop",
        "created_at": f"2026-09-02T08:00:{sequence:02d}Z",
    }


def test_complete_probe_repair_transfer_flow_stops_at_monitored() -> None:
    evidence = [
        {
            "attempt_id": f"attempt-{index}",
            "topic_id": "calculus.chain_rule",
            "hypothesis_code": "omit_inner_derivative",
            "direction": "support",
            "strength": 1.0,
            "extractor_confidence": 1.0,
            "diagnosticity": 0.6,
            "source_kind": "practice",
            "extractor_version": "cognitive-extractor-v1",
            "evidence_span": "missing inner factor",
            "session_id": f"session-{index}",
            "evidence_family_id": f"family-{index}",
        }
        for index in (1, 2)
    ]
    snapshot = project_cognitive_hypothesis(
        evidence,
        model_version="cognitive-v1",
        source_attempt_id="attempt-2",
        computed_at="2026-09-02T08:00:00Z",
    )
    events = [
        _event("question_committed", "misconception_probe", "probe", 1, validation_id="validation-probe"),
        _event("attempt_committed", "misconception_probe", "probe", 2, verdict="wrong", validation_id="validation-probe"),
        _event("question_committed", "misconception_repair", "repair", 3),
        _event("attempt_committed", "misconception_repair", "repair", 4, verdict="correct"),
        _event("question_committed", "transfer_check", "transfer", 5),
        _event("attempt_committed", "transfer_check", "transfer", 6, verdict="correct"),
    ]
    probe_evidence = {
        **evidence[0],
        "attempt_id": "attempt-probe",
        "source_kind": "misconception_probe",
        "diagnostic_validation_id": "validation-probe",
    }

    result = project_cognitive_intervention_events(
        snapshot, events, evidence_rows=[probe_evidence]
    )

    assert result["status"] == "monitored"
    assert result["intervention_stage"] == "monitored"
    assert result["last_intent"] == "transfer_check"
    assert result["last_outcome"] == "correct"


def test_report_never_promotes_synthetic_results_to_real_user_release() -> None:
    fixture = load_fixture(DEFAULT_FIXTURE)
    reference = asyncio.run(
        evaluate_samples(
            _make_extractor(_ReferenceGateway(fixture["samples"])),
            fixture["samples"],
        )
    )
    report = build_report(
        fixture=fixture,
        reference=reference,
        fail_closed={"passed": True},
        targeted_tests={
            "passed": True,
            "exit_code": 0,
            "duration_seconds": 1.0,
            "files": [],
            "summary": ["passed"],
        },
        latency={"gate_passed": True},
        real_model={
            "status": "EVALUATED",
            "runtime": {"configured": True},
            "evaluation": reference,
        },
    )

    assert report["gates"]["engineering_simulation"] == "PASS"
    assert report["gates"]["read_only"] == "NOT_RELEASED"
    assert report["gates"]["personal_beta"] == "NOT_RELEASED"
    assert set(report["real_user_gates"].values()) == {"NOT_EVALUATED"}
    assert "No full test suite was run." in report["limitations"]


def test_fixture_loader_rejects_wrong_schema(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.json"
    fixture.write_text('{"schema_version":2}', encoding="utf-8")

    try:
        load_fixture(fixture)
    except ValueError as exc:
        assert "schema_version" in str(exc)
    else:
        raise AssertionError("invalid fixture schema was accepted")
