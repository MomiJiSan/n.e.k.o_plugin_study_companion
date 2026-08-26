from __future__ import annotations

import asyncio
import importlib
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[tuple[Any, ...]] = []

    def warning(self, *args: Any, **_kwargs: Any) -> None:
        self.warnings.append(args)


class _NativeError(RuntimeError):
    def __init__(
        self,
        *,
        diagnostic: str,
        status_code: int = 0,
        request_id: str = "",
        provider_code: str = "",
        operation: str = "",
    ) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic
        self.status_code = status_code
        self.request_id = request_id
        self.provider_code = provider_code
        self.operation = operation


class _NativeClient:
    def __init__(self, *, logger: Any) -> None:
        self.logger = logger
        self.result: Any = SimpleNamespace(
            text="native result",
            model="qwen-plus",
            model_group="agent",
            request_id="native-request",
            input_tokens=10,
            output_tokens=5,
            finish_reason="stop",
            max_output_tokens=3072,
            output_limit_reached=False,
            reasoning_tokens=0,
            text_tokens=5,
            termination_unknown=False,
        )
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def call(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append({"messages": messages, **kwargs})
        if self.error is not None:
            raise self.error
        return self.result


class _ConfigManager:
    def __init__(self) -> None:
        self.configs = {
            "agent": {
                "model": "qwen-plus",
                "base_url": "https://dashscope.aliyuncs.com/api/v1",
                "api_key": "secret",
                "provider_type": "openai_compatible",
            },
            "vision": {
                "model": "vision-model",
                "base_url": "https://provider.invalid/v1",
                "api_key": "vision-secret",
                "provider_type": "anthropic",
            },
        }
        self.consumed: list[tuple[str, int]] = []
        self.reserved: list[tuple[str, int, int]] = []
        self.allow_quota = True
        self.reserve_count = 2

    async def aget_model_api_config(self, group: str) -> dict[str, str]:
        return dict(self.configs[group])

    async def aconsume_agent_daily_quota(self, *, source: str, units: int) -> tuple[bool, dict[str, Any]]:
        self.consumed.append((source, units))
        return self.allow_quota, {}

    async def areserve_agent_daily_quota(
        self, *, source: str, units: int, minimum_units: int
    ) -> tuple[int, dict[str, Any]]:
        self.reserved.append((source, units, minimum_units))
        return self.reserve_count, {}


@pytest.fixture()
def gateway_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    package_name = f"_coverage_runtime_gateway_{time.time_ns()}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    native = ModuleType(f"{package_name}.qwen_native_client")
    native._OUTPUT_TOKEN_BUDGETS = {"explain": 123}
    native.QwenNativeClient = _NativeClient
    native.QwenNativeError = _NativeError
    native.messages_have_image = lambda messages: any(
        isinstance(message.get("content"), list)
        and any(
            isinstance(block, dict) and block.get("type") == "image_url"
            for block in message["content"]
        )
        for message in messages
    )
    monkeypatch.setitem(sys.modules, native.__name__, native)

    manager = _ConfigManager()
    utils = ModuleType("utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    config_manager = ModuleType("utils.config_manager")
    config_manager.get_config_manager = lambda: manager
    llm_client = ModuleType("utils.llm_client")
    llm_client.create_chat_llm_async = None
    token_tracker = ModuleType("utils.token_tracker")

    @contextmanager
    def llm_call_context(group: str):
        token_tracker.entered.append(group)
        yield

    token_tracker.entered = []
    token_tracker.llm_call_context = llm_call_context
    monkeypatch.setitem(sys.modules, "utils", utils)
    monkeypatch.setitem(sys.modules, "utils.config_manager", config_manager)
    monkeypatch.setitem(sys.modules, "utils.llm_client", llm_client)
    monkeypatch.setitem(sys.modules, "utils.token_tracker", token_tracker)

    module = importlib.import_module(f"{package_name}.study_model_gateway")
    return SimpleNamespace(
        module=module,
        manager=manager,
        config_module=config_manager,
        token_tracker=token_tracker,
    )


def _runtime(module: Any, **overrides: Any) -> Any:
    values = {
        "model_group": "agent",
        "model": "generic-model",
        "provider_type": "openai_compatible",
        "transport": "openai_compatible",
        "api_key": "secret",
        "base_url": "https://provider.invalid/v1",
    }
    values.update(overrides)
    return module.StudyModelRuntimeSnapshot(**values)


@pytest.mark.asyncio
async def test_gateway_resolves_runtime_descriptions_and_transport_rules(gateway_env: Any) -> None:
    module = gateway_env.module
    gateway = module.StudyModelGateway(logger=_Logger())

    agent = await gateway.resolve_runtime("agent")
    assert agent.transport == "dashscope_native"
    assert agent.safe_description() == {
        "group": "agent",
        "model": "qwen-plus",
        "provider_type": "openai_compatible",
        "configured": True,
        "credential_configured": True,
        "transport": "dashscope_native",
        "transport_supported": True,
        "vision_capability": "not_applicable",
    }
    descriptions = await gateway.describe_runtimes()
    assert descriptions["vision"]["transport"] == "anthropic"
    assert descriptions["vision"]["vision_capability"] == "unknown"

    assert module._runtime_transport("qwen", "https://user@dashscope.aliyuncs.com/api/v1", "openai_compatible") == "openai_compatible"
    assert module._runtime_transport("qwen", "http://dashscope.aliyuncs.com/api/v1", "openai_compatible") == "openai_compatible"
    assert module._runtime_transport("model", "not a URL", "custom") == "unsupported"
    with pytest.raises(module.StudyModelError) as raised:
        await gateway.resolve_runtime("summary")
    assert raised.value.diagnostic == "unsupported_provider"


@pytest.mark.asyncio
async def test_gateway_uses_sync_config_and_quota_fallbacks(
    gateway_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = gateway_env.module

    class SyncManager:
        def get_model_api_config(self, group: str) -> dict[str, str]:
            return dict(gateway_env.manager.configs[group])

        def reserve_agent_daily_quota(self, source: str, units: int, minimum: int) -> tuple[int, dict[str, Any]]:
            assert (source, units, minimum) == ("study_companion:optional", 2, 1)
            return 1, {}

        def consume_agent_daily_quota(self, source: str, units: int) -> tuple[bool, dict[str, Any]]:
            assert (source, units) == ("study_companion:required", 1)
            return True, {}

    sync_manager = SyncManager()
    monkeypatch.setattr(
        module,
        "_config_manager_module",
        SimpleNamespace(get_config_manager=lambda: sync_manager),
    )
    gateway = module.StudyModelGateway(logger=_Logger())
    assert (await gateway.resolve_runtime("agent")).model == "qwen-plus"
    allowed, reservation = await gateway.reserve_optional_agent_call("optional")
    assert allowed is False
    assert reservation is not None and reservation.remaining_calls == 1
    await gateway._reserve_agent_quota("required")


class _GenericClient:
    def __init__(self, response: Any = None, *, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.closed = 0
        self.close_error: Exception | None = None

    async def ainvoke(self, _messages: Any) -> Any:
        if self.error is not None:
            raise self.error
        return self.response

    async def aclose(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


@pytest.mark.asyncio
async def test_gateway_generic_success_quota_reservation_and_cleanup(
    gateway_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = gateway_env.module
    response = SimpleNamespace(
        content=" model answer ",
        response_metadata={
            "request_id": "request-1",
            "finish_reason": "length",
            "token_usage": {
                "prompt_tokens": "11",
                "completion_tokens": 7,
                "completion_tokens_details": {"reasoning_tokens": 2, "text_tokens": 5},
            },
        },
    )
    client = _GenericClient(response)
    factory_calls: list[dict[str, Any]] = []

    async def factory(**kwargs: Any) -> _GenericClient:
        factory_calls.append(kwargs)
        return client

    monkeypatch.setattr(module, "create_chat_llm_async", factory)
    gateway = module.StudyModelGateway(logger=_Logger())
    reservation = module.AgentQuotaReservation(1)
    result = await gateway.call(
        [{"role": "user", "content": "hello"}],
        operation="explain",
        deadline=time.monotonic() + 1,
        runtime=_runtime(module),
        quota_reservation=reservation,
    )

    assert result.text == "model answer"
    assert result.output_tokens == 7
    assert result.reasoning_tokens == 2
    assert result.output_limit_reached is True
    assert result.termination_unknown is False
    assert reservation.remaining_calls == 0
    assert gateway_env.manager.consumed == []
    assert factory_calls[0]["max_completion_tokens"] == 123
    assert factory_calls[0]["max_retries"] == 0
    assert gateway_env.token_tracker.entered == ["agent"]
    assert client.closed == 1


@pytest.mark.asyncio
async def test_gateway_native_success_and_error_mapping(gateway_env: Any) -> None:
    module = gateway_env.module
    gateway = module.StudyModelGateway(logger=_Logger())
    runtime = _runtime(
        module,
        model="qwen-plus",
        base_url="https://dashscope.aliyuncs.com/api/v1",
        transport="dashscope_native",
    )
    result = await gateway.call(
        [{"role": "user", "content": "hello"}],
        operation="explain",
        deadline=time.monotonic() + 1,
        runtime=runtime,
        quota_reservation=module.AgentQuotaReservation(1),
    )
    assert result.text == "native result"
    assert gateway.native_client.calls[0]["api_config"]["api_key"] == "secret"

    gateway.native_client.error = _NativeError(
        diagnostic="rate_limited",
        status_code=429,
        request_id="request-2",
        provider_code="TooManyRequests",
        operation="native-operation",
    )
    with pytest.raises(module.StudyModelError) as raised:
        await gateway.call(
            [{"role": "user", "content": "hello"}],
            operation="explain",
            deadline=time.monotonic() + 1,
            runtime=runtime,
            quota_reservation=module.AgentQuotaReservation(1),
        )
    assert raised.value.diagnostic == "rate_limited"
    assert raised.value.request_id == "request-2"
    assert raised.value.operation == "native-operation"


@pytest.mark.asyncio
async def test_gateway_dependency_validation_timeout_cancellation_and_degradation(
    gateway_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = gateway_env.module
    gateway = module.StudyModelGateway(logger=_Logger())
    messages = [{"role": "user", "content": "hello"}]

    cases = [
        (_runtime(module, model=""), "model_unavailable"),
        (_runtime(module, api_key=""), "authentication_failed"),
        (_runtime(module, transport="unsupported"), "unsupported_provider"),
        (_runtime(module, model_group="vision"), "invalid_request"),
    ]
    for runtime, diagnostic in cases:
        with pytest.raises(module.StudyModelError) as raised:
            await gateway.call(
                messages,
                operation="explain",
                deadline=time.monotonic() + 1,
                runtime=runtime,
            )
        assert raised.value.diagnostic == diagnostic

    with pytest.raises(module.StudyModelError) as raised:
        await gateway.call(
            messages,
            operation="explain",
            deadline=time.monotonic() - 1,
            runtime=_runtime(module),
        )
    assert raised.value.diagnostic == "timeout"

    monkeypatch.setattr(module, "create_chat_llm_async", None)
    with pytest.raises(module.StudyModelError) as raised:
        await gateway.call(
            messages,
            operation="explain",
            deadline=time.monotonic() + 1,
            runtime=_runtime(module),
            quota_reservation=module.AgentQuotaReservation(1),
        )
    assert raised.value.diagnostic == "provider_unavailable"

    timeout_client = _GenericClient(error=asyncio.TimeoutError())
    monkeypatch.setattr(module, "create_chat_llm_async", lambda **_kwargs: None)

    async def timeout_factory(**_kwargs: Any) -> _GenericClient:
        return timeout_client

    monkeypatch.setattr(module, "create_chat_llm_async", timeout_factory)
    with pytest.raises(module.StudyModelError) as raised:
        await gateway.call(
            messages,
            operation="explain",
            deadline=time.monotonic() + 1,
            runtime=_runtime(module),
            quota_reservation=module.AgentQuotaReservation(1),
        )
    assert raised.value.diagnostic == "timeout"
    assert timeout_client.closed == 1

    canceled_client = _GenericClient(error=asyncio.CancelledError())

    async def canceled_factory(**_kwargs: Any) -> _GenericClient:
        return canceled_client

    monkeypatch.setattr(module, "create_chat_llm_async", canceled_factory)
    with pytest.raises(asyncio.CancelledError):
        await gateway.call(
            messages,
            operation="explain",
            deadline=time.monotonic() + 1,
            runtime=_runtime(module),
            quota_reservation=module.AgentQuotaReservation(1),
        )
    assert canceled_client.closed == 1


@pytest.mark.asyncio
async def test_gateway_maps_provider_errors_empty_response_and_quota_exhaustion(
    gateway_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = gateway_env.module
    logger = _Logger()
    gateway = module.StudyModelGateway(logger=logger)

    class ProviderFailure(RuntimeError):
        status_code = 429
        body = {"error": {"code": "rate_limit", "request_id": "body-request"}}

    failed_client = _GenericClient(error=ProviderFailure("slow down"))
    failed_client.close_error = RuntimeError("close failed")

    async def failed_factory(**_kwargs: Any) -> _GenericClient:
        return failed_client

    monkeypatch.setattr(module, "create_chat_llm_async", failed_factory)
    with pytest.raises(module.StudyModelError) as raised:
        await gateway.call(
            [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "fake"}}]}],
            operation="vision",
            deadline=time.monotonic() + 1,
            runtime=_runtime(module, model_group="vision"),
        )
    assert raised.value.diagnostic == "rate_limited"
    assert raised.value.request_id == "body-request"
    assert len(logger.warnings) == 2

    empty_client = _GenericClient(SimpleNamespace(content="", response_metadata={}))

    async def empty_factory(**_kwargs: Any) -> _GenericClient:
        return empty_client

    monkeypatch.setattr(module, "create_chat_llm_async", empty_factory)
    with pytest.raises(module.StudyModelError) as raised:
        await gateway.call(
            [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "fake"}}]}],
            operation="vision",
            deadline=time.monotonic() + 1,
            runtime=_runtime(module, model_group="vision"),
        )
    assert raised.value.diagnostic == "provider_unavailable"

    gateway_env.manager.allow_quota = False
    with pytest.raises(module.StudyModelError) as raised:
        await gateway._reserve_agent_quota("required")
    assert raised.value.diagnostic == "agent_quota_exceeded"

    gateway_env.manager.reserve_count = 0
    allowed, reservation = await gateway.reserve_optional_agent_call("optional")
    assert allowed is False and reservation is None


