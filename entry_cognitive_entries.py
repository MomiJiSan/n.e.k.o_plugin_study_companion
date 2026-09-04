from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .adaptive_learning.cognitive_catalog import COGNITIVE_CATALOG_V1
from .adaptive_learning.cognitive_state import CognitiveStateReader
from .entry_common import (
    Ok,
    SdkError,
    _entry_exception_error,
    asyncio,
    plugin_entry,
    tr,
    ui,
)
from .tutor_lifecycle import (
    release_question_lifecycle,
    reserve_question_lifecycle,
)

_CONTROL_ACTIONS = frozenset({"dismiss", "suppress", "delete", "restore"})
_BLOCKING_CONTROL_ACTIONS = frozenset({"dismiss", "suppress", "delete"})
_MAX_EVIDENCE_ITEMS = 20
_MAX_SUPPRESSION_SECONDS = 24 * 60 * 60


def _cognitive_config(owner: object) -> object | None:
    return getattr(getattr(owner, "_cfg", None), "cognitive", None)


def _config_value(config: object | None, name: str, default: object) -> object:
    if isinstance(config, Mapping):
        return config.get(name, default)
    return getattr(config, name, default) if config is not None else default


def _cognitive_catalog(owner: object):
    tracker = getattr(owner, "_knowledge_tracker", None)
    return getattr(tracker, "cognitive_catalog", COGNITIVE_CATALOG_V1)


def _ui_enabled(owner: object, topic_id: str) -> bool:
    config = _cognitive_config(owner)
    if not (
        _config_value(config, "projection_enabled", False) is True
        and _config_value(config, "ui_enabled", False) is True
        and str(_config_value(config, "read_mode", "off")).strip().lower() in {"shadow", "active"}
    ):
        return False
    tracker = getattr(owner, "_knowledge_tracker", None)
    topic_enabled = getattr(tracker, "is_cognitive_topic_enabled", None)
    if callable(topic_enabled):
        return bool(topic_enabled(topic_id))
    catalog = _cognitive_catalog(owner)
    supports_topic = getattr(catalog, "supports_topic", None)
    if callable(supports_topic):
        return bool(supports_topic(topic_id))
    canonical_topic_id = getattr(catalog, "canonical_topic_id", None)
    return bool(
        callable(canonical_topic_id) and canonical_topic_id(topic_id) is not None
    )


def _empty_payload(topic_id: str, *, status: str, enabled: bool) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "status": status,
        "topic_id": str(topic_id or "").strip(),
        "hypotheses": [],
        "restorable_controls": [],
    }


def _read_cognitive_state(owner: object, topic_id: str) -> object:
    tracker = getattr(owner, "_knowledge_tracker", None)
    tracker_reader = getattr(tracker, "read_cognitive_state", None)
    if callable(tracker_reader):
        return tracker_reader(topic_id)
    store = getattr(owner, "_store", None)
    if store is None:
        raise RuntimeError("cognitive state store unavailable")
    config = _cognitive_config(owner)
    model_version = str(_config_value(config, "model_version", "")).strip()
    if not model_version:
        raise RuntimeError("cognitive model version unavailable")
    return CognitiveStateReader(store, model_version=model_version).read_topic(topic_id)


def _view_value(view: object, name: str, default: object = None) -> object:
    if isinstance(view, Mapping):
        return view.get(name, default)
    return getattr(view, name, default)


def _hypothesis_value(hypothesis: object, name: str, default: object = None) -> object:
    if isinstance(hypothesis, Mapping):
        return hypothesis.get(name, default)
    return getattr(hypothesis, name, default)


def _hypothesis_ref_value(hypothesis: object, name: str) -> object:
    direct = _hypothesis_value(hypothesis, name)
    if direct not in (None, ""):
        return direct
    ref = _hypothesis_value(hypothesis, "ref")
    return _hypothesis_value(ref, name)


def _supported_hypotheses(view: object) -> tuple[object, ...]:
    if _view_value(view, "usable", False) is not True:
        return ()
    raw = _view_value(view, "hypotheses", ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in raw if str(_hypothesis_value(item, "evidence_status", "")).strip() == "supported")


def _safe_evidence_item(row: Mapping[str, Any]) -> dict[str, str]:
    direction = str(row.get("direction") or "").strip()
    return {
        "evidence_id": str(row.get("evidence_id") or "").strip(),
        "direction": direction if direction in {"support", "counter"} else "",
        "evidence_span": str(row.get("evidence_span") or "").strip(),
        "source_kind": str(row.get("source_kind") or "").strip(),
        "created_at": str(row.get("created_at") or "").strip(),
    }


