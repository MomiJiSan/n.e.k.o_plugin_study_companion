from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from knowledge_dungeon.bridge_contracts import (
    BootstrapRequest,
    BridgeContractError,
    CreateRunRequest,
    GetRunRequest,
    PerformActionPayload,
    PerformActionRequest,
    TrustedInvocationContext,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "knowledge_dungeon" / "schemas"


def _assert_objects_are_closed(value: Any, path: str = "schema") -> None:
    if isinstance(value, dict):
        if value.get("type") == "object":
            assert value.get("additionalProperties") is False, path
        for key, item in value.items():
            _assert_objects_are_closed(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_objects_are_closed(item, f"{path}[{index}]")


def test_all_three_c0_schemas_are_valid_json_and_close_every_object() -> None:
    names = {"bootstrap_v1.json", "bridge_requests_v1.json", "public_run_v1.json"}
    for name in names:
        schema = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        _assert_objects_are_closed(schema, name)


def test_request_contracts_reject_unknown_fields_and_wrong_versions() -> None:
    with pytest.raises(BridgeContractError) as unknown:
        CreateRunRequest.from_mapping(
            {
                "bridge_protocol_version": 1,
                "request_id": "create-run-001",
                "subject_id": "math",
                "scenario_id": "calculus_v0_1",
                "seed": 7,
            }
        )
    assert unknown.value.code == "invalid_request"

    with pytest.raises(BridgeContractError) as version:
        PerformActionRequest.from_mapping(
            {
                "bridge_protocol_version": 2,
                "request_id": "action-001",
                "expected_state_version": 1,
                "action_id": "end_turn",
            }
        )
    assert version.value.code == "unsupported_bridge_protocol"


@pytest.mark.parametrize(
    ("factory", "payload"),
    [
        (
            BootstrapRequest.from_mapping,
            {"bridge_protocol_version": 1, "run_id": "run-unexpected"},
        ),
        (
            GetRunRequest.from_mapping,
            {
                "bridge_protocol_version": 1,
                "run_id": "run-0123456789abcdef01234567",
                "request_id": "unexpected",
            },
        ),
        (
            PerformActionPayload.from_mapping,
            {
                "bridge_protocol_version": 1,
                "run_id": "run-0123456789abcdef01234567",
                "request_id": "action-001",
                "expected_state_version": 1,
                "action_id": "end_turn",
                "intent": "end_turn",
            },
        ),
    ],
)
def test_host_payload_contracts_reject_extra_fields(
    factory: Callable[[object], object], payload: dict[str, object]
) -> None:
    with pytest.raises(BridgeContractError) as exc_info:
        factory(payload)

    assert exc_info.value.code == "invalid_request"


def test_host_payload_contracts_round_trip_exact_shapes() -> None:
    assert BootstrapRequest.from_mapping({"bridge_protocol_version": 1}).to_dict() == {
        "bridge_protocol_version": 1
    }
    assert GetRunRequest.from_mapping(
        {"bridge_protocol_version": 1, "run_id": "run-0123456789abcdef01234567"}
    ).to_dict() == {
        "bridge_protocol_version": 1,
        "run_id": "run-0123456789abcdef01234567",
    }
    assert PerformActionPayload.from_mapping(
        {
            "bridge_protocol_version": 1,
            "run_id": "run-0123456789abcdef01234567",
            "request_id": "action-001",
            "expected_state_version": 7,
            "action_id": "play_card:math.calculus.limit_concept",
        }
    ).to_dict() == {
        "bridge_protocol_version": 1,
        "run_id": "run-0123456789abcdef01234567",
        "request_id": "action-001",
        "expected_state_version": 7,
        "action_id": "play_card:math.calculus.limit_concept",
    }


def test_action_request_contains_no_engine_command_fields() -> None:
    request = PerformActionRequest.from_mapping(
        {
            "bridge_protocol_version": 1,
            "request_id": "action-001",
            "expected_state_version": 7,
            "action_id": "play_card:math.calculus.limit_concept",
        }
    )

    assert request.to_dict() == {
        "bridge_protocol_version": 1,
        "request_id": "action-001",
        "expected_state_version": 7,
        "action_id": "play_card:math.calculus.limit_concept",
    }
    assert "intent" not in request.to_dict()
    assert "payload" not in request.to_dict()
    assert "command_id" not in request.to_dict()


def test_trusted_context_is_not_a_json_contract() -> None:
    context = TrustedInvocationContext(
        client_id="electron-installation-001",
        scope="study_companion:dungeon",
    )
    assert not hasattr(context, "to_dict")
