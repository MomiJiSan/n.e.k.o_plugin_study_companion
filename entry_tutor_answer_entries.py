from __future__ import annotations

import contextlib
from collections.abc import Mapping
from types import SimpleNamespace

from .adaptive_learning.answer_application import AnswerAssessmentService
from .adaptive_learning.assessment import AssessmentEngine, AssessmentRequest
from .adaptive_learning.deterministic_evaluators import (
    ExactShortAnswerEvaluator,
    MathExpressionEvaluator,
    NumericToleranceEvaluator,
)
from .entry_common import (
    LLM_OPERATION_ANSWER_EVALUATE,
    Err,
    Ok,
    SdkError,
    _entry_exception_error,
    _validate_optional_vision_image_payload,
    asyncio,
    plugin_entry,
    tr,
    ui,
)
from .evaluation_contract import canonicalize_evaluation, validate_evaluation
from .models import public_current_question_payload
from .practice_outcome import build_practice_outcome
from .request_locale import normalize_request_locale
from .target_binding import validated_target_topic_id
from .tutor_lifecycle import (
    release_question_lifecycle,
    reserve_question_lifecycle,
)

_MAX_RESPONSE_TIME_MS = 24 * 60 * 60 * 1000


def _attempt_signal_values(kwargs: Mapping[str, object]) -> tuple[int | None, bool | None]:
    """Keep optional client timing signals bounded and type-safe."""

    raw_response_time = kwargs.get("response_time_ms")
    response_time_ms = (
        raw_response_time
        if isinstance(raw_response_time, int)
        and not isinstance(raw_response_time, bool)
        and 0 <= raw_response_time <= _MAX_RESPONSE_TIME_MS
        else None
    )
    raw_used_hint = kwargs.get("used_hint")
    return response_time_ms, raw_used_hint if isinstance(raw_used_hint, bool) else None


