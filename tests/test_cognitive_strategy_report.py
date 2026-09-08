from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from adaptive_learning.cognitive_strategy_report import (
    EXCLUSION_REASONS,
    build_cognitive_strategy_report,
    canonical_report_json,
    render_report_markdown,
    report_digest,
)
from store_cognitive_strategy import (
    create_cognitive_strategy_schema,
    insert_cognitive_strategy_exposure,
    insert_cognitive_strategy_fact,
)
from tools.cognitive_strategy_report import main

CATALOG_VERSION = "strategy-catalog-v1"
VERSION_SET_ID = "cognitive-v2.1-1"


def _catalog() -> list[dict[str, object]]:
    return [
        {
            "strategy_id": "compare_steps",
            "strategy_version": "1",
            "catalog_version": CATALOG_VERSION,
            "version_set_id": VERSION_SET_ID,
            "baseline": True,
        },
        {
            "strategy_id": "fill_step",
            "strategy_version": "1",
            "catalog_version": CATALOG_VERSION,
            "version_set_id": VERSION_SET_ID,
            "baseline": False,
        },
    ]


def _exposure(
    index: int,
    *,
    strategy_id: str = "compare_steps",
    success: bool = True,
    status: str = "observed",
    learner_id: str | None = None,
) -> dict[str, object]:
    outcome: dict[str, object] = {
        "status": status,
        "success": success,
        "root_fact_seq": index + 100,
        "certified": True,
        "provenance_complete": True,
    }
    return {
        "root_fact_seq": index + 1,
        "source_id": f"source-{index:03d}",
        "exposure_id": f"exposure-{strategy_id}-{index:03d}",
        "learner_id": learner_id or f"learner-{index:03d}",
        "topic_id": "calculus.chain_rule",
        "hypothesis_id": "omit_inner_derivative",
        "episode_id": f"episode-{index:03d}",
        "question_family_id": "chain.common",
        "difficulty_bucket": "2",
        "hint_state": "none",
        "strategy_id": strategy_id,
        "strategy_version": "1",
        "catalog_version": CATALOG_VERSION,
        "version_set_id": VERSION_SET_ID,
        "question_committed": True,
        "strategy_determined_before_commit": True,
        "provenance_complete": True,
        "exposed_at": f"2026-09-{1 + index // 24:02d}T{index % 24:02d}:00:00Z",
        "outcomes": {
            "immediate": dict(outcome),
            "transfer": dict(outcome),
            "retention": {
                **outcome,
                "interval_hours": 24,
                "independent_family": True,
                "hint_used": False,
                "obligation_completed": True,
            },
        },
    }


def _snapshot(exposures: list[dict[str, object]]) -> dict[str, object]:
    return {
        "as_of_root_fact_seq": 10_000,
        "catalog_version": CATALOG_VERSION,
        "version_set_id": VERSION_SET_ID,
        "catalog": _catalog(),
        "exposures": exposures,
        "generated_at": "ignored by the pure report",
    }


def test_report_is_replay_stable_and_compares_each_endpoint_separately() -> None:
    baseline = [
        _exposure(index, success=index < 10) for index in range(20)
    ]
    strategy = [
        _exposure(index + 30, strategy_id="fill_step", success=index < 15)
        for index in range(20)
    ]
    duplicate = dict(strategy[0])
    duplicate["source_id"] = "zz-replayed-after-original"

    first = build_cognitive_strategy_report(_snapshot(strategy + [duplicate] + baseline))
    second = build_cognitive_strategy_report(_snapshot(list(reversed(baseline + strategy))))

    assert first["input"]["exposure_n"] == 40
    assert first["input"]["independent_exposure_n"] == 40
    assert first["input"]["within_24h_duplicate_n"] == 0
    assert len(first["input"]["snapshot_sha256"]) == 64
    assert first["input"]["dedupe_order"] == [
        "root_fact_seq",
        "source_id",
        "exposure_id",
    ]
    assert first["catalog_coverage"] == [
        {
            "strategy_id": "compare_steps",
            "strategy_version": "1",
            "baseline": True,
            "exposure_n": 20,
        },
        {
            "strategy_id": "fill_step",
            "strategy_version": "1",
            "baseline": False,
            "exposure_n": 20,
        },
    ]
    assert first["endpoints"] == second["endpoints"]
    assert first["input"]["snapshot_sha256"] == second["input"]["snapshot_sha256"]
    assert first["content_sha256"] == second["content_sha256"]
    assert report_digest(first) == first["content_sha256"]
    for endpoint in ("immediate", "transfer", "retention"):
        comparison = first["endpoints"][endpoint]["strata"][0]["comparisons"][0]
        assert comparison["status"] == "comparable"
        assert comparison["effect"] == {
            "numerator": 1,
            "denominator": 4,
            "value": 0.25,
        }
        assert first["endpoints"][endpoint]["aggregate_comparisons"][0][
            "effect"
        ]["value"] == 0.25


