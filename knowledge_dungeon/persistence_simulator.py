"""Small restart-recovery demonstration for the v0.2 dungeon store."""

from __future__ import annotations

import argparse
from pathlib import Path

from .engine import KnowledgeDungeonEngine
from .persistence import DungeonRunStore
from .serializer import state_hash

RUN_ID = "persistence-v0-2-demo"


def _command(
    command_id: str,
    version: int,
    intent: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "command_id": command_id,
        "run_id": RUN_ID,
        "expected_state_version": version,
        "intent": intent,
        "payload": payload or {},
    }


def run_recovery_demo(database: Path) -> tuple[str, str, bool]:
    start = _command("cmd-001", 0, "start_run", {"seed": 20260904})
    select = _command(
        "cmd-002", 1, "select_node", {"node_id": "battle_1"}
    )
    with DungeonRunStore(database) as store:
        if store.load_run(RUN_ID) is not None:
            raise RuntimeError(f"demo run already exists in {database}")
        engine = KnowledgeDungeonEngine(store)
        start_response = engine.dispatch(start)
        latest_response = engine.dispatch(select)
        if not start_response["accepted"] or not latest_response["accepted"]:
            raise RuntimeError("demo command was rejected")
        before_restart_hash = str(latest_response["state_hash"])

    with DungeonRunStore(database) as reopened_store:
        recovered_engine = KnowledgeDungeonEngine(reopened_store)
        recovered = recovered_engine.get_state(RUN_ID)
        if recovered is None:
            raise RuntimeError("persisted run was not recovered")
        after_restart_hash = state_hash(recovered)
        retry_matched = recovered_engine.dispatch(start) == start_response

    return before_restart_hash, after_restart_hash, retry_matched


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Knowledge Dungeon v0.2 persistence recovery demo"
    )
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args()
    before, after, retry_matched = run_recovery_demo(args.database)
    print(f"BEFORE_RESTART_HASH={before}")
    print(f"AFTER_RESTART_HASH={after}")
    print(f"RETRY_MATCHED={str(retry_matched).lower()}")
    return 0 if before == after and retry_matched else 1


if __name__ == "__main__":
    raise SystemExit(main())
