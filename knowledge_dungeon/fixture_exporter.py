"""Export deterministic Knowledge Dungeon fixtures for presentation clients."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .contracts import PROTOCOL_VERSION, canonical_sha256
from .engine import KnowledgeDungeonEngine
from .fixtures import calculus_card_projection

FIXTURE_VERSION = 2
SCENARIO_ID = "calculus_v0_1"
RUN_ID = "fixture-calculus-v0-2"
SEED = 20_260_904


def _command(
    serial: int,
    expected_state_version: int,
    intent: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "command_id": f"fixture-cmd-{serial:03d}",
        "run_id": RUN_ID,
        "expected_state_version": expected_state_version,
        "intent": intent,
        "payload": payload or {},
    }


def _scenario_commands() -> list[dict[str, Any]]:
    projection = calculus_card_projection().to_dict()
    planned_actions: Sequence[tuple[str, dict[str, Any]]] = (
        ("start_run", {"seed": SEED, "map_subject_id": "math", "cards": projection["cards"], "versions": projection["versions"]}),
        ("select_node", {"node_id": "battle_1"}),
        ("start_encounter", {}),
        ("play_card", {"card_id": "math.calculus.important_limits"}),
        ("play_card", {"card_id": "math.calculus.limit_laws"}),
        ("choose_run_reward", {"reward_id": "next_damage_25"}),
        ("select_node", {"node_id": "rest_1"}),
        ("start_encounter", {}),
        ("select_node", {"node_id": "boss_1"}),
        ("start_encounter", {}),
        ("play_card", {"card_id": "math.calculus.important_limits"}),
        ("play_card", {"card_id": "math.calculus.limit_laws"}),
        ("end_turn", {}),
        ("play_card", {"card_id": "math.calculus.important_limits"}),
        ("play_card", {"card_id": "math.calculus.limit_laws"}),
        ("finish_run", {}),
    )
    return [
        _command(serial, serial - 1, intent, payload)
        for serial, (intent, payload) in enumerate(planned_actions, start=1)
    ]


def build_calculus_demo_fixture() -> dict[str, Any]:
    """Run the authority engine and return its complete request/response chain."""

    projection = calculus_card_projection().to_dict()
    engine = KnowledgeDungeonEngine()
    steps: list[dict[str, Any]] = []
    for request in _scenario_commands():
        response = engine.dispatch(request)
        if not response["accepted"]:
            error = response.get("error", {})
            raise RuntimeError(
                f"fixture command {request['command_id']} rejected: "
                f"{error.get('code')}: {error.get('message')}"
            )
        steps.append({"request": request, "response": response})

    final_response = steps[-1]["response"]
    fixture: dict[str, Any] = {
        "fixture_version": FIXTURE_VERSION,
        "producer": "knowledge_dungeon.fixture_exporter",
        "scenario_id": SCENARIO_ID,
        "locale": "zh-CN",
        "title": "极限森林",
        "run_id": RUN_ID,
        "protocol_version": PROTOCOL_VERSION,
        "projection": projection,
        "route": ["entrance", "battle_1", "rest_1", "boss_1"],
        "steps": steps,
        "final_state_hash": final_response["state_hash"],
    }
    fixture["fixture_sha256"] = canonical_sha256(fixture)
    return fixture


def serialize_fixture(fixture: dict[str, Any]) -> str:
    return json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_fixture(path: Path, fixture: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_fixture(fixture), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the Python-authoritative Knowledge Dungeon demo fixture"
    )
    parser.add_argument("--output", type=Path, help="write the fixture to this path")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when --output does not already match the generated fixture",
    )
    args = parser.parse_args(argv)
    if args.check and args.output is None:
        parser.error("--check requires --output")

    rendered = serialize_fixture(build_calculus_demo_fixture())
    if args.output is None:
        print(rendered, end="")
        return 0
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"fixture is stale: {args.output}", file=sys.stderr)
            return 1
        print(f"fixture is current: {args.output}")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote fixture: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
