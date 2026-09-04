"""Command contract used by the deterministic v0.1 dungeon engine."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .contracts import PROTOCOL_VERSION

SUPPORTED_INTENTS = frozenset(
    {
        "start_run",
        "select_node",
        "start_encounter",
        "play_card",
        "end_turn",
        "choose_run_reward",
        "leave_encounter",
        "finish_run",
    }
)
_COMMAND_ENVELOPE_KEYS = frozenset(
    {"protocol_version", "command_id", "run_id", "expected_state_version", "intent", "payload"}
)
_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class CommandValidationError(ValueError):
    """Raised when an input command does not satisfy the v0.1 contract."""


@dataclass(frozen=True, slots=True)
class DungeonCommand:
    command_id: str
    run_id: str
    expected_state_version: int
    intent: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: int = PROTOCOL_VERSION

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DungeonCommand":
        if not isinstance(value, Mapping):
            raise CommandValidationError("invalid command envelope")
        if "attempt_id" in value:
            raise CommandValidationError("attempt_id is not part of dungeon v0.1 commands")
        if frozenset(value) != _COMMAND_ENVELOPE_KEYS:
            raise CommandValidationError("invalid command envelope")

        command_id = value["command_id"]
        run_id = value["run_id"]
        expected_state_version = value["expected_state_version"]
        intent = value["intent"]
        payload = value["payload"]
        protocol_version = value["protocol_version"]

        if not isinstance(command_id, str) or _IDENTIFIER_PATTERN.fullmatch(command_id) is None:
            raise CommandValidationError("command_id must match the command identifier pattern")
        if not isinstance(run_id, str) or _IDENTIFIER_PATTERN.fullmatch(run_id) is None:
            raise CommandValidationError("run_id must match the command identifier pattern")
        if isinstance(expected_state_version, bool) or not isinstance(expected_state_version, int):
            raise CommandValidationError("expected_state_version must be a non-negative integer")
        if expected_state_version < 0:
            raise CommandValidationError("expected_state_version must be non-negative")
        if not isinstance(intent, str) or intent not in SUPPORTED_INTENTS:
            raise CommandValidationError(f"unsupported intent: {intent}")
        if isinstance(protocol_version, bool) or not isinstance(protocol_version, int) or protocol_version != PROTOCOL_VERSION:
            raise CommandValidationError(f"unsupported protocol_version: {protocol_version}")
        if not isinstance(payload, Mapping):
            raise CommandValidationError("payload must be an object")
        if not all(isinstance(key, str) for key in payload):
            raise CommandValidationError("payload keys must be strings")
        if "attempt_id" in payload:
            raise CommandValidationError("attempt_id is not part of dungeon v0.1 commands")
        return cls(
            command_id=command_id,
            run_id=run_id,
            expected_state_version=expected_state_version,
            intent=intent,
            payload=dict(payload),
            protocol_version=PROTOCOL_VERSION,
        )


def command_to_dict(command: DungeonCommand) -> dict[str, Any]:
    return {
        "protocol_version": command.protocol_version,
        "command_id": command.command_id,
        "run_id": command.run_id,
        "expected_state_version": command.expected_state_version,
        "intent": command.intent,
        "payload": dict(command.payload),
    }
