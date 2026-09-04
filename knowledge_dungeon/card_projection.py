"""Pure projection from a frozen learning snapshot to playable cards."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .card_catalog import CARD_CATALOG, STARTER_CARD_ID
from .contracts import CardDefinition, LearningSnapshot, VersionBundle

FULL_MULTIPLIER_BP = 10_000
ACTIVE_MULTIPLIER_BP = FULL_MULTIPLIER_BP
FADING_LIGHT_MULTIPLIER_BP = 8_000
FADING_HEAVY_MULTIPLIER_BP = 5_000
CROSS_SUBJECT_MULTIPLIER_BP = 5_000
RELATED_SUBJECT_MULTIPLIER_BP = 7_500

ACTIVE_THRESHOLD_BP = 7_000
FADING_LIGHT_THRESHOLD_BP = 6_500
DORMANT_THRESHOLD_BP = 6_000


class CardState(str, Enum):
    LOCKED = "locked"
    ACTIVE = "active"
    FADING_LIGHT = "fading_light"
    FADING_HEAVY = "fading_heavy"
    DORMANT = "dormant"


def _state_for_mastery(mastery_bp: int) -> tuple[CardState, int]:
    if mastery_bp >= ACTIVE_THRESHOLD_BP:
        return CardState.ACTIVE, ACTIVE_MULTIPLIER_BP
    if mastery_bp >= FADING_LIGHT_THRESHOLD_BP:
        return CardState.FADING_LIGHT, FADING_LIGHT_MULTIPLIER_BP
    if mastery_bp >= DORMANT_THRESHOLD_BP:
        return CardState.FADING_HEAVY, FADING_HEAVY_MULTIPLIER_BP
    return CardState.DORMANT, 0


def subject_multiplier_bp(
    card_subject_id: str,
    map_subject_id: str,
    *,
    related_subject_pairs: Iterable[frozenset[str]] = (),
) -> int:
    """Return subject affinity without inferring any related subjects.

    v0.1 official content passes no related pairs. The explicit parameter keeps
    the future 75% policy engine-neutral without publishing inferred relations.
    """

    if card_subject_id == "neutral" or card_subject_id == map_subject_id:
        return FULL_MULTIPLIER_BP
    pair = frozenset((card_subject_id, map_subject_id))
    if len(pair) == 2 and pair in related_subject_pairs:
        return RELATED_SUBJECT_MULTIPLIER_BP
    return CROSS_SUBJECT_MULTIPLIER_BP


def calculate_effect_value(
    base_value: int,
    freshness_multiplier_bp: int,
    subject_affinity_multiplier_bp: int,
    run_buff_multiplier_bp: int = FULL_MULTIPLIER_BP,
    *,
    minimum_when_active: int = 1,
) -> int:
    """Apply all integer multipliers once with deterministic half-up rounding."""

    values = (
        (base_value, "base_value"),
        (freshness_multiplier_bp, "freshness_multiplier_bp"),
        (subject_affinity_multiplier_bp, "subject_affinity_multiplier_bp"),
        (run_buff_multiplier_bp, "run_buff_multiplier_bp"),
        (minimum_when_active, "minimum_when_active"),
    )
    for value, name in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if base_value == 0 or freshness_multiplier_bp == 0 or subject_affinity_multiplier_bp == 0:
        return 0
    denominator = FULL_MULTIPLIER_BP**3
    numerator = base_value * freshness_multiplier_bp * subject_affinity_multiplier_bp * run_buff_multiplier_bp
    rounded = (numerator + denominator // 2) // denominator
    return max(minimum_when_active, rounded)


@dataclass(frozen=True, slots=True)
class ProjectedCard:
    definition: CardDefinition
    owned: bool
    state: CardState
    mastery_bp: int
    freshness_multiplier_bp: int
    subject_multiplier_bp: int

    def __post_init__(self) -> None:
        if not isinstance(self.definition, CardDefinition):
            raise ValueError("definition must be a CardDefinition")
        if not isinstance(self.owned, bool):
            raise ValueError("owned must be a boolean")
        if not isinstance(self.state, CardState):
            raise ValueError("state must be a CardState")
        for field_name in ("mastery_bp", "freshness_multiplier_bp", "subject_multiplier_bp"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= FULL_MULTIPLIER_BP:
                raise ValueError(f"{field_name} must be an integer from 0 to 10000")
        if not self.owned and self.state is not CardState.LOCKED:
            raise ValueError("unowned card must be locked")
        if self.state in (CardState.LOCKED, CardState.DORMANT) and self.freshness_multiplier_bp != 0:
            raise ValueError("locked or dormant card must have zero freshness multiplier")

    @property
    def playable(self) -> bool:
        return self.owned and self.state not in (CardState.LOCKED, CardState.DORMANT)

    @property
    def effective_damage(self) -> int:
        if not self.playable:
            return 0
        return calculate_effect_value(
            self.definition.base_damage,
            self.freshness_multiplier_bp,
            self.subject_multiplier_bp,
        )

    def to_dict(self) -> dict[str, object]:
        result = self.definition.to_dict()
        result.update(
            {
                "owned": self.owned,
                "state": self.state.value,
                "playable": self.playable,
                "mastery_bp": self.mastery_bp,
                "freshness_multiplier_bp": self.freshness_multiplier_bp,
                "subject_multiplier_bp": self.subject_multiplier_bp,
                "effective_damage": self.effective_damage,
            }
        )
        return result


@dataclass(frozen=True, slots=True)
class CardCollectionProjection:
    snapshot_id: str
    map_subject_id: str
    cards: tuple[ProjectedCard, ...]
    newly_unlocked_card_ids: tuple[str, ...]
    versions: VersionBundle

    @property
    def playable_cards(self) -> tuple[ProjectedCard, ...]:
        return tuple(card for card in self.cards if card.playable)

    @property
    def owned_card_ids(self) -> tuple[str, ...]:
        return tuple(card.definition.card_id for card in self.cards if card.owned)

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "map_subject_id": self.map_subject_id,
            "cards": [card.to_dict() for card in self.cards],
            "newly_unlocked_card_ids": list(self.newly_unlocked_card_ids),
            "versions": self.versions.to_dict(),
        }


def project_cards(
    snapshot: LearningSnapshot,
    map_subject_id: str,
    *,
    catalog: tuple[CardDefinition, ...] = CARD_CATALOG,
    related_subject_pairs: Iterable[frozenset[str]] = (),
) -> CardCollectionProjection:
    """Project ownership and current power without mutating learning state."""

    if not isinstance(snapshot, LearningSnapshot):
        raise ValueError("snapshot must be a LearningSnapshot")
    if not isinstance(map_subject_id, str) or not map_subject_id.strip():
        raise ValueError("map_subject_id must be a non-empty string")
    if len({card.card_id for card in catalog}) != len(catalog):
        raise ValueError("catalog must not contain duplicate card_id values")

    topics = {topic.topic_id: topic for topic in snapshot.topics}
    previously_owned = set(snapshot.owned_card_ids)
    projected: list[ProjectedCard] = []
    newly_unlocked: list[str] = []

    for definition in catalog:
        affinity = subject_multiplier_bp(
            definition.subject_id,
            map_subject_id,
            related_subject_pairs=related_subject_pairs,
        )
        if definition.card_id == STARTER_CARD_ID:
            projected.append(
                ProjectedCard(
                    definition=definition,
                    owned=True,
                    state=CardState.ACTIVE,
                    mastery_bp=FULL_MULTIPLIER_BP,
                    freshness_multiplier_bp=FULL_MULTIPLIER_BP,
                    subject_multiplier_bp=FULL_MULTIPLIER_BP,
                )
            )
            continue

        topic = topics.get(definition.topic_id or "")
        newly_mastered = topic is not None and topic.mastered
        owned = definition.card_id in previously_owned or newly_mastered
        if not owned:
            state, freshness = CardState.LOCKED, 0
            mastery_bp = topic.mastery_bp if topic is not None else 0
        else:
            mastery_bp = topic.mastery_bp if topic is not None else 0
            state, freshness = _state_for_mastery(mastery_bp)
            if newly_mastered and definition.card_id not in previously_owned:
                newly_unlocked.append(definition.card_id)
        projected.append(
            ProjectedCard(
                definition=definition,
                owned=owned,
                state=state,
                mastery_bp=mastery_bp,
                freshness_multiplier_bp=freshness,
                subject_multiplier_bp=affinity,
            )
        )

    return CardCollectionProjection(
        snapshot_id=snapshot.snapshot_id,
        map_subject_id=map_subject_id,
        cards=tuple(projected),
        newly_unlocked_card_ids=tuple(newly_unlocked),
        versions=snapshot.versions,
    )
