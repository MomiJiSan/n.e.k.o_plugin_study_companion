from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _StudyGateway:
    instances: list[_StudyGateway] = []

    def __init__(self, *, logger: Any) -> None:
        self.logger = logger
        self.calls: list[dict[str, Any]] = []
        self.result = SimpleNamespace(
            text='{"evidence":[]}',
            model="test-model",
            request_id="request-1",
            output_limit_reached=False,
        )
        self.instances.append(self)

    async def call(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"messages": messages, **kwargs})
        return self.result


@pytest.fixture()
def gateway_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    package_name = f"_cognitive_model_gateway_test_{time.time_ns()}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    study_gateway = ModuleType(f"{package_name}.study_model_gateway")
    study_gateway.StudyModelGateway = _StudyGateway  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, study_gateway.__name__, study_gateway)
    _StudyGateway.instances.clear()

    module = importlib.import_module(f"{package_name}.cognitive_model_gateway")
    contracts = importlib.import_module(
        f"{package_name}.adaptive_learning.cognitive_contracts"
    )
    return SimpleNamespace(module=module, contracts=contracts)


def _request(environment: Any, **changes: Any) -> Any:
    values = {
        "payload": {
            "topic_id": "college_chain_rule",
            "learner_answer": "cos(x^2)",
            "evaluation": {"verdict": "wrong"},
        },
        "timeout_seconds": 7.5,
    }
    values.update(changes)
    return environment.contracts.CognitiveModelRequest(**values)


@pytest.mark.asyncio
async def test_bridge_sends_only_system_instruction_and_structured_payload(
    gateway_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module.module.time, "monotonic", lambda: 100.0)
    bridge = gateway_module.module.StudyCognitiveModelGateway(logger=object())
    request = _request(gateway_module)

    response = await bridge.complete_structured(request)

    gateway = _StudyGateway.instances[0]
    assert len(gateway.calls) == 1
    call = gateway.calls[0]
    assert call["operation"] == gateway_module.contracts.COGNITIVE_EXTRACT_OPERATION
    assert call["deadline"] == 107.5
    assert call["messages"][0] == {
        "role": "system",
        "content": gateway_module.module._SYSTEM_INSTRUCTION,
    }
    instruction = call["messages"][0]["content"]
    assert "at most one item per hypothesis_code" in instruction
    assert "support and counter are mutually exclusive" in instruction
    assert "First branch on evaluation.verdict" in instruction
    assert "If correct, return counter" in instruction
    assert "If wrong, never return counter" in instruction
    assert "never omit_inner_derivative" in instruction
    assert "only a sign" in instruction
    assert "is not differentiate_inner_incorrectly" in instruction
    assert call["messages"][1]["role"] == "user"
    assert json.loads(call["messages"][1]["content"]) == request.payload
    assert "```" not in call["messages"][1]["content"]
    assert response.content == '{"evidence":[]}'
    assert response.model == "test-model"
    assert response.request_id == "request-1"
    assert response.output_limit_reached is False


@pytest.mark.asyncio
async def test_bridge_rejects_non_768_budget_before_calling_shared_gateway(
    gateway_module: Any,
) -> None:
    bridge = gateway_module.module.StudyCognitiveModelGateway(logger=object())

    with pytest.raises(ValueError, match="output token budget"):
        await bridge.complete_structured(
            _request(gateway_module, max_output_tokens=769)
        )

    assert _StudyGateway.instances[0].calls == []


@pytest.mark.asyncio
async def test_bridge_rejects_an_operation_that_could_select_another_budget(
    gateway_module: Any,
) -> None:
    bridge = gateway_module.module.StudyCognitiveModelGateway(logger=object())

    with pytest.raises(ValueError, match="model operation"):
        await bridge.complete_structured(
            _request(gateway_module, operation="question_generate")
        )

    assert _StudyGateway.instances[0].calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
async def test_bridge_rejects_unbounded_or_invalid_timeout(
    gateway_module: Any,
    timeout: object,
) -> None:
    bridge = gateway_module.module.StudyCognitiveModelGateway(logger=object())

    with pytest.raises(ValueError, match="timeout"):
        await bridge.complete_structured(
            _request(gateway_module, timeout_seconds=timeout)
        )

    assert _StudyGateway.instances[0].calls == []


@pytest.mark.asyncio
async def test_bridge_preserves_output_limit_signal_for_fail_closed_validation(
    gateway_module: Any,
) -> None:
    bridge = gateway_module.module.StudyCognitiveModelGateway(logger=object())
    _StudyGateway.instances[0].result = SimpleNamespace(
        text='{"evidence":[',
        model="test-model",
        request_id="request-truncated",
        output_limit_reached=True,
    )

    response = await bridge.complete_structured(_request(gateway_module))

    assert response.content == '{"evidence":['
    assert response.request_id == "request-truncated"
    assert response.output_limit_reached is True


def test_qwen_and_generic_gateway_share_the_768_token_operation_budget(
    gateway_module: Any,
) -> None:
    package_name = gateway_module.module.__package__
    qwen = importlib.import_module(f"{package_name}.qwen_native_client")

    assert (
        qwen._OUTPUT_TOKEN_BUDGETS[
            gateway_module.contracts.COGNITIVE_EXTRACT_OPERATION
        ]
        == gateway_module.contracts.DEFAULT_COGNITIVE_OUTPUT_TOKEN_BUDGET
        == 768
    )
