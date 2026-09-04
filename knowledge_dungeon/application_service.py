"""C0 application boundary for a future authenticated local bridge."""

from __future__ import annotations

from threading import RLock
from typing import Any, Mapping

from .available_actions import command_for_action_id, resolve_available_action
from .bridge_contracts import (
    APPLICATION_SERVICE_VERSION,
    BRIDGE_PROTOCOL_VERSION,
    CALCULUS_SCENARIO,
    CALCULUS_SCENARIO_ID,
    PUBLIC_PROJECTION_VERSION,
    BridgeContractError,
    CreateRunRequest,
    PerformActionRequest,
    TrustedInvocationContext,
    require_identifier,
    require_trusted_context,
)
from .contracts import PROTOCOL_VERSION, canonical_sha256
from .engine import KnowledgeDungeonEngine
from .fixtures import calculus_card_projection
from .public_projection import project_public_run

_AUTHORITY_NAMESPACE = "study-companion.knowledge-dungeon.v0.2-c0"


class ApplicationServiceError(RuntimeError):
    """Stable public service failure with no engine or persistence internals."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class KnowledgeDungeonApplicationService:
    """Expose only bootstrap, run creation/read, and action-ID-only execution."""

    def __init__(
        self,
        engine: KnowledgeDungeonEngine | None = None,
        *,
        authority_namespace: str = _AUTHORITY_NAMESPACE,
    ) -> None:
        self._engine = engine or KnowledgeDungeonEngine()
        self._authority_namespace = require_identifier(authority_namespace, "authority_namespace")
        self._lock = RLock()

    def bootstrap(self, context: TrustedInvocationContext) -> dict[str, Any]:
        require_trusted_context(context)
        return {
            "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
            "engine_protocol_version": PROTOCOL_VERSION,
            "public_projection_version": PUBLIC_PROJECTION_VERSION,
            "application_service_version": APPLICATION_SERVICE_VERSION,
            "scenarios": [CALCULUS_SCENARIO.to_dict()],
            "capabilities": {
                "action_id_only_submission": True,
                "server_generated_run_id": True,
                "server_generated_command_id": True,
                "server_generated_seed": True,
                "real_learning_data": False,
                "learning_writeback": False,
            },
        }

    def create_run(
        self,
        context: TrustedInvocationContext,
        raw_request: CreateRunRequest | Mapping[str, Any],
    ) -> dict[str, Any]:
        trusted = require_trusted_context(context)
        request = (
            raw_request
            if isinstance(raw_request, CreateRunRequest)
            else CreateRunRequest.from_mapping(raw_request)
        )
        with self._lock:
            run_id, seed = self._derive_run_identity(trusted, request)
            projection = calculus_card_projection().to_dict()
            command_id = self._create_command_id(trusted, request, run_id)
            response = self._engine.dispatch(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "command_id": command_id,
                    "run_id": run_id,
                    "expected_state_version": 0,
                    "intent": "start_run",
                    "payload": {
                        "seed": seed,
                        "map_subject_id": CALCULUS_SCENARIO.map_subject_id,
                        "cards": projection["cards"],
                        "versions": projection["versions"],
                    },
                }
            )
            self._require_accepted(response)
            state = self._engine.get_state(run_id)
            if state is None:
                raise ApplicationServiceError("authority_failure", "created run is unavailable")
            return project_public_run(
                state,
                scenario_id=request.scenario_id,
                events=self._response_events(response),
            )

    def get_run(self, context: TrustedInvocationContext, run_id: str) -> dict[str, Any]:
        require_trusted_context(context)
        canonical_run_id = require_identifier(run_id, "run_id")
        with self._lock:
            state = self._engine.get_state(canonical_run_id)
            if state is None:
                raise ApplicationServiceError("run_not_found", "knowledge dungeon run was not found")
            return project_public_run(state, scenario_id=CALCULUS_SCENARIO_ID)

    def perform_action(
        self,
        context: TrustedInvocationContext,
        run_id: str,
        raw_request: PerformActionRequest | Mapping[str, Any],
    ) -> dict[str, Any]:
        trusted = require_trusted_context(context)
        canonical_run_id = require_identifier(run_id, "run_id")
        request = (
            raw_request
            if isinstance(raw_request, PerformActionRequest)
            else PerformActionRequest.from_mapping(raw_request)
        )
        with self._lock:
            state = self._engine.get_state(canonical_run_id)
            if state is None:
                raise ApplicationServiceError("run_not_found", "knowledge dungeon run was not found")
            intent, payload = command_for_action_id(request.action_id)
            if (
                state.state_version == request.expected_state_version
                and resolve_available_action(state, request.action_id) is None
            ):
                raise ApplicationServiceError(
                    "action_unavailable",
                    "action is stale, unknown, or unavailable in the current state",
                )
            command_id = self._action_command_id(trusted, canonical_run_id, request.request_id)
            response = self._engine.dispatch(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "command_id": command_id,
                    "run_id": canonical_run_id,
                    "expected_state_version": request.expected_state_version,
                    "intent": intent,
                    "payload": payload,
                }
            )
            self._require_accepted(response)
            next_state = self._engine.get_state(canonical_run_id)
            if next_state is None:
                raise ApplicationServiceError("authority_failure", "updated run is unavailable")
            return project_public_run(
                next_state,
                scenario_id=CALCULUS_SCENARIO_ID,
                events=self._response_events(response),
            )

    def _derive_run_identity(
        self,
        context: TrustedInvocationContext,
        request: CreateRunRequest,
    ) -> tuple[str, int]:
        digest = canonical_sha256(
            {
                "namespace": self._authority_namespace,
                "client_id": context.client_id,
                "request_id": request.request_id,
                "subject_id": request.subject_id,
                "scenario_id": request.scenario_id,
                "bridge_protocol_version": request.bridge_protocol_version,
                "application_service_version": APPLICATION_SERVICE_VERSION,
            }
        )
        return f"run-{digest[:24]}", int(digest[24:40], 16)

    def _create_command_id(
        self,
        context: TrustedInvocationContext,
        request: CreateRunRequest,
        run_id: str,
    ) -> str:
        digest = canonical_sha256(
            {
                "namespace": self._authority_namespace,
                "client_id": context.client_id,
                "request_id": request.request_id,
                "run_id": run_id,
                "subject_id": request.subject_id,
                "scenario_id": request.scenario_id,
                "bridge_protocol_version": request.bridge_protocol_version,
                "application_service_version": APPLICATION_SERVICE_VERSION,
            }
        )
        return f"command-{digest[:24]}"

    def _action_command_id(
        self,
        context: TrustedInvocationContext,
        run_id: str,
        request_id: str,
    ) -> str:
        digest = canonical_sha256(
            {
                "namespace": self._authority_namespace,
                "client_id": context.client_id,
                "run_id": run_id,
                "request_id": request_id,
            }
        )
        return f"command-{digest[:24]}"

    @staticmethod
    def _response_events(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        events = response.get("events")
        if not isinstance(events, list) or not all(isinstance(event, Mapping) for event in events):
            raise ApplicationServiceError("authority_failure", "authority returned invalid events")
        return events

    @staticmethod
    def _require_accepted(response: Mapping[str, Any]) -> None:
        if response.get("accepted") is True:
            return
        error = response.get("error")
        if isinstance(error, Mapping):
            code = str(error.get("code") or "authority_rejected")
            message = str(error.get("message") or "authority rejected the operation")
        else:
            code = "authority_rejected"
            message = "authority rejected the operation"
        raise ApplicationServiceError(code, message)


__all__ = [
    "ApplicationServiceError",
    "BridgeContractError",
    "KnowledgeDungeonApplicationService",
]
