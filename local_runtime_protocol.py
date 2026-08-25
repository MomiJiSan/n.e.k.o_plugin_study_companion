"""Wire types shared by the plugin and the local math runtime.

This module deliberately has no model, networking, or logging dependencies.  It
is safe to import from both the plugin process and the isolated runtime process.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

PROTOCOL_VERSION: Final = 1
RUNTIME_VERSION: Final = "0.1.0"
TOKEN_HEADER: Final = "X-Local-Runtime-Token"

LOCAL_RUNTIME_UNAVAILABLE: Final = "local_runtime_unavailable"
LOCAL_RUNTIME_START_FAILED: Final = "local_runtime_start_failed"
LOCAL_RUNTIME_AUTH_FAILED: Final = "local_runtime_auth_failed"
LOCAL_RUNTIME_PROTOCOL_MISMATCH: Final = "local_runtime_protocol_mismatch"
LOCAL_MODELS_NOT_INSTALLED: Final = "local_models_not_installed"
LOCAL_RUNTIME_CRASHED: Final = "local_runtime_crashed"


class LocalRuntimeState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    CRASHED = "crashed"
    STOPPING = "stopping"


class LocalRuntimeError(RuntimeError):
    """A safe error crossing the plugin/runtime boundary."""

    def __init__(
        self, code: str, message: str = "", *, diagnostic: str | None = None
    ) -> None:
        self.code = code
        self.diagnostic = diagnostic or code
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class LocalRuntimeStatus:
    protocol_version: int
    runtime_version: str
    state: LocalRuntimeState
    models: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    active_job: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "runtime_version": self.runtime_version,
            "state": self.state.value,
            "models": list(self.models),
            "capabilities": list(self.capabilities),
            "active_job": self.active_job,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "LocalRuntimeStatus":
        if not isinstance(payload, dict):
            raise LocalRuntimeError(
                LOCAL_RUNTIME_PROTOCOL_MISMATCH, "runtime returned invalid JSON"
            )
        try:
            protocol_version = int(payload.get("protocol_version"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise LocalRuntimeError(
                LOCAL_RUNTIME_PROTOCOL_MISMATCH, "runtime protocol version is missing"
            ) from exc
        if protocol_version != PROTOCOL_VERSION:
            raise LocalRuntimeError(
                LOCAL_RUNTIME_PROTOCOL_MISMATCH, "runtime protocol version is unsupported"
            )
        try:
            state = LocalRuntimeState(str(payload.get("state") or ""))
        except ValueError as exc:
            raise LocalRuntimeError(
                LOCAL_RUNTIME_PROTOCOL_MISMATCH, "runtime state is invalid"
            ) from exc
        models = payload.get("models", [])
        capabilities = payload.get("capabilities", [])
        if not isinstance(models, list) or not isinstance(capabilities, list):
            raise LocalRuntimeError(
                LOCAL_RUNTIME_PROTOCOL_MISMATCH, "runtime collections are invalid"
            )
        active_job = payload.get("active_job")
        if active_job is not None and not isinstance(active_job, str):
            raise LocalRuntimeError(
                LOCAL_RUNTIME_PROTOCOL_MISMATCH, "runtime active job is invalid"
            )
        return cls(
            protocol_version=protocol_version,
            runtime_version=str(payload.get("runtime_version") or ""),
            state=state,
            models=tuple(str(model) for model in models),
            capabilities=tuple(str(capability) for capability in capabilities),
            active_job=active_job,
        )


def ready_status() -> LocalRuntimeStatus:
    return LocalRuntimeStatus(
        protocol_version=PROTOCOL_VERSION,
        runtime_version=RUNTIME_VERSION,
        state=LocalRuntimeState.READY,
    )


def error_payload(code: str) -> dict[str, Any]:
    """Build a deliberately minimal error response with no user data."""

    return {"error": {"code": code}}


__all__ = [
    "LOCAL_MODELS_NOT_INSTALLED",
    "LOCAL_RUNTIME_AUTH_FAILED",
    "LOCAL_RUNTIME_CRASHED",
    "LOCAL_RUNTIME_PROTOCOL_MISMATCH",
    "LOCAL_RUNTIME_START_FAILED",
    "LOCAL_RUNTIME_UNAVAILABLE",
    "LocalRuntimeError",
    "LocalRuntimeState",
    "LocalRuntimeStatus",
    "PROTOCOL_VERSION",
    "RUNTIME_VERSION",
    "TOKEN_HEADER",
    "error_payload",
    "ready_status",
]