def _latest_restorable_controls(
    rows: Sequence[Mapping[str, Any]], *, now: datetime, catalog: object
) -> list[dict[str, str]]:
    latest: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        code = str(row.get("hypothesis_code") or "").strip()
        getter = getattr(catalog, "get", None)
        if code and code not in latest and callable(getter) and getter(
            str(row.get("topic_id") or "").strip(), code
        ):
            latest[code] = row
    result: list[dict[str, str]] = []
    for code, row in latest.items():
        action = str(row.get("action") or "").strip().lower()
        if action not in _BLOCKING_CONTROL_ACTIONS:
            continue
        expires_at = str(row.get("expires_at") or "").strip()
        if action == "suppress" and expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry.astimezone(timezone.utc) <= now:
                    continue
            except ValueError:
                continue
        result.append(
            {
                "topic_id": str(row.get("topic_id") or "").strip(),
                "hypothesis_code": code,
                "action": action,
                "expires_at": expires_at,
            }
        )
    return result


def _normalize_suppression_expiry(value: str, *, now: datetime) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("expires_at is required for suppress")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("expires_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("expires_at must include a timezone")
    expiry = parsed.astimezone(timezone.utc)
    seconds = (expiry - now).total_seconds()
    if seconds <= 0 or seconds > _MAX_SUPPRESSION_SECONDS:
        raise ValueError("suppress expiry must be within the next 24 hours")
    return expiry.isoformat().replace("+00:00", "Z")


async def _abandon_current_intervention(
    owner: object,
    *,
    topic_id: str,
    hypothesis_code: str,
    action: str,
) -> bool:
    hook = getattr(owner, "_abandon_current_cognitive_intervention", None)
    if not callable(hook):
        intent_mode = str(_config_value(_cognitive_config(owner), "intent_policy", "off")).strip().lower()
        if intent_mode == "on":
            raise RuntimeError("active cognitive user controls require abandonment support")
        return False
    result = hook(
        topic_id=topic_id,
        hypothesis_code=hypothesis_code,
        action=action,
    )
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, Mapping):
        return result.get("abandoned") is True
    return result is True


