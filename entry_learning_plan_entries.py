from __future__ import annotations

# Entry mixins intentionally import SDK decorator re-exports from entry_common;
# Pyright cannot resolve those attributes when lifecycle tests replace that
# module with a runtime stub.
# pyright: reportAttributeAccessIssue=false
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any

from .entry_common import (
    Err,
    Ok,
    SdkError,
    _entry_exception_error,
    asyncio,
    plugin_entry,
    ui,
)
from .learning_plan import LearningPlanError, LearningPlanService

_PREPARE_INPUT_TTL_SECONDS = 30 * 60


class _LearningPlanEntriesMixin:
    _store: Any
    _cfg: Any
    _learning_plan_service_instance: LearningPlanService
    _learning_plan_prepare_inputs_lock: threading.RLock
    _learning_plan_prepare_inputs: dict[str, dict[str, Any]]

    def _learning_plan_service(self) -> LearningPlanService:
        service = getattr(self, "_learning_plan_service_instance", None)
        if service is None:
            adaptive_loop = getattr(getattr(self, "_cfg", None), "adaptive_loop", None)
            service = LearningPlanService(
                self._store,
                max_core_topics=int(getattr(adaptive_loop, "max_core_topics", 12) or 12),
                max_prerequisite_topics=int(
                    getattr(adaptive_loop, "max_prerequisite_topics", 5) or 5
                ),
            )
            self._learning_plan_service_instance = service
        return service

    def _learning_plan_prepare_lock(self) -> threading.RLock:
        lock = getattr(self, "_learning_plan_prepare_inputs_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._learning_plan_prepare_inputs_lock = lock
        return lock

    def _register_learning_plan_prepare_input(
        self,
        analysis_job_id: str,
        source_kind: str,
        source_digest: str,
        candidates: Iterable[Mapping[str, Any]],
        unmatched_count: int = 0,
        display_title: str = "",
    ) -> None:
        """Register server-derived topic metadata for one opaque document job.

        This boundary deliberately accepts no material text and stores the
        mapping in memory only until the UI confirms creation of a draft.
        """

        job_key = str(analysis_job_id or "").strip()
        if not job_key or len(job_key) > 256:
            raise ValueError("analysis_job_id is required")
        safe_candidates = []
        for item in candidates:
            safe_candidates.append(
                {
                    "topic_id": str(item.get("topic_id") or "").strip(),
                    "role": str(item.get("role") or "core").strip(),
                    "mapping_score": item.get("mapping_score", 0.0),
                    "mapping_confidence": str(
                        item.get("mapping_confidence") or "medium"
                    ).strip(),
                    "reason_code": str(
                        item.get("reason_code") or "material_match"
                    ).strip(),
                    "required": bool(item.get("required", True)),
                }
            )
        payload = {
            "source_kind": str(source_kind or "").strip(),
            "source_digest": str(source_digest or "").strip(),
            "candidates": safe_candidates,
            "unmatched_count": max(0, int(unmatched_count or 0)),
            "display_title": str(display_title or "").strip(),
            "registered_at": time.monotonic(),
        }
        with self._learning_plan_prepare_lock():
            cache = getattr(self, "_learning_plan_prepare_inputs", None)
            if not isinstance(cache, dict):
                cache = {}
                self._learning_plan_prepare_inputs = cache
            cutoff = time.monotonic() - _PREPARE_INPUT_TTL_SECONDS
            for key, existing in list(cache.items()):
                if float(existing.get("registered_at") or 0.0) < cutoff:
                    cache.pop(key, None)
            cache[job_key] = payload

    def _material_learning_plans_enabled(self) -> bool:
        adaptive_loop = getattr(getattr(self, "_cfg", None), "adaptive_loop", None)
        return bool(getattr(adaptive_loop, "material_learning_plans_enabled", False))

    @ui.action()
    @plugin_entry(
        id="study_learning_plan_prepare",
        name="Prepare Material Learning Plan",
        description="Create a draft from a server-derived material topic mapping.",
        input_schema={
            "type": "object",
            "properties": {"analysis_job_id": {"type": "string"}},
            "required": ["analysis_job_id"],
            "additionalProperties": False,
        },
        timeout=30.0,
        llm_result_fields=["plan"],
    )
    async def study_learning_plan_prepare(self, analysis_job_id: str, **_):
        if not self._material_learning_plans_enabled():
            return Err(SdkError("material learning plans are disabled", code="FEATURE_DISABLED"))
        job_key = str(analysis_job_id or "").strip()
        with self._learning_plan_prepare_lock():
            cache = getattr(self, "_learning_plan_prepare_inputs", {})
            prepared = cache.get(job_key) if isinstance(cache, dict) else None
            if prepared is not None and (
                time.monotonic() - float(prepared.get("registered_at") or 0.0)
                > _PREPARE_INPUT_TTL_SECONDS
            ):
                cache.pop(job_key, None)
                prepared = None
        if prepared is None:
            return Err(
                SdkError(
                    "material topic mapping is unavailable or expired",
                    code="LEARNING_PLAN_PREPARE_INPUT_UNAVAILABLE",
                )
            )
        try:
            plan = await asyncio.to_thread(
                self._learning_plan_service().create_draft,
                prepared["source_kind"],
                prepared["source_digest"],
                prepared["candidates"],
                prepared["unmatched_count"],
                prepared["display_title"],
            )
            with self._learning_plan_prepare_lock():
                cache.pop(job_key, None)
            return Ok({"plan": plan})
        except LearningPlanError as exc:
            return Err(SdkError(str(exc), code=exc.code))
        except Exception as exc:
            return _entry_exception_error(self, exc, operation="study_learning_plan_prepare")

    @ui.action()
    @plugin_entry(
        id="study_learning_plan_activate",
        name="Activate Learning Plan",
        description="Confirm the server-proposed topics and activate a draft learning plan.",
        input_schema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "revision": {"type": "integer"},
                "accepted_topic_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 17,
                },
            },
            "required": ["plan_id", "revision", "accepted_topic_ids"],
            "additionalProperties": False,
        },
        timeout=30.0,
        llm_result_fields=["plan"],
    )
    async def study_learning_plan_activate(
        self,
        plan_id: str,
        revision: int,
        accepted_topic_ids: list[str] | None = None,
        **_,
    ):
        if not self._material_learning_plans_enabled():
            return Err(SdkError("material learning plans are disabled", code="FEATURE_DISABLED"))
        try:
            plan = await asyncio.to_thread(
                self._learning_plan_service().activate,
                plan_id,
                revision,
                accepted_topic_ids or [],
            )
            return Ok({"plan": await asyncio.to_thread(self._learning_plan_service().status, plan["id"])})
        except LearningPlanError as exc:
            return Err(SdkError(str(exc), code=exc.code))
        except Exception as exc:
            return _entry_exception_error(self, exc, operation="study_learning_plan_activate")

    @ui.action()
    @plugin_entry(
        id="study_learning_plan_status",
        name="Get Learning Plan Status",
        description="Return live progress derived from mastery, wrong questions, and FSRS.",
        input_schema={
            "type": "object",
            "properties": {"plan_id": {"type": "string", "default": ""}},
            "additionalProperties": False,
        },
        timeout=30.0,
        llm_result_fields=["active", "plan"],
    )
    async def study_learning_plan_status(self, plan_id: str = "", **_):
        try:
            plan = await asyncio.to_thread(
                self._learning_plan_service().status, plan_id or None
            )
            return Ok({"active": plan["status"] == "active", "plan": plan})
        except LearningPlanError as exc:
            if exc.code == "LEARNING_PLAN_NOT_FOUND" and not plan_id:
                return Ok({"active": False, "plan": {}})
            return Err(SdkError(str(exc), code=exc.code))
        except Exception as exc:
            return _entry_exception_error(self, exc, operation="study_learning_plan_status")

    @ui.action()
    @plugin_entry(
        id="study_learning_plan_list",
        name="List Learning Plans",
        description="List persisted learning-plan metadata without source material.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "default": ""},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        timeout=30.0,
        llm_result_fields=["plans"],
    )
    async def study_learning_plan_list(self, status: str = "", limit: int = 50, **_):
        try:
            plans = await asyncio.to_thread(
                self._learning_plan_service().list,
                status=status or None,
                limit=max(1, min(500, int(limit))),
            )
            return Ok({"plans": plans})
        except LearningPlanError as exc:
            return Err(SdkError(str(exc), code=exc.code))
        except Exception as exc:
            return _entry_exception_error(self, exc, operation="study_learning_plan_list")

    async def _change_learning_plan_status(
        self, operation: str, plan_id: str, revision: int | None
    ):
        try:
            method = getattr(self._learning_plan_service(), operation)
            plan = await asyncio.to_thread(method, plan_id, revision)
            return Ok({"plan": plan})
        except LearningPlanError as exc:
            return Err(SdkError(str(exc), code=exc.code))
        except Exception as exc:
            return _entry_exception_error(self, exc, operation=f"study_learning_plan_{operation}")

    @ui.action()
    @plugin_entry(
        id="study_learning_plan_pause",
        name="Pause Learning Plan",
        description="Pause automatic selection without deleting learning facts.",
        input_schema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "revision": {"type": "integer"},
            },
            "required": ["plan_id"],
            "additionalProperties": False,
        },
        timeout=30.0,
        llm_result_fields=["plan"],
    )
    async def study_learning_plan_pause(
        self, plan_id: str, revision: int | None = None, **_
    ):
        return await self._change_learning_plan_status("pause", plan_id, revision)

    @ui.action()
    @plugin_entry(
        id="study_learning_plan_cancel",
        name="Cancel Learning Plan",
        description="Cancel orchestration without deleting mastery, wrong questions, or review history.",
        input_schema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "revision": {"type": "integer"},
            },
            "required": ["plan_id"],
            "additionalProperties": False,
        },
        timeout=30.0,
        llm_result_fields=["plan"],
    )
    async def study_learning_plan_cancel(
        self, plan_id: str, revision: int | None = None, **_
    ):
        return await self._change_learning_plan_status("cancel", plan_id, revision)
