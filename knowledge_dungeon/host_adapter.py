"""Async host boundary for a future N.E.K.O runtime-specific message adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .application_service import ApplicationServiceError, KnowledgeDungeonApplicationService
from .bridge_contracts import (
    BootstrapRequest,
    BridgeContractError,
    CreateRunRequest,
    GetRunRequest,
    PerformActionPayload,
    TrustedInvocationContext,
    require_trusted_context,
)
from .engine import KnowledgeDungeonEngine
from .persistence import DungeonRunStore, DungeonStoreError

_SUPPORTED_OPERATIONS = frozenset(("bootstrap", "create_run", "get_run", "perform_action"))
_RETRYABLE_CODES = frozenset(
    (
        "authority_failure",
        "concurrent_state_change",
        "persistence_failure",
        "stale_state_version",
    )
)
_SAFE_DOMAIN_MESSAGES = {
    "action_unavailable": "The requested action is unavailable.",
    "command_id_conflict": "The request ID was already used for different input.",
    "corrupt_dungeon_state": "The run is unavailable because its state failed validation.",
    "run_not_found": "The requested run was not found.",
    "scenario_unavailable": "The requested scenario is unavailable.",
    "subject_unavailable": "The requested subject is unavailable.",
}
_SAFE_RETRYABLE_MESSAGES = {
    "authority_failure": "The dungeon authority is temporarily unavailable.",
    "concurrent_state_change": "The run changed concurrently; refresh and retry.",
    "persistence_failure": "Dungeon storage is temporarily unavailable.",
    "stale_state_version": "The run changed; refresh and retry with the latest state version.",
}


class AdapterOutcomeCategory(str, Enum):
    SUCCESS = "success"
    PROTOCOL = "protocol"
    DOMAIN = "domain"
    RETRYABLE = "retryable"


@dataclass(frozen=True, slots=True)
class AdapterError:
    code: str
    message: str
    retryable: bool

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


@dataclass(frozen=True, slots=True)
class AdapterOutcome:
    ok: bool
    category: AdapterOutcomeCategory
    value: Mapping[str, Any] | None
    error: AdapterError | None

    @classmethod
    def success(cls, value: Mapping[str, Any]) -> "AdapterOutcome":
        return cls(True, AdapterOutcomeCategory.SUCCESS, dict(value), None)

    @classmethod
    def failure(
        cls,
        category: AdapterOutcomeCategory,
        *,
        code: str,
        message: str,
    ) -> "AdapterOutcome":
        return cls(False, category, None, AdapterError(code, message, category is AdapterOutcomeCategory.RETRYABLE))

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "category": self.category.value,
            "value": dict(self.value) if self.value is not None else None,
            "error": self.error.to_dict() if self.error is not None else None,
        }


HostRequest = BootstrapRequest | CreateRunRequest | GetRunRequest | PerformActionPayload


class KnowledgeDungeonHostAdapter:
    """Run one trusted operation against one short-lived persistent engine."""

    def __init__(self, store_path: str | Path) -> None:
        self._store_path = Path(store_path)

    async def invoke(
        self,
        trusted_context: TrustedInvocationContext,
        operation: str,
        payload: Mapping[str, Any],
    ) -> AdapterOutcome:
        try:
            context = require_trusted_context(trusted_context)
            request = self._parse_request(operation, payload)
        except BridgeContractError as exc:
            return self._protocol_failure(exc.code, str(exc))

        try:
            value = await asyncio.to_thread(self._invoke_sync, context, operation, request)
            return AdapterOutcome.success(value)
        except asyncio.CancelledError:
            raise
        except BridgeContractError as exc:
            return self._protocol_failure(exc.code, str(exc))
        except ApplicationServiceError as exc:
            return self._service_failure(exc.code)
        except DungeonStoreError as exc:
            return self._service_failure(exc.code)
        except Exception:
            return AdapterOutcome.failure(
                AdapterOutcomeCategory.RETRYABLE,
                code="adapter_unavailable",
                message="The knowledge dungeon service is temporarily unavailable.",
            )

    @staticmethod
    def _parse_request(operation: object, payload: object) -> HostRequest:
        if not isinstance(operation, str) or operation not in _SUPPORTED_OPERATIONS:
            raise BridgeContractError("invalid_operation", "operation is not supported")
        if operation == "bootstrap":
            return BootstrapRequest.from_mapping(payload)
        if operation == "create_run":
            return CreateRunRequest.from_mapping(payload)
        if operation == "get_run":
            return GetRunRequest.from_mapping(payload)
        return PerformActionPayload.from_mapping(payload)

    def _invoke_sync(
        self,
        context: TrustedInvocationContext,
        operation: str,
        request: HostRequest,
    ) -> dict[str, Any]:
        with DungeonRunStore(self._store_path) as store:
            service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(store))
            if operation == "bootstrap" and isinstance(request, BootstrapRequest):
                return service.bootstrap(context)
            if operation == "create_run" and isinstance(request, CreateRunRequest):
                return service.create_run(context, request)
            if operation == "get_run" and isinstance(request, GetRunRequest):
                return service.get_run(context, request.run_id)
            if operation == "perform_action" and isinstance(request, PerformActionPayload):
                return service.perform_action(context, request.run_id, request.request)
        raise BridgeContractError("invalid_operation", "operation contract does not match payload")

    @staticmethod
    def _protocol_failure(code: str, message: str) -> AdapterOutcome:
        if code in {"scenario_unavailable", "subject_unavailable"}:
            return KnowledgeDungeonHostAdapter._service_failure(code)
        return AdapterOutcome.failure(
            AdapterOutcomeCategory.PROTOCOL,
            code=code,
            message=message,
        )

    @staticmethod
    def _service_failure(code: str) -> AdapterOutcome:
        if code in _RETRYABLE_CODES:
            return AdapterOutcome.failure(
                AdapterOutcomeCategory.RETRYABLE,
                code=code,
                message=_SAFE_RETRYABLE_MESSAGES[code],
            )
        return AdapterOutcome.failure(
            AdapterOutcomeCategory.DOMAIN,
            code=code if code in _SAFE_DOMAIN_MESSAGES else "domain_error",
            message=_SAFE_DOMAIN_MESSAGES.get(code, "The requested dungeon operation could not be completed."),
        )


__all__ = [
    "AdapterError",
    "AdapterOutcome",
    "AdapterOutcomeCategory",
    "KnowledgeDungeonHostAdapter",
]
