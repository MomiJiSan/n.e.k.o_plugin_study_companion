"""Stateful façade around the pure knowledge dungeon reducer."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Any, Iterable, Mapping

from .commands import CommandValidationError, DungeonCommand, command_to_dict
from .contracts import PROTOCOL_VERSION
from .reducer import ReducerError, reduce_command
from .serializer import state_hash
from .state import RunState


class KnowledgeDungeonEngine:
    """Own in-memory v0.1 runs and enforce concurrency/idempotency rules."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._runs: dict[str, RunState] = {}
        self._response_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def get_state(self, run_id: str) -> RunState | None:
        with self._lock:
            state = self._runs.get(run_id)
            return deepcopy(state) if state is not None else None

    def dispatch(self, raw_command: DungeonCommand | Mapping[str, Any]) -> dict[str, Any]:
        try:
            command = (
                raw_command
                if isinstance(raw_command, DungeonCommand)
                else DungeonCommand.from_mapping(raw_command)
            )
        except CommandValidationError as exc:
            return self._rejection(None, "invalid_command", str(exc))

        with self._lock:
            cache_key = (command.run_id, command.command_id)
            cached = self._response_cache.get(cache_key)
            if cached is not None:
                return deepcopy(cached)

            current = self._runs.get(command.run_id)
            if current is not None and command.command_id in current.processed_command_ids:
                # A state restored without the ephemeral response cache remains safe.
                return self._acceptance(current, [], idempotent_replay=True)
            actual_version = current.state_version if current is not None else 0
            if command.expected_state_version != actual_version:
                return self._rejection(
                    current,
                    "stale_state_version",
                    f"expected {command.expected_state_version}, actual {actual_version}",
                )
            if current is not None and current.run_id != command.run_id:
                return self._rejection(current, "run_id_mismatch", "run_id does not match state")

            try:
                transition = reduce_command(current, command)
            except ReducerError as exc:
                return self._rejection(current, exc.code, str(exc))

            next_state = transition.state
            next_state.state_version = actual_version + 1
            next_state.processed_command_ids.append(command.command_id)
            next_state.command_log.append(command_to_dict(command))
            self._runs[command.run_id] = next_state
            response = self._acceptance(next_state, transition.events)
            self._response_cache[cache_key] = deepcopy(response)
            return response

    def restore_state(self, state: RunState) -> None:
        with self._lock:
            if state.run_id in self._runs:
                raise ValueError(f"run already exists: {state.run_id}")
            self._runs[state.run_id] = deepcopy(state)

    @classmethod
    def replay(cls, commands: Iterable[DungeonCommand | Mapping[str, Any]]) -> "KnowledgeDungeonEngine":
        engine = cls()
        for command in commands:
            response = engine.dispatch(command)
            if not response["accepted"]:
                error = response.get("error", {})
                raise ValueError(f"replay rejected: {error.get('code')}: {error.get('message')}")
        return engine

    def _acceptance(
        self,
        state: RunState,
        events: list[dict[str, Any]],
        *,
        idempotent_replay: bool = False,
    ) -> dict[str, Any]:
        response = {
            "protocol_version": PROTOCOL_VERSION,
            "accepted": True,
            "state_version": state.state_version,
            "view": build_view(state),
            "events": deepcopy(events),
            "state_hash": state_hash(state),
        }
        if idempotent_replay:
            response["idempotent_replay"] = True
        return response

    def _rejection(
        self,
        state: RunState | None,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "accepted": False,
            "state_version": state.state_version if state is not None else 0,
            "view": build_view(state) if state is not None else None,
            "events": [],
            "state_hash": state_hash(state) if state is not None else None,
            "error": {"code": code, "message": message},
            "error_code": code,
        }


def build_view(state: RunState) -> dict[str, Any]:
    """Build the renderer-safe projection; no learning internals are exposed."""

    cards = {
        card_id: {
            "card_id": card.card_id,
            "name": card.name,
            "subject_id": card.subject_id,
            "base_damage": card.base_damage,
            "energy_cost": card.energy_cost,
            "freshness_bps": card.freshness_bps,
            "lifecycle_state": card.lifecycle_state,
            "rules_text": card.rules_text,
            "flavor_text": card.flavor_text,
            "starter": card.starter,
            "available_in_run": card_id not in state.dormant_card_ids,
        }
        for card_id, card in state.cards.items()
    }
    enemy = None
    if state.enemy is not None:
        enemy = {
            "enemy_id": state.enemy.enemy_id,
            "name": state.enemy.name,
            "max_hp": state.enemy.max_hp,
            "hp": state.enemy.hp,
            "attack": state.enemy.attack,
            "boss": state.enemy.boss,
        }
    return {
        "run_id": state.run_id,
        "status": state.status,
        "phase": state.phase,
        "map_subject_id": state.map_subject_id,
        "current_node_id": state.current_node_id,
        "selected_node_id": state.selected_node_id,
        "available_node_ids": list(state.available_node_ids),
        "completed_node_ids": list(state.completed_node_ids),
        "revealed_node_ids": list(state.revealed_node_ids),
        "player": {
            "hp": state.player_hp,
            "max_hp": state.player_max_hp,
            "energy": state.energy,
            "max_energy": state.max_energy,
        },
        "turn": state.turn,
        "cards": cards,
        "hand": list(state.hand),
        "draw_pile_count": len(state.draw_pile),
        "discard_pile_count": len(state.discard_pile),
        "enemy": enemy,
        "pending_rewards": list(state.pending_rewards),
        "run_modifiers": {
            "encounter_damage_bps": state.encounter_damage_bps,
            "next_encounter_damage_bps": state.next_encounter_damage_bps,
        },
        "versions": dict(state.versions),
    }
