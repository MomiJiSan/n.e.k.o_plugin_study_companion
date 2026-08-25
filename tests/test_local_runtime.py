from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_study_companion_local_runtime_test"
PACKAGE = ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
sys.modules[PACKAGE_NAME] = PACKAGE

LocalRuntimeClient = importlib.import_module(
    f"{PACKAGE_NAME}.local_runtime_client"
).LocalRuntimeClient
protocol = importlib.import_module(f"{PACKAGE_NAME}.local_runtime_protocol")
supervisor_module = importlib.import_module(f"{PACKAGE_NAME}.local_runtime_supervisor")

LocalRuntimeSupervisor = supervisor_module.LocalRuntimeSupervisor

from _study_companion_local_runtime_test.local_runtime_protocol import (  # noqa: E402
    LOCAL_MODELS_NOT_INSTALLED,
    LOCAL_RUNTIME_AUTH_FAILED,
    LOCAL_RUNTIME_PROTOCOL_MISMATCH,
    LOCAL_RUNTIME_START_FAILED,
    LOCAL_RUNTIME_UNAVAILABLE,
    LocalRuntimeError,
    LocalRuntimeState,
    LocalRuntimeStatus,
    PROTOCOL_VERSION,
)


def test_status_payload_rejects_unknown_protocol_without_sensitive_data() -> None:
    with pytest.raises(LocalRuntimeError) as raised:
        LocalRuntimeStatus.from_payload(
            {
                "protocol_version": PROTOCOL_VERSION + 1,
                "runtime_version": "0.1.0",
                "state": "ready",
                "models": [],
                "capabilities": [],
                "active_job": None,
                "token": "must-not-surface",
            }
        )

    assert raised.value.code == LOCAL_RUNTIME_PROTOCOL_MISMATCH
    assert "must-not-surface" not in str(raised.value)


@pytest.mark.asyncio
async def test_supervisor_is_lazy_and_concurrent_start_uses_one_runtime() -> None:
    supervisor = LocalRuntimeSupervisor()
    try:
        initial = await supervisor.get_status()
        assert initial.state is LocalRuntimeState.STOPPED

        first, second = await asyncio.gather(
            supervisor.ensure_started(), supervisor.ensure_started()
        )
        assert first == second
        assert first.base_url.startswith("http://127.0.0.1:")
        assert supervisor.state is LocalRuntimeState.READY
    finally:
        await supervisor.shutdown()


@pytest.mark.asyncio
async def test_stub_requires_parent_token_and_returns_no_models_error() -> None:
    supervisor = LocalRuntimeSupervisor()
    client = LocalRuntimeClient(supervisor)
    try:
        connection = await supervisor.ensure_started()
        async with httpx.AsyncClient(trust_env=False) as raw_client:
            unauthorized = await raw_client.get(f"{connection.base_url}/health")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == LOCAL_RUNTIME_AUTH_FAILED

        with pytest.raises(LocalRuntimeError) as raised:
            await client.call(
                [{"role": "user", "content": "secret question content"}],
                operation="explain",
                deadline=None,
            )
        assert raised.value.code == LOCAL_MODELS_NOT_INSTALLED
        assert raised.value.diagnostic == LOCAL_MODELS_NOT_INSTALLED
        assert "secret question content" not in str(raised.value)

        status = await client.status()
        assert status.state is LocalRuntimeState.READY
        assert status.models == ()
        unloaded = await client.unload()
        assert unloaded.state is LocalRuntimeState.READY
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_shutdown_stops_runtime_and_status_returns_stopped() -> None:
    supervisor = LocalRuntimeSupervisor()
    await supervisor.ensure_started()
    await supervisor.shutdown()
    status = await supervisor.get_status()
    assert status.state is LocalRuntimeState.STOPPED


@pytest.mark.asyncio
async def test_failed_post_ready_health_check_cleans_up_runtime() -> None:
    class _HealthFailingSupervisor(LocalRuntimeSupervisor):
        async def _verify_health(self, connection) -> None:
            raise LocalRuntimeError(LOCAL_RUNTIME_UNAVAILABLE, "health check failed")

    supervisor = _HealthFailingSupervisor()
    try:
        with pytest.raises(LocalRuntimeError) as raised:
            await supervisor.ensure_started()
        assert raised.value.code == LOCAL_RUNTIME_START_FAILED
        assert supervisor.state is LocalRuntimeState.UNAVAILABLE
        assert supervisor._connection is None
        assert supervisor._process is None
    finally:
        await supervisor.shutdown()


def test_client_refuses_non_loopback_endpoint() -> None:
    with pytest.raises(LocalRuntimeError) as raised:
        LocalRuntimeClient._normalize_loopback_base_url("http://example.com:8080")

    assert raised.value.code == LOCAL_RUNTIME_UNAVAILABLE
