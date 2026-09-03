from __future__ import annotations

import re
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from functools import wraps
from types import SimpleNamespace

from .adaptive_learning import (
    PracticeSelection,
    QuestionGenerationFailure,
    QuestionGenerationResult,
    QuestionInstance,
    QuestionPlan,
    TopicRef,
)
from .adaptive_learning.cognitive_delivery import (
    PreparedCognitiveIntervention,
    abandoned_intervention_event,
    committed_question_event,
    prepare_cognitive_intervention,
    reviewed_question_payload,
    validate_reviewed_question,
)
from .adaptive_learning.cognitive_intervention import (
    hypothesis_ref_from_payload,
    hypothesis_ref_payload,
)
from .adaptive_learning.learner_state import tracker_list_mastery
from .adaptive_learning.planner import build_question_plan
from .adaptive_learning.question_application import QuestionApplicationService
from .adaptive_learning.question_factory import (
    QuestionFactory,
    QuestionGenerationRequest,
    QuestionValidationResult,
)
from .difficulty_policy import select_targeted_difficulty
from .entry_common import (
    LLM_OPERATION_QUESTION_GENERATE,
    Any,
    Err,
    Ok,
    SdkError,
    TutorReply,
    _entry_exception_error,
    _validate_optional_vision_image_payload,
    asyncio,
    plugin_entry,
    time,
    tr,
    ui,
)
from .knowledge_graph_guidance import _canonical_necessary_relations
from .llm_prompts import ensure_targeted_prompt_context_fits
from .models import public_current_question_payload
from .practice_scope import (
    filter_question_params_to_scope,
    ordered_scope_topics,
    practice_scope_matches_topic,
)
from .question_type_mapping import (
    enforce_mapped_question_type,
    select_question_style,
)
from .targeted_question_contract import (
    canonicalize_targeted_question,
    project_target_topic_evidence,
    semantic_validation_passed,
    validate_targeted_question,
)
from .tutor_lifecycle import (
    release_question_lifecycle,
    reserve_question_lifecycle,
)

IMAGE_ONLY_QUESTION_PROMPT_EN = "Generate a study question from the pasted image."
IMAGE_ONLY_QUESTION_PROMPT_ZH_CN = "请根据这张图片生成一道学习题。"
IMAGE_ONLY_QUESTION_PROMPT_ZH_TW = "請根據這張圖片生成一道學習題。"


def _with_question_generation_reservation(function):
    """Reserve before any entry-path reads of ``_lock``-protected state."""

    @wraps(function)
    async def wrapped(self, *args, **kwargs):
        active_operation = await reserve_question_lifecycle(self, "question_generation")
        if active_operation:
            code = (
                "QUESTION_GENERATION_IN_PROGRESS"
                if active_operation == "question_generation"
                else "ANSWER_EVALUATION_IN_PROGRESS"
            )
            return Err(
                SdkError(
                    "another study question operation is already in progress; retry shortly",
                    code=code,
                )
            )
        try:
            return await function(self, *args, **kwargs)
        finally:
            await release_question_lifecycle(self, "question_generation")

    return wrapped


TARGETED_SELECTION_TTL_SECONDS = 10 * 60
TARGETED_HINT_MAX_CHARS = 240
TARGETED_GENERATION_TIMEOUT_SECONDS = 125.0
_COGNITIVE_QUESTION_INTENTS = frozenset({"misconception_probe", "misconception_repair", "transfer_check"})
_COGNITIVE_ABANDON_TIMEOUT_SECONDS = 2.0


def _cognitive_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _consume_cognitive_abandonment_task(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


def _warn_cognitive_abandonment(owner: Any, message: str, *args: Any) -> None:
    logger = getattr(owner, "logger", None) or getattr(owner, "_logger", None)
    warning = getattr(logger, "warning", None)
    if callable(warning):
        try:
            warning(message, *args)
        except Exception:
            pass


async def _record_cognitive_abandonment_best_effort(
    owner: Any,
    committed_event: Any,
    *,
    reason: str,
) -> bool:
    """Bound cancellation cleanup without hiding the publishing failure."""

    record_event = getattr(
        getattr(owner, "_store", None),
        "record_cognitive_intervention_event",
        None,
    )
    if not callable(record_event):
        _warn_cognitive_abandonment(
            owner,
            "cognitive abandonment ledger is unavailable",
        )
        return False
    try:
        abandoned = abandoned_intervention_event(
            committed_event,
            reason=reason,
        )
    except Exception as exc:
        _warn_cognitive_abandonment(
            owner,
            "cognitive abandonment event creation failed: {}",
            exc,
        )
        return False

    task = asyncio.create_task(asyncio.to_thread(record_event, asdict(abandoned)))
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=_COGNITIVE_ABANDON_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=_COGNITIVE_ABANDON_TIMEOUT_SECONDS,
            )
        except BaseException as exc:
            _warn_cognitive_abandonment(
                owner,
                "cognitive abandonment cancellation drain failed: {}",
                exc,
            )
        if not task.done():
            task.add_done_callback(_consume_cognitive_abandonment_task)
        succeeded = task.done() and not task.cancelled() and task.exception() is None
    except (asyncio.TimeoutError, Exception) as exc:
        _warn_cognitive_abandonment(
            owner,
            "cognitive abandonment persistence failed: {}",
            exc,
        )
        if not task.done():
            task.add_done_callback(_consume_cognitive_abandonment_task)
        return False
    else:
        succeeded = True

    wake_projection = getattr(owner, "_request_cognitive_projection", None) if succeeded else None
    if callable(wake_projection):
        try:
            wake_projection()
        except Exception:
            pass
    return succeeded


def _cognitive_question_fields(targeted_context: dict[str, Any] | None, *, topic_id: str) -> dict[str, Any]:
    context = dict(targeted_context or {})
    intent = str(context.get("learning_intent") or "practice").strip()
    hypothesis = hypothesis_ref_from_payload(context.get("hypothesis_target"), topic_id=topic_id)
    strategy = str(context.get("repair_strategy") or "").strip()
    decision_id = str(context.get("cognitive_decision_id") or "").strip()
    if intent not in _COGNITIVE_QUESTION_INTENTS or hypothesis is None or not strategy or not decision_id:
        return {
            "learning_intent": "practice",
            "hypothesis_target": None,
            "repair_strategy": "",
            "cognitive_decision_id": "",
            "cognitive_validator_version": "",
            "diagnostic_validation_id": "",
            "cognitive_blueprint_id": "",
            "cognitive_question_family_id": "",
        }
    return {
        "learning_intent": intent,
        "hypothesis_target": hypothesis,
        "repair_strategy": strategy,
        "cognitive_decision_id": decision_id,
        "cognitive_validator_version": str(context.get("cognitive_validator_version") or "").strip(),
        "diagnostic_validation_id": str(context.get("diagnostic_validation_id") or "").strip(),
        "cognitive_blueprint_id": str(context.get("cognitive_blueprint_id") or "").strip(),
        "cognitive_question_family_id": str(context.get("cognitive_question_family_id") or "").strip(),
    }


def _image_only_question_prompt(language: str) -> str:
    normalized = str(language or "").strip().lower()
    if normalized.startswith(("zh-tw", "zh-hk", "zh-hant")):
        return IMAGE_ONLY_QUESTION_PROMPT_ZH_TW
    if normalized.startswith("zh"):
        return IMAGE_ONLY_QUESTION_PROMPT_ZH_CN
    return IMAGE_ONLY_QUESTION_PROMPT_EN


def _compact_text(value: object, *, limit: int = 120) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else f"{text[:limit].rstrip()}..."


def _topic_name(topic: dict[str, Any] | None, fallback: str = "") -> str:
    payload = dict(topic or {})
    return str(payload.get("name") or payload.get("title") or fallback or "").strip()


def _candidate_evidence_topic_id(candidate: object) -> str:
    payload = dict(candidate) if isinstance(candidate, dict) else {}
    for source in (payload.get("payload"), payload.get("payload_summary"), payload):
        if not isinstance(source, dict):
            continue
        topic_id = str(source.get("topic_id") or source.get("id") or "").strip()
        if topic_id:
            return topic_id
    return ""


def _safe_hint(payload: dict[str, Any]) -> str:
    hint = _compact_text(payload.get("hint"), limit=TARGETED_HINT_MAX_CHARS)
    if not hint:
        return ""
    hint_lower = hint.lower()
    forbidden_values = [
        payload.get("answer"),
        payload.get("reference_answer"),
        *(payload.get("accepted_answers") or []),
    ]
    for value in forbidden_values:
        text = str(value or "").strip()
        normalized = text.lower()
        if not normalized:
            continue
        if hint_lower == normalized:
            return ""
        # A one-character answer such as "A" must not match every ordinary
        # English word containing that character.  Keep the same word-boundary
        # semantics as the targeted-question structural validator.
        if len(normalized) == 1 and normalized.isalnum():
            if re.search(rf"(?<![\w]){re.escape(normalized)}(?![\w])", hint_lower):
                return ""
        elif normalized in hint_lower:
            return ""
    for field_name in ("key_points", "solution_steps"):
        items = [str(item or "").strip() for item in (payload.get(field_name) or []) if str(item or "").strip()]
        if len(items) >= 2 and all(item.lower() in hint_lower for item in items[:3]):
            return ""
    return hint