class _TutorAnswerEntriesMixin:
    def _learning_update_evidence(
        self,
        *,
        topic_id: str,
        payload: Mapping[str, object],
        question_payload: Mapping[str, object],
        mastery_status: str,
    ) -> dict:
        """Read post-commit display facts without changing the answer transaction."""

        store = self._knowledge_tracker.store
        wrong_status = ""
        binding = question_payload.get("target_binding")
        origin_wrong_id = str(
            (binding.get("origin_wrong_question_id") if isinstance(binding, Mapping) else "")
            or ""
        ).strip()
        if origin_wrong_id:
            get_wrong = getattr(store, "get_wrong_question", None)
            wrong = get_wrong(origin_wrong_id) if callable(get_wrong) else None
            if isinstance(wrong, Mapping):
                wrong_status = str(wrong.get("status") or "").strip()
        if not wrong_status:
            active_wrong = store.list_wrong_questions(
                limit=1,
                topic_id=topic_id,
                statuses=("active", "retrying"),
            )
            if active_wrong:
                wrong_status = str((active_wrong[0] or {}).get("status") or "active").strip()

        next_review_at = ""
        get_fsrs_card = getattr(store, "get_fsrs_card", None)
        card_row = get_fsrs_card(topic_id) if callable(get_fsrs_card) else None
        card = card_row.get("card") if isinstance(card_row, Mapping) else None
        if isinstance(card, Mapping):
            next_review_at = str(card.get("due") or "").strip()

        def optional_float(value: object) -> float | None:
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError, OverflowError):
                return None

        return {
            "status": "updated",
            "topic_id": topic_id,
            "mastery_before": optional_float(payload.get("mastery_before")),
            "mastery_after": optional_float(payload.get("mastery_after")),
            "mastery_status": str(mastery_status or "insufficient_evidence"),
            "wrong_question_status": wrong_status,
            "next_review_at": next_review_at,
        }

    async def _build_adaptive_next_step(
        self,
        *,
        question_payload: Mapping[str, object],
        learning_update: dict,
        validated_target: bool,
        knowledge_tracking_status: str,
    ) -> tuple[dict, dict]:
        if knowledge_tracking_status == "qa_only" or not validated_target:
            not_applicable = {"status": "not_applicable"}
            return not_applicable, {
                "status": "not_applicable",
                "action": "choose_scope",
                "available_now": False,
            }

        adaptive_loop = getattr(getattr(self, "_cfg", None), "adaptive_loop", None)
        preview_enabled = getattr(
            adaptive_loop,
            "next_step_preview_enabled",
            getattr(getattr(self, "_cfg", None), "next_step_preview_enabled", True),
        )
        if preview_enabled is False:
            return learning_update, {
                "status": "disabled",
                "action": "choose_scope",
                "available_now": False,
            }

        try:
            plan_id = str(question_payload.get("learning_plan_id") or "").strip()
            plan_progress: dict = {}
            if plan_id:
                service_provider = getattr(self, "_learning_plan_service", None)
                service = service_provider() if callable(service_provider) else service_provider
                reconcile = getattr(service, "reconcile", None)
                if not callable(reconcile):
                    raise RuntimeError("learning plan service unavailable")
                plan_status = await asyncio.to_thread(reconcile, plan_id)
                if isinstance(plan_status, Mapping):
                    plan_progress = dict(plan_status.get("progress") or {})
                    learning_update["plan_progress"] = plan_progress
                    if str(plan_status.get("status") or "") == "completed":
                        return learning_update, {
                            "status": "ready",
                            "action": "summarize_plan",
                            "learning_plan_id": plan_id,
                            "plan_progress": plan_progress,
                            "available_now": True,
                        }

            build_context = getattr(self, "_build_targeted_question_context", None)
            if not callable(build_context):
                raise RuntimeError("adaptive question context unavailable")
            context = await asyncio.to_thread(build_context)
            if not isinstance(context, Mapping) or context.get("no_data"):
                next_review_at = str(learning_update.get("next_review_at") or "").strip()
                return learning_update, {
                    "status": "ready",
                    "action": "wait_until" if next_review_at else "choose_scope",
                    "wait_until": next_review_at,
                    "available_now": False,
                    "plan_progress": plan_progress,
                }
            reason = str(context.get("selection_reason") or "recommended")
            action = "review_due" if reason == "due_review" else "generate_question"
            return learning_update, {
                "status": "ready",
                "action": action,
                "selection_context_id": str(context.get("selection_context_id") or ""),
                "expires_at": context.get("expires_at") or 0,
                "reason": reason,
                "topic_id": str(context.get("selected_topic_id") or ""),
                "topic_name": str(context.get("selected_topic_name") or ""),
                "difficulty": context.get("difficulty") or 3,
                "available_now": bool(context.get("selection_context_id")),
                "selection_domain": str(context.get("selection_domain") or "global"),
                "learning_plan_id": str(context.get("learning_plan_id") or ""),
                "learning_plan_revision": context.get("learning_plan_revision") or 0,
                "plan_progress": dict(context.get("plan_progress") or plan_progress),
            }
        except Exception as exc:
            logger = getattr(self, "logger", None)
            if logger is not None:
                logger.warning("study adaptive next step preview failed: {}", exc)
            return learning_update, {
                "status": "temporarily_unavailable",
                "action": "choose_scope",
                "available_now": False,
            }

    def _deterministic_assessment_flags(self) -> dict[str, bool]:
        """Read only explicit opt-in assessment switches from runtime config."""

        assessment = getattr(getattr(self, "_cfg", None), "assessment", None)
        return {
            "exact_short_answer_enabled": getattr(
                assessment, "exact_short_answer_enabled", False
            )
            is True,
            "numeric_tolerance_enabled": getattr(
                assessment, "numeric_tolerance_enabled", False) is True,
            "math_expression_enabled": getattr(
                assessment, "math_expression_enabled", False) is True,
        }

    async def _try_deterministic_assessment(
        self,
        *,
        question: str,
        answer: str,
        expected_answer: str,
        mode: str,
        question_payload: Mapping[str, object],
    ) -> object | None:
        """Return a certain private-answer assessment, otherwise keep the LLM path.

        The request context contains only server-held question fields.  The
        deterministic decision is intentionally generic so neither expected
        answers nor parser inputs can enter the public evaluator payload.
        """

        flags = self._deterministic_assessment_flags()
        if not any(flags.values()):
            return None
        answer_spec = question_payload.get("answer_spec")
        equivalence_engine = question_payload.get("math_equivalence_engine")
        assessment = await AnswerAssessmentService(
            AssessmentEngine(
                deterministic_evaluators=(
                    ExactShortAnswerEvaluator(),
                    NumericToleranceEvaluator(),
                    MathExpressionEvaluator(),
                ),
                feature_flags=flags,
            )
        ).try_assess_request(
            AssessmentRequest(
                question=question,
                answer=answer,
                expected_answer=expected_answer,
                mode=mode,
                context={
                    "question_type": str(
                        question_payload.get("question_type")
                        or question_payload.get("type")
                        or ""
                    ),
                    "accepted_answers": question_payload.get("accepted_answers"),
                    "answer_spec": (
                        dict(answer_spec)
                        if isinstance(answer_spec, Mapping)
                        else {}
                    ),
                    "closed_world": question_payload.get("closed_world") is True,
                    "math_equivalence_engine": (
                        dict(equivalence_engine)
                        if isinstance(equivalence_engine, Mapping)
                        else {}
                    ),
                },
            )
        )
        if assessment is None:
            return None
        payload = dict(assessment.payload)
        payload.update(
            {
                "evaluator_type": assessment.evaluation.evaluator_type,
                "evaluator_version": assessment.evaluation.evaluator_version,
                "confidence": assessment.evaluation.confidence,
                "fallback_reason": assessment.evaluation.fallback_reason or "",
            }
        )
        return SimpleNamespace(
            operation=LLM_OPERATION_ANSWER_EVALUATE,
            input_text=answer,
            reply=str(payload.get("feedback") or ""),
            payload=payload,
            degraded=False,
            diagnostic="",
            created_at="",
        )

    def _load_practice_mastery_evidence(
        self, topic_id: str
    ) -> tuple[dict | None, bool]:
        store = self._knowledge_tracker.store
        get_snapshot = getattr(self._knowledge_tracker, "get_mastery_snapshot", None)
        snapshot = (
            get_snapshot(topic_id)
            if callable(get_snapshot)
            else store.get_latest_mastery(topic_id)
        )
        active_wrong_questions = store.list_wrong_questions(
            limit=1,
            topic_id=topic_id,
            statuses=("active", "retrying"),
        )
        return snapshot, bool(active_wrong_questions)

    async def _question_matches_active_practice_scope(
        self, question_payload: Mapping[str, object]
    ) -> bool:
        try:
            question_scope_key = str(
                question_payload.get("scope_key") or ""
            ).strip()
            question_scope_revision = int(
                question_payload.get("scope_revision") or 0
            )
        except (TypeError, ValueError, OverflowError):
            return False
        if not question_scope_key:
            return False
        try:
            # Match the lock order used by scope writes so key and revision are
            # observed from one coherent state transition.
            async with self._practice_scope_write_lock():
                async with self._lock:
                    stored_scope = getattr(
                        self._state, "active_practice_scope", {}
                    )
                    active_scope = (
                        dict(stored_scope)
                        if isinstance(stored_scope, Mapping)
                        else {}
                    )
                    active_revision = int(
                        getattr(self._state, "practice_scope_revision", 0) or 0
                    )
        except Exception as exc:
            logger = getattr(self, "logger", None)
            if logger is not None:
                logger.warning(
                    "study active practice scope read failed: {}", exc
                )
            return False
        return (
            question_scope_key
            == str(active_scope.get("scope_key") or "").strip()
            and question_scope_revision == active_revision
        )

    async def _build_practice_outcome_payload(
        self,
        *,
        payload: dict,
        question_payload: dict,
        current_question: dict,
        question_source: str,
    ) -> dict:
        topic_id = validated_target_topic_id(
            question_payload,
            current_question,
            question_source=question_source,
        )
        validated_target = bool(
            topic_id
            and str(payload.get("knowledge_tracking_status") or "") != "qa_only"
        )
        target_bound = validated_target
        mastery_snapshot = None
        has_active_wrong_question = False
        active_scope_matches = False
        if validated_target:
            try:
                (
                    mastery_snapshot,
                    has_active_wrong_question,
                ) = await asyncio.to_thread(
                    self._load_practice_mastery_evidence, topic_id
                )
            except Exception as exc:
                logger = getattr(self, "logger", None)
                if logger is not None:
                    logger.warning(
                        "study practice outcome enrichment failed: {}", exc
                    )
                validated_target = False
            if validated_target:
                active_scope_matches = (
                    await self._question_matches_active_practice_scope(
                        question_payload
                    )
                )
        outcome = build_practice_outcome(
            verdict=payload.get("verdict"),
            practice_scope=(
                question_payload.get("practice_scope")
                if isinstance(question_payload.get("practice_scope"), dict)
                else {}
            ),
            active_scope_matches=active_scope_matches,
            validated_target=validated_target,
            mastery_snapshot=mastery_snapshot,
            has_active_wrong_question=has_active_wrong_question,
        )
        knowledge_tracking_status = str(
            payload.get("knowledge_tracking_status") or ""
        ).strip()
        if knowledge_tracking_status == "qa_only" or not target_bound:
            learning_update: dict = {"status": "not_applicable"}
        else:
            try:
                learning_update = await asyncio.to_thread(
                    self._learning_update_evidence,
                    topic_id=topic_id,
                    payload=payload,
                    question_payload=question_payload,
                    mastery_status=str(outcome.get("mastery_status") or ""),
                )
            except Exception as exc:
                logger = getattr(self, "logger", None)
                if logger is not None:
                    logger.warning("study learning update enrichment failed: {}", exc)
                learning_update = {
                    "status": "temporarily_unavailable",
                    "topic_id": topic_id,
                    "mastery_before": payload.get("mastery_before"),
                    "mastery_after": payload.get("mastery_after"),
                    "mastery_status": outcome.get("mastery_status") or "",
                }
        learning_update, next_step = await self._build_adaptive_next_step(
            question_payload=question_payload,
            learning_update=learning_update,
            validated_target=target_bound,
            knowledge_tracking_status=knowledge_tracking_status,
        )
        return {
            **outcome,
            "learning_update": learning_update,
            "next_step": next_step,
        }

    async def _clear_attempt_evaluation_reservation(
        self, attempt_id: str, *, recover_cached: bool = False
    ) -> None:
        if not attempt_id:
            return
        async with self._lock:
            if str(self._state.current_question.get("attempt_id") or "") == attempt_id:
                self._state.current_question.pop("attempt_evaluation_pending", None)
                if (
                    recover_cached
                    and self._state.current_question.get("attempt_evaluated")
                    and isinstance(
                        self._state.current_question.get("answer_evaluation_cache"),
                        dict,
                    )
                ):
                    self._state.current_question["attempt_evaluation_recovery"] = True

    @ui.action()
    @plugin_entry(
        id="study_evaluate_answer",
        name=tr("entries.evaluate_answer.name", default="Evaluate Study Answer"),
        description=tr(
            "entries.evaluate_answer.description",
            default="Evaluate an answer against the current generated question or a supplied question.",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "answer": {"type": "string", "default": ""},
                "question": {"type": "string", "default": ""},
                "expected_answer": {"type": "string", "default": ""},
                "question_id": {"type": "string", "default": ""},
                "attempt_id": {"type": "string", "default": ""},
                "selected_topic_id": {"type": "string", "default": ""},
                "response_time_ms": {"type": "integer"},
                "used_hint": {"type": "boolean"},
                "vision_image_base64": {"type": "string", "default": ""},
                "locale": {"type": "string", "maxLength": 16, "default": ""},
            },
        },
        timeout=70.0,
        llm_result_fields=[
            "summary",
            "verdict",
            "score",
            "error_type",
            "feedback",
            "next_action",
            "attempt_status",
            "scope_status",
            "mastery_status",
            "learning_update",
            "next_step",
        ],
    )
    async def study_evaluate_answer(
        self, answer: str = "", question: str = "", expected_answer: str = "", **kwargs
    ):
        active_operation = await reserve_question_lifecycle(
            self, "answer_evaluation"
        )
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
            return await self._study_evaluate_answer_impl(
                answer=answer,
                question=question,
                expected_answer=expected_answer,
                **kwargs,
            )
        finally:
            await release_question_lifecycle(self, "answer_evaluation")

    async def _study_evaluate_answer_impl(
        self, answer: str = "", question: str = "", expected_answer: str = "", **kwargs
    ):
        # Preserve the legacy unavailable-agent result whenever every new
        # evaluator is disabled.  With an explicit opt-in flag enabled we
        # continue far enough to allow a certain local assessment.
        if self._agent is None and not any(
            self._deterministic_assessment_flags().values()
        ):
            return Err(SdkError("study tutor agent is not initialized"))
        target_lanlan = self._resolve_study_target_lanlan(kwargs)
        response_time_ms, used_hint = _attempt_signal_values(kwargs)
        request_language = normalize_request_locale(
            kwargs.get("locale"),
            fallback=getattr(getattr(self, "_cfg", None), "language", "zh-CN"),
        )
        async with self._lock:
            current_question = dict(self._state.current_question)
            active_mode = self._state.active_mode
            previous_answer_state = {
                "last_answer_evaluation": dict(
                    getattr(self._state, "last_answer_evaluation", {}) or {}
                ),
                "last_answer_evaluated_at": str(
                    getattr(self._state, "last_answer_evaluated_at", "") or ""
                ),
                "recent_learning_events": list(
                    getattr(self._state, "recent_learning_events", []) or []
                ),
                "session_summary_seed": dict(
                    getattr(self._state, "session_summary_seed", {}) or {}
                ),
            }
        supplied_question = str(question or "").strip()
        supplied_expected = str(expected_answer or "").strip()
        state_question = str(current_question.get("question") or "").strip()
        state_expected = str(current_question.get("answer") or "").strip()
        supplied_question_id = str(kwargs.get("question_id") or "").strip()
        supplied_attempt_id = str(kwargs.get("attempt_id") or "").strip()
        state_question_id = str(current_question.get("question_id") or "").strip()
        state_attempt_id = str(current_question.get("attempt_id") or "").strip()
        current_question_requires_identity = bool(state_question_id or state_attempt_id)
        supplied_current_identity = bool(
            current_question_requires_identity
            and (
                (
                    supplied_question_id
                    and state_question_id
                    and supplied_question_id == state_question_id
                )
                or (
                    supplied_attempt_id
                    and state_attempt_id
                    and supplied_attempt_id == state_attempt_id
                )
            )
        )
        using_current_question = (
            not supplied_question
            or supplied_question == state_question
            or supplied_current_identity
        )
        if current_question_requires_identity and using_current_question:
            if (
                not supplied_question_id
                or not supplied_attempt_id
                or supplied_question_id != state_question_id
                or supplied_attempt_id != state_attempt_id
            ):
                return Err(
                    SdkError(
                        "current question identity does not match",
                        code="QUESTION_MISMATCH",
                    )
                )
            if current_question.get("attempt_evaluated"):
                cached_evaluation = current_question.get("answer_evaluation_cache")
                if current_question.get("attempt_evaluation_recovery") and isinstance(
                    cached_evaluation, dict
                ):
                    return Ok(dict(cached_evaluation))
                return Err(
                    SdkError(
                        "attempt has already been evaluated",
                        code="ATTEMPT_ALREADY_EVALUATED",
                    )
                )
        resolved_question = (
            state_question if using_current_question else supplied_question
        )
        if not resolved_question:
            return Err(SdkError("study tutor requires a question to evaluate against"))
        vision_image_payload = str(kwargs.get("vision_image_base64") or "").strip()
        validated_vision_image = _validate_optional_vision_image_payload(
            self, vision_image_payload, operation="study_evaluate_answer"
        )
        if isinstance(validated_vision_image, Err):
            return validated_vision_image
        vision_image_payload = validated_vision_image
        if using_current_question:
            if supplied_expected and supplied_expected != state_expected:
                return Err(
                    SdkError(
                        "expected answer does not match the current question",
                        code="QUESTION_MISMATCH",
                    )
                )
            resolved_expected = state_expected
        else:
            resolved_expected = supplied_expected
        answer_text = str(answer or "").strip()
        # A validated image is a real learner response even when the text box
        # is empty.  Keep the persisted/model-facing text unchanged; this
        # marker is used only by the semantic consistency contract so an
        # image answer is not forced into the ``dont_know`` verdict.
        evaluation_learner_answer = answer_text or (
            "[validated image answer]" if vision_image_payload else ""
        )
        question_payload = dict(current_question) if using_current_question else {}
        question_payload.update(
            {
                "question": resolved_question,
                "answer": resolved_expected,
            }
        )
        source_question_id = str(
            question_payload.get("source_question_id") or ""
        ).strip()
        if not source_question_id:
            async with self._lock:
                latest_ocr_text = str(
                    getattr(self._state, "last_ocr_text", "") or ""
                ).strip()
            if latest_ocr_text and latest_ocr_text == resolved_question:
                captured_question = await self._save_current_ocr_question(
                    consent_origin="evaluate",
                    text=resolved_question,
                )
                source_question_id = str(captured_question.get("id") or "").strip()
                question_payload["source_question_id"] = source_question_id
                if using_current_question:
                    async with self._lock:
                        self._state.current_question["source_question_id"] = (
                            source_question_id
                        )
        client_topic_id = str(kwargs.get("selected_topic_id") or "").strip()
        question_topic_id = str(
            question_payload.get("selected_topic_id")
            or question_payload.get("topic_id")
            or question_payload.get("topic")
            or ""
        ).strip()
        if (
            using_current_question
            and client_topic_id
            and question_topic_id
            and client_topic_id != question_topic_id
        ):
            return Err(
                SdkError(
                    "selected topic does not match the current question",
                    code="QUESTION_MISMATCH",
                )
            )
        selected_topic_id = (
            question_topic_id
            if using_current_question
            else client_topic_id or question_topic_id
        )
        if selected_topic_id:
            question_payload["selected_topic_id"] = selected_topic_id
        reserved_attempt = False
        final_attempt_state_staged = False
        if using_current_question and state_attempt_id:
            async with self._lock:
                live_question = self._state.current_question
                if str(live_question.get("attempt_id") or "") != state_attempt_id:
                    return Err(
                        SdkError(
                            "current question identity does not match",
                            code="QUESTION_MISMATCH",
                        )
                    )
                if live_question.get("attempt_evaluated") or live_question.get(
                    "attempt_evaluation_pending"
                ):
                    return Err(
                        SdkError(
                            "attempt has already been evaluated",
                            code="ATTEMPT_ALREADY_EVALUATED",
                        )
                    )
                live_question["attempt_evaluation_pending"] = True
                reserved_attempt = True
        run_id = self._resolve_current_run_id(kwargs)
        session_id = str(kwargs.get("session_id") or "").strip()
        try:
            deterministic_reply = await self._try_deterministic_assessment(
                question=resolved_question,
                answer=answer_text,
                expected_answer=resolved_expected,
                mode=active_mode,
                question_payload=question_payload,
            )
            if deterministic_reply is None and self._agent is None:
                if reserved_attempt:
                    await self._clear_attempt_evaluation_reservation(state_attempt_id)
                    await self._persist_state()
                return Err(SdkError("study tutor agent is not initialized"))
            tutor_context = await self._build_learning_context(
                LLM_OPERATION_ANSWER_EVALUATE,
                input_text=answer_text,
                extra={
                    "question": resolved_question,
                    "expected_answer": resolved_expected,
                    "answer": answer_text,
                    "current_question": (
                        current_question if using_current_question else {}
                    ),
                    "public_current_question": (
                        public_current_question_payload(current_question)
                        if using_current_question
                        else {}
                    ),
                    "question_payload": question_payload,
                    "question_source": (
                        "current_question" if using_current_question else "supplied"
                    ),
                    "run_id": run_id,
                    "session_id": session_id,
                    "question_id": supplied_question_id or state_question_id,
                    "attempt_id": supplied_attempt_id or state_attempt_id,
                    "source_question_id": source_question_id,
                    "selected_topic_id": selected_topic_id,
                    "response_time_ms": response_time_ms,
                    "used_hint": used_hint,
                    "mode": active_mode,
                    "language": request_language,
                    **(
                        {
                            "vision_enabled": True,
                            "vision_image_base64": vision_image_payload,
                        }
                        if vision_image_payload
                        else {}
                    ),
                },
            )
            reply = deterministic_reply
            if reply is None:
                reply = await self._agent.answer_evaluate(
                    question=resolved_question,
                    answer=answer_text,
                    expected_answer=resolved_expected,
                    mode=active_mode,
                    context=tutor_context,
                )
            evaluation_validation = validate_evaluation(
                dict(reply.payload or {}), learner_answer=evaluation_learner_answer
            )
            if not reply.degraded and not evaluation_validation.valid:
                repair_context = {
                    **tutor_context,
                    "evaluation_correction": {
                        "invalid_evaluation": dict(reply.payload or {}),
                        "violations": list(evaluation_validation.errors),
                    },
                }
                repaired_reply = await self._agent.answer_evaluate(
                    question=resolved_question,
                    answer=answer_text,
                    expected_answer=resolved_expected,
                    mode=active_mode,
                    context=repair_context,
                )
                repaired_validation = validate_evaluation(
                    dict(repaired_reply.payload or {}),
                    learner_answer=evaluation_learner_answer,
                )
                if not repaired_reply.degraded and repaired_validation.valid:
                    reply = repaired_reply
                    tutor_context = repair_context
                    evaluation_validation = repaired_validation
                else:
                    if reserved_attempt:
                        await self._clear_attempt_evaluation_reservation(
                            state_attempt_id
                        )
                    return Err(
                        SdkError(
                            "answer evaluation remained inconsistent after one correction",
                            code="EVALUATION_INCONSISTENT",
                        )
                    )
            if not reply.degraded:
                reply.payload = canonicalize_evaluation(dict(reply.payload or {}))
            payload = await self._finalize_tutor_call(
                LLM_OPERATION_ANSWER_EVALUATE,
                reply,
                history_kind=LLM_OPERATION_ANSWER_EVALUATE,
                metadata={
                    "question": resolved_question,
                    "question_id": supplied_question_id or state_question_id,
                    "attempt_id": supplied_attempt_id or state_attempt_id,
                    "source_question_id": source_question_id,
                    "degraded": reply.degraded,
                    "diagnostic": reply.diagnostic,
                    "payload": reply.payload,
                    "screen_classification": tutor_context.get("screen_classification")
                    or {},
                },
                extra_context=tutor_context,
            )
            if reply.degraded:
                if reserved_attempt:
                    await self._clear_attempt_evaluation_reservation(state_attempt_id)
                    await self._persist_state()
                return Ok(payload)
            payload["question"] = resolved_question
            if supplied_question_id or state_question_id:
                payload["question_id"] = supplied_question_id or state_question_id
            if supplied_attempt_id or state_attempt_id:
                payload["attempt_id"] = supplied_attempt_id or state_attempt_id
            if source_question_id:
                payload["source_question_id"] = source_question_id
            if selected_topic_id:
                payload["selected_topic_id"] = selected_topic_id
                if using_current_question:
                    payload["topic"] = selected_topic_id
            scope_key = str(question_payload.get("scope_key") or "").strip()
            if scope_key:
                payload["scope_key"] = scope_key
                payload["scope_revision"] = int(
                    question_payload.get("scope_revision") or 0
                )
            payload.update(
                await self._build_practice_outcome_payload(
                    payload=payload,
                    question_payload=question_payload,
                    current_question=current_question,
                    question_source=(
                        "current_question" if using_current_question else "supplied"
                    ),
                )
            )
            payload["screen_classification"] = (
                tutor_context.get("screen_classification") or {}
            )
            if using_current_question and state_attempt_id:
                public_eval_cache = {
                    key: value
                    for key, value in payload.items()
                    if key
                    not in {
                        "answer",
                        "accepted_answers",
                        "key_points",
                        "rubric",
                        "solution_steps",
                        "internal_private_payload",
                        "current_question_private",
                    }
                }
                async with self._lock:
                    if (
                        str(self._state.current_question.get("attempt_id") or "")
                        == state_attempt_id
                    ):
                        self._state.current_question.pop(
                            "attempt_evaluation_pending", None
                        )
                        self._state.current_question["attempt_evaluated"] = True
                        self._state.current_question["answer_evaluation_cache"] = (
                            public_eval_cache
                        )
                        # Persist the same public, post-commit enrichment used
                        # by the attempt cache so both UIs can restore the
                        # visible loop state after a refresh or restart.
                        self._state.last_answer_evaluation = dict(public_eval_cache)
                        final_attempt_state_staged = True
                await self._persist_state()
            topic = str(
                selected_topic_id
                or question_payload.get("selected_topic_id")
                or question_payload.get("topic")
                or payload.get("selected_topic_id")
                or payload.get("topic")
                or tutor_context.get("topic")
                or ""
            ).strip()
            try:
                mastery_after = (
                    await asyncio.to_thread(self._knowledge_tracker.get_mastery, topic)
                    if topic
                    else -1.0
                )
            except Exception as exc:
                self.logger.warning("study answer mastery enrichment failed: {}", exc)
                mastery_after = -1.0
            await self._emit_answer_evaluated_event(
                verdict=str(payload.get("verdict") or ""),
                score=payload.get("score", 0.0),
                question_summary=resolved_question,
                user_answer_summary=answer_text,
                correction_hint=str(
                    payload.get("correction_hint")
                    or payload.get("feedback")
                    or payload.get("next_action")
                    or ""
                ),
                topic=topic,
                mastery_after=mastery_after,
                target_lanlan=target_lanlan,
            )
            return Ok(payload)
        except asyncio.CancelledError:
            if reserved_attempt:
                await self._clear_attempt_evaluation_reservation(
                    state_attempt_id,
                    recover_cached=final_attempt_state_staged,
                )
                with contextlib.suppress(Exception):
                    await self._persist_state()
            raise
        except SdkError as exc:
            if reserved_attempt:
                await self._clear_attempt_evaluation_reservation(
                    state_attempt_id,
                    recover_cached=final_attempt_state_staged,
                )
            persistence_failed = getattr(exc, "code", "") == "ANSWER_PERSISTENCE_FAILED"
            if persistence_failed:
                async with self._lock:
                    for key, value in previous_answer_state.items():
                        setattr(self._state, key, value)
            if reserved_attempt or persistence_failed:
                with contextlib.suppress(Exception):
                    await self._persist_state()
            return Err(exc)
        except Exception as exc:
            if reserved_attempt:
                await self._clear_attempt_evaluation_reservation(
                    state_attempt_id,
                    recover_cached=final_attempt_state_staged,
                )
                with contextlib.suppress(Exception):
                    await self._persist_state()
            return _entry_exception_error(self, exc, operation="study_evaluate_answer")
