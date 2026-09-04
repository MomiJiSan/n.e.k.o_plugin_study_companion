"""Strict bridge-facing contracts for the Knowledge Dungeon C0 service.

The bridge accepts scenario selection at run creation and frozen action IDs
during play. It never accepts engine intents, command IDs, seeds, or payloads
from a presentation client.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

APPLICATION_SERVICE_VERSION = "knowledge-dungeon-v0.2-c0"
PUBLIC_PROJECTION_VERSION = 1
BRIDGE_PROTOCOL_VERSION = 1
CALCULUS_SCENARIO_ID = "calculus_v0_1"
CALCULUS_SUBJECT_ID = "math"
REQUIRED_DUNGEON_SCOPE = "study_companion:dungeon"

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_RUN_ID_PATTERN = re.compile(r"^run-[0-9a-f]{24}$")
_ACTION_ID_PATTERN = re.compile(
    r"^(?:select_node:[a-z0-9][a-z0-9._-]*|enter_selected_node|"
    r"play_card:[a-z0-9][a-z0-9._-]*|end_turn|"
    r"choose_reward:[a-z0-9][a-z0-9._-]*|abandon_run|finish_run)$"
)


class BridgeContractError(ValueError):
    """A stable validation failure safe for a future local bridge adapter."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def require_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise BridgeContractError("invalid_request", f"{field_name} must be a canonical identifier")
    return value


def _require_bridge_protocol_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value != BRIDGE_PROTOCOL_VERSION:
        raise BridgeContractError(
            "unsupported_bridge_protocol",
            f"bridge_protocol_version must equal {BRIDGE_PROTOCOL_VERSION}",
        )
    return value


def _require_run_id(value: object) -> str:
    if not isinstance(value, str) or _RUN_ID_PATTERN.fullmatch(value) is None:
        raise BridgeContractError("invalid_request", "run_id must be a canonical dungeon run identifier")
    return value


