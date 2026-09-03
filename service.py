from __future__ import annotations

import copy
import importlib.util
import sys
from typing import Any

from .models import (
    RAPIDOCR_LANG_TYPE_INVALID_DIAGNOSTIC,
    OcrSnapshot,
    StudyConfig,
    StudyState,
    TutorReply,
    normalize_rapidocr_lang_type,
    public_current_question_payload,
    public_screen_classification_payload,
)


def build_status_payload(
    *,
    config: StudyConfig,
    state: StudyState,
    history: list[dict[str, Any]] | None = None,
    knowledge: dict[str, Any] | None = None,
    is_first_run: bool = False,
) -> dict[str, Any]:
    knowledge_payload = knowledge or {}
    state.clear_expired_ocr_session()
    current_question = public_current_question_payload(state.current_question)
    if not current_question:
        current_question_state = "none"
        can_evaluate_current_question = False
    elif bool((state.current_question or {}).get("attempt_evaluated")):
        current_question_state = "evaluated"
        can_evaluate_current_question = False
    else:
        current_question_state = "pending"
        can_evaluate_current_question = bool(current_question.get("question_id") and current_question.get("attempt_id"))
    return {
        "status": state.status,
        "is_first_run": bool(is_first_run),
        "mode": config.mode,
        "default_mode": config.default_mode,
        "active_mode": state.active_mode,
        "mode_started_at": state.mode_started_at,
        "recent_mode_switches": copy.deepcopy(state.recent_mode_switches),
        "suggestion_cooldowns": copy.deepcopy(state.suggestion_cooldowns),
        "session_suggestions": copy.deepcopy(state.session_suggestions),
        "mode_lock_until": state.mode_lock_until,
        "last_error": state.last_error,
        "current_question": current_question,
        "current_question_state": current_question_state,
        "can_evaluate_current_question": can_evaluate_current_question,
        "last_started_at": state.last_started_at,
        "has_ocr_text": bool(state.last_ocr_text.strip()),
        "last_ocr_at": state.last_ocr_at,
        "last_captured_question_id": state.last_captured_question_id,
        "screen_classification": public_screen_classification_payload(state.last_screen_classification),
        "recent_screen_classifications": [
            public_screen_classification_payload(item)
            for item in state.recent_screen_classifications
            if isinstance(item, dict)
        ],
        "last_answer_evaluation": copy.deepcopy(state.last_answer_evaluation),
        "session_summary_seed": copy.deepcopy(state.session_summary_seed),
        "recent_learning_events": copy.deepcopy(state.recent_learning_events),
        "last_question_at": state.last_question_at,
        "last_answer_evaluated_at": state.last_answer_evaluated_at,
        "last_session_summary": state.last_session_summary,
        "last_session_summary_at": state.last_session_summary_at,
        "checkpoint": copy.deepcopy(state.checkpoint),
        "dependencies": copy.deepcopy(state.dependency_status),
        "knowledge_summary": copy.deepcopy(knowledge_payload.get("knowledge_summary") or {}),
        "knowledge_quality_summary": copy.deepcopy(knowledge_payload.get("knowledge_quality_summary") or {}),
        "anonymous_knowledge_stats_summary": copy.deepcopy(
            knowledge_payload.get("anonymous_knowledge_stats_summary") or {}
        ),
        "habit": copy.deepcopy(knowledge_payload.get("habit") or {}),
        "review_queue": copy.deepcopy(knowledge_payload.get("review_queue") or []),
        "memory_deck": copy.deepcopy(knowledge_payload.get("memory_deck") or {}),
        "weak_topics": copy.deepcopy(knowledge_payload.get("weak_topics") or []),
        "mastery_overview": copy.deepcopy(knowledge_payload.get("mastery_overview") or []),
        "config": config.to_dict(),
        "history": list(history or []),
    }


def build_dependency_status(config: StudyConfig) -> dict[str, Any]:
    rapidocr = _inspect_rapidocr(config)
    dxcam = _inspect_dxcam()
    missing = []
    if (
        rapidocr.get("installed") is False
        and rapidocr.get("can_download_models") is True
        and str(rapidocr.get("detail") or "").strip().lower()
        == "missing_model_files"
    ):
        missing.append("rapidocr_models")
    return {
        "rapidocr": rapidocr,
        "dxcam": dxcam,
        "missing_installable": missing,
        "ocr_readiness": _build_ocr_readiness(
            config=config,
            rapidocr=rapidocr,
            dxcam=dxcam,
        ),
    }


