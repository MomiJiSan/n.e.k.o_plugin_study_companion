"""Run deterministic V3 personalization acceptance against isolated facts.

The tool creates a temporary production ``StudyStore`` and drives the same
ledger, selector, question writer, and answer transaction used at runtime. It
does not open the configured learner database or call a network/model service.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sqlite3
import sys
import tempfile
import tomllib
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = "_cognitive_personalization_acceptance_runtime"
if _PACKAGE not in sys.modules:
    package = ModuleType(_PACKAGE)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    sys.modules[_PACKAGE] = package
mode_manager = ModuleType(f"{_PACKAGE}.mode_manager")
mode_manager.normalize_mode = lambda value: str(  # type: ignore[attr-defined]
    value or "companion"
)
sys.modules.setdefault(mode_manager.__name__, mode_manager)

_personalization = importlib.import_module(
    f"{_PACKAGE}.adaptive_learning.cognitive_personalization"
)
_report = importlib.import_module(
    f"{_PACKAGE}.adaptive_learning.cognitive_strategy_report"
)
_policy = importlib.import_module(f"{_PACKAGE}.adaptive_learning.cognitive_policy")
_contracts = importlib.import_module(f"{_PACKAGE}.adaptive_learning.contracts")
_persistence = importlib.import_module(f"{_PACKAGE}.store_cognitive_personalization")
_strategy_store = importlib.import_module(f"{_PACKAGE}.store_cognitive_strategy")
_models = importlib.import_module(f"{_PACKAGE}.models")
_runtime = importlib.import_module(f"{_PACKAGE}.cognitive_personalization_runtime")
_store_module = importlib.import_module(f"{_PACKAGE}.store")

ALTERNATE = _personalization.ALTERNATE
BASELINE = _personalization.BASELINE
FAMILY = _personalization.FAMILY
VERSION_SET = _personalization.VERSION_SET
select_personalized_strategy = _personalization.select_personalized_strategy
HypothesisRef = _contracts.HypothesisRef
PracticeSelection = _contracts.PracticeSelection
QuestionPlan = _contracts.QuestionPlan
TopicRef = _contracts.TopicRef
decide_from_store = _persistence.decide_from_store
insert_cognitive_strategy_exposure = _strategy_store.insert_cognitive_strategy_exposure
insert_cognitive_strategy_fact = _strategy_store.insert_cognitive_strategy_fact
build_cognitive_strategy_report_snapshot = _strategy_store.build_cognitive_strategy_report_snapshot
build_cognitive_strategy_report = _report.build_cognitive_strategy_report

EVIDENCE_CLASS = "synthetic_engineering_only"
REPORT_JSON = "cognitive-v3-personalization-deterministic.json"
REPORT_MARKDOWN = "cognitive-v3-personalization-deterministic.md"
NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
REQUIRED_CASES = frozenset(
    {
        "default_off",
        "strong_evidence",
        "insufficient_evidence",
        "stale_evidence",
        "conflicting_evidence",
        "incompatible_version",
        "invalid_evidence",
        "uncertain_without_exploration",
        "uncertain_with_exploration",
        "exposure_limit",
        "consecutive_failure_stop",
        "user_stop",
    }
)


class _Logger:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _decision() -> Any:
    topic = TopicRef(id="college_chain_rule", name="Chain rule")
    original = QuestionPlan(
        plan_id="plan-personalization-acceptance",
        selection=PracticeSelection(
            reason="wrong_retry",
            target_topic=topic,
            eligible_topic_ids=("college_chain_rule",),
        ),
        difficulty=3,
        question_type="math_reasoning",
    )
    hypothesis = HypothesisRef(
        hypothesis_id="hypothesis-active",
        topic_id="college_chain_rule",
        code="omit_inner_derivative",
        status="supported",
        probability=0.9,
        model_version=VERSION_SET,
        source_snapshot_id="snapshot-active",
        source_attempt_id="attempt-source",
        projection_generation=7,
    )
    proposed = replace(
        original,
        learning_intent="misconception_repair",
        hypothesis_target=hypothesis,
        repair_strategy="complete_inner_derivative",
    )
    return _policy.CognitivePolicyDecision(
        mode="on",
        original_plan=original,
        proposed_plan=proposed,
        effective_plan=proposed,
        proposed_intent="misconception_repair",
        selected_hypothesis=hypothesis,
        repair_strategy="complete_inner_derivative",
        applied=True,
        decision_trace=("policy:accepted",),
    )


def _open_store(directory: Path) -> Any:
    store = _store_module.StudyStore(
        directory / "synthetic-personalization.db",
        directory / "empty-seed.json",
        _Logger(),
    )
    store.open()
    store.ensure_topic(topic_id="college_chain_rule", name="Chain rule")
    store.ensure_topic(topic_id="calculus.chain_rule", name="Chain rule canonical")
    store.batch_write_answer_data(
        session_id="synthetic-source-session",
        mode="companion",
        topic_id="college_chain_rule",
        question={
            "question_id": "source-question",
            "question": "Synthetic source question",
            "answer": "synthetic",
            "question_type": "math_exact",
            "difficulty": 3,
        },
        user_answer="synthetic wrong answer",
        eval_result={"verdict": "wrong", "score": 0},
        response_time_ms=100,
        attempt_id="attempt-source",
    )
    conn = store._require_conn()
    conn.execute(
        """INSERT INTO cognitive_topic_projection_queue (
        topic_id, model_version, status, requested_generation,
        claimed_generation, projected_generation)
        VALUES ('college_chain_rule', ?, 'done', 7, 7, 7)""",
        (VERSION_SET,),
    )
    conn.execute(
        """INSERT INTO cognitive_hypothesis_current (
        hypothesis_id, topic_id, hypothesis_code, evidence_status,
        intervention_stage, user_override, status, probability,
        support_count, counter_count, diagnostic_support_count, relapse_count,
        source_attempt_id, source_snapshot_id, model_version, projected_generation)
        VALUES ('hypothesis-active', 'college_chain_rule',
        'omit_inner_derivative', 'supported', 'idle', '', 'supported', 0.9,
        2, 0, 0, 0, 'attempt-source', 'snapshot-active', ?, 7)""",
        (VERSION_SET,),
    )
    conn.commit()
    return store


def _root(conn: sqlite3.Connection, source_id: str, occurred_at: datetime) -> int:
    return int(
        conn.execute(
            """INSERT INTO cognitive_fact_roots(fact_type, source_id, effective_at)
            VALUES ('synthetic_personalization_acceptance', ?, ?)""",
            (source_id, _iso(occurred_at)),
        ).lastrowid
    )


def _populate(store: Any, *, now: datetime) -> None:
    conn = store._require_conn()
    for index in range(40):
        alternate = index >= 20
        within = index % 20
        exposed_at = now - timedelta(days=3, minutes=index)
        exposure_seq = _root(conn, f"synthetic-exposure-{index:02d}", exposed_at)
        exposure = insert_cognitive_strategy_exposure(
            store,
            conn,
            {
                "event_type": "question_committed",
                "question_id": f"synthetic-question-{index:02d}",
                "strategy_id": ALTERNATE if alternate else BASELINE,
                "strategy_version": "v1",
                "catalog_version": "cognitive-strategy-catalog-v1",
                "version_set_id": VERSION_SET,
                "question_purpose": "repair",
                "topic_id": "calculus.chain_rule",
                "learner_id": "local",
                "hypothesis_id": "hypothesis-active",
                "hypothesis_code": "omit_inner_derivative",
                "decision_id": f"synthetic-decision-{index:02d}",
                "blueprint_id": "chain.omit-inner.minimal-change.v1" if alternate else "chain.omit-inner.fill-factor.v1",
                "diagnostic_validation_id": f"synthetic-validation-{index:02d}",
                "policy_version": "cognitive-intent-policy-v2",
                "validator_version": "cognitive-question-validator-v2.1-1",
                "question_family_id": "chain.sin-power.minimal-change" if alternate else "chain.cos-cube.fill-factor",
                "comparison_family_id": FAMILY,
                "difficulty_bucket": "3",
                "strategy_family": "minimal_change" if alternate else "complete_steps",
                "comparison_scope_id": "chain.omit-inner.repair",
                "baseline": not alternate,
                "strategy_determined_before_commit": True,
                "provenance_complete": True,
                "source_id": f"synthetic-question-event-{index:02d}",
                "root_fact_seq": exposure_seq,
                "occurred_at": _iso(exposed_at),
                "answer_window_expires_at": _iso(exposed_at + timedelta(hours=24)),
            },
        )
        outcome = "correct" if alternate or within < 2 else "wrong"
        for offset, fact_type in enumerate(("attempt", "transfer", "episode", "retention"), 1):
            occurred_at = exposed_at + timedelta(hours=26 if fact_type == "retention" else offset)
            fact_seq = _root(
                conn,
                f"synthetic-{fact_type}-root-{index:02d}",
                occurred_at,
            )
            fact: dict[str, Any] = {
                "exposure_id": exposure["exposure_id"],
                "fact_type": fact_type,
                "source_id": f"synthetic-{fact_type}-{index:02d}",
                "root_fact_seq": fact_seq,
                "occurred_at": _iso(occurred_at),
                "provenance_complete": True,
                "status": "observed",
            }
            if fact_type in {"attempt", "transfer", "retention"}:
                fact.update(
                    outcome=outcome,
                    attempt_id=f"synthetic-{fact_type}-attempt-{index:02d}",
                    question_id=f"synthetic-{fact_type}-question-{index:02d}",
                    evaluator_type="deterministic",
                    evaluator_version="synthetic-personalization-v1",
                    evaluator_confidence=1.0,
                    used_hint=False,
                    certified=True,
                )
            if fact_type == "episode":
                fact.update(episode_id=f"synthetic-episode-{index:02d}")
            if fact_type == "retention":
                fact.update(
                    episode_id=f"synthetic-episode-{index:02d}",
                    interval_hours=26,
                    independent_family=True,
                    obligation_completed=True,
                    development_time_override=False,
                    answer_disclosed=False,
                    independence_known=True,
                )
            insert_cognitive_strategy_fact(store, conn, fact)
    conn.commit()


def _case(
    snapshot: dict[str, Any],
    *,
    now: datetime = NOW,
    **kwargs: Any,
) -> dict[str, Any]:
    selected, audit = select_personalized_strategy(
        _decision(), snapshot, now=now, enabled=True, **kwargs
    )
    return {
        "strategy": "alternate" if selected.repair_strategy == "minimal_change" else "baseline",
        "reason": audit["reason"],
    }


def _disabled_case(snapshot: dict[str, Any]) -> dict[str, Any]:
    selected, audit = select_personalized_strategy(_decision(), snapshot, now=NOW)
    return {
        "strategy": "alternate" if selected.repair_strategy == "minimal_change" else "baseline",
        "reason": audit["reason"],
    }


def _uncertain(snapshot: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(snapshot)
    grouped: dict[str, list[dict[str, Any]]] = {BASELINE: [], ALTERNATE: []}
    for row in result["exposures"]:
        grouped[str(row["strategy_id"])].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: str(row["exposure_id"]))
        for index, row in enumerate(rows):
            success = index < (10 if row["strategy_id"] == BASELINE else 13)
            for endpoint in ("immediate", "transfer", "retention"):
                row["outcomes"][endpoint]["success"] = success
    return result


def _production_defaults_are_closed() -> bool:
    fields = (
        "strategy_personalization_enabled",
        "strategy_personalization_exploration_enabled",
        "strategy_personalization_stopped",
    )
    config = _models.CognitiveConfig()
    manifest = tomllib.loads((ROOT / "plugin.toml").read_text(encoding="utf-8"))[
        "cognitive"
    ]
    tracker = SimpleNamespace(
        cognitive_strategy_shadow_enabled=False,
        _cognitive_version_set_id=VERSION_SET,
    )
    owner = SimpleNamespace(
        _cfg=SimpleNamespace(cognitive=config),
        _knowledge_tracker=tracker,
    )
    return (
        all(getattr(config, field) is False for field in fields)
        and all(manifest.get(field) is False for field in fields)
        and _runtime.personalization_enabled(owner) is False
    )


def _personalized_question_event(
    audit: dict[str, Any],
    *,
    now: datetime,
    event_id: str = "personalized-question-event",
    decision_id: str = "personalized-decision",
    question_id: str = "personalized-question",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": "question_committed",
        "decision_id": decision_id,
        "hypothesis_target": {
            "hypothesis_id": "hypothesis-active",
            "topic_id": "college_chain_rule",
            "code": "omit_inner_derivative",
            "status": "supported",
            "probability": 0.9,
            "model_version": VERSION_SET,
            "source_snapshot_id": "snapshot-active",
            "source_attempt_id": "attempt-source",
            "projection_generation": 7,
        },
        "learning_intent": "misconception_repair",
        "repair_strategy": "minimal_change",
        "binding": {
            "plan_id": "plan-personalization-acceptance",
            "topic_id": "college_chain_rule",
            "selection_reason": "wrong_retry",
            "eligible_topic_ids": ["college_chain_rule"],
            "learning_plan_id": "synthetic-plan",
            "learning_plan_revision": 1,
            "scope_key": "synthetic-scope",
            "scope_revision": 1,
            "origin_wrong_question_id": "",
            "source_question_id": "source-question",
            "target_binding": {"learning_plan_revision": 1},
        },
        "question_id": question_id,
        "attempt_id": "",
        "blueprint_id": "chain.omit-inner.minimal-change.v1",
        "question_family_id": "chain.sin-power.minimal-change",
        "diagnostic_validation_id": "synthetic-personalization-validation",
        "evaluation_verdict": "",
        "policy_version": "cognitive-intent-policy-v2",
        "validator_version": "cognitive-question-validator-v2.1-1",
        "schema_version": 1,
        "metadata": {
            "strategy_personalization": audit,
            "strategy_exposure": {
                "strategy_id": ALTERNATE,
                "strategy_version": "v1",
                "catalog_version": "cognitive-strategy-catalog-v1",
                "version_set_id": VERSION_SET,
                "question_purpose": "repair",
                "difficulty_bucket": "3",
                "strategy_family": "minimal_change",
                "comparison_scope_id": "chain.omit-inner.repair",
                "comparison_family_id": FAMILY,
                "baseline": False,
                "answer_window_expires_at": _iso(now + timedelta(days=1)),
            },
        },
        "created_at": _iso(now),
    }


def _write_markdown(
    report_dir: Path,
    *,
    passed: bool,
    cases: dict[str, dict[str, Any]],
    checks: dict[str, Any],
) -> None:
    rows = "\n".join(
        f"| {name} | {value['strategy']} | {value['reason']} |"
        for name, value in cases.items()
    )
    markdown = (
        "# V3 Personalization Synthetic Acceptance\n\n"
        f"- Evidence class: `{EVIDENCE_CLASS}`\n"
        f"- Status: `{'PASS' if passed else 'FAIL'}`\n"
        "- Human effectiveness claim: `false`\n"
        "- Live database touched: `false`\n"
        "- Temporary production StudyStore used: `true`\n"
        f"- Canonical question recorded: `{str(checks['canonical_question_recorded']).lower()}`\n"
        f"- Canonical answer recorded: `{str(checks['canonical_answer_recorded']).lower()}`\n"
        f"- Outcome projection updated: `{str(checks['outcome_projection_updated']).lower()}`\n"
        f"- Concurrent delivery rejected: `{str(checks['concurrent_delivery_rejected']).lower()}`\n\n"
        "| Case | Strategy | Reason |\n| --- | --- | --- |\n"
        f"{rows}\n"
    )
    (report_dir / REPORT_MARKDOWN).write_text(
        markdown,
        encoding="utf-8",
        newline="\n",
    )


def run_acceptance(*, report_dir: Path) -> dict[str, Any]:
    run_now = datetime.now(timezone.utc).replace(microsecond=0)
    with tempfile.TemporaryDirectory(prefix="cognitive-personalization-") as temp:
        store = _open_store(Path(temp))
        try:
            _populate(store, now=run_now)
            selected, audit = decide_from_store(store, _decision(), now=run_now)
            snapshot = build_cognitive_strategy_report_snapshot(
                store,
                version_set_id=VERSION_SET,
            )
            stale = deepcopy(snapshot)
            for row in stale["exposures"]:
                row["exposed_at"] = _iso(run_now - timedelta(days=9))
            insufficient = deepcopy(snapshot)
            insufficient["exposures"].pop()
            conflict = deepcopy(snapshot)
            for row in conflict["exposures"]:
                if row["strategy_id"] == ALTERNATE:
                    row["outcomes"]["retention"]["success"] = False
            incompatible = deepcopy(snapshot)
            incompatible["version_set_id"] = "unsupported-version"
            invalid = deepcopy(snapshot)
            del invalid["exposures"][0]["exposed_at"]
            uncertain = _uncertain(snapshot)
            cases = {
                "default_off": _disabled_case(snapshot),
                "strong_evidence": {
                    "strategy": (
                        "alternate"
                        if selected.repair_strategy == "minimal_change"
                        else "baseline"
                    ),
                    "reason": audit["reason"],
                },
                "insufficient_evidence": _case(insufficient, now=run_now),
                "stale_evidence": _case(stale, now=run_now),
                "conflicting_evidence": _case(conflict, now=run_now),
                "incompatible_version": _case(incompatible, now=run_now),
                "invalid_evidence": _case(invalid, now=run_now),
                "uncertain_without_exploration": _case(uncertain, now=run_now),
                "uncertain_with_exploration": _case(
                    uncertain,
                    now=run_now,
                    explore=True,
                ),
                "exposure_limit": _case(snapshot, now=run_now, alternate_n=3),
                "consecutive_failure_stop": _case(
                    snapshot,
                    now=run_now,
                    consecutive_failures=2,
                ),
                "user_stop": _case(snapshot, now=run_now, stopped=True),
            }

            question_event = _personalized_question_event(audit, now=run_now)
            committed = store.record_cognitive_intervention_event(question_event)
            before_answer = build_cognitive_strategy_report_snapshot(
                store,
                version_set_id=VERSION_SET,
            )
            before_hash = build_cognitive_strategy_report(before_answer)[
                "content_sha256"
            ]
            attempt_event = {
                **question_event,
                "event_id": "personalized-attempt-event",
                "event_type": "attempt_committed",
                "attempt_id": "personalized-attempt",
                "evaluation_verdict": "wrong",
                "metadata": {},
            }
            answer_result = store.batch_write_answer_data(
                session_id="synthetic-personalization-session",
                mode="companion",
                topic_id="college_chain_rule",
                question={
                    "question_id": "personalized-question",
                    "question": "Synthetic personalized repair question",
                    "answer": "synthetic",
                    "question_type": "math_exact",
                    "difficulty": 3,
                    "target_binding": {
                        "target_topic_id": "college_chain_rule",
                        "validation_status": "passed",
                        "cognitive_learning_intent": "misconception_repair",
                        "cognitive_decision_id": "personalized-decision",
                        "diagnostic_validation_id": "synthetic-personalization-validation",
                    },
                },
                user_answer="synthetic wrong answer",
                eval_result={
                    "verdict": "wrong",
                    "score": 0,
                    "evaluator_type": "deterministic",
                    "evaluator_version": "synthetic-personalization-v1",
                    "confidence": 1.0,
                },
                response_time_ms=100,
                used_hint=False,
                attempt_id="personalized-attempt",
                cognitive_intervention_event=attempt_event,
            )
            after_answer = build_cognitive_strategy_report_snapshot(
                store,
                version_set_id=VERSION_SET,
            )
            after_hash = build_cognitive_strategy_report(after_answer)[
                "content_sha256"
            ]
            personalized_exposure = next(
                row
                for row in after_answer["exposures"]
                if row["source_id"] == "personalized-question"
            )
            stale_event = _personalized_question_event(
                audit,
                now=run_now,
                event_id="stale-personalized-question-event",
                decision_id="stale-personalized-decision",
                question_id="stale-personalized-question",
            )
            fence_rejected = False
            try:
                store.record_cognitive_intervention_event(stale_event)
            except ValueError:
                fence_rejected = True

            expected = {
                "default_off": ("baseline", "disabled"),
                "strong_evidence": ("alternate", "supported_alternate"),
                "insufficient_evidence": ("baseline", "insufficient_data"),
                "stale_evidence": ("baseline", "stale_evidence"),
                "conflicting_evidence": ("baseline", "conflicting_endpoints"),
                "incompatible_version": ("baseline", "incompatible_version"),
                "invalid_evidence": ("baseline", "invalid_evidence"),
                "uncertain_without_exploration": (
                    "baseline",
                    "uncertain_evidence",
                ),
                "uncertain_with_exploration": (
                    "alternate",
                    "bounded_exploration",
                ),
                "exposure_limit": ("baseline", "exposure_limit"),
                "consecutive_failure_stop": ("baseline", "failure_stop"),
                "user_stop": ("baseline", "user_stopped"),
            }
            checks = {
                "production_defaults_closed": _production_defaults_are_closed(),
                "ledger_exposure_n": len(snapshot["exposures"]),
                "decision_snapshot_fenced": (
                    len(str(audit.get("snapshot_sha256") or "")) == 64
                    and len(str(audit.get("history_sha256") or "")) == 64
                ),
                "canonical_question_recorded": (
                    committed["strategy_shadow"]["recorded"] is True
                    and len(before_answer["exposures"]) == 41
                ),
                "canonical_answer_recorded": (
                    answer_result["cognitive_intervention_event"]["recorded"] is True
                    and store.get_attempt_fact("personalized-attempt") is not None
                    and len(
                        store.list_cognitive_intervention_events(
                            event_types=("attempt_committed",)
                        )
                    )
                    == 1
                ),
                "outcome_projection_updated": (
                    personalized_exposure["outcomes"]["immediate"]["success"]
                    is False
                    and before_hash != after_hash
                ),
                "concurrent_delivery_rejected": fence_rejected,
                "cases": cases,
            }
            passed = (
                set(cases) == REQUIRED_CASES
                and all(
                    (cases[name]["strategy"], cases[name]["reason"])
                    == expected_value
                    for name, expected_value in expected.items()
                )
                and all(
                    checks[name] is True
                    for name in (
                        "production_defaults_closed",
                        "decision_snapshot_fenced",
                        "canonical_question_recorded",
                        "canonical_answer_recorded",
                        "outcome_projection_updated",
                        "concurrent_delivery_rejected",
                    )
                )
                and checks["ledger_exposure_n"] == 40
            )
        finally:
            store.close()

    result = {
        "evidence_class": EVIDENCE_CLASS,
        "human_effectiveness_claim": False,
        "live_database_touched": False,
        "temporary_production_store_used": True,
        "summary": {"status": "PASS" if passed else "FAIL"},
        "checks": checks,
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
        newline="\n",
    )
    _write_markdown(
        report_dir,
        passed=passed,
        cases=cases,
        checks=checks,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_acceptance(report_dir=args.report_dir)
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError, KeyError, IndexError):
        print(json.dumps({"status": "ERROR"}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result["summary"], sort_keys=True))
    return 0 if result["summary"]["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EVIDENCE_CLASS",
    "REPORT_JSON",
    "REPORT_MARKDOWN",
    "REQUIRED_CASES",
    "ROOT",
    "run_acceptance",
]
