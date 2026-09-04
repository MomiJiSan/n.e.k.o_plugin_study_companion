from __future__ import annotations

from typing import Any

from knowledge_dungeon.engine import KnowledgeDungeonEngine


def command(command_id: str, version: int, intent: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "command_id": command_id,
        "run_id": "battle-test",
        "expected_state_version": version,
        "intent": intent,
        "payload": payload or {},
    }


def cards() -> list[dict[str, Any]]:
    return [
        {
            "card_id": "math.active",
            "name": "活跃知识",
            "subject_id": "math",
            "base_damage": 8,
            "energy_cost": 1,
            "freshness_bps": 10_000,
            "lifecycle_state": "active",
        },
        {
            "card_id": "math.light",
            "name": "轻度褪色",
            "subject_id": "math",
            "base_damage": 5,
            "energy_cost": 1,
            "freshness_bps": 8_000,
            "lifecycle_state": "fading_light",
        },
        {
            "card_id": "math.heavy",
            "name": "重度褪色",
            "subject_id": "math",
            "base_damage": 8,
            "energy_cost": 1,
            "freshness_bps": 5_000,
            "lifecycle_state": "fading_heavy",
        },
        {
            "card_id": "math.dormant",
            "name": "休眠知识",
            "subject_id": "math",
            "base_damage": 20,
            "energy_cost": 1,
            "freshness_bps": 0,
            "lifecycle_state": "dormant",
        },
    ]


def started_encounter(*, subject: str = "math") -> tuple[KnowledgeDungeonEngine, int]:
    engine = KnowledgeDungeonEngine()
    response = engine.dispatch(
        command("start", 0, "start_run", {"seed": 7, "map_subject_id": subject, "cards": cards()})
    )
    assert response["accepted"]
    response = engine.dispatch(command("select", 1, "select_node", {"node_id": "battle_1"}))
    response = engine.dispatch(command("encounter", 2, "start_encounter"))
    assert response["accepted"]
    return engine, response["state_version"]


def test_fading_cards_scale_damage_and_dormant_card_stays_owned() -> None:
    engine, _ = started_encounter()
    state = engine.get_state("battle-test")
    assert state is not None
    assert "math.dormant" in state.cards
    assert "math.dormant" in state.dormant_card_ids
    assert "math.dormant" not in state.hand
    assert "math.dormant" not in state.draw_pile

    expected_damage = {"math.active": 8, "math.light": 4, "math.heavy": 4}
    for card_id, damage in expected_damage.items():
        engine, version = started_encounter()
        state = engine.get_state("battle-test")
        assert state is not None
        assert card_id in state.hand
        response = engine.dispatch(command(f"play-{card_id}", version, "play_card", {"card_id": card_id}))
        assert response["accepted"]
        event = next(event for event in response["events"] if event["type"] == "card_played")
        assert event["damage"] == damage


def test_cross_subject_cards_deal_half_of_their_faded_value() -> None:
    engine, version = started_encounter(subject="english")
    state = engine.get_state("battle-test")
    assert state is not None
    card_id = "math.active"
    assert card_id in state.hand
    response = engine.dispatch(command("cross-play", version, "play_card", {"card_id": card_id}))
    event = next(event for event in response["events"] if event["type"] == "card_played")
    assert event["subject_bps"] == 5_000
    assert event["damage"] == 4


def test_mercy_is_always_available_and_never_subject_scaled() -> None:
    engine, version = started_encounter(subject="english")
    state = engine.get_state("battle-test")
    assert state is not None
    mercy = state.cards["neutral.momiji_mercy"]
    assert mercy.base_damage == 1
    assert mercy.energy_cost == 0
    assert mercy.flavor_text == "快去学习获取更强力的卡片吧，少年！"
    response = engine.dispatch(command("mercy", version, "play_card", {"card_id": mercy.card_id}))
    event = next(event for event in response["events"] if event["type"] == "card_played")
    assert event["subject_bps"] == 10_000
    assert event["damage"] == 1
