from __future__ import annotations

import uuid

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
from .llm_prompts import ensure_targeted_prompt_context_fits
from .models import public_current_question_payload
from .practice_scope import (
    filter_question_params_to_scope,
    ordered_scope_topics,
    practice_scope_matches_topic,
)
from .targeted_question_contract import (
    semantic_validation_passed,
    validate_targeted_question,
)

IMAGE_ONLY_QUESTION_PROMPT_EN = "Generate a study question from the pasted image."
IMAGE_ONLY_QUESTION_PROMPT_ZH_CN = "请根据这张图片生成一道学习题。"
IMAGE_ONLY_QUESTION_PROMPT_ZH_TW = "請根據這張圖片生成一道學習題。"
TARGETED_SELECTION_TTL_SECONDS = 10 * 60
TARGETED_HINT_MAX_CHARS = 240
TARGETED_GENERATION_TIMEOUT_SECONDS = 125.0


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
        if text and (hint_lower == text.lower() or text.lower() in hint_lower):
            return ""
    for field_name in ("key_points", "solution_steps"):
        items = [
            str(item or "").strip()
            for item in (payload.get(field_name) or [])
            if str(item or "").strip()
        ]
        if len(items) >= 2 and all(item.lower() in hint_lower for item in items[:3]):
            return ""
    return hint


def _targeted_public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    public_payload = public_current_question_payload(payload)
    public_payload["hint"] = _safe_hint(payload)
    return public_payload


def _question_private_payload(
    payload: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
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
        "math_equivalence_engine": dict(
            private_payload.get("math_equivalence_engine") or {"enabled": False}
        ),
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
        key: source.get(key)
        for key in ("id", "topic_id", "error_type", "verdict")
        if source.get(key) not in (None, "")
    }


def _server_target_binding(
    targeted_context: dict[str, Any], *, generated_at: str
) -> dict[str, Any]:
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
    return {
        key: _without_learner_answers(value)
        for key, value in context.items()
        if key in allowed
    }


def _question_validation_context(
    payload: dict[str, Any], targeted_context: dict[str, Any]
) -> dict[str, Any]:
    params = dict(targeted_context.get("question_params") or {})
    target_topic = dict(params.get("target_topic") or {})
    target_metadata = {
        key: target_topic.get(key)
        for key in (
            "id",
            "name",
            "title",
            "subject",
            "stage",
            "course_family",
            "chapter",
            "unit",
            "description",
            "definition",
            "question_types",
            "common_mistakes",
        )
        if target_topic.get(key) not in (None, "", [], {})
    }
    raw_guidance = targeted_context.get("knowledge_guidance") or {}
    if isinstance(raw_guidance, dict):
        guidance = {
            key: raw_guidance.get(key)
            for key in ("prerequisites", "procedure", "confusions", "applications")
            if raw_guidance.get(key) not in (None, "", [], {})
        }
    else:
        guidance = {}
    if not guidance:
        guidance = {"blockers": params.get("blockers") or []}
    return {
        "question": payload.get("question") or "",
        "reference_answer": payload.get("reference_answer")
        or payload.get("answer")
        or "",
        "target_topic": target_metadata,
        "necessary_relations": guidance,
    }


