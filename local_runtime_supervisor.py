"""Lifecycle management for the isolated local math runtime process."""

from __future__ import annotations

import asyncio
import json
import secrets
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

try:
    from .local_runtime_protocol import (
        LOCAL_RUNTIME_CRASHED,
        LOCAL_RUNTIME_START_FAILED,
        LOCAL_RUNTIME_UNAVAILABLE,
        LocalRuntimeError,
        LocalRuntimeState,
        LocalRuntimeStatus,
        PROTOCOL_VERSION,
        TOKEN_HEADER,
    )
except ImportError:  # Direct imports in isolated tests.
    from local_runtime_protocol import (  # type: ignore[no-redef]
        LOCAL_RUNTIME_CRASHED,
        LOCAL_RUNTIME_START_FAILED,
        LOCAL_RUNTIME_UNAVAILABLE,
        LocalRuntimeError,
        LocalRuntimeState,
        LocalRuntimeStatus,
        PROTOCOL_VERSION,
        TOKEN_HEADER,
    )


_READY_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True, slots=True)
class LocalRuntimeConnection:
    """Ephemeral, loopback-only connection details for one runtime process."""

    base_url: str
    token: str


ProcessFactory = Callable[..., Awaitable[asyncio.subprocess.Process]]


class LocalRuntimeSupervisor:
    """Starts at most one local runtime and owns its shutdown lifecycle."""

    def __init__(
        self,
        *,
        startup_timeout_seconds: float = _READY_TIMEOUT_SECONDS,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        self._startup_timeout_seconds = max(0.1, float(startup_timeout_seconds))
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._connection: LocalRuntimeConnection | None = None
        self._restart_count = 0
        self._state = LocalRuntimeState.STOPPED

    @property
    def state(self) -> LocalRuntimeState:
        return self._state

    async def ensure_started(self) -> LocalRuntimeConnection:
        """Lazily start the runtime and return its authenticated loopback endpoint."""

        async with self._lock:
            if self._is_running() and self._connection is not None:
                return self._connection
            if self._process is not None:
                await self._clear_process_locked()
                if self._restart_count >= 1:
                    self._state = LocalRuntimeState.CRASHED
                    raise LocalRuntimeError(
                        LOCAL_RUNTIME_CRASHED,
                        "local runtime stopped unexpectedly",
                    )
                self._restart_count += 1
            return await self._start_locked()

    async def get_status(self) -> LocalRuntimeStatus:
        """Return process-level state without causing a cold start."""

        async with self._lock:
            if self._process is not None and not self._is_running():
                await self._clear_process_locked()
                self._state = LocalRuntimeState.CRASHED
            if self._connection is None:
                return LocalRuntimeStatus(
                    protocol_version=PROTOCOL_VERSION,
                    runtime_version="0.1.0",
                    state=self._state,
                )
            return LocalRuntimeStatus(
                protocol_version=PROTOCOL_VERSION,
                runtime_version="0.1.0",
                state=self._state,
            )

    async def unload(self) -> LocalRuntimeStatus:
        """The stub has no models; this keeps the future lifecycle API stable."""

        return await self.get_status()

    async def shutdown(self) -> None:
        """Terminate the child process and leave no runtime owned by this supervisor."""

        async with self._lock:
            self._state = LocalRuntimeState.STOPPING
            process = self._process
            self._connection = None
            self._process = None
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            self._state = LocalRuntimeState.STOPPED

    async def _start_locked(self) -> LocalRuntimeConnection:
        self._state = LocalRuntimeState.STARTING
        token = secrets.token_urlsafe(32)
        script = Path(__file__).with_name("local_runtime_stub.py")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = await self._process_factory(
                sys.executable,
                str(script),
                "--token",
                token,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self._process = process
            port = await self._read_ready_port(process)
        except (
            OSError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
            LocalRuntimeError,
        ) as exc:
            await self._clear_process_locked()
            self._state = LocalRuntimeState.UNAVAILABLE
            raise LocalRuntimeError(
                LOCAL_RUNTIME_START_FAILED, "local runtime could not start"
            ) from exc
        connection = LocalRuntimeConnection(
            base_url=f"http://127.0.0.1:{port}", token=token
        )
        try:
            await self._verify_health(connection)
        except LocalRuntimeError as exc:
            await self._clear_process_locked()
            self._state = LocalRuntimeState.UNAVAILABLE
            raise LocalRuntimeError(
                LOCAL_RUNTIME_START_FAILED, "local runtime could not start"
            ) from exc
        self._connection = connection
        self._state = LocalRuntimeState.READY
        return connection

    async def _verify_health(self, connection: LocalRuntimeConnection) -> None:
        """Require a successful authenticated health response before publishing ready."""

        try:
            async with httpx.AsyncClient(
                timeout=self._startup_timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    f"{connection.base_url}/health",
                    headers={TOKEN_HEADER: connection.token},
                )
            payload = response.json()
            status = LocalRuntimeStatus.from_payload(payload)
        except (httpx.HTTPError, ValueError, LocalRuntimeError) as exc:
            raise LocalRuntimeError(
                LOCAL_RUNTIME_UNAVAILABLE, "local runtime health check failed"
            ) from exc
        if response.status_code != 200 or status.state is not LocalRuntimeState.READY:
            raise LocalRuntimeError(
                LOCAL_RUNTIME_UNAVAILABLE, "local runtime health check failed"
            )

    async def _read_ready_port(self, process: asyncio.subprocess.Process) -> int:
        if process.stdout is None:
            raise ValueError("runtime stdout is unavailable")
        raw = await asyncio.wait_for(
            process.stdout.readline(), timeout=self._startup_timeout_seconds
        )
        if not raw:
            raise ValueError("runtime stopped before becoming ready")
        event: Any = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(event, dict)
            or event.get("event") != "ready"
            or event.get("protocol_version") != PROTOCOL_VERSION
        ):
            raise ValueError("runtime emitted an invalid ready event")
        port = event.get("port")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("runtime emitted an invalid port")
        return port

    def _is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _clear_process_locked(self) -> None:
        process = self._process
        self._process = None
        self._connection = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except TimeoutError:
                process.kill()
                await process.wait()


__all__ = ["LocalRuntimeConnection", "LocalRuntimeSupervisor"]
