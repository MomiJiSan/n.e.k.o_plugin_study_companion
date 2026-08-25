"""Authenticated loopback client for the isolated local math runtime."""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any
from urllib.parse import urlsplit

import httpx

try:
    from .local_runtime_protocol import (
        LOCAL_RUNTIME_AUTH_FAILED,
        LOCAL_RUNTIME_PROTOCOL_MISMATCH,
        LOCAL_RUNTIME_UNAVAILABLE,
        LocalRuntimeError,
        LocalRuntimeStatus,
        TOKEN_HEADER,
    )
    from .local_runtime_supervisor import LocalRuntimeConnection, LocalRuntimeSupervisor
except ImportError:  # Direct imports in isolated tests.
    from local_runtime_protocol import (  # type: ignore[no-redef]
        LOCAL_RUNTIME_AUTH_FAILED,
        LOCAL_RUNTIME_PROTOCOL_MISMATCH,
        LOCAL_RUNTIME_UNAVAILABLE,
        LocalRuntimeError,
        LocalRuntimeStatus,
        TOKEN_HEADER,
    )
    from local_runtime_supervisor import (  # type: ignore[no-redef]
        LocalRuntimeConnection,
        LocalRuntimeSupervisor,
    )


class LocalRuntimeClient:
    """Makes local-only calls; it never knows about or falls back to an API."""

    def __init__(
        self,
        supervisor: LocalRuntimeSupervisor,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._supervisor = supervisor
        self._transport = transport
        self._timeout_seconds = max(0.1, float(timeout_seconds))

    async def call(
        self,
        messages: list[dict[str, Any]],
        *,
        operation: str,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Send a math request to the local runtime.

        The stub always raises ``local_models_not_installed``.  The return type
        intentionally stays JSON-like for the later model runtime stage.
        """

        path = self._operation_path(operation)
        connection = await self._supervisor.ensure_started()
        timeout = self._request_timeout(deadline)
        return await self._request_json(
            connection, "POST", path, json_body={"messages": messages}, timeout=timeout
        )

    async def status(self) -> LocalRuntimeStatus:
        status = await self._supervisor.get_status()
        if status.state.value != "ready":
            return status
        connection = await self._supervisor.ensure_started()
        payload = await self._request_json(connection, "GET", "/runtime/status")
        return LocalRuntimeStatus.from_payload(payload)

    async def unload(self) -> LocalRuntimeStatus:
        status = await self._supervisor.get_status()
        if status.state.value != "ready":
            return status
        connection = await self._supervisor.ensure_started()
        payload = await self._request_json(connection, "POST", "/runtime/unload", json_body={})
        return LocalRuntimeStatus.from_payload(payload)

    async def shutdown(self) -> None:
        await self._supervisor.shutdown()

    @staticmethod
    def _operation_path(operation: str) -> str:
        normalized = str(operation or "").strip().lower()
        if normalized in {"recognize", "math_recognize", "study_recognize_math_problem"}:
            return "/v1/math/recognize"
        return "/v1/math/explain"

    def _request_timeout(self, deadline: float | None) -> float:
        if deadline is None:
            return self._timeout_seconds
        try:
            remaining = float(deadline) - asyncio.get_running_loop().time()
        except (TypeError, ValueError, OverflowError):
            return self._timeout_seconds
        return max(0.1, min(self._timeout_seconds, remaining))

    async def _request_json(
        self,
        connection: LocalRuntimeConnection,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        base_url = self._normalize_loopback_base_url(connection.base_url)
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=timeout or self._timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method,
                    f"{base_url}{path}",
                    headers={TOKEN_HEADER: connection.token},
                    json=json_body,
                )
        except httpx.TimeoutException as exc:
            raise LocalRuntimeError(
                LOCAL_RUNTIME_UNAVAILABLE, "local runtime timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise LocalRuntimeError(
                LOCAL_RUNTIME_UNAVAILABLE, "local runtime is unavailable"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise LocalRuntimeError(
                LOCAL_RUNTIME_PROTOCOL_MISMATCH, "runtime returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise LocalRuntimeError(
                LOCAL_RUNTIME_PROTOCOL_MISMATCH, "runtime returned invalid JSON"
            )
        if response.is_success:
            return payload
        code = self._error_code(payload)
        if response.status_code == 401:
            code = LOCAL_RUNTIME_AUTH_FAILED
        raise LocalRuntimeError(code, "local runtime rejected the request", diagnostic=code)

    @staticmethod
    def _error_code(payload: dict[str, Any]) -> str:
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error["code"]
        return LOCAL_RUNTIME_UNAVAILABLE

    @staticmethod
    def _normalize_loopback_base_url(base_url: str) -> str:
        parsed = urlsplit(str(base_url or ""))
        if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
            raise LocalRuntimeError(
                LOCAL_RUNTIME_UNAVAILABLE, "runtime endpoint is not a loopback HTTP URL"
            )
        try:
            is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            is_loopback = parsed.hostname.lower() == "localhost"
        if not is_loopback or parsed.query or parsed.fragment or parsed.path.rstrip("/"):
            raise LocalRuntimeError(
                LOCAL_RUNTIME_UNAVAILABLE, "runtime endpoint is not a loopback HTTP URL"
            )
        return f"http://{parsed.hostname}:{parsed.port}" if parsed.port else f"http://{parsed.hostname}"


__all__ = ["LocalRuntimeClient"]
