from __future__ import annotations

from knowledge_dungeon.engine import KnowledgeDungeonEngine
from knowledge_dungeon.serializer import state_hash


def test_command_log_replay_reconstructs_identical_state() -> None:
    engine = KnowledgeDungeonEngine()
    commands = [
        {"command_id": "1", "run_id": "replay", "expected_state_version": 0, "intent": "start_run", "payload": {"seed": 42}},
        {"command_id": "2", "run_id": "replay", "expected_state_version": 1, "intent": "select_node", "payload": {"node_id": "trap_1"}},
        {"command_id": "3", "run_id": "replay", "expected_state_version": 2, "intent": "start_encounter", "payload": {}},
        {"command_id": "4", "run_id": "replay", "expected_state_version": 3, "intent": "select_node", "payload": {"node_id": "rest_1"}},
        {"command_id": "5", "run_id": "replay", "expected_state_version": 4, "intent": "start_encounter", "payload": {}},
    ]
    for command in commands:
        assert engine.dispatch(command)["accepted"]
    original = engine.get_state("replay")
    assert original is not None

    replayed_engine = KnowledgeDungeonEngine.replay(original.command_log)
    replayed = replayed_engine.get_state("replay")
    assert replayed is not None
    assert replayed.to_dict() == original.to_dict()
    assert state_hash(replayed) == state_hash(original)
