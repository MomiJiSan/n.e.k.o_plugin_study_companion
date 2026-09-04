"""Renderer-safe run projection and fail-closed public event filtering."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping

from .available_actions import build_available_actions
from .bridge_contracts import (
    APPLICATION_SERVICE_VERSION,
    BRIDGE_PROTOCOL_VERSION,
    PUBLIC_PROJECTION_VERSION,
)
from .contracts import PROTOCOL_VERSION
from .engine import build_view
from .serializer import state_hash
from .state import RunState


class PublicProjectionError(RuntimeError):
    pass


_PUBLIC_EVENT_FIELDS: dict[str, frozenset[str]] = {
    "run_started": frozenset(("type", "run_id")),
    "deck_frozen": frozenset(("type", "active_card_ids", "dormant_card_ids")),
    "node_selected": frozenset(("type", "node_id")),
    "encounter_started": frozenset(("type", "node_id", "enemy_id", "boss")),
    "turn_started": frozenset(("type", "turn", "drawn_card_ids")),
    "card_played": frozenset(("type", "card_id", "damage", "freshness_bps", "subject_bps")),
    "enemy_damaged": frozenset(("type", "enemy_id", "damage", "hp")),
    "encounter_won": frozenset(("type", "enemy_id", "boss")),
    "run_reward_offered": frozenset(("type", "reward_ids")),
    "run_reward_chosen": frozenset(("type", "reward_id", "healed")),
    "rest_completed": frozenset(("type", "healed")),
    "node_completed": frozenset(("type", "node_id")),
    "trap_triggered": frozenset(("type", "node_id", "damage")),
    "run_failed": frozenset(("type", "reason")),
    "enemy_attacked": frozenset(("type", "enemy_id", "damage", "player_hp")),
    "boss_defeated": frozenset(("type", "node_id", "permanent_reward")),
    "run_abandoned": frozenset(("type", "reason")),
    "run_finished": frozenset(("type", "permanent_reward", "learning_fact_written")),
}
_SENSITIVE_KEYS = frozenset(
    (
        "seed",
        "rng_state",
        "rng_increment",
        "command_id",
        "command_log",
        "learner_id",
        "snapshot_id",
        "attempt_id",
    )
)


def filter_public_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Allowlist event fields, which removes the engine's run seed event field."""

    filtered: list[dict[str, Any]] = []
    for event in events:
        event_type = event.get("type")
        if not isinstance(event_type, str) or event_type not in _PUBLIC_EVENT_FIELDS:
            raise PublicProjectionError(f"unsupported public event type: {event_type!r}")
        allowed = _PUBLIC_EVENT_FIELDS[event_type]
        filtered.append({key: deepcopy(value) for key, value in event.items() if key in allowed})
    _assert_no_sensitive_keys(filtered)
    return filtered


def _assert_no_sensitive_keys(value: Any, path: str = "public") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _SENSITIVE_KEYS:
                raise PublicProjectionError(f"sensitive key leaked at {path}.{key}")
            _assert_no_sensitive_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_sensitive_keys(item, f"{path}[{index}]")


def _public_view(state: RunState) -> dict[str, Any]:
    view = build_view(state)
    cards = view.get("cards")
    if not isinstance(cards, Mapping):
        raise PublicProjectionError("engine view cards must be an object")
    view["cards"] = [deepcopy(card) for card in cards.values()]
    _assert_no_sensitive_keys(view)
    return view


def project_public_run(
    state: RunState,
    *,
    scenario_id: str,
    events: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
        "engine_protocol_version": PROTOCOL_VERSION,
        "public_projection_version": PUBLIC_PROJECTION_VERSION,
        "application_service_version": APPLICATION_SERVICE_VERSION,
        "scenario_id": scenario_id,
        "state_version": state.state_version,
        "state_hash": state_hash(state),
        "run": _public_view(state),
        "available_actions": [plan.public.to_dict() for plan in build_available_actions(state)],
        "events": filter_public_events(events),
    }
    _assert_no_sensitive_keys(response)
    return response
