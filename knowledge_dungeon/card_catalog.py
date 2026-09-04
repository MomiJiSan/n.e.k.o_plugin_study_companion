"""Fixed v0.1 card content; no production content-pack integration."""

from __future__ import annotations

from .contracts import CardDefinition

STARTER_CARD_ID = "neutral.momiji_mercy"

_DAMAGE_BY_TIER = {1: 3, 2: 5, 3: 8, 4: 12, 5: 17}
_ENERGY_BY_TIER = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3}


def knowledge_damage_for_tier(difficulty_tier: int) -> int:
    if isinstance(difficulty_tier, bool) or not isinstance(difficulty_tier, int):
        raise ValueError("difficulty_tier must be an integer from 1 to 5")
    try:
        return _DAMAGE_BY_TIER[difficulty_tier]
    except (KeyError, TypeError) as exc:
        raise ValueError("difficulty_tier must be an integer from 1 to 5") from exc


def energy_for_tier(difficulty_tier: int) -> int:
    if isinstance(difficulty_tier, bool) or not isinstance(difficulty_tier, int):
        raise ValueError("difficulty_tier must be an integer from 1 to 5")
    try:
        return _ENERGY_BY_TIER[difficulty_tier]
    except (KeyError, TypeError) as exc:
        raise ValueError("difficulty_tier must be an integer from 1 to 5") from exc


def _knowledge_card(*, card_id: str, name: str, topic_id: str, tier: int, flavor_text: str) -> CardDefinition:
    damage = knowledge_damage_for_tier(tier)
    return CardDefinition(
        card_id=card_id,
        name=name,
        subject_id="math",
        topic_id=topic_id,
        difficulty_tier=tier,
        base_damage=damage,
        energy_cost=energy_for_tier(tier),
        rules_text=f"造成 {damage} 点伤害。",
        flavor_text=flavor_text,
    )


STARTER_CARD = CardDefinition(
    card_id=STARTER_CARD_ID,
    name="红枼的怜悯",
    subject_id="neutral",
    topic_id=None,
    difficulty_tier=0,
    base_damage=1,
    energy_cost=0,
    rules_text="造成 1 点伤害。每回合只能使用一次。",
    flavor_text="快去学习获取更强力的卡片吧，少年！",
    starter=True,
    removable=False,
    once_per_turn=True,
)

MATH_CARD_CATALOG: tuple[CardDefinition, ...] = (
    _knowledge_card(
        card_id="math.calculus.limit_concept",
        name="极限概念",
        topic_id="math.calculus.limit_concept",
        tier=1,
        flavor_text="趋近并非抵达，却足以窥见答案。",
    ),
    _knowledge_card(
        card_id="math.calculus.limit_laws",
        name="极限运算法则",
        topic_id="math.calculus.limit_laws",
        tier=2,
        flavor_text="法则让无穷的变化重新变得有序。",
    ),
    _knowledge_card(
        card_id="math.calculus.important_limits",
        name="两个重要极限",
        topic_id="math.calculus.important_limits",
        tier=3,
        flavor_text="记住通往无穷的两枚路标。",
    ),
    _knowledge_card(
        card_id="math.calculus.continuity",
        name="连续性",
        topic_id="math.calculus.continuity",
        tier=2,
        flavor_text="没有断裂的道路，才能承载稳定的力量。",
    ),
)

CARD_CATALOG: tuple[CardDefinition, ...] = (STARTER_CARD, *MATH_CARD_CATALOG)
_CARD_BY_ID = {card.card_id: card for card in CARD_CATALOG}


def get_card(card_id: str) -> CardDefinition:
    try:
        return _CARD_BY_ID[card_id]
    except KeyError as exc:
        raise KeyError(f"unknown card_id: {card_id}") from exc
