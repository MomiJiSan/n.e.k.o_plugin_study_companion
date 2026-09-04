"""Serializable state types for the knowledge dungeon v0.1 prototype."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class CardState:
    card_id: str
    name: str
    subject_id: str
    base_damage: int
    energy_cost: int
    freshness_bps: int = 10_000
    lifecycle_state: str = "active"
    rules_text: str = ""
    flavor_text: str = ""
    starter: bool = False


@dataclass(slots=True)
class EnemyState:
    enemy_id: str
    name: str
    max_hp: int
    hp: int
    attack: int
    boss: bool = False


@dataclass(slots=True)
class RunState:
    run_id: str
    seed: int
    map_subject_id: str
    owner_client_id: str = ""
    state_version: int = 0
    status: str = "not_started"
    phase: str = "not_started"
    current_node_id: str | None = None
    selected_node_id: str | None = None
    available_node_ids: list[str] = field(default_factory=list)
    completed_node_ids: list[str] = field(default_factory=list)
    revealed_node_ids: list[str] = field(default_factory=list)
    player_max_hp: int = 30
    player_hp: int = 30
    max_energy: int = 3
    energy: int = 0
    turn: int = 0
    cards: dict[str, CardState] = field(default_factory=dict)
    dormant_card_ids: list[str] = field(default_factory=list)
    draw_pile: list[str] = field(default_factory=list)
    discard_pile: list[str] = field(default_factory=list)
    hand: list[str] = field(default_factory=list)
    enemy: EnemyState | None = None
    next_encounter_damage_bps: int = 10_000
    encounter_damage_bps: int = 10_000
    mercy_used_this_turn: bool = False
    pending_rewards: list[str] = field(default_factory=list)
    applied_reward_ids: list[str] = field(default_factory=list)
    processed_command_ids: list[str] = field(default_factory=list)
    rng_state: int = 0
    rng_increment: int = 0
    command_log: list[dict[str, Any]] = field(default_factory=list)
    versions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_command_log: bool = True) -> dict[str, Any]:
        result = asdict(self)
        if not self.owner_client_id:
            result.pop("owner_client_id", None)
        if not include_command_log:
            result.pop("command_log", None)
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunState":
        data = dict(value)
        data["cards"] = {
            str(card_id): CardState(**card)
            for card_id, card in dict(data.get("cards", {})).items()
        }
        enemy = data.get("enemy")
        data["enemy"] = EnemyState(**enemy) if enemy else None
        return cls(**data)


MAP_NODES: dict[str, dict[str, Any]] = {
    "entrance": {"type": "entrance", "next": ["battle_1", "trap_1"]},
    "battle_1": {"type": "battle", "next": ["rest_1"]},
    "trap_1": {"type": "trap", "next": ["rest_1"]},
    "rest_1": {"type": "rest", "next": ["boss_1"]},
    "boss_1": {"type": "boss", "next": []},
}
