from __future__ import annotations

from .constants import LLM_OPERATION_QUESTION_VALIDATE
from .tutor_llm_agent_common import Any, SdkError, TutorReply, _as_str


async def question_validate(self, *, context: dict[str, Any]) -> TutorReply:
    operation_context = {
        **dict(context or {}),
        "operation": LLM_OPERATION_QUESTION_VALIDATE,
        "language": self._config.language,
    }
    return await self._invoke_structured_operation(
        LLM_OPERATION_QUESTION_VALIDATE, operation_context
    )


def _normalize_question_validation(
    self, raw: dict[str, Any], _context: dict[str, Any]
) -> dict[str, Any]:
    for key in ("relevant", "answer_supported", "retry"):
        if not isinstance(raw.get(key), bool):
            raise SdkError(f"question validation field {key} must be boolean")
    reason = _as_str(raw.get("reason")).strip()
    if not reason:
        raise SdkError("question validation reason is required")
    relevant = bool(raw["relevant"])
    answer_supported = bool(raw["answer_supported"])
    retry = bool(raw["retry"])
    if retry != (not relevant or not answer_supported):
        raise SdkError("question validation fields are inconsistent")
    return {
        "relevant": relevant,
        "answer_supported": answer_supported,
        "retry": retry,
        "reason": reason[:400],
    }


def _fallback_question_validation(self, _context: dict[str, Any]) -> dict[str, Any]:
    return {
        "relevant": False,
        "answer_supported": False,
        "retry": True,
        "reason": "Question validation was unavailable.",
    }
