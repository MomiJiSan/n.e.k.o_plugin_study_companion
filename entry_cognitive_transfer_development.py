"""Developer-only entry orchestration; all question delivery stays in its owner."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from .store_cognitive_development import _PreparationBlocked
from .store_cognitive_transfer_development import CODE, CONFIRMATION, DEV_ENV, TOPIC, preview_transfer_retest
from .tutor_lifecycle import release_question_lifecycle, reserve_question_lifecycle


async def retest_transfer(
    owner: Any, *, topic_id: str, hypothesis_code: str, expected_source_attempt_id: str,
    apply: bool, confirmation: str,
) -> dict[str, Any]:
    def blocked(reason: str) -> dict[str, Any]:
        return {"enabled": reason != "dev_tools_disabled", "status": "blocked", "reason_code": reason}

    if os.environ.get(DEV_ENV) != "1":
        return blocked("dev_tools_disabled")
    if topic_id != TOPIC or hypothesis_code != CODE or not isinstance(apply, bool):
        return blocked("unsupported_target")
    if apply and confirmation != CONFIRMATION:
        return blocked("confirmation_required")
    if apply and not expected_source_attempt_id:
        return blocked("source_attempt_mismatch")
    operation = "cognitive_dev_retest_transfer"
    if await reserve_question_lifecycle(owner, operation):
        return blocked("operation_busy")
    try:
        cognitive = getattr(getattr(owner, "_cfg", None), "cognitive", None)
        if not (
            getattr(cognitive, "projection_enabled", False) is True
            and getattr(cognitive, "read_mode", "") == "active"
            and getattr(cognitive, "intent_policy", "") == "on"
            and getattr(cognitive, "retention_enabled", False) is True
        ):
            return blocked("retention_disabled")
        store = getattr(owner, "_store", None)
        if store is None:
            return blocked("legacy_transfer_required")
        preview = await asyncio.to_thread(preview_transfer_retest, store, expected_source_attempt_id)
        if preview["status"] != "ready":
            return preview
        async with owner._lock:
            current = dict(owner._state.current_question or {})
        if current and not current.get("attempt_evaluated"):
            return blocked("question_active")
        if not apply:
            return preview
        context = await asyncio.to_thread(owner._build_targeted_question_context)
        if context.get("selected_topic_id") != TOPIC or context.get("selection_reason") in {
            "blocked_diagnostic", "no_data",
        }:
            return blocked("selection_conflict")
        context = {**context, "_development_transfer_source": expected_source_attempt_id}
        payload = await owner._generate_question_payload(
            source_text="Generate the selected development transfer retest.",
            topic=TOPIC, source="targeted_question", targeted_context=context,
            lifecycle_reserved=True,
        )
        return {"enabled": True, "status": "generated", "reason_code": "",
                "source_attempt_id": expected_source_attempt_id,
                "question_id": str(payload.get("question_id") or ""),
                "attempt_id": str(payload.get("attempt_id") or ""),
                "learning_intent": "transfer_check"}
    except _PreparationBlocked as exc:
        return blocked(exc.reason_code)
    except Exception:
        return blocked("generation_failed")
    finally:
        await release_question_lifecycle(owner, operation)