def _targeted_public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    public_payload = public_current_question_payload(payload)
    public_payload["hint"] = _safe_hint(payload)
    return public_payload


def _question_private_payload(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    private_payload = dict(payload or {})
    answer = str(private_payload.get("answer") or "").strip()
    reference = str(private_payload.get("reference_answer") or answer).strip()
    private_payload["answer"] = answer or reference
    private_payload["reference_answer"] = reference or answer
    private_payload.setdefault("accepted_answers", [])
    private_payload.setdefault("key_points", [])
    private_payload.setdefault("rubric", {})
    private_payload.setdefault("solution_steps", [])
    private_payload.setdefault("math_equivalence_engine", {"enabled": False})
    private_payload["internal_private_payload"] = {
        "answer": private_payload.get("answer") or "",
        "reference_answer": private_payload.get("reference_answer") or "",
        "accepted_answers": list(private_payload.get("accepted_answers") or []),
        "key_points": list(private_payload.get("key_points") or []),
        "rubric": dict(private_payload.get("rubric") or {}),
        "solution_steps": list(private_payload.get("solution_steps") or []),
        "math_equivalence_engine": dict(private_payload.get("math_equivalence_engine") or {"enabled": False}),
    }
    private_payload.update(context)
    private_payload.setdefault("question_id", f"q_{uuid.uuid4().hex}")
    private_payload.setdefault("attempt_id", f"a_{uuid.uuid4().hex}")
    private_payload["attempt_evaluated"] = False
    private_payload.pop("answer_evaluation_cache", None)
    return private_payload


def _safe_wrong_question_summary(value: dict[str, Any]) -> dict[str, Any]:
    source = dict(value or {})
    return {
        key: source.get(key) for key in ("id", "topic_id", "error_type", "verdict") if source.get(key) not in (None, "")
    }


def _server_target_binding(targeted_context: dict[str, Any], *, generated_at: str) -> dict[str, Any]:
    target_topic_id = str(targeted_context.get("selected_topic_id") or "").strip()
    params = dict(targeted_context.get("question_params") or {})
    retry = dict(params.get("retry_wrong_question") or {})
    origin_wrong_question_id = ""
    if (
        str(targeted_context.get("selection_reason") or "").strip() == "retry"
        and str(retry.get("topic_id") or "").strip() == target_topic_id
    ):
        origin_wrong_question_id = str(retry.get("id") or "").strip()
    return {
        "target_topic_id": target_topic_id,
        "validation_status": "passed",
        "generated_at": str(generated_at or "").strip() or str(time.time()),
        "origin_wrong_question_id": origin_wrong_question_id,
    }


def _without_learner_answers(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _without_learner_answers(item)
            for key, item in value.items()
            if str(key) not in {"user_answer", "learner_answer", "submitted_answer"}
        }
    if isinstance(value, list):
        return [_without_learner_answers(item) for item in value]
    return value


def _targeted_model_context(context: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "operation",
        "input_text",
        "language",
        "mode",
        "source",
        "source_text",
        "text",
        "targeted_question",
        "selected_topic_id",
        "selected_topic_name",
        "selection_reason",
        "selection_reason_payload",
        "knowledge_question_params",
        "knowledge_guidance",
        "scope_key",
        "scope_revision",
        "practice_scope",
        "scope_topic_count",
        "generation_feedback",
    }
    return {key: _without_learner_answers(value) for key, value in context.items() if key in allowed}


def _question_validation_context(
    payload: dict[str, Any],
    targeted_context: dict[str, Any],
    *,
    canonical_relations: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    params = dict(targeted_context.get("question_params") or {})
    target_topic = dict(params.get("target_topic") or {})
    target_metadata = project_target_topic_evidence(target_topic)
    return {
        "question": payload.get("question") or "",
        "reference_answer": payload.get("reference_answer") or payload.get("answer") or "",
        # This private validation-only context never becomes the public
        # current-question payload.  It lets the semantic judge compare every
        # grading artifact without expanding its response schema.
        "accepted_answers": list(payload.get("accepted_answers") or []),
        "key_points": list(payload.get("key_points") or []),
        "rubric": dict(payload.get("rubric") or {}),
        "solution_steps": list(payload.get("solution_steps") or []),
        "hint": payload.get("hint") or "",
        "difficulty": payload.get("difficulty"),
        "question_type": payload.get("question_type") or "",
        "target_topic": target_metadata,
        # Semantic validation must not inherit the generation prompt's graph
        # summary (nor client-supplied blockers).  Its relation evidence is
        # rebuilt from the server's canonical selected topic below.
        "necessary_relations": dict(canonical_relations or {}),
    }


async def _canonical_validation_relations_for_target(owner: Any, *, selected_topic_id: str) -> dict[str, list[str]]:
    """Load canonical records once and safely derive validation evidence.

    A missing store/cache is deliberately non-fatal: the validator can still
    judge a candidate against its private target metadata, but it receives no
    unverified relation summary.
    """
    topic_id = str(selected_topic_id or "").strip()
    if not topic_id:
        return {}
    try:
        cache = getattr(owner, "_knowledge_guidance_topics_cache", None)
        topics = cache.get("all:5000") if isinstance(cache, dict) else None
        if topics is None:
            store = getattr(owner, "_store", None)
            list_topics = getattr(store, "list_topics", None)
            if not callable(list_topics):
                return {}
            topics = await asyncio.to_thread(list_topics, 5000, None, None)
            if not isinstance(cache, dict):
                cache = {}
                setattr(owner, "_knowledge_guidance_topics_cache", cache)
            cache["all:5000"] = list(topics or [])
        return _canonical_necessary_relations(topics=list(topics or []), topic_id=topic_id)
    except Exception:
        return {}


class _TutorQuestionEntriesMixin:
    async def _abandon_current_cognitive_intervention(
        self,
        *,
        topic_id: str,
        hypothesis_code: str,
        action: str,
    ) -> bool:
        """Abandon one still-current cognitive question before user override."""

        if str(action or "").strip() not in {
            "dismiss",
            "suppress",
            "delete",
            "replace",
        }:
            return False
        async with self._lock:
            current = dict(getattr(self._state, "current_question", {}) or {})
        if current.get("attempt_evaluated"):
            return False
        binding = current.get("target_binding")
        private_binding = binding if isinstance(binding, dict) else {}
        hypothesis = hypothesis_ref_from_payload(
            private_binding.get("cognitive_hypothesis_target"),
            topic_id=str(topic_id or "").strip(),
        )
        decision_id = str(private_binding.get("cognitive_decision_id") or "").strip()
        question_id = str(current.get("question_id") or "").strip()
        attempt_id = str(current.get("attempt_id") or "").strip()
        if (
            hypothesis is None
            or hypothesis.code != str(hypothesis_code or "").strip()
            or not decision_id
            or not question_id
        ):
            return False
        list_events = getattr(
            getattr(self, "_store", None),
            "list_cognitive_intervention_events",
            None,
        )
        record_event = getattr(
            getattr(self, "_store", None),
            "record_cognitive_intervention_event",
            None,
        )
        if not callable(list_events) or not callable(record_event):
            raise RuntimeError("cognitive intervention ledger is unavailable")
        rows = await asyncio.to_thread(
            list_events,
            decision_id=decision_id,
            event_types=("question_committed",),
            limit=10,
        )
        committed = next(
            (
                row
                for row in rows
                if str(row.get("question_id") or "").strip() == question_id
                and str((row.get("hypothesis_target") or {}).get("code") or "").strip() == hypothesis.code
            ),
            None,
        )
        if not isinstance(committed, dict):
            return False
        abandoned = dict(committed)
        abandoned.update(
            {
                "event_id": f"cognitive-event:{uuid.uuid4().hex}",
                "event_type": "intervention_abandoned",
                "attempt_id": "",
                "evaluation_verdict": "",
                "abandonment_reason": f"user_{str(action or '').strip()}",
                "created_at": _cognitive_now_iso(),
            }
        )
        abandoned.pop("event_seq", None)
        await asyncio.to_thread(record_event, abandoned)
        wake_projection = getattr(self, "_request_cognitive_projection", None)
        if callable(wake_projection):
            try:
                wake_projection()
            except Exception:
                # The committed ledger fact leaves the topic stale, so a
                # later answer/startup wake can safely retry projection.
                pass

        async with self._lock:
            live = getattr(self._state, "current_question", {})
            if (
                str(live.get("question_id") or "").strip() != question_id
                or str(live.get("attempt_id") or "").strip() != attempt_id
            ):
                return False
            live_binding = live.get("target_binding")
            if isinstance(live_binding, dict):
                for key in tuple(live_binding):
                    if key.startswith("cognitive_") or key == "diagnostic_validation_id":
                        live_binding.pop(key, None)
            for key in (
                "learning_intent",
                "hypothesis_target",
                "repair_strategy",
                "cognitive_decision_id",
                "cognitive_validator_version",
                "diagnostic_validation_id",
                "cognitive_blueprint_id",
                "cognitive_question_family_id",
            ):
                live.pop(key, None)
        persist = getattr(self, "_persist_state", None)
        if callable(persist):
            await persist()
        return True

    def _resolved_learning_plan_service(self):
        """Return the optional plan service without requiring constructor changes."""

        provider = getattr(self, "_learning_plan_service", None)
        return provider() if callable(provider) else provider

    def _active_learning_plan_selection_scope(self) -> dict[str, Any] | None:
        service = self._resolved_learning_plan_service()
        if service is None:
            return None
        try:
            payload = service.active_selection_scope()
        except Exception as exc:
            logger = getattr(self, "logger", None)
            if logger is not None:
                logger.warning("study learning plan selection read failed: {}", exc)
            code = str(getattr(exc, "code", "") or "").strip()
            raise SdkError(
                "learning plan selection is temporarily unavailable",
                code=code or "LEARNING_PLAN_TEMPORARILY_UNAVAILABLE",
            ) from exc
        return dict(payload) if isinstance(payload, dict) else None

    def _validate_learning_plan_selection_context(self, context: dict[str, Any]) -> None:
        if str(context.get("selection_domain") or "") != "learning_plan":
            return
        plan_id = str(context.get("learning_plan_id") or "").strip()
        topic_id = str(context.get("selected_topic_id") or "").strip()
        try:
            revision = int(context.get("learning_plan_revision") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SdkError(
                "learning plan changed after question selection",
                code="LEARNING_PLAN_CHANGED",
            ) from exc
        service = self._resolved_learning_plan_service()
        if service is None or not plan_id:
            raise SdkError(
                "learning plan is no longer active",
                code="LEARNING_PLAN_NOT_ACTIVE",
            )
        try:
            active = service.active_selection_scope()
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "").strip()
            raise SdkError(str(exc), code=code or "LEARNING_PLAN_NOT_ACTIVE") from exc
        active = dict(active) if isinstance(active, dict) else {}
        if str(active.get("learning_plan_id") or "").strip() != plan_id:
            raise SdkError(
                "learning plan is no longer active",
                code="LEARNING_PLAN_NOT_ACTIVE",
            )
        try:
            active_revision = int(active.get("learning_plan_revision") or 0)
        except (TypeError, ValueError, OverflowError):
            active_revision = -1
        if active_revision != revision:
            raise SdkError(
                "learning plan changed after question selection",
                code="LEARNING_PLAN_CHANGED",
            )
        eligible = {
            str(value or "").strip() for value in (active.get("eligible_topic_ids") or []) if str(value or "").strip()
        }
        if not topic_id or topic_id not in eligible:
            raise SdkError(
                "selected topic was removed from the learning plan",
                code="LEARNING_PLAN_TOPIC_REMOVED",
            )
        contains_topic = getattr(service, "contains_topic", None)
        if callable(contains_topic):
            try:
                contained = contains_topic(plan_id, revision, topic_id)
            except Exception as exc:
                code = str(getattr(exc, "code", "") or "").strip()
                raise SdkError(str(exc), code=code or "LEARNING_PLAN_CHANGED") from exc
            if contained is False:
                raise SdkError(
                    "selected topic was removed from the learning plan",
                    code="LEARNING_PLAN_TOPIC_REMOVED",
                )

    def _targeted_context_cache(self) -> dict[str, dict[str, Any]]:
        cache = getattr(self, "_targeted_question_contexts", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, "_targeted_question_contexts", cache)
        return cache

    def _prune_targeted_context_cache(self, now: float | None = None) -> None:
        current_time = time.time() if now is None else float(now)
        cache = self._targeted_context_cache()
        expired = [
            context_id
            for context_id, context in cache.items()
            if float(context.get("expires_at") or 0.0) <= current_time
        ]
        for context_id in expired:
            cache.pop(context_id, None)

    def _store_targeted_context(self, context: dict[str, Any]) -> dict[str, Any]:
        context_lock = getattr(self, "_targeted_context_lock", None)
        if context_lock is not None:
            with context_lock:
                return self._store_targeted_context_locked(context)
        return self._store_targeted_context_locked(context)

    def _store_targeted_context_locked(self, context: dict[str, Any]) -> dict[str, Any]:
        self._prune_targeted_context_cache()
        cache = self._targeted_context_cache()
        context_id = f"scq_{uuid.uuid4().hex}"
        stored = {
            **dict(context),
            "selection_context_id": context_id,
            "created_at": time.time(),
            "expires_at": time.time() + TARGETED_SELECTION_TTL_SECONDS,
            "consumed": False,
        }
        cache[context_id] = stored
        if len(cache) > 32:
            ordered = sorted(cache.items(), key=lambda item: float(item[1].get("created_at") or 0.0))
            for old_id, _ in ordered[: max(0, len(cache) - 32)]:
                cache.pop(old_id, None)
        return stored

    def _load_targeted_context(self, selection_context_id: str) -> dict[str, Any]:
        context_lock = getattr(self, "_targeted_context_lock", None)
        if context_lock is None:
            return self._load_targeted_context_locked(selection_context_id)
        with context_lock:
            return self._load_targeted_context_locked(selection_context_id)

    def _load_targeted_context_locked(self, selection_context_id: str) -> dict[str, Any]:
        self._prune_targeted_context_cache()
        context_id = str(selection_context_id or "").strip()
        cache = self._targeted_context_cache()
        cached = cache.get(context_id)
        if not cached or cached.get("consumed"):
            raise SdkError("selection context expired", code="SELECTION_CONTEXT_EXPIRED")
        cached_revision = int(cached.get("scope_revision") or 0)
        active_revision = int(getattr(self._state, "practice_scope_revision", 0) or 0)
        cached_scope_key = str(cached.get("scope_key") or "").strip()
        active_scope = self._resolve_active_practice_scope()
        active_scope_key = active_scope.scope_key if active_scope else ""
        if cached_revision != active_revision or cached_scope_key != active_scope_key:
            cache.pop(context_id, None)
            raise SdkError(
                "practice scope changed after question selection",
                code="SELECTION_SCOPE_CHANGED",
            )
        topic_id = str(cached.get("selected_topic_id") or "").strip()
        if topic_id and not self._knowledge_tracker.store.get_topic(topic_id):
            cache.pop(context_id, None)
            raise SdkError("selection context expired", code="SELECTION_CONTEXT_EXPIRED")
        if active_scope and topic_id not in set(active_scope.eligible_topic_ids):
            cache.pop(context_id, None)
            raise SdkError(
                "selected topic is outside the active practice scope",
                code="SELECTION_SCOPE_CHANGED",
            )
        try:
            self._validate_learning_plan_selection_context(cached)
        except SdkError:
            cache.pop(context_id, None)
            raise
        cached["consumed"] = True
        return dict(cached)

    def _selection_from_question_params(self, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params or {})
        target_topic = dict(params.get("target_topic") or {})
        target_topic_id = str(params.get("target_topic_id") or "").strip()
        weak_topics = list(params.get("weak_topics") or [])
        due_reviews = list(params.get("due_reviews") or [])
        retry = dict(params.get("retry_wrong_question") or {})
        candidate_evidence = list(params.get("candidate_evidence") or [])

        reason = "no_data"
        selected_topic_id = target_topic_id
        selected_topic_name = _topic_name(target_topic, selected_topic_id)
        reason_payload: dict[str, Any] = {}

        if retry:
            selected_topic_id = str(retry.get("topic_id") or selected_topic_id).strip()
            retry_topic = self._knowledge_tracker.store.get_topic(selected_topic_id)
            selected_topic_name = _topic_name(retry_topic, selected_topic_id)
            reason = "retry"
            reason_payload = {"wrong_question": _safe_wrong_question_summary(retry)}
        elif due_reviews:
            first_due = dict(due_reviews[0] or {})
            due_topic = dict(first_due.get("topic") or {})
            selected_topic_id = str(first_due.get("topic_id") or due_topic.get("id") or selected_topic_id).strip()
            selected_topic_name = _topic_name(due_topic, selected_topic_id)
            reason = "due_review"
            reason_payload = {"due_review": first_due}
        elif weak_topics:
            first_weak = dict(weak_topics[0] or {})
            selected_topic_id = str(first_weak.get("topic_id") or first_weak.get("id") or selected_topic_id).strip()
            weak_topic = self._knowledge_tracker.store.get_topic(selected_topic_id)
            selected_topic_name = _topic_name(
                weak_topic,
                str(
                    first_weak.get("topic_name")
                    or first_weak.get("name")
                    or first_weak.get("topic")
                    or selected_topic_id
                ),
            )
            reason = "weak_topic"
            reason_payload = {"weak_topic": first_weak}
        elif params.get("blocked_diagnostic"):
            diagnostic = dict(params.get("blocked_diagnostic") or {})
            selected_topic_id = str(diagnostic.get("target_topic_id") or selected_topic_id).strip()
            diagnostic_topic = self._knowledge_tracker.store.get_topic(selected_topic_id)
            selected_topic_name = _topic_name(diagnostic_topic, selected_topic_id)
            reason = "blocked_diagnostic"
            reason_payload = {"blocked_diagnostic": diagnostic}
        elif candidate_evidence:
            first_candidate = dict(candidate_evidence[0] or {})
            candidate_payload = dict(first_candidate.get("payload") or {})
            candidate_summary = dict(first_candidate.get("payload_summary") or {})
            selected_topic_id = str(
                candidate_payload.get("topic_id")
                or candidate_summary.get("topic_id")
                or first_candidate.get("topic_id")
                or selected_topic_id
            ).strip()
            selected_topic_name = str(
                candidate_payload.get("name")
                or candidate_summary.get("name")
                or first_candidate.get("name")
                or selected_topic_id
            ).strip()
            reason = "recommended"
            reason_payload = {"candidate": first_candidate}
        elif selected_topic_id:
            reason = "recommended"
            reason_payload = {"target_topic": target_topic}

        if not selected_topic_id and not selected_topic_name:
            reason = "no_data"

        # The planner is being introduced behind a conservative adapter: it
        # may confirm the server-owned topic choice only when it produces the
        # exact legacy result.  Public targeted-context fields, reason
        # payloads, difficulty coercion, and the no-data path deliberately
        # remain owned by this compatibility block until the full planner
        # migration is complete.
        planner_catalog = (
            {
                selected_topic_id: {
                    "id": selected_topic_id,
                    "name": selected_topic_name or selected_topic_id,
                }
            }
            if selected_topic_id
            else {}
        )
        try:
            question_plan = build_question_plan(
                params,
                plan_id="targeted-selection-adapter",
                topics_by_id=planner_catalog,
            )
        except Exception:
            # A new internal adapter must never change the established public
            # selection behavior when it cannot interpret legacy input.
            question_plan = None
        public_reason = (
            "retry"
            if question_plan is not None and question_plan.selection.reason == "wrong_retry"
            else (question_plan.selection.reason if question_plan is not None else "")
        )
        if question_plan is not None and public_reason == reason and question_plan.target_topic.id == selected_topic_id:
            selected_topic_id = question_plan.target_topic.id
        return {
            "selected_topic_id": selected_topic_id,
            "selected_topic_name": selected_topic_name or selected_topic_id,
            "selection_reason": reason,
            "selection_reason_payload": reason_payload,
            "difficulty": params.get("suggested_difficulty") or 3,
            "weak_topics": weak_topics,
            "due_reviews": due_reviews,
            "mastery_overview": [],
            "question_params": params,
        }

    def _scoped_question_params(self, scope) -> dict[str, Any]:
        eligible = set(scope.eligible_topic_ids)
        if scope.mode == "explicit_topic":
            topic = self._knowledge_tracker.store.get_topic(scope.topic_id)
            topics = (
                [topic] if topic is not None and practice_scope_matches_topic(scope.to_public_dict(), topic) else []
            )
        else:
            topics = [
                topic
                for topic in self._knowledge_tracker.store.list_topics(
                    5000,
                    scope.subject or None,
                    scope.stage or None,
                    chapter=scope.chapter or None,
                    unit=scope.unit or None,
                    course_family=scope.course_family or None,
                )
                if str(topic.get("id") or "") in eligible
            ]
        mastery_overview = tracker_list_mastery(self._knowledge_tracker, eligible)
        mastery_by_topic = {
            str(item.get("topic_id") or ""): dict(item)
            for item in mastery_overview
            if str(item.get("topic_id") or "") in eligible
        }
        ordered_topics = ordered_scope_topics(topics, attempted_topic_ids=set(mastery_by_topic))
        if not ordered_topics:
            raise SdkError(
                "practice scope no longer contains any topics",
                code="PRACTICE_SCOPE_INVALIDATED",
            )
        readiness_reader = getattr(
            getattr(self._knowledge_tracker, "graph", None),
            "readiness_in_scope",
            None,
        )
        readiness_enabled = (
            bool(
                getattr(
                    getattr(self, "_cfg", None),
                    "adaptive_practice_readiness_enabled",
                    True,
                )
            )
            and scope.mode != "explicit_topic"
            and callable(readiness_reader)
        )
        ready_topic_ids: set[str] = set()
        blockers_by_topic: dict[str, list[dict[str, Any]]] = {}
        selectable_topics = ordered_topics
        if readiness_enabled:
            ready_topic_ids, blockers_by_topic = readiness_reader(eligible)
            ready_topics = [topic for topic in ordered_topics if str(topic.get("id") or "") in ready_topic_ids]
            if ready_topics:
                selectable_topics = ready_topics
        unattempted = [topic for topic in selectable_topics if str(topic.get("id") or "") not in mastery_by_topic]
        if unattempted:
            fallback_topic = unattempted[0]
        else:
            fallback_topic = min(
                selectable_topics,
                key=lambda topic: (
                    float(mastery_by_topic.get(str(topic.get("id") or ""), {}).get("mastery") or 0.0),
                    str(topic.get("id") or ""),
                ),
            )
        target_topic_id = scope.topic_id or str(fallback_topic.get("id") or "")
        topics_by_id = {str(topic.get("id") or ""): dict(topic) for topic in topics if str(topic.get("id") or "")}
        params = self._knowledge_tracker.preview_next_question_params(
            target_topic_id,
            candidate_topic_ids=eligible,
            candidate_limit=5000,
            candidate_topics_by_id=topics_by_id,
        )
        if scope.mode == "explicit_topic":
            retries = self._knowledge_tracker.store.list_wrong_questions(
                limit=5000,
                topic_ids=eligible,
                statuses=("active", "retrying"),
            )
        else:
            retries = self._knowledge_tracker.store.list_auto_retry_candidates(
                limit=5000,
                topic_ids=eligible,
            )
        params["retry_wrong_questions"] = retries
        params["retry_wrong_question"] = retries[0] if retries else {}
        params = filter_question_params_to_scope(params, eligible)
        if scope.mode == "explicit_topic":
            params["weak_topics"] = []
            params["candidate_evidence"] = []
        elif readiness_enabled:
            params["candidate_evidence"] = [
                item
                for item in params.get("candidate_evidence") or []
                if _candidate_evidence_topic_id(item) in ready_topic_ids
            ]
            if (
                not ready_topic_ids
                and not params.get("retry_wrong_question")
                and not params.get("due_reviews")
                and not params.get("weak_topics")
            ):
                params["blocked_diagnostic"] = {
                    "target_topic_id": target_topic_id,
                    "blockers": blockers_by_topic.get(target_topic_id, []),
                    "scope_topic_ids": sorted(eligible),
                }
        if not params.get("target_topic_id"):
            params["target_topic_id"] = target_topic_id
            params["target_topic"] = self._knowledge_tracker.store.get_topic(target_topic_id) or {}
        params["mastery_overview"] = list(mastery_by_topic.values())
        return params

    def _unscoped_question_params(self) -> dict[str, Any]:
        params = self._knowledge_tracker.preview_next_question_params("")
        retries = self._knowledge_tracker.store.list_auto_retry_candidates(
            limit=5000,
        )
        params["retry_wrong_questions"] = retries
        params["retry_wrong_question"] = retries[0] if retries else {}
        return params

    def _focus_selected_question_params(
        self,
        selection: dict[str, Any],
        initial_params: dict[str, Any],
        scope,
    ) -> None:
        selected_topic_id = str(selection.get("selected_topic_id") or "").strip()
        if not selected_topic_id:
            return
        eligible = set(scope.eligible_topic_ids) if scope is not None else None
        focused = self._knowledge_tracker.preview_next_question_params(
            selected_topic_id,
            candidate_topic_ids=eligible,
            candidate_limit=5000 if eligible is not None else 5,
        )
        if eligible is not None:
            focused = filter_question_params_to_scope(focused, eligible)
        focused["retry_wrong_questions"] = []
        focused["retry_wrong_question"] = {}
        if selection.get("selection_reason") == "retry":
            selected_retry = dict(initial_params.get("retry_wrong_question") or {})
            if str(selected_retry.get("topic_id") or "").strip() == selected_topic_id:
                focused["retry_wrong_questions"] = [selected_retry]
                focused["retry_wrong_question"] = selected_retry
        else:
            guidance_builder = getattr(self._knowledge_tracker, "_question_guidance", None)
            if callable(guidance_builder):
                mastery = dict(focused.get("mastery") or {})
                focused["prompt_guidance"] = guidance_builder(
                    float(mastery.get("mastery") or 0.0),
                    blockers=list(focused.get("blockers") or []),
                    retry=None,
                )
            else:
                focused.pop("prompt_guidance", None)
        if selection.get("selection_reason") == "blocked_diagnostic":
            diagnostic = dict(initial_params.get("blocked_diagnostic") or {})
            focused["blocked_diagnostic"] = diagnostic
            blocker_names = [
                str(item.get("name") or item.get("id") or "")
                for item in diagnostic.get("blockers") or []
                if isinstance(item, dict)
            ]
            foundation_guidance = (
                "Generate a diagnostic foundation question before advancing. "
                "Keep the question inside the selected practice scope; do not "
                "redirect the learner to a prerequisite outside that scope."
            )
            if blocker_names:
                foundation_guidance += f" Diagnose readiness around: {', '.join(blocker_names)}."
            existing_guidance = str(focused.get("prompt_guidance") or "").strip()
            focused["prompt_guidance"] = "\n".join(item for item in (existing_guidance, foundation_guidance) if item)
        focused["target_topic_id"] = selected_topic_id
        focused["target_topic"] = self._knowledge_tracker.store.get_topic(selected_topic_id) or {}
        planned_difficulty = select_targeted_difficulty(
            focused["target_topic"],
            mastery=dict(focused.get("mastery") or {}),
            blockers=list(focused.get("blockers") or []),
            selection_reason=str(selection.get("selection_reason") or ""),
            retry_wrong_question=dict(focused.get("retry_wrong_question") or {}),
            recent_results=focused.get("recent_results"),
        )
        # Both keys are private prompt context.  Keeping the legacy
        # ``suggested_difficulty`` current preserves established prompt
        # guidance, while ``planned_difficulty`` is the exact validation
        # binding used below.
        focused["suggested_difficulty"] = planned_difficulty
        focused["planned_difficulty"] = planned_difficulty
        focused["scope_candidates"] = {
            "retry_wrong_questions": initial_params.get("retry_wrong_questions") or [],
            "due_reviews": initial_params.get("due_reviews") or [],
            "weak_topics": initial_params.get("weak_topics") or [],
        }
        selection["question_params"] = focused
        selection["difficulty"] = planned_difficulty

    def _build_targeted_question_context(self) -> dict[str, Any]:
        scope = self._resolve_active_practice_scope()
        plan_scope = None
        selection_domain = "practice_scope" if scope is not None else "global"
        if scope is None:
            active_plan = self._active_learning_plan_selection_scope()
            if active_plan is not None:
                eligible_topic_ids = tuple(
                    dict.fromkeys(
                        str(topic_id or "").strip()
                        for topic_id in (active_plan.get("eligible_topic_ids") or [])
                        if str(topic_id or "").strip()
                    )
                )
                if eligible_topic_ids:
                    plan_scope = SimpleNamespace(
                        eligible_topic_ids=eligible_topic_ids,
                        mode="learning_plan",
                        topic_id="",
                        subject="",
                        stage="",
                        chapter="",
                        unit="",
                        course_family="",
                    )
                    selection_domain = "learning_plan"
        effective_scope = scope if scope is not None else plan_scope
        params = (
            self._scoped_question_params(effective_scope)
            if effective_scope is not None
            else self._unscoped_question_params()
        )
        selection = self._selection_from_question_params(params)
        self._focus_selected_question_params(selection, params, effective_scope)
        if scope is not None:
            selection.update(
                {
                    "selection_domain": selection_domain,
                    "scope_key": scope.scope_key,
                    "scope_revision": scope.scope_revision,
                    "practice_scope": scope.to_public_dict(),
                    "scope_topic_count": len(scope.eligible_topic_ids),
                    "mastery_overview": params.get("mastery_overview") or [],
                    "eligible_topic_ids": list(scope.eligible_topic_ids),
                }
            )
        else:
            selection.update(
                {
                    "selection_domain": selection_domain,
                    "scope_key": "",
                    "scope_revision": int(getattr(self._state, "practice_scope_revision", 0) or 0),
                    "practice_scope": {},
                    "scope_topic_count": 0,
                    "eligible_topic_ids": (list(plan_scope.eligible_topic_ids) if plan_scope else []),
                }
            )
            if plan_scope is not None and active_plan is not None:
                selection.update(
                    {
                        "learning_plan_id": active_plan.get("learning_plan_id") or "",
                        "learning_plan_revision": active_plan.get("learning_plan_revision") or 0,
                        "plan_progress": active_plan.get("progress") or {},
                    }
                )
        if selection["selection_reason"] == "no_data":
            return {
                **selection,
                "selection_context_id": "",
                "no_data": True,
            }
        stored = self._store_targeted_context(selection)
        return stored

    async def _generate_question_payload_impl(
        self,
        *,
        source_text: str,
        topic: str = "",
        source: str = "manual",
        source_question_id: str = "",
        vision_image_payload: str = "",
        targeted_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            previous_question = dict(getattr(self._state, "current_question", {}) or {})
            active_mode = self._state.active_mode
        previous_binding = previous_question.get("target_binding")
        previous_binding = previous_binding if isinstance(previous_binding, dict) else {}
        previous_topic = str(
            previous_binding.get("target_topic_id")
            or previous_question.get("selected_topic_id")
            or previous_question.get("topic")
            or ""
        ).strip()
        previous_hypothesis = hypothesis_ref_from_payload(
            previous_binding.get("cognitive_hypothesis_target"),
            topic_id=previous_topic,
        )
        if previous_hypothesis is not None and not previous_question.get("attempt_evaluated"):
            try:
                abandoned = await self._abandon_current_cognitive_intervention(
                    topic_id=previous_topic,
                    hypothesis_code=previous_hypothesis.code,
                    action="replace",
                )
            except Exception as exc:
                raise SdkError(
                    "the previous cognitive intervention could not be safely abandoned",
                    code="COGNITIVE_INTERVENTION_ABANDON_FAILED",
                ) from exc
            if not abandoned:
                raise SdkError(
                    "the previous cognitive intervention could not be safely abandoned",
                    code="COGNITIVE_INTERVENTION_ABANDON_FAILED",
                )
        question_type_mapping = None
        extra_context = {
            "source": source,
            "source_text": source_text,
            "topic_hint": str(topic or "").strip(),
            "mode": active_mode,
        }
        if source_question_id:
            extra_context["source_question_id"] = source_question_id
        if targeted_context:
            question_params = dict(targeted_context.get("question_params") or {})
            retry = dict(question_params.get("retry_wrong_question") or {})
            previous_question = dict(retry.get("question") or {})
            mastery = dict(question_params.get("mastery") or {})
            try:
                attempt_count = max(0, int(mastery.get("attempts") or 0))
            except (TypeError, ValueError):
                attempt_count = 0
            question_type_mapping = select_question_style(
                dict(question_params.get("target_topic") or {}),
                attempt_count=attempt_count,
                selection_reason=str(targeted_context.get("selection_reason") or ""),
                previous_question_style=str(previous_question.get("question_style") or ""),
                error_type=str(retry.get("error_type") or ""),
            )
            question_params.update(question_type_mapping.to_context())
            extra_context.update(
                {
                    "source": "targeted_question",
                    "targeted_question": True,
                    "selected_topic_id": targeted_context.get("selected_topic_id") or "",
                    "selected_topic_name": targeted_context.get("selected_topic_name") or "",
                    "selection_context_id": targeted_context.get("selection_context_id") or "",
                    "selection_reason": targeted_context.get("selection_reason") or "",
                    "selection_reason_payload": targeted_context.get("selection_reason_payload") or {},
                    "knowledge_question_params": question_params,
                    "scope_key": targeted_context.get("scope_key") or "",
                    "scope_revision": targeted_context.get("scope_revision") or 0,
                    "practice_scope": targeted_context.get("practice_scope") or {},
                    "scope_topic_count": targeted_context.get("scope_topic_count") or 0,
                    "selection_domain": targeted_context.get("selection_domain") or "global",
                    "learning_plan_id": targeted_context.get("learning_plan_id") or "",
                    "learning_plan_revision": targeted_context.get("learning_plan_revision") or 0,
                    "plan_progress": targeted_context.get("plan_progress") or {},
                }
            )
        if vision_image_payload:
            extra_context.update({"vision_enabled": True, "vision_image_base64": vision_image_payload})
        tutor_context = await self._build_learning_context(
            LLM_OPERATION_QUESTION_GENERATE,
            input_text=source_text,
            extra=extra_context,
        )
        if targeted_context:
            tutor_context = _targeted_model_context(tutor_context)
            try:
                ensure_targeted_prompt_context_fits(tutor_context)
            except ValueError as exc:
                raise SdkError(str(exc), code="TARGETED_CONTEXT_TOO_LARGE") from exc
        reply = None
        validation_failure = ""
        attempts = 2 if targeted_context else 1
        canonical_relations = (
            await _canonical_validation_relations_for_target(
                self,
                selected_topic_id=str(targeted_context.get("selected_topic_id") or ""),
            )
            if targeted_context
            else {}
        )
        selected_topic_id = str((targeted_context or {}).get("selected_topic_id") or topic or "").strip()
        selected_topic_name = str((targeted_context or {}).get("selected_topic_name") or selected_topic_id).strip()
        selection_reason = str((targeted_context or {}).get("selection_reason") or "recommended")
        if selection_reason == "retry":
            selection_reason = "wrong_retry"
        if selection_reason not in {"wrong_retry", "due_review", "weak_topic", "recommended", "default"}:
            selection_reason = "recommended"
        planned_difficulty = (
            dict((targeted_context or {}).get("question_params") or {}).get("planned_difficulty")
            if targeted_context
            else 0
        )
        if targeted_context and (
            isinstance(planned_difficulty, bool)
            or not isinstance(planned_difficulty, int)
            or not 1 <= planned_difficulty <= 5
        ):
            raise SdkError(
                "targeted question plan has an invalid planned difficulty",
                code="INVALID_TARGETED_QUESTION_PLAN",
            )
        if not targeted_context:
            planned_difficulty = 0
        cognitive_fields = _cognitive_question_fields(
            None,
            topic_id=selected_topic_id,
        )
        retry_binding = dict(
            dict((targeted_context or {}).get("question_params") or {}).get("retry_wrong_question") or {}
        )
        origin_wrong_question_id = (
            str(retry_binding.get("id") or "").strip()
            if selection_reason == "wrong_retry"
            and str(retry_binding.get("topic_id") or "").strip() == selected_topic_id
            else ""
        )
        eligible_topic_ids = tuple(
            dict.fromkeys(
                str(topic_id or "").strip()
                for topic_id in ((targeted_context or {}).get("eligible_topic_ids") or ())
                if str(topic_id or "").strip()
            )
        )
        plan_target_binding = (
            {
                "plan_id": str(targeted_context.get("selection_context_id") or "").strip(),
                "target_topic_id": selected_topic_id,
                "selection_reason": selection_reason,
                "eligible_topic_ids": eligible_topic_ids,
                "learning_plan_id": str(targeted_context.get("learning_plan_id") or "").strip(),
                "learning_plan_revision": int(targeted_context.get("learning_plan_revision") or 0),
                "scope_key": str(targeted_context.get("scope_key") or "").strip(),
                "scope_revision": int(targeted_context.get("scope_revision") or 0),
                "origin_wrong_question_id": origin_wrong_question_id,
                "source_question_id": source_question_id,
            }
            if targeted_context
            else {}
        )
        original_plan = QuestionPlan(
            plan_id=str((targeted_context or {}).get("selection_context_id") or ""),
            selection=PracticeSelection(
                reason=selection_reason,
                target_topic=TopicRef(id=selected_topic_id, name=selected_topic_name),
                eligible_topic_ids=eligible_topic_ids,
                origin_wrong_question_id=origin_wrong_question_id or None,
            ),
            difficulty=planned_difficulty,
            question_type=(question_type_mapping.machine_question_type if question_type_mapping is not None else ""),
            mode=active_mode,
            source_question_id=source_question_id,
            target_binding=plan_target_binding,
            scope_key=str((targeted_context or {}).get("scope_key") or ""),
            scope_revision=int((targeted_context or {}).get("scope_revision") or 0),
        )
        plan = original_plan
        prepared_cognitive: PreparedCognitiveIntervention | None = None
        cognitive_validation = None
        repair_question_family_id = ""
        cognitive_fallback = bool((targeted_context or {}).get("_cognitive_fallback"))
        if targeted_context and not cognitive_fallback:
            propose = getattr(
                getattr(self, "_knowledge_tracker", None),
                "propose_cognitive_intent",
                None,
            )
            if callable(propose):
                try:
                    decision = await asyncio.to_thread(propose, original_plan)
                    candidate = prepare_cognitive_intervention(decision)
                    if candidate is not None:
                        record_event = getattr(
                            getattr(self, "_store", None),
                            "record_cognitive_intervention_event",
                            None,
                        )
                        if not callable(record_event):
                            candidate = None
                        else:
                            await asyncio.to_thread(
                                record_event,
                                asdict(candidate.proposal_event),
                            )
                    if candidate is not None and candidate.active:
                        blueprint = candidate.blueprint
                        if blueprint is None:
                            raise RuntimeError("active cognitive decision has no blueprint")
                        prepared_cognitive = candidate
                        plan = candidate.proposed_plan
                        hypothesis = plan.hypothesis_target
                        cognitive_fields = {
                            "learning_intent": plan.learning_intent,
                            "hypothesis_target": hypothesis,
                            "repair_strategy": plan.repair_strategy,
                            "cognitive_decision_id": candidate.decision_id,
                            "cognitive_validator_version": "",
                            "diagnostic_validation_id": "",
                            "cognitive_blueprint_id": blueprint.blueprint_id,
                            "cognitive_question_family_id": (blueprint.question_family_id),
                        }
                        if plan.learning_intent == "transfer_check" and hypothesis:
                            list_events = getattr(
                                getattr(self, "_store", None),
                                "list_cognitive_intervention_events",
                                None,
                            )
                            if callable(list_events):
                                raw_prior_events = await asyncio.to_thread(
                                    list_events,
                                    topic_id=plan.target_topic.id,
                                    hypothesis_code=hypothesis.code,
                                    event_types=("attempt_committed",),
                                    limit=200,
                                )
                                prior_events = raw_prior_events if isinstance(raw_prior_events, list) else []
                                repair_question_family_id = next(
                                    (
                                        str(item.get("question_family_id") or "").strip()
                                        for item in reversed(prior_events)
                                        if str(item.get("learning_intent") or "").strip() == "misconception_repair"
                                        and str(item.get("evaluation_verdict") or "").strip() == "correct"
                                        and str(item.get("question_family_id") or "").strip()
                                    ),
                                    "",
                                )
                except Exception:
                    # Shadow/Active cognition is optional.  An unavailable
                    # reader, ledger, or blueprint yields the original plan.
                    prepared_cognitive = None
                    plan = original_plan
                    cognitive_fields = _cognitive_question_fields(
                        None,
                        topic_id=selected_topic_id,
                    )

        async def generate_candidate(
            request: QuestionGenerationRequest,
        ) -> QuestionGenerationResult:
            generation_context = dict(request.context)
            if validation_failure:
                generation_context["generation_feedback"] = validation_failure
            if prepared_cognitive is not None:
                reviewed_payload = reviewed_question_payload(prepared_cognitive)
                reviewed_payload["question_id"] = f"q_{uuid.uuid4().hex}"
                reviewed_payload["attempt_id"] = f"a_{uuid.uuid4().hex}"
                candidate_reply = TutorReply(
                    operation=LLM_OPERATION_QUESTION_GENERATE,
                    input_text=source_text,
                    reply=str(reviewed_payload["question"]),
                    payload=reviewed_payload,
                )
            else:
                candidate_reply = await self._agent.question_generate(
                    source_text,
                    mode=active_mode,
                    context=generation_context,
                )
            candidate_payload = dict(candidate_reply.payload or {})
            repair_codes: tuple[str, ...] = ()
            if targeted_context and prepared_cognitive is None:
                candidate_payload = enforce_mapped_question_type(candidate_payload, question_type_mapping)
                candidate_payload["target_topic_id"] = selected_topic_id
                candidate_payload, repair_codes = canonicalize_targeted_question(
                    candidate_payload,
                    target_topic_id=selected_topic_id,
                    planned_difficulty=planned_difficulty,
                )
                safe_hint = _safe_hint(candidate_payload)
                if str(candidate_payload.get("hint") or "").strip() and not safe_hint:
                    repair_codes = (*repair_codes, "hint_removed")
                candidate_payload["hint"] = safe_hint
                # Provenance markers are strictly internal to model-output
                # normalization and must never reach validation persistence.
                candidate_payload.pop("_answer_reference_answer_consistent", None)
                candidate_payload.pop("_targeted_difficulty_valid", None)
                if repair_codes:
                    logger = getattr(self, "logger", None)
                    log_repair = getattr(logger, "info", None)
                    if callable(log_repair):
                        log_repair(
                            "targeted question repair selection_context_id={} codes={}",
                            plan.plan_id,
                            ",".join(repair_codes),
                        )
            normalized_reply = TutorReply(
                operation=candidate_reply.operation,
                input_text=candidate_reply.input_text,
                reply=candidate_reply.reply,
                payload=candidate_payload,
                degraded=candidate_reply.degraded,
                diagnostic=candidate_reply.diagnostic,
                created_at=candidate_reply.created_at,
            )
            return QuestionGenerationResult(
                question=QuestionInstance(
                    question_id=str(candidate_payload.get("question_id") or ""),
                    plan_id=plan.plan_id,
                    target_topic=plan.target_topic,
                    question_type=str(candidate_payload.get("question_type") or plan.question_type),
                    difficulty=int(candidate_payload.get("difficulty") or 0),
                    public_payload=candidate_payload,
                    generator_metadata={
                        "reply": normalized_reply,
                        "repair_codes": repair_codes,
                    },
                    mode=active_mode,
                    source_question_id=source_question_id,
                    target_binding=plan.target_binding,
                    scope_key=plan.scope_key,
                    scope_revision=plan.scope_revision,
                    status="generated",
                    learning_intent=plan.learning_intent,
                    hypothesis_target=plan.hypothesis_target,
                    repair_strategy=plan.repair_strategy,
                    cognitive_decision_id=cognitive_fields["cognitive_decision_id"],
                    cognitive_validator_version=cognitive_fields["cognitive_validator_version"],
                    diagnostic_validation_id=cognitive_fields["diagnostic_validation_id"],
                ),
                payload=candidate_payload,
                raw_result=normalized_reply,
            )

        async def validate_candidate(
            _request: QuestionGenerationRequest,
            generation: QuestionGenerationResult,
        ) -> QuestionValidationResult:
            nonlocal cognitive_validation, validation_failure
            if not targeted_context or generation.question is None:
                return QuestionValidationResult(valid=True)
            candidate_payload = dict(generation.question.public_payload)
            params = dict(targeted_context.get("question_params") or {})
            structural = validate_targeted_question(
                candidate_payload,
                target_topic_id=selected_topic_id,
                target_topic_name=selected_topic_name,
                origin_wrong_question=dict(params.get("retry_wrong_question") or {}),
                expected_difficulty=planned_difficulty,
            )
            if not structural.valid:
                validation_failure = "Structural validation failed: " + ", ".join(structural.errors)
                return QuestionValidationResult(valid=False, errors=tuple(structural.errors), raw_result=structural)
            if prepared_cognitive is not None:
                cognitive_validation = validate_reviewed_question(
                    prepared_cognitive,
                    generation.question,
                    repair_question_family_id=repair_question_family_id,
                )
                if not cognitive_validation.valid:
                    validation_failure = "Cognitive validation failed: " + ", ".join(cognitive_validation.errors)
                    return QuestionValidationResult(
                        valid=False,
                        errors=tuple(cognitive_validation.errors),
                        raw_result=cognitive_validation,
                    )
                return QuestionValidationResult(
                    valid=True,
                    raw_result=cognitive_validation,
                )
            validation_reply = await self._agent.question_validate(
                context=_question_validation_context(
                    candidate_payload,
                    targeted_context,
                    canonical_relations=canonical_relations,
                ),
            )
            if not semantic_validation_passed(dict(validation_reply.payload or {}), degraded=validation_reply.degraded):
                validation_failure = "Semantic validation failed: " + str(
                    (validation_reply.payload or {}).get("reason") or validation_reply.diagnostic or "retry"
                )
                return QuestionValidationResult(valid=False, errors=(validation_failure,), raw_result=validation_reply)
            generation.question.public_payload.update(candidate_payload)
            return QuestionValidationResult(valid=True, raw_result=validation_reply)

        try:
            question_instance = await QuestionApplicationService(
                QuestionFactory(
                    generator=generate_candidate,
                    validator=validate_candidate if targeted_context else None,
                ),
                max_attempts=attempts,
            ).generate(
                QuestionGenerationRequest(
                    plan=plan,
                    source_text=source_text,
                    source=source,
                    context=tutor_context,
                )
            )
        except QuestionGenerationFailure as exc:
            if prepared_cognitive is not None and targeted_context:
                record_event = getattr(
                    getattr(self, "_store", None),
                    "record_cognitive_intervention_event",
                    None,
                )
                if callable(record_event):
                    try:
                        abandoned = abandoned_intervention_event(
                            prepared_cognitive.proposal_event,
                            reason="question_validation_failed",
                        )
                        await asyncio.to_thread(record_event, asdict(abandoned))
                        wake_projection = getattr(self, "_request_cognitive_projection", None)
                        if callable(wake_projection):
                            try:
                                wake_projection()
                            except Exception:
                                pass
                    except Exception:
                        pass
                fallback_context = {
                    **targeted_context,
                    "_cognitive_fallback": True,
                }
                return await self._generate_question_payload_impl(
                    source_text=source_text,
                    topic=topic,
                    source=source,
                    source_question_id=source_question_id,
                    vision_image_payload=vision_image_payload,
                    targeted_context=fallback_context,
                )
            raise SdkError(
                validation_failure or str(exc) or "generated question failed validation",
                code="QUESTION_VALIDATION_FAILED",
            ) from exc
        if prepared_cognitive is not None:
            if cognitive_validation is None or not cognitive_validation.valid or not cognitive_validation.validation_id:
                fallback_context = {
                    **dict(targeted_context or {}),
                    "_cognitive_fallback": True,
                }
                return await self._generate_question_payload_impl(
                    source_text=source_text,
                    topic=topic,
                    source=source,
                    source_question_id=source_question_id,
                    vision_image_payload=vision_image_payload,
                    targeted_context=fallback_context,
                )
            question_instance = replace(
                question_instance,
                cognitive_validator_version=cognitive_validation.validator_version,
                diagnostic_validation_id=cognitive_validation.validation_id,
            )
            cognitive_fields["cognitive_validator_version"] = cognitive_validation.validator_version
            cognitive_fields["diagnostic_validation_id"] = cognitive_validation.validation_id
        reply = question_instance.generator_metadata.get("reply")
        if not isinstance(reply, TutorReply):
            raise SdkError("generated question is missing its internal reply")
        if targeted_context:
            await asyncio.to_thread(
                self._validate_learning_plan_selection_context,
                targeted_context,
            )
            async with self._lock:
                active_revision = int(getattr(self._state, "practice_scope_revision", 0) or 0)
                active_scope = self._resolve_active_practice_scope()
                active_scope_key = active_scope.scope_key if active_scope else ""
            if (
                int(targeted_context.get("scope_revision") or 0) != active_revision
                or str(targeted_context.get("scope_key") or "").strip() != active_scope_key
            ):
                raise SdkError(
                    "practice scope changed during question generation",
                    code="SELECTION_SCOPE_CHANGED",
                )
        committed_cognitive_event = None
        if prepared_cognitive is not None:
            try:
                committed_cognitive_event = committed_question_event(
                    prepared_cognitive,
                    question_id=question_instance.question_id,
                    validation=cognitive_validation,
                )
                record_event = getattr(
                    getattr(self, "_store", None),
                    "record_cognitive_intervention_event",
                    None,
                )
                if not callable(record_event):
                    raise RuntimeError("cognitive intervention ledger is unavailable")
                await asyncio.to_thread(
                    record_event,
                    asdict(committed_cognitive_event),
                )
                wake_projection = getattr(self, "_request_cognitive_projection", None)
                if callable(wake_projection):
                    try:
                        wake_projection()
                    except Exception:
                        # Projection is asynchronous and optional; the dirty
                        # generation prevents stale Active reads meanwhile.
                        pass
            except Exception:
                fallback_context = {
                    **dict(targeted_context or {}),
                    "_cognitive_fallback": True,
                }
                return await self._generate_question_payload_impl(
                    source_text=source_text,
                    topic=topic,
                    source=source,
                    source_question_id=source_question_id,
                    vision_image_payload=vision_image_payload,
                    targeted_context=fallback_context,
                )
        public_payload = None
        if targeted_context:
            target_binding = _server_target_binding(
                targeted_context,
                generated_at=reply.created_at,
            )
            target_binding.update(dict(plan.target_binding))
            if question_instance.hypothesis_target is not None:
                target_binding.update(
                    {
                        "plan_id": question_instance.plan_id,
                        "selection_reason": plan.selection.reason,
                        "learning_plan_id": str(targeted_context.get("learning_plan_id") or "").strip(),
                        "learning_plan_revision": int(targeted_context.get("learning_plan_revision") or 0),
                        "scope_key": plan.scope_key,
                        "scope_revision": plan.scope_revision,
                        "cognitive_learning_intent": (question_instance.learning_intent),
                        "cognitive_hypothesis_target": hypothesis_ref_payload(question_instance.hypothesis_target),
                        "cognitive_repair_strategy": (question_instance.repair_strategy),
                        "cognitive_decision_id": (question_instance.cognitive_decision_id),
                        "cognitive_validator_version": (question_instance.cognitive_validator_version),
                        "diagnostic_validation_id": cognitive_fields["diagnostic_validation_id"],
                        "cognitive_blueprint_id": cognitive_fields["cognitive_blueprint_id"],
                        "cognitive_question_family_id": cognitive_fields["cognitive_question_family_id"],
                    }
                )
            private_payload = _question_private_payload(
                dict(reply.payload or {}),
                {
                    "source": "targeted_question",
                    "target_binding": target_binding,
                    "selected_topic_id": targeted_context.get("selected_topic_id") or "",
                    "topic": targeted_context.get("selected_topic_id") or "",
                    "selected_topic_name": targeted_context.get("selected_topic_name") or "",
                    "selection_context_id": targeted_context.get("selection_context_id") or "",
                    "selection_reason": targeted_context.get("selection_reason") or "",
                    "selection_reason_payload": targeted_context.get("selection_reason_payload") or {},
                    "scope_key": targeted_context.get("scope_key") or "",
                    "scope_revision": targeted_context.get("scope_revision") or 0,
                    "practice_scope": targeted_context.get("practice_scope") or {},
                    "scope_topic_count": targeted_context.get("scope_topic_count") or 0,
                    "selection_domain": targeted_context.get("selection_domain") or "global",
                    "learning_plan_id": targeted_context.get("learning_plan_id") or "",
                    "learning_plan_revision": targeted_context.get("learning_plan_revision") or 0,
                    "plan_progress": targeted_context.get("plan_progress") or {},
                },
            )
            reply = TutorReply(
                operation=reply.operation,
                input_text=reply.input_text,
                reply=reply.reply,
                payload=private_payload,
                degraded=reply.degraded,
                diagnostic=reply.diagnostic,
                created_at=reply.created_at,
            )
            public_payload = _targeted_public_payload(private_payload)
        if source_question_id:
            reply.payload["source_question_id"] = source_question_id
        metadata_payload = public_payload if public_payload is not None else dict(reply.payload or {})
        finalize_metadata = {
            "degraded": reply.degraded,
            "diagnostic": reply.diagnostic,
            "payload": metadata_payload,
            "screen_classification": tutor_context.get("screen_classification") or {},
        }
        try:
            if targeted_context:
                async with self._practice_scope_write_lock():
                    await asyncio.to_thread(
                        self._validate_learning_plan_selection_context,
                        targeted_context,
                    )
                    async with self._lock:
                        active_revision = int(getattr(self._state, "practice_scope_revision", 0) or 0)
                        active_scope = self._resolve_active_practice_scope()
                        active_scope_key = active_scope.scope_key if active_scope else ""
                    if (
                        int(targeted_context.get("scope_revision") or 0) != active_revision
                        or str(targeted_context.get("scope_key") or "").strip() != active_scope_key
                    ):
                        raise SdkError(
                            "practice scope changed during question generation",
                            code="SELECTION_SCOPE_CHANGED",
                        )
                    payload = await self._finalize_tutor_call(
                        LLM_OPERATION_QUESTION_GENERATE,
                        reply,
                        history_kind=LLM_OPERATION_QUESTION_GENERATE,
                        metadata=finalize_metadata,
                        extra_context=tutor_context,
                        public_payload=public_payload,
                    )
            else:
                payload = await self._finalize_tutor_call(
                    LLM_OPERATION_QUESTION_GENERATE,
                    reply,
                    history_kind=LLM_OPERATION_QUESTION_GENERATE,
                    metadata=finalize_metadata,
                    extra_context=tutor_context,
                    public_payload=public_payload,
                )
        except BaseException:
            if committed_cognitive_event is not None:
                await _record_cognitive_abandonment_best_effort(
                    self,
                    committed_cognitive_event,
                    reason="question_commit_not_published",
                )
            raise
        payload["screen_classification"] = tutor_context.get("screen_classification") or {}
        if targeted_context:
            # Usage means an accepted question was committed as the current
            # attempt, not merely that a candidate was selected for an LLM.
            tracker = getattr(self, "_knowledge_tracker", None)
            record_usage = getattr(tracker, "record_prompt_usage_for_question_params", None)
            if callable(record_usage):
                await asyncio.to_thread(record_usage, targeted_context.get("question_params") or {})
        return payload

    async def _generate_question_payload(
        self,
        *,
        source_text: str,
        topic: str = "",
        source: str = "manual",
        source_question_id: str = "",
        vision_image_payload: str = "",
        targeted_context: dict[str, Any] | None = None,
        lifecycle_reserved: bool = False,
    ) -> dict[str, Any]:
        """Generate a question without allowing another request to replace it.

        The reservation spans the whole operation, including finalization that
        writes ``current_question``.  It deliberately does not hold ``_lock``
        while awaiting LLM calls; callers that subsequently need question state
        acquire the lifecycle reservation before ``_lock``.
        """

        if lifecycle_reserved:
            return await self._generate_question_payload_impl(
                source_text=source_text,
                topic=topic,
                source=source,
                source_question_id=source_question_id,
                vision_image_payload=vision_image_payload,
                targeted_context=targeted_context,
            )

        active_operation = await reserve_question_lifecycle(self, "question_generation")
        if active_operation:
            code = (
                "QUESTION_GENERATION_IN_PROGRESS"
                if active_operation == "question_generation"
                else "ANSWER_EVALUATION_IN_PROGRESS"
            )
            raise SdkError(
                "another study question operation is already in progress; retry shortly",
                code=code,
            )
        try:
            return await self._generate_question_payload_impl(
                source_text=source_text,
                topic=topic,
                source=source,
                source_question_id=source_question_id,
                vision_image_payload=vision_image_payload,
                targeted_context=targeted_context,
            )
        finally:
            await release_question_lifecycle(self, "question_generation")

    @ui.action()
    @plugin_entry(
        id="study_question_context",
        name=tr("entries.question_context.name", default="Study Question Context"),
        description=tr(
            "entries.question_context.description",
            default="Return the next adaptive practice target without generating a question.",
        ),
        input_schema={"type": "object", "properties": {}},
        timeout=30.0,
        llm_result_fields=[
            "selection_context_id",
            "selected_topic_id",
            "selected_topic_name",
            "selection_reason",
        ],
    )
    async def study_question_context(self, **_):
        try:
            context = await asyncio.to_thread(self._build_targeted_question_context)
            return Ok(
                {
                    "selection_context_id": context.get("selection_context_id") or "",
                    "selected_topic_id": context.get("selected_topic_id") or "",
                    "selected_topic_name": context.get("selected_topic_name") or "",
                    "selection_reason": context.get("selection_reason") or "no_data",
                    "selection_reason_payload": context.get("selection_reason_payload") or {},
                    "difficulty": context.get("difficulty") or 3,
                    "weak_topics": context.get("weak_topics") or [],
                    "due_reviews": context.get("due_reviews") or [],
                    "mastery_overview": context.get("mastery_overview") or [],
                    "no_data": bool(context.get("no_data")),
                    "scope_key": context.get("scope_key") or "",
                    "scope_revision": context.get("scope_revision") or 0,
                    "practice_scope": context.get("practice_scope") or {},
                    "scope_topic_count": context.get("scope_topic_count") or 0,
                    "selection_domain": context.get("selection_domain") or "global",
                    "learning_plan_id": context.get("learning_plan_id") or "",
                    "learning_plan_revision": context.get("learning_plan_revision") or 0,
                    "plan_progress": context.get("plan_progress") or {},
                    "expires_at": context.get("expires_at") or 0,
                }
            )
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:
            return _entry_exception_error(self, exc, operation="study_question_context")

    @ui.action()
    @plugin_entry(
        id="study_generate_targeted_question",
        name=tr(
            "entries.generate_targeted_question.name",
            default="Generate Adaptive Practice Question",
        ),
        description=tr(
            "entries.generate_targeted_question.description",
            default="Generate one adaptive practice question from tracked study data.",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "selection_context_id": {"type": "string", "default": ""},
            },
        },
        # Two bounded generation calls (45s each) and two fail-closed semantic
        # validations (15s each), plus a small framework handoff margin.
        timeout=TARGETED_GENERATION_TIMEOUT_SECONDS,
        llm_result_fields=[
            "question",
            "hint",
            "difficulty",
            "question_type",
            "question_id",
            "attempt_id",
            "selection_context_id",
            "selected_topic_id",
            "selected_topic_name",
            "selection_reason",
        ],
    )
    @_with_question_generation_reservation
    async def study_generate_targeted_question(self, selection_context_id: str = "", **_):
        if self._agent is None:
            return Err(SdkError("study tutor agent is not initialized"))
        try:
            context_id = str(selection_context_id or "").strip()
            if context_id:
                targeted_context = await asyncio.to_thread(self._load_targeted_context, context_id)
            else:
                pending_context = await asyncio.to_thread(self._build_targeted_question_context)
                pending_context_id = str(pending_context.get("selection_context_id") or "").strip()
                targeted_context = (
                    await asyncio.to_thread(self._load_targeted_context, pending_context_id)
                    if pending_context_id
                    else pending_context
                )
            if targeted_context.get("selection_reason") == "no_data":
                return Err(
                    SdkError(
                        "not enough tracked study data to generate a practice question",
                        code="NO_TARGETED_QUESTION_DATA",
                    )
                )
            source_text = (
                "Generate one adaptive practice question.\n"
                f"Target topic: {targeted_context.get('selected_topic_name') or targeted_context.get('selected_topic_id')}\n"
                f"Reason: {targeted_context.get('selection_reason')}\n"
                f"Guidance: {(targeted_context.get('question_params') or {}).get('prompt_guidance') or ''}"
            )
            payload = await self._generate_question_payload(
                source_text=source_text,
                topic=str(targeted_context.get("selected_topic_id") or ""),
                source="targeted_question",
                targeted_context=targeted_context,
                lifecycle_reserved=True,
            )
            return Ok(payload)
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:
            return _entry_exception_error(self, exc, operation="study_generate_targeted_question")

    @ui.action()
    @plugin_entry(
        id="study_generate_question",
        name=tr("entries.generate_question.name", default="Generate Study Question"),
        description=tr(
            "entries.generate_question.description",
            default="Generate one study question from supplied text or the latest OCR text.",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "default": ""},
                "topic": {"type": "string", "default": ""},
                "vision_image_base64": {"type": "string", "default": ""},
            },
        },
        timeout=70.0,
        llm_result_fields=[
            "summary",
            "question",
            "answer",
            "hint",
            "difficulty",
            "topic",
        ],
    )
    @_with_question_generation_reservation
    async def study_generate_question(
        self,
        text: str = "",
        topic: str = "",
        vision_image_base64: str = "",
        **_,
    ):
        if self._agent is None:
            return Err(SdkError("study tutor agent is not initialized"))
        source_text = str(text or "").strip()
        vision_image_payload = str(vision_image_base64 or "").strip()
        used_ocr_fallback = False
        if not source_text and not vision_image_payload:
            async with self._lock:
                source_text = self._state.last_ocr_text
            used_ocr_fallback = bool(source_text.strip())
        source_text = source_text.strip()
        ocr_derived_text = used_ocr_fallback
        current_ocr_matcher = getattr(self, "_is_current_ocr_text", None)
        if not ocr_derived_text and source_text and callable(current_ocr_matcher):
            ocr_derived_text = await current_ocr_matcher(source_text)
        if not source_text and not vision_image_payload:
            return Err(
                SdkError(
                    "study tutor requires text, an image, or a non-empty OCR snapshot",
                    code="MISSING_TEXT",
                )
            )
        validated_vision_image = _validate_optional_vision_image_payload(
            self, vision_image_payload, operation="study_generate_question"
        )
        if isinstance(validated_vision_image, Err):
            return validated_vision_image
        vision_image_payload = validated_vision_image
        try:
            image_only_source = False
            if not source_text and vision_image_payload:
                source_text = _image_only_question_prompt(self._cfg.language)
                image_only_source = True
            source_question_id = ""
            if ocr_derived_text:
                captured_question = await self._save_current_ocr_question(
                    consent_origin="generate",
                    topic_id=topic,
                    text=source_text,
                )
                source_question_id = str(captured_question.get("id") or "").strip()
            payload = await self._generate_question_payload(
                source_text=source_text,
                topic=topic,
                source=("ocr_snapshot" if used_ocr_fallback else ("vision_image" if image_only_source else "manual")),
                source_question_id=source_question_id,
                vision_image_payload=vision_image_payload,
                lifecycle_reserved=True,
            )
            return Ok(payload)
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:
            return _entry_exception_error(self, exc, operation="study_generate_question")
