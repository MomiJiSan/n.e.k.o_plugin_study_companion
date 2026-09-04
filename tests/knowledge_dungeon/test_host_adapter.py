from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from knowledge_dungeon.application_service import KnowledgeDungeonApplicationService
from knowledge_dungeon.bridge_contracts import TrustedInvocationContext
from knowledge_dungeon.host_adapter import KnowledgeDungeonHostAdapter
from knowledge_dungeon.persistence import DungeonRunStore

CONTEXT = TrustedInvocationContext("electron-client-test", "study_companion:dungeon")


def _create_payload(request_id: str = "create-run-001") -> dict[str, object]:
    return {
        "bridge_protocol_version": 1,
        "request_id": request_id,
        "subject_id": "math",
        "scenario_id": "calculus_v0_1",
    }


def _perform_payload(
    run_id: str,
    *,
    request_id: str = "select-battle-001",
    state_version: int = 1,
    action_id: str = "select_node:battle_1",
) -> dict[str, object]:
    return {
        "bridge_protocol_version": 1,
        "run_id": run_id,
        "request_id": request_id,
        "expected_state_version": state_version,
        "action_id": action_id,
    }


@pytest.mark.asyncio
async def test_adapter_exposes_only_four_strict_operations(tmp_path: Path) -> None:
    adapter = KnowledgeDungeonHostAdapter(tmp_path / "dungeon.sqlite3")

    bootstrap = await adapter.invoke(CONTEXT, "bootstrap", {"bridge_protocol_version": 1})
    assert bootstrap.to_dict()["category"] == "success"
    assert bootstrap.value is not None
    assert bootstrap.value["bridge_protocol_version"] == 1

    unsupported = await adapter.invoke(CONTEXT, "delete_run", {"bridge_protocol_version": 1})
    assert unsupported.to_dict() == {
        "ok": False,
        "category": "protocol",
        "value": None,
        "error": {
            "code": "invalid_operation",
            "message": "operation is not supported",
            "retryable": False,
        },
    }

    for operation, payload in (
        ("bootstrap", {"bridge_protocol_version": 1, "run_id": "run-unexpected"}),
        ("get_run", {"bridge_protocol_version": 1, "run_id": "not-a-server-run-id"}),
        (
            "get_run",
            {
                "bridge_protocol_version": 1,
                "run_id": "run-0123456789abcdef01234567",
                "request_id": "unexpected",
            },
        ),
        (
            "perform_action",
            {
                **_perform_payload("run-0123456789abcdef01234567"),
                "intent": "select_node",
            },
        ),
    ):
        outcome = await adapter.invoke(CONTEXT, operation, payload)
        assert outcome.ok is False
        assert outcome.category.value == "protocol"
        assert outcome.error is not None
        assert outcome.error.code == "invalid_request"

    unavailable_subject = await adapter.invoke(
        CONTEXT,
        "create_run",
        {**_create_payload(), "subject_id": "english"},
    )
    assert unavailable_subject.category.value == "domain"
    assert unavailable_subject.error is not None
    assert unavailable_subject.error.code == "subject_unavailable"


@pytest.mark.asyncio
async def test_adapter_requires_internal_trusted_context(tmp_path: Path) -> None:
    adapter = KnowledgeDungeonHostAdapter(tmp_path / "dungeon.sqlite3")

    outcome = await adapter.invoke(  # type: ignore[arg-type]
        {"client_id": "renderer", "scope": "study_companion:dungeon"},
        "bootstrap",
        {"bridge_protocol_version": 1},
    )

    assert outcome.ok is False
    assert outcome.category.value == "protocol"
    assert outcome.error is not None
    assert outcome.error.code == "untrusted_invocation"


@pytest.mark.asyncio
async def test_adapter_recovers_real_persisted_run_across_instances(tmp_path: Path) -> None:
    store_path = tmp_path / "dungeon.sqlite3"
    first_adapter = KnowledgeDungeonHostAdapter(store_path)
    created = await first_adapter.invoke(CONTEXT, "create_run", _create_payload())
    assert created.ok is True
    assert created.value is not None
    run_id = str(created.value["run"]["run_id"])  # type: ignore[index]

    second_adapter = KnowledgeDungeonHostAdapter(store_path)
    recovered = await second_adapter.invoke(
        CONTEXT,
        "get_run",
        {"bridge_protocol_version": 1, "run_id": run_id},
    )
    assert recovered.ok is True
    assert recovered.value is not None
    assert recovered.value["run"] == created.value["run"]
    assert recovered.value["state_hash"] == created.value["state_hash"]

    selected = await second_adapter.invoke(
        CONTEXT,
        "perform_action",
        _perform_payload(run_id),
    )
    assert selected.ok is True
    assert selected.value is not None
    assert selected.value["state_version"] == 2

    third_adapter = KnowledgeDungeonHostAdapter(store_path)
    recovered_after_action = await third_adapter.invoke(
        CONTEXT,
        "get_run",
        {"bridge_protocol_version": 1, "run_id": run_id},
    )
    assert recovered_after_action.ok is True
    assert recovered_after_action.value is not None
    assert recovered_after_action.value["state_version"] == 2
    assert recovered_after_action.value["state_hash"] == selected.value["state_hash"]


