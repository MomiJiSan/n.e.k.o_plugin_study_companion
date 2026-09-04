"""Strict, JSON-safe contracts for the Knowledge Dungeon v0.1 prototype."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

PROTOCOL_VERSION = 1
STATE_SCHEMA_VERSION = 1
CONTENT_PACK_VERSION = "calculus-v0.1.0"
ENGINE_VERSION = "knowledge-dungeon-v0.1.0"
CARD_POLICY_VERSION = "knowledge-cards-v1"
RNG_ALGORITHM = "pcg32-v1"

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _require_text(value: str, field_name: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
    if identifier and _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical lowercase identifier")
    return value


def _require_int(value: int, field_name: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        maximum_text = f" and <= {maximum}" if maximum is not None else ""
        raise ValueError(f"{field_name} must be >= {minimum}{maximum_text}")
    return value


def _json_safe(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_safe(value.to_dict())
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON cannot contain non-finite floats")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical JSON object keys must be strings")
            result[key] = _json_safe(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize a contract value identically across runtimes."""

    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VersionBundle:
    protocol_version: int = PROTOCOL_VERSION
    state_schema_version: int = STATE_SCHEMA_VERSION
    content_pack_version: str = CONTENT_PACK_VERSION
    engine_version: str = ENGINE_VERSION
    card_policy_version: str = CARD_POLICY_VERSION
    rng_algorithm: str = RNG_ALGORITHM

    def __post_init__(self) -> None:
        _require_int(self.protocol_version, "protocol_version", minimum=1)
        _require_int(self.state_schema_version, "state_schema_version", minimum=1)
        _require_text(self.content_pack_version, "content_pack_version")
        _require_text(self.engine_version, "engine_version")
        _require_text(self.card_policy_version, "card_policy_version")
        _require_text(self.rng_algorithm, "rng_algorithm")

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "state_schema_version": self.state_schema_version,
            "content_pack_version": self.content_pack_version,
            "engine_version": self.engine_version,
            "card_policy_version": self.card_policy_version,
            "rng_algorithm": self.rng_algorithm,
        }


@dataclass(frozen=True, slots=True)
class CardDefinition:
    card_id: str
    name: str
    subject_id: str
    difficulty_tier: int
    base_damage: int
    energy_cost: int
    rules_text: str
    flavor_text: str
    topic_id: str | None = None
    starter: bool = False
    removable: bool = True
    once_per_turn: bool = False

    def __post_init__(self) -> None:
        _require_text(self.card_id, "card_id", identifier=True)
        _require_text(self.name, "name")
        _require_text(self.subject_id, "subject_id", identifier=True)
        _require_int(self.difficulty_tier, "difficulty_tier", minimum=0, maximum=5)
        _require_int(self.base_damage, "base_damage", minimum=1)
        _require_int(self.energy_cost, "energy_cost", minimum=0)
        _require_text(self.rules_text, "rules_text")
        _require_text(self.flavor_text, "flavor_text")
        if self.topic_id is not None:
            _require_text(self.topic_id, "topic_id", identifier=True)
        if self.starter:
            if self.topic_id is not None or self.subject_id != "neutral" or self.difficulty_tier != 0:
                raise ValueError("starter card must be neutral, tier 0, and have no topic_id")
        elif self.topic_id is None or self.difficulty_tier == 0:
            raise ValueError("knowledge card must have a topic_id and difficulty tier 1..5")

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "name": self.name,
            "subject_id": self.subject_id,
            "topic_id": self.topic_id,
            "difficulty_tier": self.difficulty_tier,
            "base_damage": self.base_damage,
            "energy_cost": self.energy_cost,
            "rules_text": self.rules_text,
            "flavor_text": self.flavor_text,
            "starter": self.starter,
            "removable": self.removable,
            "once_per_turn": self.once_per_turn,
        }


@dataclass(frozen=True, slots=True)
class TopicSnapshot:
    topic_id: str
    subject_id: str
    mastery_bp: int
    mastered: bool

    def __post_init__(self) -> None:
        _require_text(self.topic_id, "topic_id", identifier=True)
        _require_text(self.subject_id, "subject_id", identifier=True)
        _require_int(self.mastery_bp, "mastery_bp", minimum=0, maximum=10_000)
        if not isinstance(self.mastered, bool):
            raise ValueError("mastered must be a boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "topic_id": self.topic_id,
            "subject_id": self.subject_id,
            "mastery_bp": self.mastery_bp,
            "mastered": self.mastered,
        }


