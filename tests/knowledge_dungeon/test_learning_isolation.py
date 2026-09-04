from __future__ import annotations

from pathlib import Path

from knowledge_dungeon.engine import KnowledgeDungeonEngine


def test_v0_1_command_rejects_learning_attempt_identifier() -> None:
    response = KnowledgeDungeonEngine().dispatch(
        {
            "command_id": "start",
            "run_id": "isolated",
            "expected_state_version": 0,
            "intent": "start_run",
            "attempt_id": "must-not-exist",
            "payload": {},
        }
    )
    assert response["accepted"] is False
    assert response["error"]["code"] == "invalid_command"


def test_engine_modules_do_not_import_learning_or_store_domains() -> None:
    package = Path(__file__).parents[2] / "knowledge_dungeon"
    forbidden = ("adaptive_learning", "store_", "fsrs", "cognitive_")
    for filename in ("commands.py", "state.py", "reducer.py", "engine.py", "serializer.py"):
        source = (package / filename).read_text(encoding="utf-8")
        assert not any(f"import {name}" in source or f"from {name}" in source for name in forbidden)


def test_boss_finish_explicitly_emits_no_learning_or_permanent_reward() -> None:
    # The isolation invariant is part of the public event contract, so renderer
    # fixtures cannot accidentally treat v0.1 combat as learning evidence.
    source = (Path(__file__).parents[2] / "knowledge_dungeon" / "reducer.py").read_text(encoding="utf-8")
    assert '"learning_fact_written": False' in source
    assert '"permanent_reward": None' in source