class _TutorQuestionEntriesMixin:
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
            ordered = sorted(
                cache.items(), key=lambda item: float(item[1].get("created_at") or 0.0)
            )
            for old_id, _ in ordered[: max(0, len(cache) - 32)]:
                cache.pop(old_id, None)
        return stored

    def _load_targeted_context(self, selection_context_id: str) -> dict[str, Any]:
        context_lock = getattr(self, "_targeted_context_lock", None)
        if context_lock is None:
            return self._load_targeted_context_locked(selection_context_id)
        with context_lock:
            return self._load_targeted_context_locked(selection_context_id)

    def _load_targeted_context_locked(
        self, selection_context_id: str
    ) -> dict[str, Any]:
        self._prune_targeted_context_cache()
        context_id = str(selection_context_id or "").strip()
        cache = self._targeted_context_cache()
        cached = cache.get(context_id)
        if not cached or cached.get("consumed"):
            raise SdkError(
                "selection context expired", code="SELECTION_CONTEXT_EXPIRED"
            )
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
            raise SdkError(
                "selection context expired", code="SELECTION_CONTEXT_EXPIRED"
            )
        if active_scope and topic_id not in set(active_scope.eligible_topic_ids):
            cache.pop(context_id, None)
            raise SdkError(
                "selected topic is outside the active practice scope",
                code="SELECTION_SCOPE_CHANGED",
            )
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
            selected_topic_id = str(
                first_due.get("topic_id") or due_topic.get("id") or selected_topic_id
            ).strip()
            selected_topic_name = _topic_name(due_topic, selected_topic_id)
            reason = "due_review"
            reason_payload = {"due_review": first_due}
        elif weak_topics:
            first_weak = dict(weak_topics[0] or {})
            selected_topic_id = str(
                first_weak.get("topic_id") or first_weak.get("id") or selected_topic_id
            ).strip()
            selected_topic_name = str(
                first_weak.get("name") or first_weak.get("topic") or selected_topic_id
            ).strip()
            reason = "weak_topic"
            reason_payload = {"weak_topic": first_weak}
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
                [topic]
                if topic is not None
                and practice_scope_matches_topic(scope.to_public_dict(), topic)
                else []
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
        mastery_overview = self._knowledge_tracker.store.list_latest_mastery_for_topics(
            eligible
        )
        mastery_by_topic = {
            str(item.get("topic_id") or ""): dict(item)
            for item in mastery_overview
            if str(item.get("topic_id") or "") in eligible
        }
        ordered_topics = ordered_scope_topics(
            topics, attempted_topic_ids=set(mastery_by_topic)
        )
        if not ordered_topics:
            raise SdkError(
                "practice scope no longer contains any topics",
                code="PRACTICE_SCOPE_INVALIDATED",
            )
        unattempted = [
            topic
            for topic in ordered_topics
            if str(topic.get("id") or "") not in mastery_by_topic
        ]
        if unattempted:
            fallback_topic = unattempted[0]
        else:
            fallback_topic = min(
                ordered_topics,
                key=lambda topic: (
                    float(
                        mastery_by_topic.get(str(topic.get("id") or ""), {}).get(
                            "mastery"
                        )
                        or 0.0
                    ),
                    str(topic.get("id") or ""),
                ),
            )
        target_topic_id = scope.topic_id or str(fallback_topic.get("id") or "")
        topics_by_id = {
            str(topic.get("id") or ""): dict(topic)
            for topic in topics
            if str(topic.get("id") or "")
        }
        params = self._knowledge_tracker.preview_next_question_params(
            target_topic_id,
            candidate_topic_ids=eligible,
            candidate_limit=5000,
            candidate_topics_by_id=topics_by_id,
        )
        params["retry_wrong_questions"] = (
            self._knowledge_tracker.store.list_wrong_questions(
                limit=5000,
                topic_ids=eligible,
                statuses=("active", "retrying"),
            )
        )
        params = filter_question_params_to_scope(params, eligible)
        if scope.mode == "explicit_topic":
            params["weak_topics"] = []
            params["candidate_evidence"] = []
        if not params.get("target_topic_id"):
            params["target_topic_id"] = target_topic_id
            params["target_topic"] = (
                self._knowledge_tracker.store.get_topic(target_topic_id) or {}
            )
        params["mastery_overview"] = list(mastery_by_topic.values())
        return params

    def _unscoped_question_params(self) -> dict[str, Any]:
        params = self._knowledge_tracker.preview_next_question_params("")
        retries = self._knowledge_tracker.store.list_wrong_questions(
            limit=5000,
            statuses=("active", "retrying"),
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
        focused["target_topic_id"] = selected_topic_id
        focused["target_topic"] = (
            self._knowledge_tracker.store.get_topic(selected_topic_id) or {}
        )
        focused["scope_candidates"] = {
            "retry_wrong_questions": initial_params.get("retry_wrong_questions") or [],
            "due_reviews": initial_params.get("due_reviews") or [],
            "weak_topics": initial_params.get("weak_topics") or [],
        }
        selection["question_params"] = focused
        selection["difficulty"] = (
            focused.get("suggested_difficulty") or selection["difficulty"]
        )

    def _build_targeted_question_context(self) -> dict[str, Any]:
        scope = self._resolve_active_practice_scope()
        params = (
            self._scoped_question_params(scope)
            if scope is not None
            else self._unscoped_question_params()
        )
        selection = self._selection_from_question_params(params)
        self._focus_selected_question_params(selection, params, scope)
        if scope is not None:
            selection.update(
                {
                    "scope_key": scope.scope_key,
                    "scope_revision": scope.scope_revision,
                    "practice_scope": scope.to_public_dict(),
                    "scope_topic_count": len(scope.eligible_topic_ids),
                    "mastery_overview": params.get("mastery_overview") or [],
                }
            )
        else:
            selection.update(
                {
                    "scope_key": "",
                    "scope_revision": int(
                        getattr(self._state, "practice_scope_revision", 0) or 0
                    ),
                    "practice_scope": {},
                    "scope_topic_count": 0,
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

    async def _generate_question_payload(
        self,
        *,
        source_text: str,
        topic: str = "",
        source: str = "manual",
        vision_image_payload: str = "",
        targeted_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            active_mode = self._state.active_mode
        extra_context = {
            "source": source,
            "source_text": source_text,
            "topic_hint": str(topic or "").strip(),
            "mode": active_mode,
        }
        if targeted_context:
            extra_context.update(
                {
                    "source": "targeted_question",
                    "targeted_question": True,
                    "selected_topic_id": targeted_context.get("selected_topic_id")
                    or "",
                    "selected_topic_name": targeted_context.get("selected_topic_name")
                    or "",
                    "selection_context_id": targeted_context.get("selection_context_id")
                    or "",
                    "selection_reason": targeted_context.get("selection_reason") or "",
                    "selection_reason_payload": targeted_context.get(
                        "selection_reason_payload"
                    )
                    or {},
                    "knowledge_question_params": targeted_context.get("question_params")
                    or {},
                    "scope_key": targeted_context.get("scope_key") or "",
                    "scope_revision": targeted_context.get("scope_revision") or 0,
                    "practice_scope": targeted_context.get("practice_scope") or {},
                    "scope_topic_count": targeted_context.get("scope_topic_count") or 0,
                }
            )
        if vision_image_payload:
            extra_context.update(
                {"vision_enabled": True, "vision_image_base64": vision_image_payload}
            )
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
        for generation_attempt in range(attempts):
            generation_context = dict(tutor_context)
            if validation_failure:
                generation_context["generation_feedback"] = validation_failure
            candidate_reply = await self._agent.question_generate(
                source_text, mode=active_mode, context=generation_context
            )
            if not targeted_context:
                reply = candidate_reply
                break
            candidate_payload = dict(candidate_reply.payload or {})
            # The binding is authoritative server state, never an LLM claim.
            candidate_payload["target_topic_id"] = str(
                targeted_context.get("selected_topic_id") or ""
            )
            candidate_reply = TutorReply(
                operation=candidate_reply.operation,
                input_text=candidate_reply.input_text,
                reply=candidate_reply.reply,
                payload=candidate_payload,
                degraded=candidate_reply.degraded,
                diagnostic=candidate_reply.diagnostic,
                created_at=candidate_reply.created_at,
            )
            params = dict(targeted_context.get("question_params") or {})
            structural = validate_targeted_question(
                candidate_payload,
                target_topic_id=str(targeted_context.get("selected_topic_id") or ""),
                target_topic_name=str(
                    targeted_context.get("selected_topic_name") or ""
                ),
                origin_wrong_question=dict(params.get("retry_wrong_question") or {}),
            )
            if not structural.valid:
                validation_failure = "Structural validation failed: " + ", ".join(
                    structural.errors
                )
                continue
            validation_reply = await self._agent.question_validate(
                context=_question_validation_context(
                    candidate_payload,
                    {
                        **targeted_context,
                        "knowledge_guidance": generation_context.get(
                            "knowledge_guidance"
                        )
                        or {},
                    },
                )
            )
            if not semantic_validation_passed(
                dict(validation_reply.payload or {}), degraded=validation_reply.degraded
            ):
                validation_failure = "Semantic validation failed: " + str(
                    (validation_reply.payload or {}).get("reason")
                    or validation_reply.diagnostic
                    or "retry"
                )
                continue
            candidate_payload.pop("_targeted_difficulty_valid", None)
            candidate_reply = TutorReply(
                operation=candidate_reply.operation,
                input_text=candidate_reply.input_text,
                reply=candidate_reply.reply,
                payload=candidate_payload,
                degraded=candidate_reply.degraded,
                diagnostic=candidate_reply.diagnostic,
                created_at=candidate_reply.created_at,
            )
            reply = candidate_reply
            break
        if reply is None:
            raise SdkError(
                validation_failure or "generated question failed validation",
                code="QUESTION_VALIDATION_FAILED",
            )
        if targeted_context:
            async with self._lock:
                active_revision = int(
                    getattr(self._state, "practice_scope_revision", 0) or 0
                )
                active_scope = self._resolve_active_practice_scope()
                active_scope_key = active_scope.scope_key if active_scope else ""
            if (
                int(targeted_context.get("scope_revision") or 0) != active_revision
                or str(targeted_context.get("scope_key") or "").strip()
                != active_scope_key
            ):
                raise SdkError(
                    "practice scope changed during question generation",
                    code="SELECTION_SCOPE_CHANGED",
                )
        public_payload = None
        if targeted_context:
            private_payload = _question_private_payload(
                dict(reply.payload or {}),
                {
                    "source": "targeted_question",
                    "target_binding": _server_target_binding(
                        targeted_context,
                        generated_at=reply.created_at,
                    ),
                    "selected_topic_id": targeted_context.get("selected_topic_id")
                    or "",
                    "topic": targeted_context.get("selected_topic_id") or "",
                    "selected_topic_name": targeted_context.get("selected_topic_name")
                    or "",
                    "selection_context_id": targeted_context.get("selection_context_id")
                    or "",
                    "selection_reason": targeted_context.get("selection_reason") or "",
                    "selection_reason_payload": targeted_context.get(
                        "selection_reason_payload"
                    )
                    or {},
                    "scope_key": targeted_context.get("scope_key") or "",
                    "scope_revision": targeted_context.get("scope_revision") or 0,
                    "practice_scope": targeted_context.get("practice_scope") or {},
                    "scope_topic_count": targeted_context.get("scope_topic_count") or 0,
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
        metadata_payload = (
            public_payload if public_payload is not None else dict(reply.payload or {})
        )
        finalize_metadata = {
            "degraded": reply.degraded,
            "diagnostic": reply.diagnostic,
            "payload": metadata_payload,
            "screen_classification": tutor_context.get("screen_classification") or {},
        }
        if targeted_context:
            async with self._practice_scope_write_lock():
                async with self._lock:
                    active_revision = int(
                        getattr(self._state, "practice_scope_revision", 0) or 0
                    )
                    active_scope = self._resolve_active_practice_scope()
                    active_scope_key = active_scope.scope_key if active_scope else ""
                if (
                    int(targeted_context.get("scope_revision") or 0) != active_revision
                    or str(targeted_context.get("scope_key") or "").strip()
                    != active_scope_key
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
        payload["screen_classification"] = (
            tutor_context.get("screen_classification") or {}
        )
        return payload

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
                    "selection_reason_payload": context.get("selection_reason_payload")
                    or {},
                    "difficulty": context.get("difficulty") or 3,
                    "weak_topics": context.get("weak_topics") or [],
                    "due_reviews": context.get("due_reviews") or [],
                    "mastery_overview": context.get("mastery_overview") or [],
                    "no_data": bool(context.get("no_data")),
                    "scope_key": context.get("scope_key") or "",
                    "scope_revision": context.get("scope_revision") or 0,
                    "practice_scope": context.get("practice_scope") or {},
                    "scope_topic_count": context.get("scope_topic_count") or 0,
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
    async def study_generate_targeted_question(
        self, selection_context_id: str = "", **_
    ):
        if self._agent is None:
            return Err(SdkError("study tutor agent is not initialized"))
        try:
            context_id = str(selection_context_id or "").strip()
            if context_id:
                targeted_context = await asyncio.to_thread(
                    self._load_targeted_context, context_id
                )
            else:
                pending_context = await asyncio.to_thread(
                    self._build_targeted_question_context
                )
                pending_context_id = str(
                    pending_context.get("selection_context_id") or ""
                ).strip()
                targeted_context = (
                    await asyncio.to_thread(
                        self._load_targeted_context, pending_context_id
                    )
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
            await asyncio.to_thread(
                self._knowledge_tracker.record_prompt_usage_for_question_params,
                targeted_context.get("question_params") or {},
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
            )
            return Ok(payload)
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:
            return _entry_exception_error(
                self, exc, operation="study_generate_targeted_question"
            )

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
            payload = await self._generate_question_payload(
                source_text=source_text,
                topic=topic,
                source=(
                    "ocr_snapshot"
                    if used_ocr_fallback
                    else ("vision_image" if image_only_source else "manual")
                ),
                vision_image_payload=vision_image_payload,
            )
            return Ok(payload)
        except Exception as exc:
            return _entry_exception_error(
                self, exc, operation="study_generate_question"
            )