class _CognitiveEntriesMixin:
    @ui.action()
    @plugin_entry(
        id="study_cognitive_evidence",
        name=tr(
            "entries.cognitive_evidence.name",
            default="Study Cognitive Evidence",
        ),
        description=tr(
            "entries.cognitive_evidence.description",
            default="Return supported, user-visible cognitive evidence for the selected practice topic.",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "topic_id": {"type": "string"},
                "limit": {"type": "integer", "default": 8},
            },
            "required": ["topic_id"],
        },
        llm_result_fields=[
            "enabled",
            "status",
            "topic_id",
            "hypotheses",
            "restorable_controls",
        ],
    )
    async def study_cognitive_evidence(self, topic_id: str = "", limit: int = 8, **_):
        topic_key = str(topic_id or "").strip()
        if not topic_key or not _ui_enabled(self, topic_key):
            return Ok(_empty_payload(topic_key, status="disabled", enabled=False))
        safe_limit = max(1, min(_MAX_EVIDENCE_ITEMS, int(limit or 8)))

        def _read() -> dict[str, Any]:
            store = getattr(self, "_store", None)
            if store is None:
                return _empty_payload(topic_key, status="unavailable", enabled=True)
            try:
                catalog = _cognitive_catalog(self)
                controls_method = getattr(store, "list_cognitive_user_controls", None)
                control_rows = controls_method(topic_id=topic_key, limit=100) if callable(controls_method) else []
                restorable = _latest_restorable_controls(
                    tuple(row for row in control_rows if isinstance(row, Mapping)),
                    now=datetime.now(timezone.utc),
                    catalog=catalog,
                )
                view = _read_cognitive_state(self, topic_key)
                supported = _supported_hypotheses(view)
                if not supported:
                    payload = _empty_payload(
                        topic_key,
                        status=("empty" if _view_value(view, "usable", False) is True else "unavailable"),
                        enabled=True,
                    )
                    payload["restorable_controls"] = restorable
                    return payload
                list_evidence = getattr(store, "list_cognitive_evidence", None)
                if not callable(list_evidence):
                    return _empty_payload(topic_key, status="unavailable", enabled=True)
                hypotheses: list[dict[str, Any]] = []
                for item in supported:
                    code = str(_hypothesis_ref_value(item, "code") or "").strip()
                    hypothesis_id = str(_hypothesis_ref_value(item, "hypothesis_id") or "").strip()
                    if not code or catalog.get(topic_key, code) is None:
                        continue
                    evidence_rows = list_evidence(
                        topic_id=topic_key,
                        hypothesis_code=code,
                        limit=safe_limit,
                    )
                    evidence = [
                        _safe_evidence_item(row)
                        for row in evidence_rows
                        if isinstance(row, Mapping) and str(row.get("evidence_span") or "").strip()
                    ]
                    hypotheses.append(
                        {
                            "hypothesis_id": hypothesis_id,
                            "topic_id": topic_key,
                            "hypothesis_code": code,
                            "support_count": max(
                                0,
                                int(_hypothesis_value(item, "support_count", 0) or 0),
                            ),
                            "diagnostic_support_count": max(
                                0,
                                int(_hypothesis_value(item, "diagnostic_support_count", 0) or 0),
                            ),
                            "intervention_stage": str(
                                _hypothesis_value(item, "intervention_stage", "idle")
                                or "idle"
                            ).strip(),
                            "relapse_count": max(
                                0,
                                int(_hypothesis_value(item, "relapse_count", 0) or 0),
                            ),
                            "evidence": evidence,
                            "computed_at": str(_hypothesis_value(item, "computed_at", "") or "").strip(),
                        }
                    )
                return {
                    "enabled": True,
                    "status": "ready" if hypotheses else "empty",
                    "topic_id": topic_key,
                    "hypotheses": hypotheses,
                    "restorable_controls": restorable,
                }
            except Exception:
                return _empty_payload(topic_key, status="unavailable", enabled=True)

        return Ok(await asyncio.to_thread(_read))

    @ui.action()
    @plugin_entry(
        id="study_cognitive_control",
        name=tr(
            "entries.cognitive_control.name",
            default="Control Study Cognitive Evidence",
        ),
        description=tr(
            "entries.cognitive_control.description",
            default="Dismiss, temporarily suppress, delete, or restore one cognitive hypothesis.",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "topic_id": {"type": "string"},
                "hypothesis_code": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["dismiss", "suppress", "delete", "restore"],
                },
                "reason": {"type": "string", "default": ""},
                "expires_at": {"type": "string", "default": ""},
            },
            "required": ["topic_id", "hypothesis_code", "action"],
        },
        llm_result_fields=[
            "enabled",
            "status",
            "topic_id",
            "hypothesis_code",
            "action",
            "abandoned_current_intervention",
        ],
    )
    async def study_cognitive_control(
        self,
        topic_id: str = "",
        hypothesis_code: str = "",
        action: str = "",
        reason: str = "",
        expires_at: str = "",
        **_,
    ):
        topic_key = str(topic_id or "").strip()
        code_key = str(hypothesis_code or "").strip()
        action_key = str(action or "").strip().lower()
        if not topic_key or not _ui_enabled(self, topic_key):
            return Ok(
                {
                    "enabled": False,
                    "status": "disabled",
                    "topic_id": topic_key,
                    "hypothesis_code": code_key,
                    "action": action_key,
                    "abandoned_current_intervention": False,
                }
            )
        lifecycle_operation = "cognitive_control"
        active_operation = await reserve_question_lifecycle(
            self,
            lifecycle_operation,
        )
        if active_operation:
            code = (
                "QUESTION_GENERATION_IN_PROGRESS"
                if active_operation == "question_generation"
                else "ANSWER_EVALUATION_IN_PROGRESS"
            )
            return _entry_exception_error(
                self,
                SdkError(
                    "another study question operation is already in progress; retry shortly",
                    code=code,
                ),
                operation="study_cognitive_control",
            )
        try:
            if action_key not in _CONTROL_ACTIONS:
                raise ValueError("unsupported cognitive control action")
            if _cognitive_catalog(self).get(topic_key, code_key) is None:
                raise ValueError("unsupported cognitive hypothesis")
            now = datetime.now(timezone.utc)
            expiry = _normalize_suppression_expiry(expires_at, now=now) if action_key == "suppress" else ""

            if action_key != "restore":

                def _require_supported() -> None:
                    supported_codes = {
                        str(_hypothesis_ref_value(item, "code") or "").strip()
                        for item in _supported_hypotheses(_read_cognitive_state(self, topic_key))
                    }
                    if code_key not in supported_codes:
                        raise ValueError("only a current supported cognitive hypothesis can be controlled")

                await asyncio.to_thread(_require_supported)
                abandoned = await _abandon_current_intervention(
                    self,
                    topic_id=topic_key,
                    hypothesis_code=code_key,
                    action=action_key,
                )
            else:
                abandoned = False

            def _write() -> dict[str, Any]:
                store = getattr(self, "_store", None)
                writer = getattr(store, "record_cognitive_user_control", None)
                if not callable(writer):
                    raise RuntimeError("cognitive user controls unavailable")
                writer(
                    topic_id=topic_key,
                    hypothesis_code=code_key,
                    action=action_key,
                    reason=str(reason or "")[:500],
                    expires_at=expiry,
                )
                return {
                    "enabled": True,
                    "status": "updated",
                    "topic_id": topic_key,
                    "hypothesis_code": code_key,
                    "action": action_key,
                    "expires_at": expiry,
                    "abandoned_current_intervention": abandoned,
                }

            payload = await asyncio.to_thread(_write)
            wake_projection = getattr(self, "_request_cognitive_projection", None)
            if callable(wake_projection):
                try:
                    wake_projection()
                except Exception:
                    # The control is already durable and has dirtied the
                    # topic.  Keep the user action successful; stale reads
                    # fail closed until a later wake rebuilds the topic.
                    pass
            return Ok(payload)
        except Exception as exc:
            return _entry_exception_error(self, exc, operation="study_cognitive_control")
        finally:
            await release_question_lifecycle(self, lifecycle_operation)


__all__ = ["_CognitiveEntriesMixin"]
