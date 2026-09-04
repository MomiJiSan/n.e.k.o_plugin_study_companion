from __future__ import annotations

from knowledge_dungeon.engine import KnowledgeDungeonEngine
from knowledge_dungeon.fixtures import calculus_card_projection
from knowledge_dungeon.serializer import deserialize_state, serialize_state, state_hash


def test_state_hash_is_stable_across_serialization_roundtrip() -> None:
    engine = KnowledgeDungeonEngine()
    response = engine.dispatch(
        {
            "protocol_version": 1,
            "command_id": "start",
            "run_id": "hash",
            "expected_state_version": 0,
            "intent": "start_run",
            "payload": {"seed": 123, "map_subject_id": "math"},
        }
    )
    state = engine.get_state("hash")
    assert state is not None
    restored = deserialize_state(serialize_state(state))
    assert response["state_hash"] == state_hash(state) == state_hash(restored)


def test_identical_input_produces_identical_hash() -> None:
    command = {"protocol_version": 1, "command_id": "start", "run_id": "same", "expected_state_version": 0, "intent": "start_run", "payload": {"seed": 99}}
    assert KnowledgeDungeonEngine().dispatch(command)["state_hash"] == KnowledgeDungeonEngine().dispatch(command)["state_hash"]


def test_mapping_state_hash_ignores_command_log_like_run_state() -> None:
    engine = KnowledgeDungeonEngine()
    response = engine.dispatch(
        {
            "protocol_version": 1,
            "command_id": "start",
            "run_id": "mapping-hash",
            "expected_state_version": 0,
            "intent": "start_run",
            "payload": {},
        }
    )
    assert response["accepted"]
    state = engine.get_state("mapping-hash")
    assert state is not None
    assert state.command_log
    assert state_hash(state) == state_hash(state.to_dict())


def test_exported_electron_protocol_sample_hash_is_reproducible() -> None:
    projection = calculus_card_projection().to_dict()
    response = KnowledgeDungeonEngine().dispatch(
        {
            "protocol_version": 1,
            "command_id": "fixture-start-001",
            "run_id": "fixture-calculus-v0-1",
            "expected_state_version": 0,
            "intent": "start_run",
            "payload": {
                "seed": 20260904,
                "map_subject_id": "math",
                "cards": projection["cards"],
                "versions": projection["versions"],
            },
        }
    )

    assert response["state_hash"] == "b596868b83997e870690793a0877833a616a4ea7cb9427594d9b03ef2e361034"
