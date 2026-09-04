from __future__ import annotations

from dataclasses import FrozenInstanceError

from knowledge_dungeon.card_catalog import (
    MATH_CARD_CATALOG,
    STARTER_CARD,
    energy_for_tier,
    knowledge_damage_for_tier,
)
from knowledge_dungeon.card_projection import CardState, calculate_effect_value, project_cards
from knowledge_dungeon.contracts import LearningSnapshot, TopicSnapshot, canonical_json, canonical_sha256
from knowledge_dungeon.fixtures import learned_calculus_snapshot, new_learner_snapshot
from knowledge_dungeon.rng import PCG32


def _by_id(projection):
    return {card.definition.card_id: card for card in projection.cards}


def test_starter_card_contract_is_exact_and_permanent() -> None:
    projected = project_cards(new_learner_snapshot(), "english")
    starter = _by_id(projected)["neutral.momiji_mercy"]

    assert STARTER_CARD.name == "红枼的怜悯"
    assert STARTER_CARD.flavor_text == "快去学习获取更强力的卡片吧，少年！"
    assert STARTER_CARD.base_damage == 1
    assert STARTER_CARD.energy_cost == 0
    assert STARTER_CARD.once_per_turn is True
    assert STARTER_CARD.removable is False
    assert starter.owned is True
    assert starter.state is CardState.ACTIVE
    assert starter.freshness_multiplier_bp == 10_000
    assert starter.subject_multiplier_bp == 10_000
    assert starter.effective_damage == 1


def test_new_learner_has_only_the_starter_as_owned_and_playable() -> None:
    projection = project_cards(new_learner_snapshot(), "math")

    assert projection.owned_card_ids == ("neutral.momiji_mercy",)
    assert [card.definition.card_id for card in projection.playable_cards] == ["neutral.momiji_mercy"]
    assert all(card.state is CardState.LOCKED for card in projection.cards[1:])


def test_mastered_topic_unlocks_card_but_ownership_is_independent_of_usability() -> None:
    projection = project_cards(learned_calculus_snapshot(), "math")
    cards = _by_id(projection)

    assert projection.newly_unlocked_card_ids == ("math.calculus.limit_concept",)
    assert cards["math.calculus.limit_concept"].owned is True
    assert cards["math.calculus.limit_concept"].state is CardState.ACTIVE
    assert cards["math.calculus.continuity"].owned is True
    assert cards["math.calculus.continuity"].state is CardState.DORMANT
    assert cards["math.calculus.continuity"].playable is False
    assert cards["math.calculus.continuity"].effective_damage == 0


def _assert_threshold(mastery_bp: int, expected_state: CardState, expected_multiplier: int) -> None:
    card = MATH_CARD_CATALOG[0]
    snapshot = LearningSnapshot(
        snapshot_id="fixture.threshold.v1",
        learner_id="simulated.threshold",
        computed_at="2026-09-04T00:00:00Z",
        topics=(TopicSnapshot(card.topic_id or "", "math", mastery_bp, False),),
        owned_card_ids=(card.card_id,),
    )
    projected = _by_id(project_cards(snapshot, "math"))[card.card_id]

    assert projected.state is expected_state
    assert projected.freshness_multiplier_bp == expected_multiplier


def test_mastery_10000_is_active() -> None:
    _assert_threshold(10_000, CardState.ACTIVE, 10_000)


def test_mastery_7000_is_active() -> None:
    _assert_threshold(7_000, CardState.ACTIVE, 10_000)


def test_mastery_6999_is_fading_light() -> None:
    _assert_threshold(6_999, CardState.FADING_LIGHT, 8_000)


def test_mastery_6500_is_fading_light() -> None:
    _assert_threshold(6_500, CardState.FADING_LIGHT, 8_000)


def test_mastery_6499_is_fading_heavy() -> None:
    _assert_threshold(6_499, CardState.FADING_HEAVY, 5_000)


def test_mastery_6000_is_fading_heavy() -> None:
    _assert_threshold(6_000, CardState.FADING_HEAVY, 5_000)


def test_mastery_5999_is_dormant() -> None:
    _assert_threshold(5_999, CardState.DORMANT, 0)


def test_mastery_zero_is_dormant() -> None:
    _assert_threshold(0, CardState.DORMANT, 0)


def test_tier_budget_and_fixture_damage_are_fixed() -> None:
    assert [knowledge_damage_for_tier(tier) for tier in range(1, 6)] == [3, 5, 8, 12, 17]
    assert [energy_for_tier(tier) for tier in range(1, 6)] == [1, 1, 2, 2, 3]
    assert [card.base_damage for card in MATH_CARD_CATALOG] == [3, 5, 8, 5]


def test_integer_effect_calculation_rounds_half_up_and_keeps_active_attack_minimum() -> None:
    assert calculate_effect_value(5, 8_000, 10_000) == 4
    assert calculate_effect_value(5, 5_000, 10_000) == 3
    assert calculate_effect_value(3, 5_000, 5_000) == 1
    assert calculate_effect_value(1, 5_000, 5_000) == 1
    assert calculate_effect_value(17, 0, 10_000) == 0


def test_contracts_are_immutable_and_canonical_serialization_is_stable() -> None:
    snapshot = learned_calculus_snapshot()

    try:
        snapshot.snapshot_id = "changed"  # type: ignore[misc]
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("LearningSnapshot must remain immutable")

    assert canonical_json({"z": 1, "name": "红枼", "a": (2, 3)}) == '{"a":[2,3],"name":"红枼","z":1}'
    assert canonical_sha256(snapshot) == canonical_sha256(snapshot.to_dict())


def test_pcg32_reference_vector_and_shuffle_are_deterministic() -> None:
    rng = PCG32(seed=42, sequence=54)
    assert [rng.next_uint32() for _ in range(5)] == [
        0xA15C02B7,
        0x7B47F409,
        0xBA1D3330,
        0x83D2F293,
        0xBFA4784B,
    ]

    first = list(range(10))
    second = list(range(10))
    PCG32(2026).shuffle(first)
    PCG32(2026).shuffle(second)
    assert first == second
    assert first != list(range(10))