@pytest.mark.asyncio
async def test_same_action_request_is_concurrently_idempotent(tmp_path: Path) -> None:
    store_path = tmp_path / "dungeon.sqlite3"
    adapter = KnowledgeDungeonHostAdapter(store_path)
    created = await adapter.invoke(CONTEXT, "create_run", _create_payload())
    assert created.value is not None
    run_id = str(created.value["run"]["run_id"])  # type: ignore[index]
    payload = _perform_payload(run_id)

    first, second = await asyncio.gather(
        KnowledgeDungeonHostAdapter(store_path).invoke(CONTEXT, "perform_action", payload),
        KnowledgeDungeonHostAdapter(store_path).invoke(CONTEXT, "perform_action", payload),
    )

    assert first.ok is True
    assert second.ok is True
    assert first.to_dict() == second.to_dict()

    recovered = await KnowledgeDungeonHostAdapter(store_path).invoke(
        CONTEXT,
        "get_run",
        {"bridge_protocol_version": 1, "run_id": run_id},
    )
    assert recovered.value is not None
    assert recovered.value["state_version"] == 2


@pytest.mark.asyncio
async def test_competing_actions_return_success_and_retryable_stale_state(tmp_path: Path) -> None:
    store_path = tmp_path / "dungeon.sqlite3"
    adapter = KnowledgeDungeonHostAdapter(store_path)
    created = await adapter.invoke(CONTEXT, "create_run", _create_payload())
    assert created.value is not None
    run_id = str(created.value["run"]["run_id"])  # type: ignore[index]

    outcomes = await asyncio.gather(
        KnowledgeDungeonHostAdapter(store_path).invoke(
            CONTEXT,
            "perform_action",
            _perform_payload(run_id, request_id="choose-battle", action_id="select_node:battle_1"),
        ),
        KnowledgeDungeonHostAdapter(store_path).invoke(
            CONTEXT,
            "perform_action",
            _perform_payload(run_id, request_id="choose-trap", action_id="select_node:trap_1"),
        ),
    )

    assert sorted(outcome.category.value for outcome in outcomes) == ["retryable", "success"]
    retryable = next(outcome for outcome in outcomes if not outcome.ok)
    assert retryable.error is not None
    assert retryable.error.code == "stale_state_version"
    assert retryable.error.retryable is True


@pytest.mark.asyncio
async def test_cancellation_propagates_and_worker_eventually_closes_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    original_bootstrap = KnowledgeDungeonApplicationService.bootstrap
    original_close = DungeonRunStore.close

    def slow_bootstrap(
        service: KnowledgeDungeonApplicationService,
        context: TrustedInvocationContext,
    ) -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=5)
        return original_bootstrap(service, context)

    def tracking_close(store: DungeonRunStore) -> None:
        try:
            original_close(store)
        finally:
            closed.set()

    monkeypatch.setattr(KnowledgeDungeonApplicationService, "bootstrap", slow_bootstrap)
    monkeypatch.setattr(DungeonRunStore, "close", tracking_close)
    adapter = KnowledgeDungeonHostAdapter(tmp_path / "dungeon.sqlite3")
    task = asyncio.create_task(
        adapter.invoke(CONTEXT, "bootstrap", {"bridge_protocol_version": 1})
    )
    assert await asyncio.to_thread(started.wait, 2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release.set()
    assert await asyncio.to_thread(closed.wait, 2)


@pytest.mark.asyncio
async def test_failures_are_categorized_without_leaking_storage_path(tmp_path: Path) -> None:
    missing = await KnowledgeDungeonHostAdapter(tmp_path / "dungeon.sqlite3").invoke(
        CONTEXT,
        "get_run",
        {
            "bridge_protocol_version": 1,
            "run_id": "run-0123456789abcdef01234567",
        },
    )
    assert missing.ok is False
    assert missing.category.value == "domain"
    assert missing.error is not None
    assert missing.error.code == "run_not_found"
    assert missing.error.retryable is False

    unavailable = await KnowledgeDungeonHostAdapter(tmp_path).invoke(
        CONTEXT,
        "bootstrap",
        {"bridge_protocol_version": 1},
    )
    serialized = unavailable.to_dict()
    assert serialized == {
        "ok": False,
        "category": "retryable",
        "value": None,
        "error": {
            "code": "adapter_unavailable",
            "message": "The knowledge dungeon service is temporarily unavailable.",
            "retryable": True,
        },
    }
    assert str(tmp_path) not in str(serialized)
