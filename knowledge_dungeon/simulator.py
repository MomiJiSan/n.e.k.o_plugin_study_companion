"""Scripted CLI demonstration for the knowledge dungeon v0.1 engine."""

from __future__ import annotations

import argparse
import json
from typing import Any

from .engine import KnowledgeDungeonEngine
from .fixtures import calculus_card_projection


def _demo_projection() -> dict[str, Any]:
    return calculus_card_projection().to_dict()


def _command(
    command_id: str,
    version: int,
    intent: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "command_id": command_id,
        "run_id": "calculus-v0-1-demo",
        "expected_state_version": version,
        "intent": intent,
        "payload": payload or {},
    }


def run_calculus_demo() -> KnowledgeDungeonEngine:
    engine = KnowledgeDungeonEngine()
    version = 0

    def send(command_id: str, intent: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal version
        response = engine.dispatch(_command(command_id, version, intent, payload))
        if not response["accepted"]:
            raise RuntimeError(response["error"])
        version = response["state_version"]
        for event in response["events"]:
            print(json.dumps(event, ensure_ascii=False, sort_keys=True))
        return response

    send(
        "cmd-001",
        "start_run",
        {
            "seed": 20260904,
            "map_subject_id": "math",
            "cards": _demo_projection()["cards"],
            "versions": _demo_projection()["versions"],
        },
    )
    send("cmd-002", "select_node", {"node_id": "battle_1"})
    send("cmd-003", "start_encounter")

    serial = 4
    while True:
        state = engine.get_state("calculus-v0-1-demo")
        assert state is not None
        if state.phase != "encounter":
            break
        playable = next(
            (
                card_id
                for card_id in state.hand
                if state.cards[card_id].energy_cost <= state.energy
                and not (card_id == "neutral.momiji_mercy" and state.mercy_used_this_turn)
            ),
            None,
        )
        if playable is None:
            send(f"cmd-{serial:03d}", "end_turn")
        else:
            send(f"cmd-{serial:03d}", "play_card", {"card_id": playable})
        serial += 1

    send(f"cmd-{serial:03d}", "choose_run_reward", {"reward_id": "next_damage_25"})
    serial += 1
    send(f"cmd-{serial:03d}", "select_node", {"node_id": "rest_1"})
    serial += 1
    send(f"cmd-{serial:03d}", "start_encounter")
    serial += 1
    send(f"cmd-{serial:03d}", "select_node", {"node_id": "boss_1"})
    serial += 1
    send(f"cmd-{serial:03d}", "start_encounter")
    serial += 1

    while True:
        state = engine.get_state("calculus-v0-1-demo")
        assert state is not None
        if state.phase != "encounter":
            break
        playable = next(
            (
                card_id
                for card_id in state.hand
                if state.cards[card_id].energy_cost <= state.energy
                and not (card_id == "neutral.momiji_mercy" and state.mercy_used_this_turn)
            ),
            None,
        )
        if playable is None:
            send(f"cmd-{serial:03d}", "end_turn")
        else:
            send(f"cmd-{serial:03d}", "play_card", {"card_id": playable})
        serial += 1

    final = send(f"cmd-{serial:03d}", "finish_run")
    print(f"FINAL_STATE_HASH={final['state_hash']}")
    return engine


def main() -> int:
    parser = argparse.ArgumentParser(description="Knowledge Dungeon v0.1 deterministic simulator")
    parser.add_argument("--scenario", choices=["calculus_v0_1"], default="calculus_v0_1")
    args = parser.parse_args()
    if args.scenario == "calculus_v0_1":
        run_calculus_demo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
