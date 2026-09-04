"""Canonical JSON and SHA-256 helpers shared by replay and bridge fixtures."""

from __future__ import annotations

import json
from typing import Any, Mapping

from .contracts import canonical_json as contract_canonical_json
from .contracts import canonical_sha256
from .state import RunState


def canonical_json(value: Any) -> str:
    return contract_canonical_json(value)


def state_hash(state: RunState | Mapping[str, Any]) -> str:
    if isinstance(state, RunState):
        value = state.to_dict(include_command_log=False)
    else:
        value = dict(state)
        value.pop("command_log", None)
    return canonical_sha256(value)


def serialize_state(state: RunState) -> str:
    return canonical_json(state.to_dict())


def deserialize_state(value: str) -> RunState:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("serialized dungeon state must be an object")
    return RunState.from_dict(decoded)