def test_under_twenty_is_insufficient_and_missing_has_sensitivity_bounds() -> None:
    exposures = [_exposure(index, success=index < 10) for index in range(20)]
    exposures.extend(
        _exposure(index + 40, strategy_id="fill_step", success=True)
        for index in range(19)
    )
    missing = _exposure(80, strategy_id="fill_step")
    missing["outcomes"] = {
        endpoint: {"status": "missing_outcome"}
        for endpoint in ("immediate", "transfer", "retention")
    }
    exposures.append(missing)

    report = build_cognitive_strategy_report(_snapshot(exposures))
    comparison = report["endpoints"]["immediate"]["strata"][0]["comparisons"][0]

    assert comparison["status"] == "insufficient_data"
    assert comparison["effect"] is None
    assert comparison["sensitivity_bounds"] is None
    strategy = report["endpoints"]["immediate"]["strata"][0]["strategies"][1]
    assert strategy["eligible_n"] == 19
    assert strategy["missing_outcome_n"] == 1


def test_statuses_exclusions_and_rolling_twenty_four_hour_independence() -> None:
    records = [
        _exposure(1, learner_id="same"),
        _exposure(2, learner_id="same"),
        _exposure(3, learner_id="same"),
    ]
    for index, hour in enumerate((0, 1, 25)):
        records[index]["episode_id"] = "same-episode"
        records[index]["exposed_at"] = f"2026-09-01T{hour % 24:02d}:00:00Z" if hour < 24 else "2026-09-02T01:00:00Z"
    pending = _exposure(10)
    pending["outcomes"] = {endpoint: {"status": "pending"} for endpoint in ("immediate", "transfer", "retention")}
    missing = _exposure(11)
    missing["outcomes"] = {endpoint: {"status": "missing_outcome"} for endpoint in ("immediate", "transfer", "retention")}
    abandoned = _exposure(12)
    abandoned["status"] = "abandoned"
    replaced = _exposure(13)
    replaced["status"] = "replaced"
    records.extend((pending, missing, abandoned, replaced))
    for offset, reason in enumerate(EXCLUSION_REASONS, start=20):
        invalid = _exposure(offset)
        invalid["exclusion_reasons"] = [reason]
        records.append(invalid)

    report = build_cognitive_strategy_report(_snapshot(records))
    immediate = report["endpoints"]["immediate"]

    assert report["input"]["independent_exposure_n"] == 6
    assert immediate["status_counts"] == {
        "observed": 3,
        "pending": 1,
        "missing_outcome": 1,
        "abandoned": 1,
        "replaced": 1,
        "excluded": len(EXCLUSION_REASONS),
    }
    assert immediate["exclusion_reason_counts"] == {
        reason: 1 for reason in EXCLUSION_REASONS
    }
    stats = immediate["strata"][0]["strategies"][0]
    assert stats["exposure_n"] == 7
    assert stats["independent_n"] == 6
    assert stats["eligible_n"] == 2


