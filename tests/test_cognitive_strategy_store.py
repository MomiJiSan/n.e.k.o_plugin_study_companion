from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "store_cognitive_strategy", ROOT / "store_cognitive_strategy.py"
)
assert SPEC is not None and SPEC.loader is not None
strategy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(strategy)


class _Store:
    def __init__(self, *, with_cutoffs: bool = False) -> None:
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        strategy.create_cognitive_strategy_schema(self.conn)
        if with_cutoffs:
            self.conn.execute(
                """
                CREATE TABLE cognitive_delete_cutoffs (
                    topic_id TEXT NOT NULL,
                    hypothesis_code TEXT NOT NULL,
                    delete_cutoff_seq INTEGER NOT NULL,
                    PRIMARY KEY(topic_id, hypothesis_code)
                )
                """
            )
        self.conn.commit()

    def _require_conn(self) -> sqlite3.Connection:
        return self.conn

    def _require_read_conn(self) -> sqlite3.Connection:
        return self.conn

    def close(self) -> None:
        self.conn.close()


def _add_attempt_tables(store: _Store) -> None:
    store.conn.execute(
        """
        CREATE TABLE attempts (
            attempt_id TEXT PRIMARY KEY,
            question_id TEXT NOT NULL,
            root_fact_seq INTEGER NOT NULL,
            response_time_ms INTEGER,
            used_hint INTEGER,
            submitted_at TEXT NOT NULL
        )
        """
    )
    store.conn.execute(
        """
        CREATE TABLE evaluations (
            attempt_id TEXT PRIMARY KEY,
            evaluator_type TEXT NOT NULL,
            evaluator_version TEXT NOT NULL,
            confidence REAL
        )
        """
    )


def _insert_attempt(
    store: _Store,
    *,
    attempt_id: str,
    question_id: str,
    root_fact_seq: int,
) -> None:
    store.conn.execute(
        "INSERT INTO attempts VALUES (?, ?, ?, 800, 0, ?)",
        (
            attempt_id,
            question_id,
            root_fact_seq,
            f"2026-09-08T10:00:{root_fact_seq:02d}Z",
        ),
    )
    store.conn.execute(
        "INSERT INTO evaluations VALUES (?, 'deterministic', 'evaluator-v1', 1.0)",
        (attempt_id,),
    )


def _exposure(
    question_id: str = "question-1", *, root_fact_seq: int = 11, **changes: Any
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "event_type": "question_committed",
        "question_id": question_id,
        "strategy_id": "compare_steps",
        "strategy_version": "1",
        "catalog_version": "v3-pr1",
        "version_set_id": "cognitive-v3-shadow-1",
        "question_purpose": "repair",
        "topic_id": "calculus.chain_rule",
        "learner_id": "local",
        "hypothesis_id": "hypothesis-omit-inner",
        "hypothesis_code": "omit_inner_derivative",
        "decision_id": f"decision:{question_id}",
        "blueprint_id": "chain.compare-steps.v1",
        "diagnostic_validation_id": f"validation:{question_id}",
        "policy_version": "cognitive-intent-policy-v2",
        "validator_version": "cognitive-question-validator-v2.1-1",
        "question_family_id": "chain.polynomial-affine.repair",
        "difficulty_bucket": "medium",
        "strategy_family": "compare_correct_wrong_steps",
        "comparison_scope_id": "chain.omit-inner.repair",
        "baseline": True,
        "strategy_determined_before_commit": True,
        "provenance_complete": True,
        "source_id": f"question-event:{question_id}",
        "root_fact_seq": root_fact_seq,
        "occurred_at": f"2026-09-08T08:00:{root_fact_seq:02d}Z",
        "answer_window_expires_at": "2026-09-09T08:00:00Z",
    }
    value.update(changes)
    return value


