from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

# The reusable verifier checks this package from its parent repository.
# isort: off
import knowledge_dungeon.engine as engine_module
import pytest
from knowledge_dungeon.engine import KnowledgeDungeonEngine
from knowledge_dungeon.persistence import (
    CorruptDungeonState,
    DungeonRunStore,
    DungeonStoreError,
)
# isort: on


def command(
    command_id: str,
    version: int,
    intent: str,
    payload: dict[str, object] | None = None,
    *,
    run_id: str = "persistent-run",
) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "command_id": command_id,
        "run_id": run_id,
        "expected_state_version": version,
        "intent": intent,
        "payload": payload or {},
    }


def test_restart_recovers_state_and_exact_command_receipts(tmp_path: Path) -> None:
    database = tmp_path / "dungeon.sqlite3"
    start = command("start", 0, "start_run", {"seed": 42})
    select = command("select", 1, "select_node", {"node_id": "battle_1"})

    with DungeonRunStore(database) as store:
        engine = KnowledgeDungeonEngine(store)
        first_response = engine.dispatch(start)
        latest_response = engine.dispatch(select)
        assert first_response["accepted"] is True
        assert latest_response["accepted"] is True

    with DungeonRunStore(database) as reopened_store:
        recovered_engine = KnowledgeDungeonEngine(reopened_store)
        recovered = recovered_engine.get_state("persistent-run")
        assert recovered is not None
        assert recovered.state_version == 2
        assert latest_response["state_hash"] == engine_module.state_hash(recovered)
        assert recovered_engine.dispatch(start) == first_response


def test_same_command_id_with_different_request_is_rejected(tmp_path: Path) -> None:
    with DungeonRunStore(tmp_path / "dungeon.sqlite3") as store:
        engine = KnowledgeDungeonEngine(store)
        original = command("start", 0, "start_run", {"seed": 1})
        assert engine.dispatch(original)["accepted"] is True
        before = engine.get_state("persistent-run")

        conflict = engine.dispatch(
            command("start", 0, "start_run", {"seed": 2})
        )

        assert conflict["accepted"] is False
        assert conflict["error_code"] == "command_id_conflict"
        assert engine.get_state("persistent-run") == before


@pytest.mark.parametrize("fault_point", ["after_run_write", "after_receipt_write"])
def test_fault_during_commit_rolls_back_state_and_receipt(
    tmp_path: Path,
    fault_point: str,
) -> None:
    fault_raised = False

    def inject_fault(point: str) -> None:
        nonlocal fault_raised
        if point == fault_point and not fault_raised:
            fault_raised = True
            raise RuntimeError("simulated process failure")

    database = tmp_path / f"{fault_point}.sqlite3"
    with DungeonRunStore(database, fault_hook=inject_fault) as store:
        response = KnowledgeDungeonEngine(store).dispatch(
            command("start", 0, "start_run")
        )
        assert response["accepted"] is False
        assert response["error_code"] == "persistence_failure"
        assert store.list_active_run_ids() == []
        assert store.load_receipt("persistent-run", "start") is None

        retry = KnowledgeDungeonEngine(store).dispatch(
            command("start", 0, "start_run")
        )
        assert retry["accepted"] is True

    with DungeonRunStore(database) as reopened_store:
        assert reopened_store.load_run("persistent-run") is not None
        assert reopened_store.load_receipt("persistent-run", "start") is not None