@pytest.mark.asyncio
async def test_gateway_reports_missing_configuration_manager(
    gateway_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = gateway_env.module
    monkeypatch.setattr(module, "_config_manager_module", None)
    gateway = module.StudyModelGateway(logger=_Logger())
    with pytest.raises(module.StudyModelError) as raised:
        await gateway.resolve_runtime("agent")
    assert raised.value.diagnostic == "provider_unavailable"
    with pytest.raises(module.StudyModelError) as raised:
        await gateway._reserve_agent_quota("required")
    assert raised.value.diagnostic == "provider_unavailable"


@pytest.fixture()
def transport_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    package_name = f"_coverage_runtime_transport_{time.time_ns()}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    return importlib.import_module(f"{package_name}.qwen_compatible_transport")


@pytest.mark.asyncio
async def test_compatible_transport_success_uses_mock_port(transport_module: Any) -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            headers={"x-request-id": "request-1"},
            json={
                "model": "qwen-plus",
                "choices": [
                    {
                        "message": {"content": [{"text": "first"}, {"text": "second"}]},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 9,
                    "completion_tokens": 4,
                    "completion_tokens_details": {"reasoning_tokens": 1, "text_tokens": 3},
                },
            },
        )

    transport = transport_module.QwenCompatibleTransport(
        transport=httpx.MockTransport(handler)
    )
    result = await transport.chat_completions(
        base_url="https://DASHSCOPE.ALIYUNCS.COM/compatible-mode/v1/",
        api_key="secret",
        model="qwen-plus",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=64,
        timeout_seconds=1,
    )

    assert result.text == "first\nsecond"
    assert result.request_id == "request-1"
    assert result.reasoning_tokens == 1
    assert result.termination_unknown is False
    request = captured["request"]
    assert request.url.path == "/compatible-mode/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer secret"
    assert b'"enable_thinking":false' in request.content


@pytest.mark.parametrize(
    ("endpoint", "diagnostic"),
    [
        ("http://dashscope.aliyuncs.com/compatible-mode/v1", "invalid_endpoint"),
        ("https://example.com/compatible-mode/v1", "invalid_endpoint"),
        ("https://user@dashscope.aliyuncs.com/compatible-mode/v1", "invalid_endpoint"),
        ("https://dashscope.aliyuncs.com:443/compatible-mode/v1", "invalid_endpoint"),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1?x=1", "invalid_endpoint"),
    ],
)
def test_compatible_transport_rejects_unsafe_endpoints(
    transport_module: Any, endpoint: str, diagnostic: str
) -> None:
    with pytest.raises(transport_module.QwenCompatibleTransportError) as raised:
        transport_module.compatible_chat_completions_url(endpoint)
    assert raised.value.diagnostic == diagnostic


@pytest.mark.asyncio
async def test_compatible_transport_validates_credentials_model_and_budgets(
    transport_module: Any,
) -> None:
    transport = transport_module.QwenCompatibleTransport(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    )
    kwargs = {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "secret",
        "model": "qwen-plus",
        "messages": [],
        "max_tokens": 1,
        "timeout_seconds": 1,
    }
    with pytest.raises(transport_module.QwenCompatibleTransportError) as raised:
        await transport.chat_completions(**{**kwargs, "api_key": ""})
    assert raised.value.diagnostic == "authentication_failed"
    with pytest.raises(transport_module.QwenCompatibleTransportError) as raised:
        await transport.chat_completions(**{**kwargs, "model": "other"})
    assert raised.value.diagnostic == "model_not_supported"
    with pytest.raises(ValueError, match="numeric"):
        await transport.chat_completions(**{**kwargs, "max_tokens": object()})
    with pytest.raises(ValueError, match="positive"):
        await transport.chat_completions(**{**kwargs, "timeout_seconds": 0})


@pytest.mark.parametrize(
    ("status", "error", "diagnostic"),
    [
        (401, {"code": "auth", "message": "bad key"}, "authentication_failed"),
        (429, {"code": "rate_limit", "message": "slow"}, "rate_limited"),
        (400, {"code": "bad", "message": "context length exceeded"}, "context_limit_exceeded"),
        (400, {"code": "ModelNotFound", "message": "bad model"}, "model_not_supported"),
        (404, {"code": "missing", "message": "missing"}, "invalid_endpoint"),
        (500, {"code": "server", "message": "down"}, "provider_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_compatible_transport_maps_provider_failures(
    transport_module: Any, status: int, error: dict[str, str], diagnostic: str
) -> None:
    transport = transport_module.QwenCompatibleTransport(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                status,
                headers={"x-dashscope-request-id": "request-error"},
                json={"error": error},
            )
        )
    )
    with pytest.raises(transport_module.QwenCompatibleTransportError) as raised:
        await transport.chat_completions(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="secret",
            model="qwen-plus",
            messages=[],
            max_tokens=64,
            timeout_seconds=1,
        )
    assert raised.value.diagnostic == diagnostic
    assert raised.value.request_id == "request-error"


@pytest.mark.asyncio
async def test_compatible_transport_invalid_json_empty_timeout_and_cancellation(
    transport_module: Any,
) -> None:
    kwargs = {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "secret",
        "model": "qwen-plus",
        "messages": [],
        "max_tokens": 64,
        "timeout_seconds": 1,
    }

    invalid_json = transport_module.QwenCompatibleTransport(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"not-json")
        )
    )
    with pytest.raises(transport_module.QwenCompatibleTransportError) as raised:
        await invalid_json.chat_completions(**kwargs)
    assert raised.value.diagnostic == "provider_unavailable"

    empty = transport_module.QwenCompatibleTransport(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"choices": []})
        )
    )
    with pytest.raises(transport_module.QwenCompatibleTransportError) as raised:
        await empty.chat_completions(**kwargs)
    assert raised.value.diagnostic == "provider_unavailable"

    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("fake timeout", request=request)

    timeout_transport = transport_module.QwenCompatibleTransport(
        transport=httpx.MockTransport(timeout_handler)
    )
    with pytest.raises(transport_module.QwenCompatibleTransportError) as raised:
        await timeout_transport.chat_completions(**kwargs)
    assert raised.value.diagnostic == "timeout"

    async def cancel_handler(_request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    cancel_transport = transport_module.QwenCompatibleTransport(
        transport=httpx.MockTransport(cancel_handler)
    )
    with pytest.raises(asyncio.CancelledError):
        await cancel_transport.chat_completions(**kwargs)