def _fact(
    exposure_id: str,
    fact_type: str,
    root_fact_seq: int,
    **changes: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "exposure_id": exposure_id,
        "fact_type": fact_type,
        "source_id": f"{fact_type}-source-{root_fact_seq}",
        "root_fact_seq": root_fact_seq,
        "occurred_at": f"2026-09-08T09:00:{root_fact_seq:02d}Z",
    }
    if fact_type in {"attempt", "transfer"}:
        value.update(
            attempt_id=f"attempt-{root_fact_seq}",
            question_id=f"outcome-question-{root_fact_seq}",
            outcome="correct",
            evaluator_type="deterministic",
            evaluator_version="evaluator-v1",
            evaluator_confidence=1.0,
            used_hint=False,
            response_time_ms=1200,
        )
    elif fact_type == "episode":
        value.update(episode_id="episode-1", status="open")
    elif fact_type == "retention":
        value.update(
            episode_id="episode-1",
            attempt_id=f"attempt-{root_fact_seq}",
            outcome="resolved",
            certified=True,
        )
    elif fact_type == "abandoned":
        value["reason"] = "scope_revision_changed"
    elif fact_type == "replaced":
        value["linked_exposure_id"] = "cognitive-strategy-exposure:replacement"
    value.update(changes)
    return value