def test_cli_writes_exact_canonical_json_and_deterministic_markdown(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    json_path = tmp_path / "report.json"
    markdown_path = tmp_path / "report.md"
    snapshot_path.write_text(json.dumps(_snapshot([_exposure(1)])), encoding="utf-8")

    assert main(["--input", str(snapshot_path), "--output", str(json_path)]) == 0
    report = build_cognitive_strategy_report(_snapshot([_exposure(1)]))
    assert json_path.read_text(encoding="utf-8") == canonical_report_json(report)

    assert (
        main(
            [
                "--input",
                str(snapshot_path),
                "--format",
                "markdown",
                "--output",
                str(markdown_path),
            ]
        )
        == 0
    )
    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown == render_report_markdown(report)
    assert report["content_sha256"] in markdown
    assert "generated_at" not in canonical_report_json(report)


def test_cli_reads_database_in_query_only_mode_and_freezes_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "study.db"
    report_path = tmp_path / "database-report.json"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE cognitive_fact_roots (
            root_fact_seq INTEGER PRIMARY KEY,
            effective_at TEXT NOT NULL
        )"""
    )
    connection.executemany(
        "INSERT INTO cognitive_fact_roots(root_fact_seq, effective_at) VALUES (?, ?)",
        [
            (1, "2026-09-08T08:00:00Z"),
            (2, "2026-09-08T08:01:00Z"),
        ],
    )
    create_cognitive_strategy_schema(connection)

    class _Store:
        def _require_conn(self) -> sqlite3.Connection:
            return connection

    exposure_payload = {
            "event_type": "question_committed",
            "question_id": "question-database-report",
            "strategy_id": "chain.omit-inner.complete-steps",
            "strategy_version": "v1",
            "catalog_version": "cognitive-strategy-catalog-v1",
            "version_set_id": VERSION_SET_ID,
            "question_purpose": "repair",
            "topic_id": "calculus.chain_rule",
            "learner_id": "local",
            "hypothesis_id": "calculus.chain_rule:omit_inner_derivative",
            "hypothesis_code": "omit_inner_derivative",
            "decision_id": "decision-database-report",
            "blueprint_id": "chain.omit-inner.fill-factor.v1",
            "diagnostic_validation_id": "validation-database-report",
            "policy_version": "cognitive-intent-policy-v2",
            "validator_version": "cognitive-question-validator-v2.1-1",
            "question_family_id": "chain.common",
            "difficulty_bucket": "3",
            "strategy_family": "complete_steps",
            "comparison_scope_id": "chain.omit-inner.repair",
            "baseline": True,
            "strategy_determined_before_commit": True,
            "provenance_complete": True,
            "source_id": "question-database-report",
            "root_fact_seq": 1,
            "occurred_at": "2026-09-08T08:00:00Z",
            "answer_window_expires_at": "2026-09-09T08:00:00Z",
    }
    exposure = insert_cognitive_strategy_exposure(
        _Store(),
        connection,
        exposure_payload,
    )
    insert_cognitive_strategy_exposure(
        _Store(),
        connection,
        {
            **exposure_payload,
            "question_id": "question-database-pending",
            "decision_id": "decision-database-pending",
            "diagnostic_validation_id": "validation-database-pending",
            "source_id": "question-database-pending",
        },
    )
    insert_cognitive_strategy_fact(
        _Store(),
        connection,
        {
            "exposure_id": exposure["exposure_id"],
            "fact_type": "attempt",
            "source_id": "attempt-database-report",
            "root_fact_seq": 2,
            "attempt_id": "attempt-database-report",
            "question_id": "question-database-report",
            "outcome": "correct",
            "status": "observed",
            "evaluator_type": "deterministic",
            "evaluator_version": "deterministic-v1",
            "evaluator_confidence": 1.0,
            "used_hint": False,
            "certified": True,
            "provenance_complete": True,
            "occurred_at": "2026-09-08T08:01:00Z",
        },
    )
    connection.commit()
    connection.close()

    assert main(["--database", str(database), "--output", str(report_path)]) == 0
    first = report_path.read_bytes()
    assert main(["--database", str(database), "--output", str(report_path)]) == 0
    assert report_path.read_bytes() == first
    payload = json.loads(first)
    assert payload["boundary"] == {
        "as_of_root_fact_seq": 2,
        "catalog_version": "cognitive-strategy-catalog-v1",
        "version_set_id": VERSION_SET_ID,
    }
    assert payload["endpoints"]["immediate"]["status_counts"]["observed"] == 1
    assert payload["endpoints"]["immediate"]["status_counts"]["pending"] == 1

    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO cognitive_fact_roots(root_fact_seq, effective_at) VALUES (?, ?)",
        (3, "2026-09-10T08:00:00Z"),
    )
    connection.commit()
    connection.close()
    assert main(["--database", str(database), "--output", str(report_path)]) == 0
    expired = json.loads(report_path.read_bytes())
    assert expired["endpoints"]["immediate"]["status_counts"][
        "missing_outcome"
    ] == 1