@dataclass(frozen=True, slots=True)
class LearningSnapshot:
    snapshot_id: str
    learner_id: str
    computed_at: str
    topics: tuple[TopicSnapshot, ...]
    owned_card_ids: tuple[str, ...] = ()
    versions: VersionBundle = VersionBundle()

    def __post_init__(self) -> None:
        _require_text(self.snapshot_id, "snapshot_id", identifier=True)
        _require_text(self.learner_id, "learner_id", identifier=True)
        _require_text(self.computed_at, "computed_at")
        if not isinstance(self.topics, tuple) or not all(isinstance(item, TopicSnapshot) for item in self.topics):
            raise ValueError("topics must be a tuple of TopicSnapshot values")
        topic_ids = [topic.topic_id for topic in self.topics]
        if len(topic_ids) != len(set(topic_ids)):
            raise ValueError("topics must not contain duplicate topic_id values")
        if not isinstance(self.owned_card_ids, tuple):
            raise ValueError("owned_card_ids must be a tuple")
        for card_id in self.owned_card_ids:
            _require_text(card_id, "owned_card_ids item", identifier=True)
        if len(self.owned_card_ids) != len(set(self.owned_card_ids)):
            raise ValueError("owned_card_ids must not contain duplicates")
        if not isinstance(self.versions, VersionBundle):
            raise ValueError("versions must be a VersionBundle")

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "learner_id": self.learner_id,
            "computed_at": self.computed_at,
            "topics": [topic.to_dict() for topic in self.topics],
            "owned_card_ids": list(self.owned_card_ids),
            "versions": self.versions.to_dict(),
        }


class CommandIntent(str, Enum):
    START_RUN = "start_run"
    SELECT_NODE = "select_node"
    START_ENCOUNTER = "start_encounter"
    PLAY_CARD = "play_card"
    END_TURN = "end_turn"
    CHOOSE_RUN_REWARD = "choose_run_reward"
    LEAVE_ENCOUNTER = "leave_encounter"
    FINISH_RUN = "finish_run"


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    command_id: str
    run_id: str
    expected_state_version: int
    intent: CommandIntent
    payload: Mapping[str, object]
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _require_text(self.command_id, "command_id", identifier=True)
        _require_text(self.run_id, "run_id", identifier=True)
        _require_int(self.expected_state_version, "expected_state_version", minimum=0)
        if not isinstance(self.intent, CommandIntent):
            raise ValueError("intent must be a CommandIntent")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")
        _json_safe(self.payload)
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol_version: {self.protocol_version}")

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "command_id": self.command_id,
            "run_id": self.run_id,
            "expected_state_version": self.expected_state_version,
            "intent": self.intent.value,
            "payload": _json_safe(self.payload),
        }


@dataclass(frozen=True, slots=True)
class DungeonResponse:
    accepted: bool
    state_version: int
    view: Mapping[str, object]
    events: tuple[Mapping[str, object], ...]
    state_hash: str
    error_code: str | None = None
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise ValueError("accepted must be a boolean")
        _require_int(self.state_version, "state_version", minimum=0)
        if not isinstance(self.view, Mapping):
            raise ValueError("view must be a mapping")
        if not isinstance(self.events, tuple) or not all(isinstance(event, Mapping) for event in self.events):
            raise ValueError("events must be a tuple of mappings")
        if re.fullmatch(r"[0-9a-f]{64}", self.state_hash) is None:
            raise ValueError("state_hash must be a lowercase SHA-256 hex digest")
        if self.error_code is not None:
            _require_text(self.error_code, "error_code", identifier=True)
        if self.accepted and self.error_code is not None:
            raise ValueError("accepted response cannot contain error_code")
        _json_safe(self.view)
        _json_safe(self.events)
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol_version: {self.protocol_version}")

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "accepted": self.accepted,
            "state_version": self.state_version,
            "view": _json_safe(self.view),
            "events": _json_safe(self.events),
            "state_hash": self.state_hash,
            "error_code": self.error_code,
        }