def _build_ocr_readiness(
    *,
    config: StudyConfig,
    rapidocr: dict[str, Any],
    dxcam: dict[str, Any],
) -> dict[str, Any]:
    enabled = bool(config.ocr_enabled)
    selected_backend = "rapidocr"
    capture_backend = str(config.ocr_capture_backend or "auto").strip().lower()
    if capture_backend == "auto":
        capture_backend = "dxcam"
    if capture_backend == "dxcam":
        capture_ready = dxcam.get("installed") is True
    elif capture_backend in {"mss", "pyautogui"}:
        capture_ready = importlib.util.find_spec(capture_backend) is not None
    elif capture_backend == "printwindow":
        capture_ready = sys.platform == "win32"
    else:
        capture_ready = False

    selected_backend_ready = rapidocr.get("installed") is True
    ready = enabled and selected_backend_ready and capture_ready

    if not enabled:
        diagnostic = "ocr_disabled"
    elif not selected_backend_ready:
        detail = str(rapidocr.get("detail") or "").strip().lower()
        diagnostic = {
            "invalid_language": RAPIDOCR_LANG_TYPE_INVALID_DIAGNOSTIC,
            "missing_model_files": "rapidocr_models_missing",
        }.get(detail, "rapidocr_runtime_missing")
    elif capture_backend not in {"dxcam", "mss", "pyautogui", "printwindow"}:
        diagnostic = "unsupported_capture_backend"
    elif not capture_ready:
        diagnostic = "capture_dependency_missing"
    else:
        diagnostic = "ready"

    return {
        "enabled": enabled,
        "selected_backend": selected_backend,
        "selected_backend_ready": selected_backend_ready,
        "capture_ready": capture_ready,
        "ready": ready,
        "diagnostic": diagnostic,
    }


def _inspect_rapidocr(config: StudyConfig) -> dict[str, Any]:
    lang_type, input_diagnostic = normalize_rapidocr_lang_type(
        config.rapidocr_lang_type
    )
    diagnostic = str(
        getattr(config, "_rapidocr_lang_type_diagnostic", "")
        or input_diagnostic
    ).strip()
    if diagnostic:
        return {
            "install_supported": False,
            "installed": False,
            "can_install": False,
            "detected_path": "",
            "target_dir": "",
            "detail": "invalid_language",
            "runtime_error": diagnostic,
            "lang_type": lang_type,
        }

    from plugin.plugins._shared.rapidocr.rapidocr_support import (
        inspect_rapidocr_installation,
    )

    return inspect_rapidocr_installation(
        install_target_dir_raw=config.rapidocr_install_target_dir,
        engine_type=config.rapidocr_engine_type,
        lang_type=lang_type,
        model_type=config.rapidocr_model_type,
        ocr_version=config.rapidocr_ocr_version,
        plugin_id="study_companion",
    )
def _inspect_dxcam() -> dict[str, Any]:
    supported = sys.platform == "win32"
    spec = importlib.util.find_spec("dxcam") if supported else None
    origin = str(getattr(spec, "origin", "") or "") if spec is not None else ""
    installed = bool(origin)
    return {
        "install_supported": supported,
        "installed": installed,
        "can_install": False,
        "detected_path": origin,
        "package_name": "dxcam",
        "target_dir": "current_python_environment",
        "detail": "installed" if installed else ("missing" if supported else "unsupported_platform"),
        "runtime_error": "",
    }


def build_tutor_payload(reply: Any) -> dict[str, Any]:
    normalized_reply = (
        reply
        if isinstance(reply, TutorReply)
        else TutorReply(
            operation=str(getattr(reply, "operation", "") or ""),
            input_text=str(getattr(reply, "input_text", "") or ""),
            reply=str(getattr(reply, "reply", "") or ""),
            payload=(
                dict(getattr(reply, "payload", {}) or {})
                if isinstance(getattr(reply, "payload", {}), dict)
                else {}
            ),
            degraded=bool(getattr(reply, "degraded", False)),
            diagnostic=str(getattr(reply, "diagnostic", "") or ""),
            created_at=str(getattr(reply, "created_at", "") or ""),
        )
    )
    payload = normalized_reply.to_dict()
    if normalized_reply.payload:
        payload.update(copy.deepcopy(normalized_reply.payload))
    if not payload.get("summary"):
        payload["summary"] = normalized_reply.reply
    return payload


def build_explain_payload(reply: TutorReply) -> dict[str, Any]:
    return build_tutor_payload(reply)


def build_ocr_payload(snapshot: OcrSnapshot) -> dict[str, Any]:
    payload = snapshot.to_dict()
    payload["summary"] = snapshot.text or snapshot.status
    return payload
