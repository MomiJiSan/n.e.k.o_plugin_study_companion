"""Route study inference to either the hosted gateway or the local runtime.

The router is deliberately narrow: it owns the mode decision but does not
perform any fallback.  In particular, a local-runtime failure must remain a
local-runtime failure so callers never send a question to an API implicitly.
"""

from __future__ import annotations

from typing import Any, Final

from .local_runtime_client import LocalRuntimeClient
from .local_runtime_supervisor import LocalRuntimeSupervisor
from .models import StudyConfig
from .study_model_gateway import (
    AgentQuotaReservation,
    StudyModelGateway,
    StudyModelResult,
    StudyModelRuntimeSnapshot,
)

# The local-runtime infrastructure remains in the plugin, but the user-facing
# product is intentionally paused until a future model decision re-enables it.
LOCAL_MODELS_PRODUCT_ENABLED: Final = False


class StudyInferenceRouter:
    """Choose the explicitly configured study-inference transport.

    ``local_models_enabled`` is intentionally opt-in and defaults to false for
    configurations saved before local inference existed.  The API gateway is
    only touched in API mode; local calls never reserve API quota or resolve
    hosted-model credentials.
    """

    def __init__(
        self,
        *,
        logger: Any,
        config: StudyConfig,
        api_gateway: StudyModelGateway,
        local_client: LocalRuntimeClient | None = None,
    ) -> None:
        self._logger = logger
        self._config = config
        self._api_gateway = api_gateway
        self._local_client = local_client or LocalRuntimeClient(
            LocalRuntimeSupervisor()
        )

    def update_config(self, config: StudyConfig) -> None:
        """Use the latest mode without starting or stopping the runtime."""

        self._config = config

    @property
    def local_models_enabled(self) -> bool:
        """Return whether the paused local-model product is available."""

        return LOCAL_MODELS_PRODUCT_ENABLED and bool(
            getattr(self._config, "local_models_enabled", False)
        )

    async def call(
        self,
        messages: list[dict[str, Any]],
        *,
        operation: str,
        deadline: float,
        runtime: StudyModelRuntimeSnapshot | None = None,
        quota_reservation: AgentQuotaReservation | None = None,
    ) -> StudyModelResult:
        """Call exactly one configured transport; never perform a fallback."""

        if self.local_models_enabled:
            # The local client deliberately has no API gateway reference.  Any
            # LocalRuntimeError is allowed to propagate to the caller unchanged.
            return await self._local_client.call(
                messages, operation=operation, deadline=deadline
            )

        call_kwargs: dict[str, Any] = {
            "operation": operation,
            "deadline": deadline,
            "runtime": runtime,
        }
        if quota_reservation is not None:
            call_kwargs["quota_reservation"] = quota_reservation
        return await self._api_gateway.call(messages, **call_kwargs)

    async def describe_local_runtime(self) -> dict[str, object]:
        """Return local-runtime diagnostics without starting a child process.

        A disabled local mode is represented as a stopped runtime rather than
        an error.  This keeps the settings page informative while preserving
        the important invariant that merely opening it does not create a
        process or load a model.
        """

        if not self.local_models_enabled:
            return {
                "state": "stopped",
                "models": [],
                "capabilities": [],
                "active_job": None,
                "error_code": "",
            }
        try:
            status = await self._local_client.status()
        except Exception as exc:
            return {
                "state": "unavailable",
                "models": [],
                "capabilities": [],
                "active_job": None,
                "error_code": str(
                    getattr(exc, "diagnostic", "")
                    or getattr(exc, "code", "")
                    or "local_runtime_unavailable"
                ),
            }
        payload = status.to_payload()
        return {
            "state": str(payload.get("state") or "unavailable"),
            "models": list(payload.get("models") or []),
            "capabilities": list(payload.get("capabilities") or []),
            "active_job": payload.get("active_job"),
            "error_code": "",
        }

    async def shutdown(self) -> None:
        """Release the local-runtime child process when the plugin exits."""

        await self._local_client.shutdown()


__all__ = ["StudyInferenceRouter"]
