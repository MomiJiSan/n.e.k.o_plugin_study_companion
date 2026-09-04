"""Command contract used by the deterministic v0.1 dungeon engine."""

from __future__ import annotations

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
        if "attempt_id" in value or "attempt_id" in (value.get("payload") or {}):
            raise CommandValidationError("attempt_id is not part of dungeon v0.1 commands")
        try:
            command_id = str(value["command_id"]).strip()
            run_id = str(value["run_id"]).strip()
            expected_state_version = int(value["expected_state_version"])
            intent = str(value["intent"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandValidationError("invalid command envelope") from exc
        payload = value.get("payload", {})
        protocol_version = value.get("protocol_version", PROTOCOL_VERSION)
        if not command_id or not run_id:
            raise CommandValidationError("command_id and run_id are required")
        if expected_state_version < 0:
            raise CommandValidationError("expected_state_version must be non-negative")
        if intent not in SUPPORTED_INTENTS:
            raise CommandValidationError(f"unsupported intent: {intent}")
        if protocol_version != PROTOCOL_VERSION:
            raise CommandValidationError(f"unsupported protocol_version: {protocol_version}")
        if not isinstance(payload, Mapping):
            raise CommandValidationError("payload must be an object")
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
