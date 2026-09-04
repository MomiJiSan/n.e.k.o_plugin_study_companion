"""Trusted local-app binding for the Knowledge Dungeon authority."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast

from .knowledge_dungeon.bridge_contracts import TrustedInvocationContext
from .knowledge_dungeon.host_adapter import (
    AdapterOutcome,
    AdapterOutcomeCategory,
    KnowledgeDungeonHostAdapter,
)

_KNOWLEDGE_DUNGEON_APP_ID = "knowledge_dungeon"
_KNOWLEDGE_DUNGEON_SCOPE = "study_companion:dungeon"
_STORE_FILENAME = "knowledge_dungeon.sqlite3"
_OperationDecorator = Callable[
    [str], Callable[[Callable[..., Any]], Callable[..., Any]]
]


class _PluginDataPath(Protocol):
    def data_path(self, filename: str) -> Path: ...


def _unavailable_operation_decorator(
    _operation: str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Leave no callable metadata when the host predates the local-app SDK."""

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        return function

    return decorator


try:
    _local_app_sdk = import_module("plugin.sdk.local_app")
except ModuleNotFoundError as exc:
    if exc.name != "plugin.sdk.local_app":
        raise
    _TRUSTED_LOCAL_APP_SDK_AVAILABLE = False
    _TrustedLocalAppContextType: type[Any] | None = None
    trusted_local_app_operation = _unavailable_operation_decorator
else:
    _context_type = getattr(_local_app_sdk, "TrustedLocalAppPluginContext", None)
    _operation_decorator = getattr(_local_app_sdk, "trusted_local_app_operation", None)
    if isinstance(_context_type, type) and callable(_operation_decorator):
        _TRUSTED_LOCAL_APP_SDK_AVAILABLE = True
        _TrustedLocalAppContextType = _context_type
        trusted_local_app_operation = cast(
            _OperationDecorator,
            _operation_decorator,
        )
    else:
        _TRUSTED_LOCAL_APP_SDK_AVAILABLE = False
        _TrustedLocalAppContextType = None
        trusted_local_app_operation = _unavailable_operation_decorator


def _safe_failure(
    category: AdapterOutcomeCategory,
    *,
    code: str,
    message: str,
) -> dict[str, object]:
    return AdapterOutcome.failure(
        category,
        code=code,
        message=message,
    ).to_dict()


def _validated_invocation_context(
    raw_context: object,
    *,
    expected_operation: str,
) -> TrustedInvocationContext | None:
    context_type = _TrustedLocalAppContextType
    if context_type is None or not isinstance(raw_context, context_type):
        return None
    try:
        context = context_type.from_mapping(
            {
                "app_id": getattr(raw_context, "app_id"),
                "client_id": getattr(raw_context, "client_id"),
                "session_id": getattr(raw_context, "session_id"),
                "scope": getattr(raw_context, "scope"),
                "operation": getattr(raw_context, "operation"),
            }
        )
        if context.app_id != _KNOWLEDGE_DUNGEON_APP_ID:
            return None
        if context.scope != _KNOWLEDGE_DUNGEON_SCOPE:
            return None
        if context.operation != expected_operation:
            return None
        # Session identity is deliberately validated above but excluded here:
        # deterministic run/command identity must survive session replacement.
        return TrustedInvocationContext(
            client_id=context.client_id,
            scope=context.scope,
        )
    except (AttributeError, TypeError, ValueError):
        return None


class _KnowledgeDungeonLocalAppMixin:
    async def _invoke_knowledge_dungeon_local_app(
        self,
        raw_context: object,
        payload: Mapping[str, Any],
        *,
        trusted_operation: str,
        adapter_operation: str,
    ) -> dict[str, object]:
        if not _TRUSTED_LOCAL_APP_SDK_AVAILABLE:
            return _safe_failure(
                AdapterOutcomeCategory.PROTOCOL,
                code="local_app_sdk_unavailable",
                message="Trusted local application operations are unavailable.",
            )
        context = _validated_invocation_context(
            raw_context,
            expected_operation=trusted_operation,
        )
        if context is None:
            return _safe_failure(
                AdapterOutcomeCategory.PROTOCOL,
                code="untrusted_local_app_context",
                message="Trusted local application context is invalid.",
            )
        try:
            owner = cast(_PluginDataPath, self)
            adapter = KnowledgeDungeonHostAdapter(owner.data_path(_STORE_FILENAME))
            outcome = await adapter.invoke(context, adapter_operation, payload)
            return outcome.to_dict()
        except asyncio.CancelledError:
            raise
        except Exception:
            return _safe_failure(
                AdapterOutcomeCategory.RETRYABLE,
                code="local_app_binding_unavailable",
                message="The knowledge dungeon binding is temporarily unavailable.",
            )

    @trusted_local_app_operation("knowledge_dungeon.bootstrap")
    async def knowledge_dungeon_bootstrap(
        self,
        context: object,
        payload: Mapping[str, Any],
    ) -> dict[str, object]:
        return await self._invoke_knowledge_dungeon_local_app(
            context,
            payload,
            trusted_operation="knowledge_dungeon.bootstrap",
            adapter_operation="bootstrap",
        )

    @trusted_local_app_operation("knowledge_dungeon.create_run")
    async def knowledge_dungeon_create_run(
        self,
        context: object,
        payload: Mapping[str, Any],
    ) -> dict[str, object]:
        return await self._invoke_knowledge_dungeon_local_app(
            context,
            payload,
            trusted_operation="knowledge_dungeon.create_run",
            adapter_operation="create_run",
        )

    @trusted_local_app_operation("knowledge_dungeon.get_run")
    async def knowledge_dungeon_get_run(
        self,
        context: object,
        payload: Mapping[str, Any],
    ) -> dict[str, object]:
        return await self._invoke_knowledge_dungeon_local_app(
            context,
            payload,
            trusted_operation="knowledge_dungeon.get_run",
            adapter_operation="get_run",
        )

    @trusted_local_app_operation("knowledge_dungeon.perform_action")
    async def knowledge_dungeon_perform_action(
        self,
        context: object,
        payload: Mapping[str, Any],
    ) -> dict[str, object]:
        return await self._invoke_knowledge_dungeon_local_app(
            context,
            payload,
            trusted_operation="knowledge_dungeon.perform_action",
            adapter_operation="perform_action",
        )


__all__ = ["_KnowledgeDungeonLocalAppMixin"]