def test_exposure_id_uses_exact_prefixed_compact_utf8_contract() -> None:
    identity = ["问题-一", "步骤对比", "1", "目录-1", "版本集-1"]
    encoded = json.dumps(
        identity, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    expected = f"cognitive-strategy-exposure:{hashlib.sha256(encoded).hexdigest()}"

    assert strategy.compute_cognitive_strategy_exposure_id(*identity) == expected


def test_schema_is_additive_and_contains_no_answer_prompt_or_token_columns() -> None:
    store = _Store()
    try:
        tables = {
            str(row["name"])
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert tables == {
            "cognitive_strategy_exposures",
            "cognitive_strategy_exposure_facts",
        }
        columns = {
            str(row["name"])
            for table in tables
            for row in store.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        assert not {
            "answer",
            "user_answer",
            "reference_answer",
            "prompt",
            "tokens",
            "evaluation_json",
        } & columns
        assert not {
            "fsrs_cards",
            "mastery_snapshots_v2",
            "topics",
            "wrong_questions",
        } & tables
    finally:
        store.close()


def test_exposure_requires_committed_provenance_and_is_immutable_idempotent() -> None:
    store = _Store()
    try:
        event = _exposure()
        created = strategy.record_cognitive_strategy_exposure(store, event)
        repeated = strategy.record_cognitive_strategy_exposure(store, event)
        assert repeated == created
        assert len(strategy.list_cognitive_strategy_exposures(store)) == 1

        with pytest.raises(ValueError, match="question_committed"):
            strategy.record_cognitive_strategy_exposure(
                store, {**event, "event_type": "intent_proposed"}
            )
        with pytest.raises(ValueError, match="identity collision"):
            strategy.record_cognitive_strategy_exposure(
                store, {**event, "difficulty_bucket": "easy"}
            )
        with pytest.raises(ValueError, match="provenance collision"):
            strategy.record_cognitive_strategy_exposure(
                store,
                _exposure(
                    strategy_id="complete_step",
                    source_id="question-event:changed",
                ),
            )
        with pytest.raises(ValueError, match="canonical identity"):
            strategy.record_cognitive_strategy_exposure(
                store, {**_exposure("question-2"), "exposure_id": "forged"}
            )
        with pytest.raises(ValueError, match="not permitted"):
            strategy.record_cognitive_strategy_exposure(
                store, {**_exposure("question-3"), "metadata": {"prompt": "secret"}}
            )
    finally:
        store.close()


def test_append_only_facts_preserve_every_history_in_root_and_source_order() -> None:
    store = _Store()
    try:
        exposure = strategy.record_cognitive_strategy_exposure(store, _exposure())
        exposure_id = str(exposure["exposure_id"])
        facts = [
            _fact(exposure_id, "retention", 17),
            _fact(exposure_id, "episode", 15),
            _fact(exposure_id, "attempt", 13),
            _fact(exposure_id, "transfer", 14),
            _fact(exposure_id, "abandoned", 18),
            _fact(exposure_id, "replaced", 19),
        ]
        for fact in facts:
            created = strategy.record_cognitive_strategy_fact(store, fact)
            assert strategy.record_cognitive_strategy_fact(store, fact) == created

        listed = strategy.list_cognitive_strategy_facts(store)
        assert [item["fact_type"] for item in listed] == [
            "attempt",
            "transfer",
            "episode",
            "retention",
            "abandoned",
            "replaced",
        ]
        assert listed[0]["used_hint"] is False
        assert listed[3]["certified"] is True

        with pytest.raises(ValueError, match="identity collision"):
            strategy.record_cognitive_strategy_fact(
                store, {**facts[2], "outcome": "wrong"}
            )
        with pytest.raises(ValueError, match="existing exposure"):
            strategy.record_cognitive_strategy_fact(
                store, _fact("missing-exposure", "attempt", 20)
            )
        with pytest.raises(ValueError, match="not permitted"):
            strategy.record_cognitive_strategy_fact(
                store, {**_fact(exposure_id, "attempt", 21), "user_answer": "x"}
            )
        assert len(strategy.list_cognitive_strategy_facts(store)) == 6
    finally:
        store.close()


def test_snapshot_is_replayable_at_a_frozen_root_fact_boundary() -> None:
    store = _Store()
    try:
        exposure = strategy.record_cognitive_strategy_exposure(store, _exposure())
        exposure_id = str(exposure["exposure_id"])
        strategy.record_cognitive_strategy_fact(
            store, _fact(exposure_id, "attempt", 13)
        )
        strategy.record_cognitive_strategy_fact(
            store, _fact(exposure_id, "episode", 15)
        )
        strategy.record_cognitive_strategy_fact(
            store, _fact(exposure_id, "retention", 17)
        )

        earlier = strategy.get_cognitive_strategy_exposure_snapshot(
            store, exposure_id, as_of_root_fact_seq=14
        )
        assert earlier is not None
        assert earlier["current_status"] == "attempted"
        assert [fact["fact_type"] for fact in earlier["facts"]] == ["attempt"]

        latest = strategy.get_cognitive_strategy_exposure_snapshot(store, exposure_id)
        assert latest is not None
        assert latest["current_status"] == "retention_observed"
        assert set(latest["latest_by_type"]) == {"attempt", "episode", "retention"}
    finally:
        store.close()


def test_delete_cutoff_hides_old_history_but_allows_new_exposure() -> None:
    store = _Store(with_cutoffs=True)
    try:
        old = strategy.record_cognitive_strategy_exposure(store, _exposure(root_fact_seq=11))
        strategy.record_cognitive_strategy_fact(
            store, _fact(str(old["exposure_id"]), "attempt", 12)
        )
        store.conn.execute(
            """
            INSERT INTO cognitive_delete_cutoffs
                (topic_id, hypothesis_code, delete_cutoff_seq)
            VALUES (?, ?, ?)
            """,
            ("calculus.chain_rule", "omit_inner_derivative", 20),
        )
        store.conn.commit()
        strategy.record_cognitive_strategy_exposure(
            store, _exposure("question-new", root_fact_seq=21)
        )

        assert [item["question_id"] for item in strategy.list_cognitive_strategy_exposures(store)] == [
            "question-new"
        ]
        assert strategy.list_cognitive_strategy_facts(store) == []
        assert len(strategy.list_cognitive_strategy_exposures(store, include_deleted=True)) == 2
        assert len(strategy.list_cognitive_strategy_facts(store, include_deleted=True)) == 1
    finally:
        store.close()


def test_rebuild_is_deterministic_and_applies_input_delete_cutoffs() -> None:
    store = _Store()
    try:
        old = _exposure("question-old", root_fact_seq=8)
        first = _exposure("question-a", root_fact_seq=21)
        second = _exposure("question-b", root_fact_seq=22)
        first_id = strategy.compute_cognitive_strategy_exposure_id(
            first["question_id"],
            first["strategy_id"],
            first["strategy_version"],
            first["catalog_version"],
            first["version_set_id"],
        )
        second_id = strategy.compute_cognitive_strategy_exposure_id(
            second["question_id"],
            second["strategy_id"],
            second["strategy_version"],
            second["catalog_version"],
            second["version_set_id"],
        )
        facts = [
            _fact(second_id, "attempt", 25, source_id="z-source"),
            _fact(first_id, "attempt", 24, source_id="a-source"),
        ]
        cutoff = [
            {
                "topic_id": "calculus.chain_rule",
                "hypothesis_code": "omit_inner_derivative",
                "delete_cutoff_seq": 20,
            }
        ]
        first_result = strategy.rebuild_cognitive_strategy_ledger(
            store,
            [second, old, first],
            list(reversed(facts)),
            delete_cutoffs=cutoff,
        )
        first_dump = json.dumps(
            {
                "exposures": strategy.list_cognitive_strategy_exposures(store),
                "facts": strategy.list_cognitive_strategy_facts(store),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        second_result = strategy.rebuild_cognitive_strategy_ledger(
            store, [first, second, old], facts, delete_cutoffs=cutoff
        )
        second_dump = json.dumps(
            {
                "exposures": strategy.list_cognitive_strategy_exposures(store),
                "facts": strategy.list_cognitive_strategy_facts(store),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        assert first_result == second_result == {"exposures": 2, "facts": 2}
        assert first_dump == second_dump
        assert "question-old" not in second_dump
    finally:
        store.close()


def test_capture_links_repair_transfer_episode_and_retention_append_only() -> None:
    store = _Store()
    _add_attempt_tables(store)
    try:
        repair = strategy.insert_cognitive_strategy_exposure(
            store, store.conn, _exposure(root_fact_seq=1)
        )
        strategy.insert_cognitive_strategy_fact(
            store,
            store.conn,
            _fact(
                str(repair["exposure_id"]),
                "attempt",
                2,
                source_id="repair-attempt",
            ),
        )
        transfer_question = "question-transfer"
        strategy.insert_cognitive_strategy_exposure(
            store,
            store.conn,
            _exposure(
                transfer_question,
                root_fact_seq=3,
                strategy_id="chain.omit-inner.alternate-representation",
                strategy_version="v1",
                catalog_version="cognitive-strategy-catalog-v1",
                version_set_id="cognitive-v2.1-1",
                question_purpose="transfer",
                blueprint_id="chain.omit-inner.cross-form-transfer.v1",
                diagnostic_validation_id="validation-transfer",
                question_family_id="chain.cross-form.transfer",
                strategy_family="alternate_representation",
                comparison_scope_id="chain.omit-inner.transfer",
                source_id=transfer_question,
            ),
        )
        _insert_attempt(
            store,
            attempt_id="transfer-attempt",
            question_id=transfer_question,
            root_fact_seq=4,
        )
        captured = strategy.capture_cognitive_strategy_intervention(
            store,
            store.conn,
            {
                "event_type": "attempt_committed",
                "question_id": transfer_question,
                "attempt_id": "transfer-attempt",
                "evaluation_verdict": "correct",
                "root_fact_seq": 4,
            },
        )
        assert captured["recorded"] is True
        transfer = [
            fact
            for fact in strategy.list_cognitive_strategy_facts(store)
            if fact["fact_type"] == "transfer"
        ]
        assert len(transfer) == 1
        assert transfer[0]["exposure_id"] == repair["exposure_id"]

        episode = strategy.capture_cognitive_strategy_episode(
            store,
            store.conn,
            transfer_attempt_id="transfer-attempt",
            episode_id="episode-linked",
            occurred_at="2026-09-08T10:01:00Z",
        )
        assert episode["recorded"] is True
        _insert_attempt(
            store,
            attempt_id="retention-attempt",
            question_id="question-retention",
            root_fact_seq=5,
        )
        retention = strategy.capture_cognitive_strategy_retention(
            store,
            store.conn,
            episode_id="episode-linked",
            attempt_id="retention-attempt",
            outcome="resolved",
            occurred_at="2026-09-09T10:01:00Z",
            evaluator_type="deterministic",
            evaluator_version="evaluator-v1",
            evaluator_confidence=1.0,
            used_hint=False,
            certified=True,
            interval_hours=24.0,
            independent_family=True,
        )
        assert retention["recorded"] is True
        linked_fact_types = [
            fact["fact_type"]
            for fact in strategy.list_cognitive_strategy_facts(
                store, exposure_id=str(repair["exposure_id"])
            )
        ]
        assert len(linked_fact_types) == 4
        assert set(linked_fact_types) == {
            "attempt",
            "transfer",
            "episode",
            "retention",
        }
    finally:
        store.close()


def test_capture_marks_crossed_repair_ancestry_as_ambiguous() -> None:
    store = _Store()
    _add_attempt_tables(store)
    try:
        repairs: list[dict[str, Any]] = []
        for index in (1, 2):
            repair = strategy.insert_cognitive_strategy_exposure(
                store,
                store.conn,
                _exposure(f"repair-{index}", root_fact_seq=index * 2 - 1),
            )
            repairs.append(repair)
            strategy.insert_cognitive_strategy_fact(
                store,
                store.conn,
                _fact(
                    str(repair["exposure_id"]),
                    "attempt",
                    index * 2,
                    source_id=f"repair-attempt-{index}",
                ),
            )
        strategy.insert_cognitive_strategy_exposure(
            store,
            store.conn,
            _exposure(
                "ambiguous-transfer",
                root_fact_seq=5,
                strategy_id="transfer-strategy",
                question_purpose="transfer",
                source_id="ambiguous-transfer",
            ),
        )
        _insert_attempt(
            store,
            attempt_id="ambiguous-transfer-attempt",
            question_id="ambiguous-transfer",
            root_fact_seq=6,
        )
        strategy.capture_cognitive_strategy_intervention(
            store,
            store.conn,
            {
                "event_type": "attempt_committed",
                "question_id": "ambiguous-transfer",
                "attempt_id": "ambiguous-transfer-attempt",
                "evaluation_verdict": "correct",
                "root_fact_seq": 6,
            },
        )

        exclusions = [
            fact
            for fact in strategy.list_cognitive_strategy_facts(store)
            if fact["fact_type"] == "attribution_excluded"
        ]
        assert len(exclusions) == 2
        assert {fact["exposure_id"] for fact in exclusions} == {
            repair["exposure_id"] for repair in repairs
        }
        assert {fact["reason"] for fact in exclusions} == {
            "ambiguous_attribution"
        }
        assert not [
            fact
            for fact in strategy.list_cognitive_strategy_facts(store)
            if fact["fact_type"] == "transfer"
        ]
    finally:
        store.close()


def test_committed_replacement_links_new_exposure_without_rewriting_old_history() -> None:
    store = _Store()
    try:
        old = strategy.insert_cognitive_strategy_exposure(
            store, store.conn, _exposure("question-old", root_fact_seq=1)
        )
        strategy.insert_cognitive_strategy_fact(
            store,
            store.conn,
            _fact(
                str(old["exposure_id"]),
                "abandoned",
                2,
                source_id="abandon-old",
                reason="user_replace",
            ),
        )
        captured = strategy.capture_cognitive_strategy_intervention(
            store,
            store.conn,
            {
                "event_type": "question_committed",
                "question_id": "question-new",
                "decision_id": "decision-new",
                "hypothesis_target": {
                    "hypothesis_id": "hypothesis-omit-inner",
                    "topic_id": "calculus.chain_rule",
                    "code": "omit_inner_derivative",
                },
                "blueprint_id": "chain.omit-inner.fill-factor.v1",
                "question_family_id": "chain.polynomial-affine.repair",
                "diagnostic_validation_id": "validation-new",
                "policy_version": "cognitive-intent-policy-v2",
                "validator_version": "cognitive-question-validator-v2.1-1",
                "root_fact_seq": 3,
                "created_at": "2026-09-08T11:00:00Z",
                "metadata": {
                    "replacement_for_question_id": "question-old",
                    "strategy_exposure": {
                        "strategy_id": "chain.omit-inner.complete-steps",
                        "strategy_version": "v1",
                        "catalog_version": "cognitive-strategy-catalog-v1",
                        "version_set_id": "cognitive-v2.1-1",
                        "question_purpose": "repair",
                        "difficulty_bucket": "3",
                        "strategy_family": "complete_steps",
                        "comparison_scope_id": "chain.omit-inner.repair",
                        "baseline": True,
                        "answer_window_expires_at": "2026-09-09T11:00:00Z",
                    },
                },
            },
        )

        assert captured["recorded"] is True
        assert len(strategy.list_cognitive_strategy_exposures(store)) == 2
        old_facts = strategy.list_cognitive_strategy_facts(
            store, exposure_id=str(old["exposure_id"])
        )
        assert [fact["fact_type"] for fact in old_facts] == [
            "abandoned",
            "replaced",
        ]
        assert old_facts[-1]["linked_exposure_id"] == captured["exposure_id"]
        assert strategy.get_cognitive_strategy_exposure_snapshot(
            store, str(old["exposure_id"])
        )["current_status"] == "replaced"
    finally:
        store.close()
