from __future__ import annotations

from knowledge_dungeon.engine import KnowledgeDungeonEngine


def test_duplicate_command_returns_cached_response_without_reapplying_damage() -> None:
    engine = KnowledgeDungeonEngine()
    start = {
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
    engine.dispatch({"command_id": "start", "run_id": "stale", "expected_state_version": 0, "intent": "start_run", "payload": {}})
    before = engine.get_state("stale")
    response = engine.dispatch({"command_id": "stale-select", "run_id": "stale", "expected_state_version": 0, "intent": "select_node", "payload": {"node_id": "battle_1"}})
    after = engine.get_state("stale")
    assert response["accepted"] is False
    assert response["error"]["code"] == "stale_state_version"
    assert before == after
