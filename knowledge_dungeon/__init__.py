"""Knowledge Dungeon deterministic prototype contracts.

This package is intentionally isolated from the production learning domains.
It still consumes simulated snapshots only; v0.2 adds optional run persistence.
"""

from .card_catalog import (
    MATH_CARD_CATALOG,
    STARTER_CARD,
    STARTER_CARD_ID,
    energy_for_tier,
    get_card,
    knowledge_damage_for_tier,
)
from .card_projection import (
    ACTIVE_MULTIPLIER_BP,
    CROSS_SUBJECT_MULTIPLIER_BP,
    FADING_HEAVY_MULTIPLIER_BP,
    FADING_LIGHT_MULTIPLIER_BP,
    FULL_MULTIPLIER_BP,
    RELATED_SUBJECT_MULTIPLIER_BP,
    CardCollectionProjection,
    CardState,
    ProjectedCard,
    calculate_effect_value,
    project_cards,
    subject_multiplier_bp,
)
from .commands import DungeonCommand
from .contracts import (
    CARD_POLICY_VERSION,
    CONTENT_PACK_VERSION,
    ENGINE_VERSION,
    PROTOCOL_VERSION,
    RNG_ALGORITHM,
    STATE_SCHEMA_VERSION,
    CardDefinition,
    CommandEnvelope,
    CommandIntent,
    DungeonResponse,
    LearningSnapshot,
    TopicSnapshot,
    VersionBundle,
    canonical_json,
    canonical_sha256,
)
from .engine import KnowledgeDungeonEngine
from .fixtures import (
    calculus_card_projection,
    english_stub_card_projection,
    learned_calculus_snapshot,
    new_learner_snapshot,
)
from .persistence import DungeonRunStore, DungeonStoreError
from .rng import PCG32
from .serializer import deserialize_state, serialize_state, state_hash
from .state import RunState

__all__ = [
    "ACTIVE_MULTIPLIER_BP",
    "CARD_POLICY_VERSION",
    "CONTENT_PACK_VERSION",
    "CROSS_SUBJECT_MULTIPLIER_BP",
    "ENGINE_VERSION",
    "FADING_HEAVY_MULTIPLIER_BP",
    "FADING_LIGHT_MULTIPLIER_BP",
    "FULL_MULTIPLIER_BP",
    "MATH_CARD_CATALOG",
    "PROTOCOL_VERSION",
    "RELATED_SUBJECT_MULTIPLIER_BP",
    "RNG_ALGORITHM",
    "STARTER_CARD",
    "STARTER_CARD_ID",
    "STATE_SCHEMA_VERSION",
    "CardCollectionProjection",
    "CardDefinition",
    "CardState",
    "CommandEnvelope",
    "CommandIntent",
    "DungeonResponse",
    "DungeonCommand",
    "DungeonRunStore",
    "DungeonStoreError",
    "LearningSnapshot",
    "PCG32",
    "ProjectedCard",
    "TopicSnapshot",
    "KnowledgeDungeonEngine",
    "RunState",
    "VersionBundle",
    "calculate_effect_value",
    "calculus_card_projection",
    "canonical_json",
    "canonical_sha256",
    "deserialize_state",
    "energy_for_tier",
    "english_stub_card_projection",
    "get_card",
    "knowledge_damage_for_tier",
    "learned_calculus_snapshot",
    "new_learner_snapshot",
    "project_cards",
    "serialize_state",
    "state_hash",
    "subject_multiplier_bp",
]
