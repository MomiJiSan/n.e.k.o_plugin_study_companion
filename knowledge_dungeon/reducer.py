"""Pure command reducer for the deterministic v0.1 dungeon state machine."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .card_projection import calculate_effect_value, subject_multiplier_bp
from .commands import DungeonCommand
from .contracts import VersionBundle
from .rng import PCG32
from .state import MAP_NODES, CardState, EnemyState, RunState

STARTER_CARD_ID = "neutral.momiji_mercy"
STARTER_CARD_NAME = "红枼的怜悯"
MERCY_FLAVOR_TEXT = "快去学习获取更强力的卡片吧，少年！"
SUBJECT_MATCH_BPS = 10_000
CROSS_SUBJECT_BPS = 5_000
FRESHNESS_ACTIVE_BPS = 10_000


class ReducerError(ValueError):
    """A stable, user-safe reducer rejection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class Transition:
    state: RunState
    events: list[dict[str, Any]]


def _integer_from(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _card_from_mapping(raw: Mapping[str, Any]) -> CardState:
    card_id = str(raw.get("card_id") or raw.get("id") or "").strip()
    if not card_id:
        raise ReducerError("invalid_card", "card_id is required")
    lifecycle = str(raw.get("lifecycle_state") or raw.get("state") or "active")
    freshness_bps = _integer_from(
        raw.get(
            "freshness_bps",
            raw.get("freshness_multiplier_bp", raw.get("power_multiplier_bp", 10_000)),
        ),
        10_000,
    )
    return CardState(
        card_id=card_id,
        name=str(raw.get("name") or card_id),
        subject_id=str(raw.get("subject_id") or raw.get("subject") or "neutral"),
        base_damage=max(0, _integer_from(raw.get("base_damage"), 0)),
        energy_cost=max(0, _integer_from(raw.get("energy_cost"), 1)),
        freshness_bps=max(0, min(10_000, freshness_bps)),
        lifecycle_state=lifecycle,
        rules_text=str(raw.get("rules_text") or ""),
        flavor_text=str(raw.get("flavor_text") or ""),
        starter=bool(raw.get("starter", False)),
    )


def _starter_card() -> CardState:
    return CardState(
        card_id=STARTER_CARD_ID,
        name=STARTER_CARD_NAME,
        subject_id="neutral",
        base_damage=1,
        energy_cost=0,
        freshness_bps=10_000,
        lifecycle_state="active",
        rules_text="造成 1 点伤害。每回合只能使用一次。",
        flavor_text=MERCY_FLAVOR_TEXT,
        starter=True,
    )


def _extract_cards(payload: Mapping[str, Any]) -> tuple[dict[str, CardState], list[str]]:
    raw_cards: Any = payload.get("cards")
    if raw_cards is None:
        snapshot = payload.get("snapshot", {})
        if isinstance(snapshot, Mapping):
            raw_cards = snapshot.get("cards", snapshot.get("projected_cards", []))
    projection_cards = getattr(raw_cards, "cards", None)
    if projection_cards is not None:
        raw_cards = projection_cards
    if raw_cards is None:
        raw_cards = []
    if isinstance(raw_cards, Mapping):
        raw_cards = list(raw_cards.values())
    if not isinstance(raw_cards, Iterable) or isinstance(raw_cards, (str, bytes)):
        raise ReducerError("invalid_cards", "cards must be an array")

    cards: dict[str, CardState] = {STARTER_CARD_ID: _starter_card()}
    dormant: list[str] = []
    for raw in raw_cards:
        if hasattr(raw, "to_dict"):
            raw = raw.to_dict()
        if not isinstance(raw, Mapping):
            raise ReducerError("invalid_card", "each card must be an object")
        if raw.get("owned") is False:
            continue
        card = _card_from_mapping(raw)
        if card.card_id == STARTER_CARD_ID:
            continue
        if card.card_id in cards:
            raise ReducerError("duplicate_card", f"duplicate card: {card.card_id}")
        cards[card.card_id] = card
        if card.lifecycle_state == "dormant" or card.freshness_bps == 0:
            dormant.append(card.card_id)
    active_count = len(cards) - len(dormant)
    if active_count > 12:
        raise ReducerError("deck_too_large", "active deck may contain at most 12 cards")
    return cards, dormant


def _start_run(command: DungeonCommand) -> Transition:
    cards, dormant = _extract_cards(command.payload)
    seed = _integer_from(command.payload.get("seed"), 1)
    if seed < 0 or seed > (1 << 64) - 1:
        raise ReducerError("invalid_seed", "seed must be an unsigned 64-bit integer")
    map_subject_id = str(command.payload.get("map_subject_id") or "math")
    raw_versions = command.payload.get("versions")
    versions = dict(raw_versions) if isinstance(raw_versions, Mapping) else VersionBundle().to_dict()
    state = RunState(
        run_id=command.run_id,
        seed=seed,
        map_subject_id=map_subject_id,
        status="active",
        phase="map",
        current_node_id="entrance",
        available_node_ids=list(MAP_NODES["entrance"]["next"]),
        completed_node_ids=["entrance"],
        revealed_node_ids=["entrance", "battle_1", "trap_1"],
        cards=cards,
        dormant_card_ids=dormant,
        versions=versions,
    )
    return Transition(
        state,
        [
            {"type": "run_started", "run_id": command.run_id, "seed": seed},
            {
                "type": "deck_frozen",
                "active_card_ids": _active_card_ids(state),
                "dormant_card_ids": list(dormant),
            },
        ],
    )


def _active_card_ids(state: RunState) -> list[str]:
    dormant = set(state.dormant_card_ids)
    return [card_id for card_id in state.cards if card_id not in dormant]


def _require_phase(state: RunState, expected: str) -> None:
    if state.phase != expected:
        raise ReducerError("invalid_phase", f"command requires phase {expected}, got {state.phase}")


def _select_node(state: RunState, command: DungeonCommand) -> list[dict[str, Any]]:
    _require_phase(state, "map")
    node_id = str(command.payload.get("node_id") or "")
    if node_id not in state.available_node_ids:
        raise ReducerError("node_unavailable", f"node is not available: {node_id}")
    state.selected_node_id = node_id
    return [{"type": "node_selected", "node_id": node_id}]


def _shuffle_for_encounter(state: RunState, values: list[str]) -> None:
    # A versioned PCG32 implementation lives behind this tiny adapter. Deriving the
    # seed from immutable run data keeps replay independent of process/runtime state.
    salt = sum(ord(char) for char in (state.selected_node_id or ""))
    rng = PCG32(state.seed ^ salt, sequence=54)
    rng.shuffle(values)
    exported = rng.export_state()
    state.rng_state = int(exported["state"])
    state.rng_increment = int(exported["increment"])


def _draw_cards(state: RunState, count: int = 5) -> list[str]:
    drawn: list[str] = []
    while len(drawn) < count:
        if not state.draw_pile:
            if not state.discard_pile:
                break
            state.draw_pile = list(state.discard_pile)
            state.discard_pile.clear()
            # The turn participates in the deterministic reshuffle seed.
            rng = PCG32(state.seed ^ (state.turn * 0x9E3779B9), sequence=54)
            rng.shuffle(state.draw_pile)
            exported = rng.export_state()
            state.rng_state = int(exported["state"])
            state.rng_increment = int(exported["increment"])
        card_id = state.draw_pile.pop()
        state.hand.append(card_id)
        drawn.append(card_id)
    return drawn


def _complete_noncombat_node(state: RunState, node_id: str) -> list[dict[str, Any]]:
    node = MAP_NODES[node_id]
    events: list[dict[str, Any]] = []
    if node["type"] == "trap":
        damage = 3
        state.player_hp = max(0, state.player_hp - damage)
        events.append({"type": "trap_triggered", "node_id": node_id, "damage": damage})
        if state.player_hp == 0:
            state.status = "failed"
            state.phase = "failed"
            return events + [{"type": "run_failed", "reason": "trap"}]
    elif node["type"] == "rest":
        before = state.player_hp
        state.player_hp = min(state.player_max_hp, state.player_hp + 4)
        events.append({"type": "rest_completed", "healed": state.player_hp - before})
    state.completed_node_ids.append(node_id)
    state.current_node_id = node_id
    state.selected_node_id = None
    state.available_node_ids = list(node["next"])
    state.revealed_node_ids.extend(
        candidate for candidate in node["next"] if candidate not in state.revealed_node_ids
    )
    state.phase = "map"
    return events + [{"type": "node_completed", "node_id": node_id}]


def _start_encounter(state: RunState, _command: DungeonCommand) -> list[dict[str, Any]]:
    _require_phase(state, "map")
    node_id = state.selected_node_id
    if not node_id:
        raise ReducerError("node_not_selected", "select a node before starting it")
    node = MAP_NODES[node_id]
    if node["type"] in {"trap", "rest"}:
        return _complete_noncombat_node(state, node_id)
    if node["type"] not in {"battle", "boss"}:
        raise ReducerError("not_an_encounter", f"node has no encounter: {node_id}")

    boss = node["type"] == "boss"
    state.enemy = EnemyState(
        enemy_id="limit_guardian" if boss else "formula_wisp",
        name="极限守卫" if boss else "公式微灵",
        max_hp=18 if boss else 8,
        hp=18 if boss else 8,
        attack=4 if boss else 3,
        boss=boss,
    )
    state.current_node_id = node_id
    state.phase = "encounter"
    state.turn = 1
    state.energy = state.max_energy
    state.encounter_damage_bps = state.next_encounter_damage_bps
    state.next_encounter_damage_bps = 10_000
    state.mercy_used_this_turn = False
    state.draw_pile = _active_card_ids(state)
    state.discard_pile.clear()
    state.hand.clear()
    _shuffle_for_encounter(state, state.draw_pile)
    drawn = _draw_cards(state)
    return [
        {
            "type": "encounter_started",
            "node_id": node_id,
            "enemy_id": state.enemy.enemy_id,
            "boss": boss,
        },
        {"type": "turn_started", "turn": state.turn, "drawn_card_ids": drawn},
    ]


def _play_card(state: RunState, command: DungeonCommand) -> list[dict[str, Any]]:
    _require_phase(state, "encounter")
    if state.enemy is None:
        raise ReducerError("encounter_missing", "encounter has no enemy")
    card_id = str(command.payload.get("card_id") or "")
    if card_id not in state.hand:
        raise ReducerError("card_not_in_hand", f"card is not in hand: {card_id}")
    card = state.cards[card_id]
    if card.lifecycle_state == "dormant" or card_id in state.dormant_card_ids:
        raise ReducerError("card_dormant", "dormant cards cannot be played")
    if card.energy_cost > state.energy:
        raise ReducerError("insufficient_energy", "not enough energy")
    if card_id == STARTER_CARD_ID and state.mercy_used_this_turn:
        raise ReducerError("mercy_already_used", "红枼的怜悯每回合只能使用一次")

    subject_bps = subject_multiplier_bp(card.subject_id, state.map_subject_id)
    damage = calculate_effect_value(
        card.base_damage,
        card.freshness_bps,
        subject_bps,
        state.encounter_damage_bps,
    )
    state.energy -= card.energy_cost
    state.hand.remove(card_id)
    state.discard_pile.append(card_id)
    state.enemy.hp = max(0, state.enemy.hp - damage)
    if card_id == STARTER_CARD_ID:
        state.mercy_used_this_turn = True
    events: list[dict[str, Any]] = [
        {
            "type": "card_played",
            "card_id": card_id,
            "damage": damage,
            "freshness_bps": card.freshness_bps,
            "subject_bps": subject_bps,
        },
        {"type": "enemy_damaged", "enemy_id": state.enemy.enemy_id, "damage": damage, "hp": state.enemy.hp},
    ]
    if state.enemy.hp == 0:
        events.extend(_win_encounter(state))
    return events


def _win_encounter(state: RunState) -> list[dict[str, Any]]:
    assert state.enemy is not None
    node_id = state.current_node_id
    assert node_id is not None
    was_boss = state.enemy.boss
    enemy_id = state.enemy.enemy_id
    state.completed_node_ids.append(node_id)
    state.selected_node_id = None
    state.draw_pile.clear()
    state.discard_pile.clear()
    state.hand.clear()
    state.energy = 0
    state.enemy = None
    if was_boss:
        state.status = "boss_defeated"
        state.phase = "map"
        state.available_node_ids = []
        return [
            {"type": "encounter_won", "enemy_id": enemy_id, "boss": True},
            {"type": "boss_defeated", "node_id": node_id, "permanent_reward": None},
        ]
    state.phase = "reward"
    state.pending_rewards = ["heal_6", "next_damage_25", "reveal_map"]
    state.available_node_ids = []
    return [
        {"type": "encounter_won", "enemy_id": enemy_id, "boss": False},
        {"type": "run_reward_offered", "reward_ids": list(state.pending_rewards)},
    ]


def _end_turn(state: RunState, _command: DungeonCommand) -> list[dict[str, Any]]:
    _require_phase(state, "encounter")
    if state.enemy is None:
        raise ReducerError("encounter_missing", "encounter has no enemy")
    damage = state.enemy.attack
    state.player_hp = max(0, state.player_hp - damage)
    events: list[dict[str, Any]] = [
        {"type": "enemy_attacked", "enemy_id": state.enemy.enemy_id, "damage": damage, "player_hp": state.player_hp}
    ]
    if state.player_hp == 0:
        state.status = "failed"
        state.phase = "failed"
        return events + [{"type": "run_failed", "reason": "player_hp_depleted"}]
    state.discard_pile.extend(state.hand)
    state.hand.clear()
    state.turn += 1
    state.energy = state.max_energy
    state.mercy_used_this_turn = False
    drawn = _draw_cards(state)
    return events + [{"type": "turn_started", "turn": state.turn, "drawn_card_ids": drawn}]


def _choose_reward(state: RunState, command: DungeonCommand) -> list[dict[str, Any]]:
    _require_phase(state, "reward")
    reward_id = str(command.payload.get("reward_id") or "")
    if reward_id not in state.pending_rewards:
        raise ReducerError("reward_unavailable", f"reward is not available: {reward_id}")
    event: dict[str, Any] = {"type": "run_reward_chosen", "reward_id": reward_id}
    if reward_id == "heal_6":
        before = state.player_hp
        state.player_hp = min(state.player_max_hp, state.player_hp + 6)
        event["healed"] = state.player_hp - before
    elif reward_id == "next_damage_25":
        state.next_encounter_damage_bps = 12_500
    elif reward_id == "reveal_map":
        for node_id in MAP_NODES:
            if node_id not in state.revealed_node_ids:
                state.revealed_node_ids.append(node_id)
    state.applied_reward_ids.append(reward_id)
    state.pending_rewards.clear()
    node_id = state.current_node_id
    assert node_id is not None
    state.available_node_ids = list(MAP_NODES[node_id]["next"])
    state.revealed_node_ids.extend(
        candidate for candidate in state.available_node_ids if candidate not in state.revealed_node_ids
    )
    state.phase = "map"
    return [event]


def _leave_encounter(state: RunState, _command: DungeonCommand) -> list[dict[str, Any]]:
    _require_phase(state, "encounter")
    state.status = "abandoned"
    state.phase = "complete"
    state.enemy = None
    state.hand.clear()
    state.draw_pile.clear()
    state.discard_pile.clear()
    return [{"type": "run_abandoned", "reason": "left_encounter"}]


def _finish_run(state: RunState, _command: DungeonCommand) -> list[dict[str, Any]]:
    if state.status != "boss_defeated":
        raise ReducerError("boss_not_defeated", "defeat the boss before finishing the run")
    state.status = "completed"
    state.phase = "complete"
    return [{"type": "run_finished", "permanent_reward": None, "learning_fact_written": False}]


def reduce_command(state: RunState | None, command: DungeonCommand) -> Transition:
    """Return a new state without mutating the supplied state."""

    if command.intent == "start_run":
        if state is not None:
            raise ReducerError("run_exists", "run has already started")
        return _start_run(command)
    if state is None:
        raise ReducerError("run_not_found", "start_run must be the first command")

    next_state = deepcopy(state)
    handlers = {
        "select_node": _select_node,
        "start_encounter": _start_encounter,
        "play_card": _play_card,
        "end_turn": _end_turn,
        "choose_run_reward": _choose_reward,
        "leave_encounter": _leave_encounter,
        "finish_run": _finish_run,
    }
    events = handlers[command.intent](next_state, command)
    return Transition(next_state, events)
