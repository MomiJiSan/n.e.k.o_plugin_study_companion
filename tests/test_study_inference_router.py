from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def _package(monkeypatch: pytest.MonkeyPatch, name: str) -> str:
    root = Path(__file__).resolve().parents[1]
    package = ModuleType(name)
    package.__path__ = [str(root)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    return name


def _install_i18n_host_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide the one host import needed when the models module is loaded."""

    plugin = ModuleType("plugin")
    plugin.__path__ = []  # type: ignore[attr-defined]
    sdk = ModuleType("plugin.sdk")
    sdk.__path__ = []  # type: ignore[attr-defined]
    shared = ModuleType("plugin.sdk.shared")
    shared.__path__ = []  # type: ignore[attr-defined]
    i18n = ModuleType("plugin.sdk.shared.i18n")
    i18n.load_plugin_i18n_from_dir = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    sdk_plugin = ModuleType("plugin.sdk.plugin")

    class SdkError(Exception):
        pass

    sdk_plugin.SdkError = SdkError
    utils = ModuleType("utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    tokenize = ModuleType("utils.tokenize")
    tokenize.count_tokens = lambda text: len(str(text))  # type: ignore[attr-defined]
    tokenize.truncate_to_tokens = lambda text, size: str(text)[:size]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "plugin", plugin)
    monkeypatch.setitem(sys.modules, "plugin.sdk", sdk)
    monkeypatch.setitem(sys.modules, "plugin.sdk.shared", shared)
    monkeypatch.setitem(sys.modules, "plugin.sdk.shared.i18n", i18n)
    monkeypatch.setitem(sys.modules, "plugin.sdk.plugin", sdk_plugin)
    monkeypatch.setitem(sys.modules, "utils", utils)
    monkeypatch.setitem(sys.modules, "utils.tokenize", tokenize)


class _Gateway:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []

    async def call(
        self, messages: list[dict[str, Any]], **kwargs: Any
    ) -> object:
        self.calls.append((messages, kwargs))
        return SimpleNamespace(text="api")


class _LocalClient:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, Any]], str, float]] = []
        self.status_calls = 0
        self.shutdown_calls = 0
        self.call_error: Exception | None = None
        self.status_payload: dict[str, object] = {
            "state": "stopped",
            "models": [],
            "capabilities": [],
            "active_job": None,
        }

    async def call(
        self,
        messages: list[dict[str, Any]],
        *,
        operation: str,
        deadline: float,
    ) -> object:
        self.calls.append((messages, operation, deadline))
        if self.call_error is not None:
            raise self.call_error
        return SimpleNamespace(text="local")

    async def status(self) -> object:
        self.status_calls += 1
        return SimpleNamespace(to_payload=lambda: dict(self.status_payload))

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def _router(
    monkeypatch: pytest.MonkeyPatch,
    *,
    local_models_enabled: bool,
) -> tuple[Any, _Gateway, _LocalClient, SimpleNamespace]:
    _install_i18n_host_stub(monkeypatch)
    package = _package(monkeypatch, "_study_inference_router_test")
    module = importlib.import_module(f"{package}.study_inference_router")
    gateway = _Gateway()
    local = _LocalClient()
    config = SimpleNamespace(local_models_enabled=local_models_enabled)
    return (
        module.StudyInferenceRouter(
            logger=SimpleNamespace(),
            config=config,
            api_gateway=gateway,
            local_client=local,
        ),
        gateway,
        local,
        config,
    )


@pytest.mark.asyncio
async def test_api_mode_keeps_gateway_path_and_never_starts_local_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, gateway, local, _config = _router(
        monkeypatch, local_models_enabled=False
    )
    messages = [{"role": "user", "content": "hello"}]
    runtime = SimpleNamespace()
    quota = SimpleNamespace()

    result = await router.call(
        messages,
        operation="concept_explain",
        deadline=123.0,
        runtime=runtime,
        quota_reservation=quota,
    )

    assert result.text == "api"
    assert len(gateway.calls) == 1
    assert gateway.calls[0][1] == {
        "operation": "concept_explain",
        "deadline": 123.0,
        "runtime": runtime,
        "quota_reservation": quota,
    }
    assert local.calls == []
    assert local.status_calls == 0


@pytest.mark.asyncio
async def test_paused_local_mode_uses_gateway_even_for_a_legacy_enabled_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, gateway, local, _config = _router(
        monkeypatch, local_models_enabled=True
    )
    result = await router.call(
        [{"role": "user", "content": "hello"}],
        operation="concept_explain",
        deadline=123.0,
    )

    assert result.text == "api"
    assert len(gateway.calls) == 1
    assert local.calls == []


@pytest.mark.asyncio
async def test_disabled_local_status_does_not_touch_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, _gateway, local, _config = _router(
        monkeypatch, local_models_enabled=False
    )

    status = await router.describe_local_runtime()

    assert status == {
        "available": False,
        "state": "unavailable",
        "models": [],
        "capabilities": [],
        "active_job": None,
        "error_code": "local_model_store_unavailable",
    }
    assert local.status_calls == 0


@pytest.mark.asyncio
async def test_paused_local_status_does_not_touch_a_legacy_enabled_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, _gateway, local, _config = _router(
        monkeypatch, local_models_enabled=True
    )
    status = await router.describe_local_runtime()

    assert status == {
        "available": False,
        "state": "unavailable",
        "models": [],
        "capabilities": [],
        "active_job": None,
        "error_code": "local_model_store_unavailable",
    }
    assert local.status_calls == 0


@pytest.mark.asyncio
async def test_config_update_cannot_reenable_the_paused_local_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, gateway, local, config = _router(
        monkeypatch, local_models_enabled=False
    )

    router.update_config(SimpleNamespace(local_models_enabled=True))
    await router.call([], operation="concept_explain", deadline=123.0)

    assert config.local_models_enabled is False
    assert len(gateway.calls) == 1
    assert local.calls == []


@pytest.mark.asyncio
async def test_shutdown_delegates_to_local_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, _gateway, local, _config = _router(
        monkeypatch, local_models_enabled=False
    )

    await router.shutdown()

    assert local.shutdown_calls == 1


class _AgentRouter:
    def __init__(self, *, local_models_enabled: bool) -> None:
        self.local_models_enabled = local_models_enabled
        self.calls: list[dict[str, Any]] = []
        self.updated: list[object] = []
        self.shutdown_calls = 0

    def update_config(self, config: object) -> None:
        self.updated.append(config)

    async def call(self, messages: list[dict[str, Any]], **kwargs: Any) -> object:
        self.calls.append({"messages": messages, **kwargs})
        return SimpleNamespace(text="local")

    async def shutdown(self) -> None:
        self.shutdown_calls += 1

    async def describe_local_runtime(self) -> dict[str, object]:
        return {"state": "stopped"}


class _LegacyNativeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call(self, messages: list[dict[str, Any]], **kwargs: Any) -> object:
        self.calls.append({"messages": messages, **kwargs})
        return SimpleNamespace(text="legacy-api")


def _agent_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    _install_i18n_host_stub(monkeypatch)
    package = _package(monkeypatch, "_study_inference_tutor_agent_test")
    return importlib.import_module(f"{package}.tutor_llm_agent")


@pytest.mark.asyncio
async def test_tutor_agent_keeps_legacy_api_seam_but_local_mode_cannot_bypass_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _agent_module(monkeypatch)
    config = SimpleNamespace(local_models_enabled=False)
    agent = module.TutorLLMAgent(logger=SimpleNamespace(), config=config)
    legacy = _LegacyNativeClient()
    api_router = _AgentRouter(local_models_enabled=False)
    agent._qwen_client = legacy
    agent._inference_router = api_router

    api_result = await agent._call_model_result(
        [], operation="concept_explain", deadline=123.0
    )

    assert api_result.text == "legacy-api"
    assert len(legacy.calls) == 1
    assert api_router.calls == []

    local_router = _AgentRouter(local_models_enabled=True)
    agent._inference_router = local_router
    local_result = await agent._call_model_result(
        [], operation="concept_explain", deadline=123.0
    )

    assert local_result.text == "local"
    assert len(legacy.calls) == 1
    assert len(local_router.calls) == 1


@pytest.mark.asyncio
async def test_tutor_agent_forwards_config_shutdown_and_local_status_to_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _agent_module(monkeypatch)
    agent = module.TutorLLMAgent(
        logger=SimpleNamespace(), config=SimpleNamespace(local_models_enabled=False)
    )
    router = _AgentRouter(local_models_enabled=False)
    agent._inference_router = router
    next_config = SimpleNamespace(local_models_enabled=True)

    agent.update_config(next_config)
    status = await agent.describe_local_runtime()
    await agent.shutdown()

    assert router.updated == [next_config]
    assert status == {"state": "stopped"}
    assert router.shutdown_calls == 1
