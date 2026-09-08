"""Run the V3 strategy ledger and reporter with isolated synthetic facts.

This is engineering acceptance only.  It writes no learner answers, makes no
network or model calls, and never opens the configured Study Companion database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from adaptive_learning.cognitive_strategy_report import (
    build_cognitive_strategy_report,
    render_report_markdown,
)
from store_cognitive_strategy import (
    build_cognitive_strategy_report_snapshot,
    create_cognitive_strategy_schema,
    insert_cognitive_strategy_exposure,
    insert_cognitive_strategy_fact,
)

EVIDENCE_CLASS = "synthetic_engineering_only"
CATALOG_VERSION = "cognitive-strategy-catalog-v1"
VERSION_SET_ID = "cognitive-v2.1-1"
COMPARISON_FAMILY_ID = "chain.omit-inner.repair.v1"
REPORT_JSON = "cognitive-v3-synthetic-acceptance.json"
REPORT_MARKDOWN = "cognitive-v3-synthetic-acceptance.md"
_START = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


class _Store:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _require_conn(self) -> sqlite3.Connection:
        return self.connection


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _root(
    connection: sqlite3.Connection, sequence: int, effective_at: datetime
) -> None:
    connection.execute(
        "INSERT INTO cognitive_fact_roots(root_fact_seq, effective_at) VALUES (?, ?)",
        (sequence, _iso(effective_at)),
    )


def _fact(
    *,
    exposure_id: str,
    fact_type: str,
    index: int,
    root_fact_seq: int,
    occurred_at: datetime,
    outcome: str = "",
) -> dict[str, object]:
    value: dict[str, object] = {
        "exposure_id": exposure_id,
        "fact_type": fact_type,
        "source_id": f"synthetic-{fact_type}-{index:02d}",
        "root_fact_seq": root_fact_seq,
        "occurred_at": _iso(occurred_at),
        "provenance_complete": True,
    }
    if fact_type in {"attempt", "transfer"}:
        value.update(
            {
                "attempt_id": f"synthetic-{fact_type}-attempt-{index:02d}",
                "question_id": f"synthetic-{fact_type}-question-{index:02d}",
                "outcome": outcome,
                "status": "observed",
                "evaluator_type": "deterministic",
                "evaluator_version": "synthetic-acceptance-v1",
                "evaluator_confidence": 1.0,
                "used_hint": False,
                "certified": True,
                "response_time_ms": 1_200,
            }
        )
    elif fact_type == "episode":
        value.update(
            {
                "episode_id": f"synthetic-episode-{index:02d}",
                "status": "open",
            }
        )
    elif fact_type == "retention":
        value.update(
            {
                "episode_id": f"synthetic-episode-{index:02d}",
                "attempt_id": f"synthetic-retention-attempt-{index:02d}",
                "outcome": outcome,
                "status": "observed",
                "evaluator_type": "deterministic",
                "evaluator_version": "synthetic-acceptance-v1",
                "evaluator_confidence": 1.0,
                "used_hint": False,
                "certified": True,
                "interval_hours": 26.0,
                "independent_family": True,
                "obligation_completed": True,
                "development_time_override": False,
                "answer_disclosed": False,
                "independence_known": True,
            }
        )
    return value


def _populate(connection: sqlite3.Connection) -> None:
    store = _Store(connection)
    strategies = (
        {
            "strategy_id": "chain.omit-inner.complete-steps",
            "strategy_family": "complete_steps",
            "question_family_id": "chain.cos-cube.fill-factor",
            "blueprint_id": "chain.omit-inner.fill-factor.v1",
            "baseline": True,
            "success_n": 10,
        },
        {
            "strategy_id": "chain.omit-inner.minimal-change",
            "strategy_family": "minimal_change",
            "question_family_id": "chain.sin-power.minimal-change",
            "blueprint_id": "chain.omit-inner.minimal-change.v1",
            "baseline": False,
            "success_n": 15,
        },
    )
    for index in range(40):
        strategy = strategies[index // 20]
        within_strategy = index % 20
        exposed_at = _START + timedelta(minutes=index)
        exposure_seq = index + 1
        _root(connection, exposure_seq, exposed_at)
        exposure = insert_cognitive_strategy_exposure(
            store,
            connection,
            {
                "event_type": "question_committed",
                "question_id": f"synthetic-repair-question-{index:02d}",
                "strategy_id": strategy["strategy_id"],
                "strategy_version": "v1",
                "catalog_version": CATALOG_VERSION,
                "version_set_id": VERSION_SET_ID,
                "question_purpose": "repair",
                "topic_id": "calculus.chain_rule",
                "learner_id": f"synthetic-learner-{index:02d}",
                "hypothesis_id": "calculus.chain_rule:omit_inner_derivative",
                "hypothesis_code": "omit_inner_derivative",
                "decision_id": f"synthetic-decision-{index:02d}",
                "blueprint_id": strategy["blueprint_id"],
                "diagnostic_validation_id": f"synthetic-validation-{index:02d}",
                "policy_version": "cognitive-intent-policy-v2",
                "validator_version": "cognitive-question-validator-v2.1-1",
                "question_family_id": strategy["question_family_id"],
                "comparison_family_id": COMPARISON_FAMILY_ID,
                "difficulty_bucket": "3",
                "strategy_family": strategy["strategy_family"],
                "comparison_scope_id": "chain.omit-inner.repair",
                "baseline": strategy["baseline"],
                "strategy_determined_before_commit": True,
                "provenance_complete": True,
                "source_id": f"synthetic-question-event-{index:02d}",
                "root_fact_seq": exposure_seq,
                "occurred_at": _iso(exposed_at),
                "answer_window_expires_at": _iso(exposed_at + timedelta(hours=24)),
            },
        )
        succeeded = within_strategy < int(strategy["success_n"])
        outcome = "correct" if succeeded else "incorrect"
        facts = (
            ("attempt", index + 41, exposed_at + timedelta(minutes=5)),
            ("transfer", index + 81, exposed_at + timedelta(hours=1)),
            ("episode", index + 121, exposed_at + timedelta(hours=1, minutes=1)),
            ("retention", index + 161, exposed_at + timedelta(hours=26)),
        )
        for fact_type, fact_seq, occurred_at in facts:
            _root(connection, fact_seq, occurred_at)
            insert_cognitive_strategy_fact(
                store,
                connection,
                _fact(
                    exposure_id=str(exposure["exposure_id"]),
                    fact_type=fact_type,
                    index=index,
                    root_fact_seq=fact_seq,
                    occurred_at=occurred_at,
                    outcome=outcome,
                ),
            )
    connection.commit()


def run_acceptance(*, report_dir: Path) -> dict[str, Any]:
    """Execute the production ledger/snapshot/reporter chain in memory."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE cognitive_fact_roots (
            root_fact_seq INTEGER PRIMARY KEY,
            effective_at TEXT NOT NULL
        )
        """
    )
    create_cognitive_strategy_schema(connection)
    try:
        _populate(connection)
        store = _Store(connection)
        snapshot = build_cognitive_strategy_report_snapshot(store)
        report = build_cognitive_strategy_report(snapshot)
        endpoint_results: dict[str, dict[str, Any]] = {}
        for endpoint in ("immediate", "transfer", "retention"):
            payload = report["endpoints"][endpoint]
            comparison = payload["strata"][0]["comparisons"][0]
            endpoint_results[endpoint] = {
                "status": comparison["status"],
                "effect": comparison["effect"],
                "observed_n": payload["status_counts"]["observed"],
            }
        checks = {
            "ledger_exposure_n": connection.execute(
                "SELECT COUNT(*) FROM cognitive_strategy_exposures"
            ).fetchone()[0],
            "ledger_fact_n": connection.execute(
                "SELECT COUNT(*) FROM cognitive_strategy_exposure_facts"
            ).fetchone()[0],
            "independent_exposure_n": report["input"]["independent_exposure_n"],
            "common_strata_n": len(report["endpoints"]["immediate"]["strata"]),
            "all_endpoints_comparable": all(
                result["status"] == "comparable"
                for result in endpoint_results.values()
            ),
        }
        passed = checks == {
            "ledger_exposure_n": 40,
            "ledger_fact_n": 160,
            "independent_exposure_n": 40,
            "common_strata_n": 1,
            "all_endpoints_comparable": True,
        }
        result: dict[str, Any] = {
            "evidence_class": EVIDENCE_CLASS,
            "human_effectiveness_claim": False,
            "live_database_touched": False,
            "summary": {"status": "PASS" if passed else "FAIL"},
            "checks": checks,
            "contract": {
                "comparison_family_id": COMPARISON_FAMILY_ID,
                "delivered_question_family_ids": [
                    "chain.cos-cube.fill-factor",
                    "chain.sin-power.minimal-change",
                ],
                "minimum_comparison_n_per_strategy": 20,
            },
            "endpoints": endpoint_results,
            "report": report,
        }
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / REPORT_JSON).write_text(
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        markdown = (
            "# V3 Synthetic Engineering Acceptance\n\n"
            f"- Evidence class: `{EVIDENCE_CLASS}`\n"
            f"- Status: `{'PASS' if passed else 'FAIL'}`\n"
            "- Human effectiveness claim: `false`\n"
            "- Live database touched: `false`\n\n"
            + render_report_markdown(report)
        )
        (report_dir / REPORT_MARKDOWN).write_text(markdown, encoding="utf-8")
        return result
    finally:
        connection.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_acceptance(report_dir=args.report_dir)
    except (OSError, sqlite3.DatabaseError, ValueError, KeyError, IndexError):
        print(json.dumps({"status": "ERROR"}, sort_keys=True), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "evidence_class": result["evidence_class"],
                "status": result["summary"]["status"],
                "checks": result["checks"],
            },
            sort_keys=True,
        )
    )
    return 0 if result["summary"]["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["EVIDENCE_CLASS", "REPORT_JSON", "REPORT_MARKDOWN", "run_acceptance"]
