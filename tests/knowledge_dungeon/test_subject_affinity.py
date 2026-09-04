from __future__ import annotations

from knowledge_dungeon.card_projection import (
    CardState,
    calculate_effect_value,
    project_cards,
    subject_multiplier_bp,
)
from knowledge_dungeon.fixtures import learned_calculus_snapshot


def _by_id(projection):
    return {card.definition.card_id: card for card in projection.cards}


def test_same_subject_is_full_strength_and_cross_subject_is_half_strength() -> None:
    math_cards = _by_id(project_cards(learned_calculus_snapshot(), "math"))
    english_cards = _by_id(project_cards(learned_calculus_snapshot(), "english"))

    active_id = "math.calculus.limit_concept"
    light_id = "math.calculus.limit_laws"
    heavy_id = "math.calculus.important_limits"

    assert math_cards[active_id].effective_damage == 3
    assert english_cards[active_id].effective_damage == 2
    assert math_cards[light_id].state is CardState.FADING_LIGHT
    assert math_cards[light_id].effective_damage == 4
    assert english_cards[light_id].effective_damage == 2
    assert math_cards[heavy_id].state is CardState.FADING_HEAVY
    assert math_cards[heavy_id].effective_damage == 4
    assert english_cards[heavy_id].effective_damage == 2


def test_neutral_starter_never_receives_subject_penalty() -> None:
    projection = _by_id(project_cards(learned_calculus_snapshot(), "language.unknown"))
    starter = projection["neutral.momiji_mercy"]

    assert starter.subject_multiplier_bp == 10_000
    assert starter.freshness_multiplier_bp == 10_000
    assert starter.effective_damage == 1


def test_related_subject_multiplier_requires_explicit_relation() -> None:
    assert subject_multiplier_bp("math", "physics") == 5_000
    assert (
        subject_multiplier_bp(
            "math",
            "physics",
            related_subject_pairs=(frozenset(("math", "physics")),),
        )
        == 7_500
    )


def test_subject_and_freshness_multipliers_compose_as_integer_basis_points() -> None:
    assert calculate_effect_value(12, 8_000, 5_000) == 5
    assert calculate_effect_value(12, 5_000, 5_000) == 3
    assert calculate_effect_value(12, 10_000, 10_000, 12_500) == 15