def _require_exact_keys(value: object, expected: frozenset[str], contract_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise BridgeContractError("invalid_request", f"{contract_name} must be an object")
    received = frozenset(value)
    if received != expected:
        unexpected = sorted(received - expected)
        missing = sorted(expected - received)
        details: list[str] = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if unexpected:
            details.append(f"unexpected={','.join(unexpected)}")
        raise BridgeContractError("invalid_request", f"invalid {contract_name} fields ({'; '.join(details)})")
    return value


@dataclass(frozen=True, slots=True)
class TrustedInvocationContext:
    """Host-authenticated context; never decoded from renderer JSON."""

    client_id: str
    scope: str

    def __post_init__(self) -> None:
        require_identifier(self.client_id, "client_id")
        if self.scope != REQUIRED_DUNGEON_SCOPE:
            raise BridgeContractError("forbidden_scope", "knowledge dungeon scope is required")


def require_trusted_context(value: object) -> TrustedInvocationContext:
    if not isinstance(value, TrustedInvocationContext):
        raise BridgeContractError("untrusted_invocation", "trusted invocation context is required")
    if value.scope != REQUIRED_DUNGEON_SCOPE:
        raise BridgeContractError("forbidden_scope", "knowledge dungeon scope is required")
    return value


@dataclass(frozen=True, slots=True)
class BootstrapRequest:
    bridge_protocol_version: int = BRIDGE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _require_bridge_protocol_version(self.bridge_protocol_version)

    @classmethod
    def from_mapping(cls, value: object) -> "BootstrapRequest":
        raw = _require_exact_keys(
            value,
            frozenset(("bridge_protocol_version",)),
            "bootstrap payload",
        )
        return cls(
            bridge_protocol_version=_require_bridge_protocol_version(raw["bridge_protocol_version"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {"bridge_protocol_version": self.bridge_protocol_version}


@dataclass(frozen=True, slots=True)
class GetRunRequest:
    run_id: str
    bridge_protocol_version: int = BRIDGE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _require_bridge_protocol_version(self.bridge_protocol_version)
        _require_run_id(self.run_id)

    @classmethod
    def from_mapping(cls, value: object) -> "GetRunRequest":
        raw = _require_exact_keys(
            value,
            frozenset(("bridge_protocol_version", "run_id")),
            "get_run payload",
        )
        return cls(
            bridge_protocol_version=_require_bridge_protocol_version(raw["bridge_protocol_version"]),
            run_id=_require_run_id(raw["run_id"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {"bridge_protocol_version": self.bridge_protocol_version, "run_id": self.run_id}


@dataclass(frozen=True, slots=True)
class CreateRunRequest:
    request_id: str
    subject_id: str
    scenario_id: str
    bridge_protocol_version: int = BRIDGE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _require_bridge_protocol_version(self.bridge_protocol_version)
        require_identifier(self.request_id, "request_id")
        require_identifier(self.subject_id, "subject_id")
        require_identifier(self.scenario_id, "scenario_id")
        if self.subject_id != CALCULUS_SUBJECT_ID:
            raise BridgeContractError("subject_unavailable", f"unsupported subject: {self.subject_id}")
        if self.scenario_id != CALCULUS_SCENARIO_ID:
            raise BridgeContractError("scenario_unavailable", f"unsupported scenario: {self.scenario_id}")

    @classmethod
    def from_mapping(cls, value: object) -> "CreateRunRequest":
        raw = _require_exact_keys(
            value,
            frozenset(("bridge_protocol_version", "request_id", "subject_id", "scenario_id")),
            "create_run request",
        )
        return cls(
            bridge_protocol_version=_require_bridge_protocol_version(raw["bridge_protocol_version"]),
            request_id=require_identifier(raw["request_id"], "request_id"),
            subject_id=require_identifier(raw["subject_id"], "subject_id"),
            scenario_id=require_identifier(raw["scenario_id"], "scenario_id"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "bridge_protocol_version": self.bridge_protocol_version,
            "request_id": self.request_id,
            "subject_id": self.subject_id,
            "scenario_id": self.scenario_id,
        }


@dataclass(frozen=True, slots=True)
class PerformActionRequest:
    request_id: str
    expected_state_version: int
    action_id: str
    bridge_protocol_version: int = BRIDGE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _require_bridge_protocol_version(self.bridge_protocol_version)
        require_identifier(self.request_id, "request_id")
        if (
            isinstance(self.expected_state_version, bool)
            or not isinstance(self.expected_state_version, int)
            or self.expected_state_version < 1
        ):
            raise BridgeContractError("invalid_request", "expected_state_version must be a positive integer")
        if not isinstance(self.action_id, str) or _ACTION_ID_PATTERN.fullmatch(self.action_id) is None:
            raise BridgeContractError("invalid_request", "action_id is invalid")

    @classmethod
    def from_mapping(cls, value: object) -> "PerformActionRequest":
        raw = _require_exact_keys(
            value,
            frozenset(("bridge_protocol_version", "request_id", "expected_state_version", "action_id")),
            "perform_action request",
        )
        return cls(
            bridge_protocol_version=_require_bridge_protocol_version(raw["bridge_protocol_version"]),
            request_id=require_identifier(raw["request_id"], "request_id"),
            expected_state_version=raw["expected_state_version"],
            action_id=raw["action_id"] if isinstance(raw["action_id"], str) else "",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "bridge_protocol_version": self.bridge_protocol_version,
            "request_id": self.request_id,
            "expected_state_version": self.expected_state_version,
            "action_id": self.action_id,
        }


@dataclass(frozen=True, slots=True)
class PerformActionPayload:
    run_id: str
    request: PerformActionRequest

    def __post_init__(self) -> None:
        _require_run_id(self.run_id)
        if not isinstance(self.request, PerformActionRequest):
            raise BridgeContractError("invalid_request", "request must be a PerformActionRequest")

    @classmethod
    def from_mapping(cls, value: object) -> "PerformActionPayload":
        raw = _require_exact_keys(
            value,
            frozenset(
                (
                    "bridge_protocol_version",
                    "run_id",
                    "request_id",
                    "expected_state_version",
                    "action_id",
                )
            ),
            "perform_action payload",
        )
        run_id = _require_run_id(raw["run_id"])
        request = PerformActionRequest.from_mapping(
            {key: raw[key] for key in raw if key != "run_id"}
        )
        return cls(run_id=run_id, request=request)

    def to_dict(self) -> dict[str, object]:
        return {"run_id": self.run_id, **self.request.to_dict()}


@dataclass(frozen=True, slots=True)
class PublicAction:
    action_id: str
    action_type: str
    label: str
    target_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or _ACTION_ID_PATTERN.fullmatch(self.action_id) is None:
            raise ValueError("action_id does not match the public action format")
        require_identifier(self.action_type, "action_type")
        if not isinstance(self.label, str) or not self.label.strip() or self.label != self.label.strip():
            raise ValueError("label must be non-empty without surrounding whitespace")
        if self.target_id is not None:
            require_identifier(self.target_id, "target_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "label": self.label,
            "target_id": self.target_id,
        }


@dataclass(frozen=True, slots=True)
class ScenarioDescriptor:
    scenario_id: str
    title: str
    map_subject_id: str
    content_status: str = "simulated"

    def __post_init__(self) -> None:
        require_identifier(self.scenario_id, "scenario_id")
        require_identifier(self.map_subject_id, "map_subject_id")
        require_identifier(self.content_status, "content_status")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("title must be non-empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "map_subject_id": self.map_subject_id,
            "content_status": self.content_status,
        }


CALCULUS_SCENARIO = ScenarioDescriptor(
    scenario_id=CALCULUS_SCENARIO_ID,
    title="极限森林",
    map_subject_id=CALCULUS_SUBJECT_ID,
)
