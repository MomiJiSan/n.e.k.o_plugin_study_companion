"""Export representative snapshots by playing the real Python authority."""

import argparse
import json
from pathlib import Path

from .application_service import KnowledgeDungeonApplicationService
from .bridge_contracts import TrustedInvocationContext


def build_forest_snapshots():
    service = KnowledgeDungeonApplicationService(seed_factory=lambda: 1)
    context = TrustedInvocationContext("forest-fixture", "study_companion:dungeon")
    snapshots = []
    state = service.create_run(
        context,
        dict(bridge_protocol_version=1, request_id="forest-snapshots", subject_id="math", scenario_id="forest_v0_2"),
    )
    snapshots.append(state)

    def act(action):
        nonlocal state
        state = service.perform_action(
            context,
            state["run"]["run_id"],
            dict(
                bridge_protocol_version=1,
                request_id=f"step-{state['state_version']}",
                expected_state_version=state["state_version"],
                action_id=action,
            ),
        )
        snapshots.append(state)

    for node, choice in [
        ("old_camp", "read_journal"),
        ("edge_spring", "heal"),
        ("convergence", "anchor"),
        ("broken_creek", "search"),
        ("memory_spring", "prepare"),
        ("guardian_seal", "gather"),
        ("guardian", None),
    ]:
        act(f"select_node:{node}")
        act("enter_selected_node")
        if choice:
            act(f"choose_event:{choice}")
    while state["run"]["phase"] == "encounter":
        act(next((a["action_id"] for a in state["available_actions"] if a["action_type"] == "play_card"), "end_turn"))
    act("finish_run")
    act("repair_camp")
    return {
        "fixture_version": 1,
        "producer": "knowledge_dungeon.forest_fixture_exporter",
        "bootstrap": service.bootstrap(context),
        "snapshots": snapshots,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(build_forest_snapshots(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
