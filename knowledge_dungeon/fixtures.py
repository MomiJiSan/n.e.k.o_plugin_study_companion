"""Simulated-only snapshots and projections for the v0.1 prototype."""

from __future__ import annotations

from .card_projection import CardCollectionProjection, project_cards
from .contracts import LearningSnapshot, TopicSnapshot, VersionBundle

FIXTURE_COMPUTED_AT = "2026-09-04T00:00:00Z"


def new_learner_snapshot() -> LearningSnapshot:
    return LearningSnapshot(
        snapshot_id="fixture.new_learner.v1",
        learner_id="simulated.new_learner",
        computed_at=FIXTURE_COMPUTED_AT,
        topics=(),
        owned_card_ids=(),
        versions=VersionBundle(),
    )


def learned_calculus_snapshot() -> LearningSnapshot:
    return LearningSnapshot(
        snapshot_id="fixture.learned_calculus.v1",
        learner_id="simulated.learned_calculus",
        computed_at=FIXTURE_COMPUTED_AT,
        topics=(
            TopicSnapshot("math.calculus.limit_concept", "math", 8_000, True),
            TopicSnapshot("math.calculus.limit_laws", "math", 6_700, False),
            TopicSnapshot("math.calculus.important_limits", "math", 6_200, False),
            TopicSnapshot("math.calculus.continuity", "math", 5_900, False),
        ),
        owned_card_ids=(
            "math.calculus.limit_laws",
            "math.calculus.important_limits",
            "math.calculus.continuity",
        ),
        versions=VersionBundle(),
    )


def calculus_card_projection() -> CardCollectionProjection:
    return project_cards(learned_calculus_snapshot(), "math")


def english_stub_card_projection() -> CardCollectionProjection:
    """Test-only projection; this is not an official English content pack."""

    return project_cards(learned_calculus_snapshot(), "english")
