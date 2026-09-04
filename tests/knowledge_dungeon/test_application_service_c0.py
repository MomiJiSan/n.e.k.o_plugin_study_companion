from __future__ import annotations

from typing import Any

import pytest
from knowledge_dungeon.application_service import (
    ApplicationServiceError,
    KnowledgeDungeonApplicationService,
)
from knowledge_dungeon.bridge_contracts import BridgeContractError, TrustedInvocationContext
from knowledge_dungeon.engine import KnowledgeDungeonEngine
from knowledge_dungeon.persistence import DungeonRunStore

CONTEXT = TrustedInvocationContext(
    client_id="electron-client-test",
    scope="study_companion:dungeon",
)
CREATE_REQUEST = {
    "bridge_protocol_version": 1,
    "request_id": "create-run-001",
    "subject_id": "math",
    "scenario_id": "calculus_v0_1",
}


def _sensitive_paths(value: Any, path: str = "root") -> list[str]:
    sensitive = {"seed", "command_id", "command_log", "rng_state", "rng_increment", "learner_id", "snapshot_id"}
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in sensitive:
                found.append(f"{path}.{key}")
            found.extend(_sensitive_paths(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_sensitive_paths(item, f"{path}[{index}]"))
    return found


def test_public_service_surface_contains_only_four_operations() -> None:
    public_methods = {
        name
        for name, value in KnowledgeDungeonApplicationService.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert public_methods == {"bootstrap", "create_run", "get_run", "perform_action"}


def test_create_run_generates_authority_identifiers_and_hides_internal_state() -> None:
    engine = KnowledgeDungeonEngine()
    service = KnowledgeDungeonApplicationService(engine)
    response = service.create_run(CONTEXT, CREATE_REQUEST)
    run_id = response["run"]["run_id"]
    internal = engine.get_state(run_id)

    assert internal is not None
    assert internal.seed > 0
    assert internal.command_log[0]["command_id"].startswith("command-")
    assert internal.command_log[0]["payload"]["seed"] == internal.seed
    assert _sensitive_paths(response) == []
    assert response["events"][0] == {"type": "run_started", "run_id": run_id}
    assert response["run"]["cards"][0]["card_id"] == "neutral.momiji_mercy"


def test_stable_client_and_request_generate_deterministic_identity() -> None:
    first = KnowledgeDungeonApplicationService().create_run(CONTEXT, CREATE_REQUEST)
    second = KnowledgeDungeonApplicationService().create_run(CONTEXT, CREATE_REQUEST)

    assert first["run"]["run_id"] == second["run"]["run_id"]
    assert first["state_hash"] == second["state_hash"]
    assert first["available_actions"] == second["available_actions"]


def test_client_submits_only_action_id_and_service_builds_engine_command() -> None:
    engine = KnowledgeDungeonEngine()
    service = KnowledgeDungeonApplicationService(engine)
    created = service.create_run(CONTEXT, CREATE_REQUEST)
    run_id = created["run"]["run_id"]
    select_battle = next(
        action for action in created["available_actions"] if action["target_id"] == "battle_1"
    )

    response = service.perform_action(
        CONTEXT,
        run_id,
        {
            "bridge_protocol_version": 1,
            "request_id": "select-battle-001",
            "expected_state_version": 1,
            "action_id": select_battle["action_id"],
        },
    )
    state = engine.get_state(run_id)

    assert state is not None
    assert response["state_version"] == 2
    assert response["run"]["selected_node_id"] == "battle_1"
    assert state.command_log[-1]["intent"] == "select_node"
    assert state.command_log[-1]["payload"] == {"node_id": "battle_1"}
    assert state.command_log[-1]["command_id"].startswith("command-")
    assert set(select_battle) == {"action_id", "action_type", "label", "target_id"}


def test_duplicate_action_is_idempotent_and_stale_sibling_is_rejected() -> None:
    service = KnowledgeDungeonApplicationService()
    created = service.create_run(CONTEXT, CREATE_REQUEST)
    run_id = created["run"]["run_id"]
    battle, trap = created["available_actions"]
    request = {
        "bridge_protocol_version": 1,
        "request_id": "select-node-001",
        "expected_state_version": 1,
        "action_id": battle["action_id"],
    }

    first = service.perform_action(CONTEXT, run_id, request)
    duplicate = service.perform_action(CONTEXT, run_id, request)

    assert duplicate == first
    with pytest.raises(ApplicationServiceError) as error:
        service.perform_action(
            CONTEXT,
            run_id,
            {
                "bridge_protocol_version": 1,
                "request_id": "select-node-002",
                "expected_state_version": 1,
                "action_id": trap["action_id"],
            },
        )
    assert error.value.code == "stale_state_version"
    assert service.get_run(CONTEXT, run_id)["state_version"] == 2


def test_create_run_does_not_accept_client_generated_authority_fields() -> None:
    service = KnowledgeDungeonApplicationService()
    for forbidden in ("run_id", "command_id", "seed"):
        with pytest.raises(ValueError, match="unexpected"):
            service.create_run(CONTEXT, {**CREATE_REQUEST, forbidden: "client-controlled"})


def test_perform_action_does_not_accept_intent_or_payload() -> None:
    service = KnowledgeDungeonApplicationService()
    created = service.create_run(CONTEXT, CREATE_REQUEST)
    run_id = created["run"]["run_id"]
    with pytest.raises(ValueError, match="unexpected"):
        service.perform_action(
            CONTEXT,
            run_id,
            {
                "bridge_protocol_version": 1,
                "request_id": "bad-action-001",
                "expected_state_version": 1,
                "action_id": created["available_actions"][0]["action_id"],
                "intent": "finish_run",
                "payload": {},
            },
        )


def test_selected_node_keeps_reselection_actions_and_adds_enter_action() -> None:
    service = KnowledgeDungeonApplicationService()
    created = service.create_run(CONTEXT, CREATE_REQUEST)
    run_id = created["run"]["run_id"]
    selected = service.perform_action(
        CONTEXT,
        run_id,
        {
            "bridge_protocol_version": 1,
            "request_id": "select-battle-reselect-test",
            "expected_state_version": 1,
            "action_id": "select_node:battle_1",
        },
    )

    assert [action["action_id"] for action in selected["available_actions"]] == [
        "select_node:battle_1",
        "select_node:trap_1",
        "enter_selected_node",
    ]


def test_action_retry_after_response_loss_survives_service_restart(tmp_path) -> None:
    database_path = tmp_path / "dungeon.sqlite3"
    with DungeonRunStore(database_path) as first_store:
        first_service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(first_store))
        created = first_service.create_run(CONTEXT, CREATE_REQUEST)
        run_id = created["run"]["run_id"]
        action_request = {
            "bridge_protocol_version": 1,
            "request_id": "restart-action-001",
            "expected_state_version": 1,
            "action_id": "select_node:battle_1",
        }
        acted = first_service.perform_action(CONTEXT, run_id, action_request)

    with DungeonRunStore(database_path) as second_store:
        restarted = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(second_store))
        retried_action = restarted.perform_action(CONTEXT, run_id, action_request)

    assert retried_action == acted