@pytest.mark.parametrize("fault_point", ["after_run_write", "after_receipt_write"])
def test_abrupt_process_exit_leaves_no_partial_transition(
    tmp_path: Path,
    fault_point: str,
) -> None:
    database = tmp_path / f"crash-{fault_point}.sqlite3"
    script = textwrap.dedent(
        """
        import os
        import sys

        from knowledge_dungeon.engine import KnowledgeDungeonEngine
        from knowledge_dungeon.persistence import DungeonRunStore

        target = sys.argv[2]

        def crash(point):
            if point == target:
                os._exit(91)

        store = DungeonRunStore(sys.argv[1], fault_hook=crash)
        engine = KnowledgeDungeonEngine(store)
        engine.dispatch({
            "protocol_version": 1,
            "command_id": "start",
            "run_id": "persistent-run",
            "expected_state_version": 0,
            "intent": "start_run",
            "payload": {},
        })
        raise SystemExit(92)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(database), fault_point],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 91, completed.stderr

    with DungeonRunStore(database) as reopened_store:
        assert reopened_store.load_run("persistent-run") is None
        assert reopened_store.load_receipt("persistent-run", "start") is None


def test_two_engines_cannot_commit_the_same_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "dungeon.sqlite3"
    first_store = DungeonRunStore(database)
    second_store = DungeonRunStore(database)
    try:
        first_engine = KnowledgeDungeonEngine(first_store)
        second_engine = KnowledgeDungeonEngine(second_store)
        assert first_engine.dispatch(command("start", 0, "start_run"))["accepted"]

        original_reduce_command = engine_module.reduce_command
        first_entered = Event()
        release_first = Event()

        def blocking_reduce(current, dungeon_command):
            if dungeon_command.command_id == "select-a":
                first_entered.set()
                assert release_first.wait(timeout=2)
            return original_reduce_command(current, dungeon_command)

        monkeypatch.setattr(engine_module, "reduce_command", blocking_reduce)
        select_a = command(
            "select-a", 1, "select_node", {"node_id": "battle_1"}
        )
        select_b = command(
            "select-b", 1, "select_node", {"node_id": "trap_1"}
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            delayed = executor.submit(first_engine.dispatch, select_a)
            assert first_entered.wait(timeout=1)
            winner = second_engine.dispatch(select_b)
            release_first.set()
            loser = delayed.result(timeout=2)

        assert winner["accepted"] is True
        assert loser["accepted"] is False
        assert loser["error_code"] == "stale_state_version"
        final_state = first_engine.get_state("persistent-run")
        assert final_state is not None
        assert final_state.state_version == 2
        assert final_state.selected_node_id == "trap_1"
    finally:
        first_store.close()
        second_store.close()


def test_concurrent_quarantine_cannot_be_overwritten_by_a_commit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "dungeon.sqlite3"
    with DungeonRunStore(database) as seed_store:
        assert KnowledgeDungeonEngine(seed_store).dispatch(
            command("start", 0, "start_run")
        )["accepted"]

    before_transaction = Event()
    release_transaction = Event()

    def pause_before_transaction(point: str) -> None:
        if point == "before_transaction_begin":
            before_transaction.set()
            assert release_transaction.wait(timeout=2)

    writer_store = DungeonRunStore(database, fault_hook=pause_before_transaction)
    quarantine_store = DungeonRunStore(database)
    try:
        writer_engine = KnowledgeDungeonEngine(writer_store)
        select = command(
            "select", 1, "select_node", {"node_id": "battle_1"}
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(writer_engine.dispatch, select)
            assert before_transaction.wait(timeout=1)
            try:
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        "UPDATE dungeon_runs SET state_json = ? WHERE run_id = ?",
                        ('{"run_id":"tampered"}', "persistent-run"),
                    )
                with pytest.raises(CorruptDungeonState):
                    quarantine_store.load_run("persistent-run")
            finally:
                release_transaction.set()
            response = pending.result(timeout=2)

        assert response["accepted"] is False
        assert response["error_code"] == "corrupt_dungeon_state"
        assert response["view"] is None
        assert writer_store.list_active_run_ids() == []
        assert len(writer_store.list_quarantined_runs()) == 1
    finally:
        release_transaction.set()
        writer_store.close()
        quarantine_store.close()


def test_sqlite_read_failure_becomes_a_controlled_engine_rejection(
    tmp_path: Path,
) -> None:
    store = DungeonRunStore(tmp_path / "dungeon.sqlite3")
    engine = KnowledgeDungeonEngine(store)
    store.close()

    response = engine.dispatch(command("start", 0, "start_run"))

    assert response["accepted"] is False
    assert response["error_code"] == "persistence_failure"
    with pytest.raises(DungeonStoreError) as captured:
        store.list_active_run_ids()
    assert captured.value.code == "persistence_failure"


def test_corrupt_state_is_quarantined_and_cannot_be_silently_recreated(
    tmp_path: Path,
) -> None:
    database = tmp_path / "dungeon.sqlite3"
    with DungeonRunStore(database) as store:
        engine = KnowledgeDungeonEngine(store)
        assert engine.dispatch(command("start", 0, "start_run"))["accepted"]
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE dungeon_runs SET state_json = ? WHERE run_id = ?",
                ('{"run_id":"tampered"}', "persistent-run"),
            )

        response = engine.dispatch(
            command("select", 1, "select_node", {"node_id": "battle_1"})
        )
        assert response["accepted"] is False
        assert response["error_code"] == "corrupt_dungeon_state"
        assert response["view"] is None
        assert store.list_active_run_ids() == []
        quarantine = store.list_quarantined_runs()
        assert [entry["run_id"] for entry in quarantine] == ["persistent-run"]

        retry = engine.dispatch(command("new-start", 0, "start_run"))
        assert retry["accepted"] is False
        assert retry["error_code"] == "corrupt_dungeon_state"


def test_corrupt_receipt_quarantines_its_run(tmp_path: Path) -> None:
    database = tmp_path / "dungeon.sqlite3"
    with DungeonRunStore(database) as store:
        engine = KnowledgeDungeonEngine(store)
        start = command("start", 0, "start_run")
        assert engine.dispatch(start)["accepted"]
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                UPDATE dungeon_command_receipts
                SET response_json = ?
                WHERE run_id = ? AND command_id = ?
                """,
                ("not-json", "persistent-run", "start"),
            )

        response = engine.dispatch(start)
        assert response["accepted"] is False
        assert response["error_code"] == "corrupt_dungeon_state"
        assert response["view"] is None
        assert store.list_active_run_ids() == []
        assert len(store.list_quarantined_runs()) == 1


def test_store_uses_only_dungeon_owned_tables(tmp_path: Path) -> None:
    database = tmp_path / "dungeon.sqlite3"
    with DungeonRunStore(database):
        pass
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert tables >= {
        "dungeon_runs",
        "dungeon_command_receipts",
        "dungeon_quarantine",
    }
    assert not any(name.startswith("mastery") for name in tables)
