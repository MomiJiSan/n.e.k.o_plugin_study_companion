from __future__ import annotations

from types import SimpleNamespace

import shadow_observability as shadow

SAFE_CONFIG = {
    "mastery": {
        "v2_shadow_enabled": True,
        "read_model": "v1",
        "model_version": "mastery-v2-shadow-1",
    },
    "assessment": {
        "exact_short_answer_enabled": False,
        "numeric_tolerance_enabled": False,
        "math_expression_enabled": False,
    },
}


def _snapshot(*, topic_id: str = "topic-a", mastery: float = 0.8) -> dict[str, object]:
    return {
        "topic_id": topic_id,
        "mastery": mastery,
        "accuracy": 0.9,
        "recency": 0.8,
        "consistency": 0.7,
        "confidence": 0.6,
        "evidence_count": 2,
        "unresolved_wrong_count": 0,
        "mastery_model_version": "mastery-v2-shadow-1",
        "source_attempt_id": f"attempt-{topic_id}",
        "computed_at": "2026-08-27T00:00:00Z",
    }


def test_report_is_read_only_and_captures_all_shadow_comparisons() -> None:
    incremental = _snapshot()
    report = shadow.build_shadow_observability_report(
        config=SAFE_CONFIG,
        v1_mastery_rows=[{"topic_id": "topic-a", "mastery": 0.5}, {"topic_id": "v1-only", "mastery": 0.3}],
        v2_mastery_rows=[{"topic_id": "topic-a", "mastery": 0.8}, {"topic_id": "v2-only", "mastery": 0.4}],
        projection_queue_rows=[
            {
                "attempt_id": "done-1",
                "status": "done",
                "created_at": "2026-08-27T00:00:00Z",
            },
            {
                "attempt_id": "failed-1",
                "status": "failed",
                "retry_count": 2,
                "last_error": "temporary error",
                "created_at": "2026-08-27T00:01:00Z",
            },
        ],
        incremental_snapshots=[incremental],
        rebuild_snapshots=[dict(incremental)],
        llm_assessments=[{"attempt_id": "a-1", "verdict": "correct"}],
        deterministic_assessments=[{"attempt_id": "a-1", "verdict": "correct"}],
        math_equivalence_cases=[
            {"case_id": "m-1", "equivalent": True, "llm_verdict": "correct", "deterministic_verdict": "correct"}
        ],
        relationship_v1_contexts=[{"context_id": "r-1", "context": {"path": ["a"]}}],
        relationship_v2_contexts=[{"context_id": "r-1", "context": {"path": ["b"]}}],
        high_delta_reviewed=True,
        non_math_retrieval_covered=True,
        as_of="2026-08-27T00:02:00Z",
    )

    assert report["read_only"] is True
    assert report["mastery_difference"]["compared_topic_count"] == 1
    assert report["mastery_difference"]["high_delta_topics"][0]["topic_id"] == "topic-a"
    assert report["projection_queue"]["projection_success_rate"] == 0.5
    assert report["projection_queue"]["backlog_count"] == 1
    assert report["incremental_rebuild_parity"]["fully_consistent"] is True
    assert report["assessment_agreement"]["agreement_rate"] == 1.0
    assert report["math_equivalence"]["equivalent_answer_disagreement_count"] == 0
    assert report["relationship_context_difference"]["changed_context_ids"] == ["r-1"]
    assert report["shadow_gate"]["read_model"] == "v1"
    assert report["shadow_gate"]["promotion_allowed"] is False


def test_parity_rejects_any_snapshot_field_difference_and_empty_evidence() -> None:
    incremental = _snapshot()
    mismatch = {**incremental, "computed_at": "2026-08-27T00:01:00Z"}

    comparison = shadow.compare_projection_snapshots([incremental], [mismatch])
    empty = shadow.compare_projection_snapshots([], [])

    assert comparison["fully_consistent"] is False
    assert comparison["mismatches"][0]["differing_fields"] == ["computed_at"]
    assert empty["fully_consistent"] is False


def test_gate_requires_v1_reads_and_deterministic_features_to_remain_off() -> None:
    report = shadow.build_shadow_observability_report(
        config={
            "mastery": {"v2_shadow_enabled": True, "read_model": "v2"},
            "assessment": {"exact_short_answer_enabled": True},
        },
        projection_queue_rows=[],
        incremental_snapshots=[_snapshot()],
        rebuild_snapshots=[_snapshot()],
        llm_assessments=[{"attempt_id": "a", "verdict": "correct"}],
        deterministic_assessments=[{"attempt_id": "a", "verdict": "correct"}],
        non_math_retrieval_covered=True,
    )

    checks = report["shadow_gate"]["checks"]
    assert checks["read_model_remains_v1"] is False
    assert checks["deterministic_scoring_remains_disabled"] is False
    assert report["shadow_gate"]["promotion_allowed"] is False


def test_development_shadow_config_never_promotes_v2_reads_or_scoring() -> None:
    assert shadow.SHADOW_DEVELOPMENT_CONFIG["mastery"] == {
        "v2_shadow_enabled": True,
        "read_model": "v1",
        "model_version": "mastery-v2-shadow-1",
    }
    assert shadow.SHADOW_DEVELOPMENT_CONFIG["assessment"] == {
        "exact_short_answer_enabled": False,
        "numeric_tolerance_enabled": False,
        "math_expression_enabled": False,
    }


def test_collector_uses_existing_store_list_methods_without_mutating_store() -> None:
    class ReadOnlyStore:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def list_topics(self, *, limit: int) -> list[dict[str, str]]:
            self.calls.append(f"topics:{limit}")
            return [{"id": "topic-a"}]

        def list_latest_mastery_for_topics(self, topic_ids: list[str]) -> list[dict[str, object]]:
            self.calls.append(f"v1:{','.join(topic_ids)}")
            return [{"topic_id": "topic-a", "mastery": 0.4}]

        def list_latest_mastery_v2_for_topics(
            self, topic_ids: list[str], *, mastery_model_version: str
        ) -> list[dict[str, object]]:
            self.calls.append(f"v2:{mastery_model_version}:{','.join(topic_ids)}")
            return [{"topic_id": "topic-a", "mastery": 0.5}]

        def list_mastery_projection_queue(self, *, limit: int) -> list[dict[str, object]]:
            self.calls.append(f"queue:{limit}")
            return []

    store = ReadOnlyStore()
    config = SimpleNamespace(
        mastery=SimpleNamespace(
            v2_shadow_enabled=False,
            read_model="v1",
            model_version="mastery-v2-shadow-1",
        ),
        assessment=SimpleNamespace(
            exact_short_answer_enabled=False,
            numeric_tolerance_enabled=False,
            math_expression_enabled=False,
        ),
    )

    report = shadow.collect_shadow_observability(store, config=config)

    assert report["mastery_difference"]["compared_topic_count"] == 1
    assert store.calls == [
        "topics:5000",
        "v1:topic-a",
        "v2:mastery-v2-shadow-1:topic-a",
        "queue:5000",
    ]