def test_create_retry_survives_service_restart(tmp_path) -> None:
    database_path = tmp_path / "dungeon.sqlite3"
    with DungeonRunStore(database_path) as first_store:
        created = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(first_store)).create_run(
            CONTEXT, CREATE_REQUEST
        )
    with DungeonRunStore(database_path) as second_store:
        retried = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(second_store)).create_run(
            CONTEXT, CREATE_REQUEST
        )
    assert retried == created


def test_same_action_request_id_with_different_body_reaches_engine_conflict() -> None:
    service = KnowledgeDungeonApplicationService()
    created = service.create_run(CONTEXT, CREATE_REQUEST)
    run_id = created["run"]["run_id"]
    shared = {
        "bridge_protocol_version": 1,
        "request_id": "conflicting-request-001",
        "expected_state_version": 1,
    }

    service.perform_action(CONTEXT, run_id, {**shared, "action_id": "select_node:battle_1"})
    with pytest.raises(ApplicationServiceError) as error:
        service.perform_action(CONTEXT, run_id, {**shared, "action_id": "select_node:trap_1"})
    assert error.value.code == "command_id_conflict"


def test_every_operation_requires_exact_trusted_scope() -> None:
    service = KnowledgeDungeonApplicationService()
    with pytest.raises(BridgeContractError) as missing:
        service.bootstrap(object())  # type: ignore[arg-type]
    assert missing.value.code == "untrusted_invocation"

    with pytest.raises(BridgeContractError) as wrong_scope:
        TrustedInvocationContext(client_id="electron-client-test", scope="study_companion:other")
    assert wrong_scope.value.code == "forbidden_scope"
