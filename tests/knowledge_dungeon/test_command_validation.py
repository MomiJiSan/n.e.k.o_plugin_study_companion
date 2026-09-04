from __future__ import annotations

from typing import Any

from knowledge_dungeon.engine import KnowledgeDungeonEngine


def valid_command() -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "command_id": "start",
        "run_id": "validation",
        "expected_state_version": 0,
        "intent": "start_run",
        "payload": {},
    }


def test_mapping_commands_must_match_the_schema_envelope() -> None:
    valid = valid_command()
    invalid_commands = [
        {key: value for key, value in valid.items() if key != "protocol_version"},
        {key: value for key, value in valid.items() if key != "payload"},
        {**valid, "unexpected": True},
        {**valid, "command_id": 1},
        {**valid, "command_id": "Invalid"},
        {**valid, "run_id": "-invalid"},
        {**valid, "intent": 1},
        {**valid, "expected_state_version": 0.9},
        {**valid, "expected_state_version": True},
        {**valid, "payload": {"attempt_id": "forbidden"}},
    ]

    for command in invalid_commands:
        engine = KnowledgeDungeonEngine()
        response = engine.dispatch(command)
        assert response["accepted"] is False, command
        assert response["error_code"] == "invalid_command", command
        assert engine.get_state("validation") is None


def test_schema_valid_mapping_command_is_accepted() -> None:
    assert KnowledgeDungeonEngine().dispatch(valid_command())["accepted"] is True
