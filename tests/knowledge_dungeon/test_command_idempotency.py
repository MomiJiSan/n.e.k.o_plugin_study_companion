from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import knowledge_dungeon.engine as engine_module
from knowledge_dungeon.engine import KnowledgeDungeonEngine


def test_duplicate_command_returns_cached_response_without_reapplying_damage() -> None:
    engine = KnowledgeDungeonEngine()
    start = {
        "protocol_version": 1,
        "command_id": "start",
        "run_id": "idempotent",
        "expected_state_version": 0,
        "intent": "start_run",
        "payload": {"seed": 1},
    }
    assert engine.dispatch(start)["accepted"]
    assert engine.dispatch({**start, "command_id": "select", "expected_state_version": 1, "intent": "select_node", "payload": {"node_id": "battle_1"}})["accepted"]
    assert engine.dispatch({**start, "command_id": "encounter", "expected_state_version": 2, "intent": "start_encounter", "payload": {}})["accepted"]
    play = {**start, "command_id": "play", "expected_state_version": 3, "intent": "play_card", "payload": {"card_id": "neutral.momiji_mercy"}}
    first = engine.dispatch(play)
    second = engine.dispatch(play)
    state = engine.get_state("idempotent")
    assert first == second
    assert state is not None
    assert state.state_version == 4
    assert state.enemy is not None and state.enemy.hp == 7
    assert state.processed_command_ids.count("play") == 1


def test_stale_state_version_is_rejected_without_state_change() -> None:
    engine = KnowledgeDungeonEngine()
    engine.dispatch({"protocol_version": 1, "command_id": "start", "run_id": "stale", "expected_state_version": 0, "intent": "start_run", "payload": {}})
    before = engine.get_state("stale")
    response = engine.dispatch({"protocol_version": 1, "command_id": "stale-select", "run_id": "stale", "expected_state_version": 0, "intent": "select_node", "payload": {"node_id": "battle_1"}})
    after = engine.get_state("stale")
    assert response["accepted"] is False
    assert response["error"]["code"] == "stale_state_version"
    assert before == after


def test_concurrent_commands_cannot_commit_the_same_state_version(monkeypatch) -> None:
    engine = KnowledgeDungeonEngine()
    start = {
        "protocol_version": 1,
        "command_id": "start",
        "run_id": "concurrent",
        "expected_state_version": 0,
        "intent": "start_run",
        "payload": {},
    }
    assert engine.dispatch(start)["accepted"]

    original_reduce_command = engine_module.reduce_command
    first_entered = Event()
    second_started = Event()
    second_entered = Event()
    release_first = Event()

    def blocking_reduce(current, command):
        if command.command_id == "select-a":
            first_entered.set()
            assert release_first.wait(timeout=2)
        elif command.command_id == "select-b":
            second_entered.set()
        return original_reduce_command(current, command)

    monkeypatch.setattr(engine_module, "reduce_command", blocking_reduce)
    command_a = {
        **start,
        "command_id": "select-a",
        "expected_state_version": 1,
        "intent": "select_node",
        "payload": {"node_id": "battle_1"},
    }
    command_b = {**command_a, "command_id": "select-b", "payload": {"node_id": "trap_1"}}

    def dispatch_second():
        second_started.set()
        return engine.dispatch(command_b)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(engine.dispatch, command_a)
        assert first_entered.wait(timeout=1)
        second = executor.submit(dispatch_second)
        assert second_started.wait(timeout=1)
        try:
            assert not second_entered.wait(timeout=0.1)
        finally:
            release_first.set()
        responses = (first.result(timeout=2), second.result(timeout=2))

    assert sum(response["accepted"] for response in responses) == 1
    assert {response.get("error_code") for response in responses} == {None, "stale_state_version"}
    state = engine.get_state("concurrent")
    assert state is not None
    assert state.state_version == 2
