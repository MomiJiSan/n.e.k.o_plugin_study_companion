from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

LEARNING_PLAN_SCHEMA_VERSION = 1
DEFAULT_LEARNING_PLAN_TITLE = "导入的学习计划"
LEARNING_PLAN_STATUSES = frozenset({"draft", "active", "paused", "completed", "canceled"})
LEARNING_PLAN_ROLES = frozenset({"core", "prerequisite", "optional"})
MAPPING_CONFIDENCE_LEVELS = frozenset({"high", "medium"})


class LearningPlanError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = str(code or "LEARNING_PLAN_ERROR")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime | None = None) -> str:
    current = (value or _utc_now()).astimezone(timezone.utc)
    return current.isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_now(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return _parse_utc(value) or _utc_now()


def _finite_non_negative(value: object, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LearningPlanError(
            f"{field} must be a finite non-negative number",
            code="INVALID_LEARNING_PLAN_CANDIDATE",
        ) from exc
    if not math.isfinite(number) or number < 0:
        raise LearningPlanError(
            f"{field} must be a finite non-negative number",
            code="INVALID_LEARNING_PLAN_CANDIDATE",
        )
    return number


class LearningPlanService:
    """Persist and derive learning-plan state without retaining source material."""

    def __init__(
        self,
        store: Any,
        *,
        mastery_threshold: float = 0.8,
        min_attempts: int = 3,
        max_core_topics: int = 12,
        max_prerequisite_topics: int = 5,
    ) -> None:
        self._store = store
        self._mastery_threshold = max(0.0, min(1.0, float(mastery_threshold)))
        self._min_attempts = max(1, int(min_attempts))
        self._max_core_topics = max(1, min(12, int(max_core_topics)))
        self._max_prerequisite_topics = max(0, min(5, int(max_prerequisite_topics)))

    def create_draft(
        self,
        source_kind: str,
        source_digest: str,
        candidates: Iterable[Mapping[str, Any]],
        unmatched_count: int = 0,
        display_title: str = "",
    ) -> dict[str, Any]:
        kind = str(source_kind or "").strip().lower()
        digest = str(source_digest or "").strip()
        if not kind or len(kind) > 64:
            raise LearningPlanError("source_kind is required", code="INVALID_LEARNING_PLAN_SOURCE")
        if not digest or len(digest) > 256:
            raise LearningPlanError("source_digest is required", code="INVALID_LEARNING_PLAN_SOURCE")
        normalized = self._normalize_candidates(candidates)
        core_count = sum(1 for item in normalized if item["role"] == "core")
        prerequisite_count = sum(1 for item in normalized if item["role"] == "prerequisite")
        if core_count < 1:
            raise LearningPlanError(
                "at least one core topic is required", code="LEARNING_PLAN_CORE_TOPIC_REQUIRED"
            )
        if core_count > self._max_core_topics:
            raise LearningPlanError(
                "learning plan has too many core topics", code="LEARNING_PLAN_TOO_LARGE"
            )
        if prerequisite_count > min(self._max_prerequisite_topics, core_count):
            raise LearningPlanError(
                "learning plan has too many prerequisite topics", code="LEARNING_PLAN_TOO_LARGE"
            )
        now = _utc_iso()
        title = str(display_title or "").strip() or DEFAULT_LEARNING_PLAN_TITLE
        if len(title) > 120:
            title = title[:120]
        plan = {
            "id": f"lp_{uuid.uuid4().hex}",
            "schema_version": LEARNING_PLAN_SCHEMA_VERSION,
            "status": "draft",
            "source_kind": kind,
            "source_digest": digest,
            "display_title": title,
            "revision": 1,
            "unmatched_count": max(0, int(unmatched_count or 0)),
            "created_at": now,
            "updated_at": now,
        }
        return self._store.create_learning_plan(plan=plan, items=normalized)

    def activate(
        self,
        plan_id: str,
        revision: int,
        accepted_topic_ids: Iterable[str],
    ) -> dict[str, Any]:
        plan = self._require_plan(plan_id)
        accepted = list(dict.fromkeys(str(item or "").strip() for item in accepted_topic_ids))
        accepted = [item for item in accepted if item]
        candidate_ids = {str(item["topic_id"]) for item in plan.get("items", [])}
        if not accepted or not set(accepted).issubset(candidate_ids):
            raise LearningPlanError(
                "accepted_topic_ids must be a non-empty subset of draft candidates",
                code="LEARNING_PLAN_TOPIC_INJECTION_REJECTED",
            )
        accepted_core = any(
            item["topic_id"] in accepted and item["role"] == "core"
            for item in plan.get("items", [])
        )
        if not accepted_core:
            raise LearningPlanError(
                "at least one core topic must be accepted",
                code="LEARNING_PLAN_CORE_TOPIC_REQUIRED",
            )
        for topic_id in accepted:
            if self._store.get_topic(topic_id) is None:
                raise LearningPlanError(
                    "a learning-plan topic is no longer available",
                    code="LEARNING_PLAN_TOPIC_REMOVED",
                )
        try:
            return self._store.activate_learning_plan(
                plan_id=str(plan_id),
                expected_revision=int(revision),
                accepted_topic_ids=accepted,
            )
        except (KeyError, ValueError) as exc:
            self._raise_store_error(exc)
        raise AssertionError("unreachable")

    def get(self, plan_id: str) -> dict[str, Any]:
        return self._require_plan(plan_id)

    def list(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        status_key = str(status or "").strip().lower()
        if status_key and status_key not in LEARNING_PLAN_STATUSES:
            raise LearningPlanError("unsupported plan status", code="INVALID_LEARNING_PLAN_STATUS")
        return self._store.list_learning_plans(status=status_key or None, limit=limit)

    def status(
        self,
        plan_id: str | None = None,
        *,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        return self.reconcile(plan_id, now=now)

    def pause(self, plan_id: str, revision: int | None = None) -> dict[str, Any]:
        plan = self._require_plan(plan_id)
        if plan["status"] not in {"active", "paused"}:
            raise LearningPlanError(
                "only an active plan can be paused", code="LEARNING_PLAN_NOT_ACTIVE"
            )
        return self._update_status(plan_id, "paused", revision)

    def cancel(self, plan_id: str, revision: int | None = None) -> dict[str, Any]:
        plan = self._require_plan(plan_id)
        if plan["status"] == "canceled":
            return plan
        return self._update_status(plan_id, "canceled", revision)

    def reconcile(
        self,
        plan_id: str | None = None,
        *,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        current = _coerce_now(now)
        plan = self._resolve_plan(plan_id, now=current)
        payload = self._derive_status(plan, current)
        if plan["status"] == "active" and self._is_complete(payload):
            plan = self._update_status(plan["id"], "completed", plan["revision"])
            payload = self._derive_status(plan, current)
        elif plan["status"] == "completed" and payload["progress"]["review_due"] > 0:
            try:
                plan = self._update_status(plan["id"], "active", plan["revision"])
            except LearningPlanError as exc:
                if exc.code != "ACTIVE_LEARNING_PLAN_EXISTS":
                    raise
            else:
                payload = self._derive_status(plan, current)
        return payload

    def active_selection_scope(
        self, *, now: datetime | str | None = None
    ) -> dict[str, Any] | None:
        try:
            status = self.reconcile(now=now)
        except LearningPlanError as exc:
            if exc.code != "LEARNING_PLAN_NOT_FOUND":
                raise
            return None
        if status["status"] != "active":
            return None
        eligible = [
            str(item["topic_id"])
            for item in status["items"]
            if item["state"] != "mastered" or int(item["active_wrong_count"] or 0) > 0
        ]
        return {
            "selection_domain": "learning_plan",
            "learning_plan_id": str(status["id"]),
            "learning_plan_revision": int(status["revision"]),
            "eligible_topic_ids": eligible,
            "progress": status["progress"],
            "items": status["items"],
        }

    def contains_topic(self, plan_id: str, revision: int, topic_id: str) -> bool:
        plan = self._store.get_learning_plan(str(plan_id or "").strip())
        if plan is None or plan["status"] != "active" or int(plan["revision"]) != int(revision):
            return False
        topic_key = str(topic_id or "").strip()
        return any(str(item["topic_id"]) == topic_key for item in plan.get("items", []))

    def _normalize_candidates(
        self, candidates: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        by_topic: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise LearningPlanError(
                    "each candidate must be an object", code="INVALID_LEARNING_PLAN_CANDIDATE"
                )
            topic_id = str(candidate.get("topic_id") or "").strip()
            topic = self._store.get_topic(topic_id)
            if not topic_id or topic is None:
                raise LearningPlanError(
                    "candidate topic does not exist", code="LEARNING_PLAN_TOPIC_REMOVED"
                )
            role = str(candidate.get("role") or "core").strip().lower()
            confidence = str(candidate.get("mapping_confidence") or "medium").strip().lower()
            if role not in LEARNING_PLAN_ROLES or confidence not in MAPPING_CONFIDENCE_LEVELS:
                raise LearningPlanError(
                    "candidate role or confidence is invalid", code="INVALID_LEARNING_PLAN_CANDIDATE"
                )
            reason_code = str(candidate.get("reason_code") or "material_match").strip()
            if not reason_code or len(reason_code) > 64:
                raise LearningPlanError(
                    "candidate reason_code is invalid", code="INVALID_LEARNING_PLAN_CANDIDATE"
                )
            normalized = {
                "topic_id": topic_id,
                "role": role,
                "mapping_score": _finite_non_negative(candidate.get("mapping_score", 0.0), "mapping_score"),
                "mapping_confidence": confidence,
                "reason_code": reason_code,
                "required": bool(candidate.get("required", True)),
            }
            prior = by_topic.get(topic_id)
            if prior is None or normalized["mapping_score"] > prior["mapping_score"]:
                by_topic[topic_id] = normalized
        role_order = {"prerequisite": 0, "core": 1, "optional": 2}
        ordered = sorted(
            by_topic.values(),
            key=lambda item: (role_order[item["role"]], -item["mapping_score"], item["topic_id"]),
        )
        now = _utc_iso()
        for ordinal, item in enumerate(ordered, start=1):
            item["ordinal"] = ordinal
            item["created_at"] = now
        return ordered

    def _resolve_plan(
        self, plan_id: str | None, *, now: datetime
    ) -> dict[str, Any]:
        if str(plan_id or "").strip():
            return self._require_plan(str(plan_id))
        active = self._store.get_active_learning_plan()
        if active is not None:
            return active
        completed_plans = self._store.list_learning_plans(
            status="completed", limit=50
        )
        for completed in completed_plans:
            candidate = self._derive_status(completed, now)
            if int(candidate["progress"]["review_due"] or 0) <= 0:
                continue
            try:
                return self._update_status(
                    completed["id"], "active", completed["revision"]
                )
            except LearningPlanError as exc:
                if exc.code != "ACTIVE_LEARNING_PLAN_EXISTS":
                    raise
                concurrent_active = self._store.get_active_learning_plan()
                if concurrent_active is not None:
                    return concurrent_active
        concurrent_active = self._store.get_active_learning_plan()
        if concurrent_active is not None:
            return concurrent_active
        paused_plans = self._store.list_learning_plans(status="paused", limit=1)
        if paused_plans:
            return paused_plans[0]
        if completed_plans:
            return completed_plans[0]
        raise LearningPlanError("no active learning plan", code="LEARNING_PLAN_NOT_FOUND")

    def _require_plan(self, plan_id: str) -> dict[str, Any]:
        plan = self._store.get_learning_plan(str(plan_id or "").strip())
        if plan is None:
            raise LearningPlanError("learning plan not found", code="LEARNING_PLAN_NOT_FOUND")
        return plan

    def _update_status(
        self, plan_id: str, status: str, revision: int | None
    ) -> dict[str, Any]:
        try:
            return self._store.update_learning_plan_status(
                plan_id=str(plan_id), status=status, expected_revision=revision
            )
        except (KeyError, ValueError) as exc:
            self._raise_store_error(exc)
        raise AssertionError("unreachable")

    @staticmethod
    def _raise_store_error(exc: Exception) -> None:
        raw = str(exc).strip("'\"")
        messages = {
            "LEARNING_PLAN_NOT_FOUND": "learning plan not found",
            "LEARNING_PLAN_NOT_DRAFT": "learning plan cannot be activated from its current state",
            "LEARNING_PLAN_CHANGED": "learning plan changed; refresh and try again",
            "ACTIVE_LEARNING_PLAN_EXISTS": "another learning plan is already active",
        }
        code = raw if raw in messages else "LEARNING_PLAN_ERROR"
        raise LearningPlanError(messages.get(code, raw or "learning plan operation failed"), code=code) from exc

    def _derive_status(self, plan: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        items = [dict(item) for item in plan.get("items", [])]
        topic_ids = [str(item["topic_id"]) for item in items]
        mastery_by_topic = {
            str(item["topic_id"]): item
            for item in self._store.list_latest_mastery_for_topics(topic_ids)
        }
        wrong_counts: dict[str, int] = {topic_id: 0 for topic_id in topic_ids}
        for wrong in self._store.list_wrong_questions(
            limit=None, topic_ids=topic_ids, statuses=("active", "retrying")
        ):
            topic_id = str(wrong.get("topic_id") or "")
            wrong_counts[topic_id] = wrong_counts.get(topic_id, 0) + 1
        cards_by_topic = {
            str(item["topic_id"]): item
            for item in self._store.list_fsrs_cards(limit=None, topic_ids=topic_ids)
        }
        counts = {"total": len(items), "mastered": 0, "progressing": 0, "pending": 0, "review_due": 0}
        derived_items: list[dict[str, Any]] = []
        for item in items:
            topic_id = str(item["topic_id"])
            mastery = mastery_by_topic.get(topic_id) or {}
            mastery_value = float(mastery.get("mastery") or 0.0)
            attempts = int(mastery.get("attempts") or 0)
            confidence = float(mastery.get("confidence") or 0.0)
            flags = {str(flag) for flag in (mastery.get("flags") or [])}
            active_wrong_count = int(wrong_counts.get(topic_id, 0))
            card = cards_by_topic.get(topic_id) or {}
            next_review_at = str((card.get("card") or {}).get("due") or "")
            due_at = _parse_utc(next_review_at)
            mastered = (
                bool(mastery)
                and mastery_value >= self._mastery_threshold
                and attempts >= self._min_attempts
                and "low_confidence" not in flags
                and active_wrong_count == 0
            )
            if mastered and due_at is not None and due_at <= now:
                state = "review_due"
            elif mastered:
                state = "mastered"
            elif mastery:
                state = "progressing"
            else:
                state = "pending"
            counts[state] += 1
            item.update(
                {
                    "state": state,
                    "mastery": mastery_value if mastery else None,
                    "attempts": attempts,
                    "confidence": confidence if mastery else None,
                    "active_wrong_count": active_wrong_count,
                    "next_review_at": next_review_at,
                }
            )
            derived_items.append(item)
        state_order = {"review_due": 0, "progressing": 1, "pending": 2, "mastered": 3}
        role_order = {"prerequisite": 0, "core": 1, "optional": 2}
        current_candidates = [item for item in derived_items if item["state"] != "mastered"]
        current_candidates.sort(
            key=lambda item: (
                state_order[item["state"]],
                role_order.get(str(item["role"]), 3),
                int(item["ordinal"]),
            )
        )
        payload = {key: value for key, value in plan.items() if key != "items"}
        payload["progress"] = counts
        payload["current_topic"] = dict(current_candidates[0]) if current_candidates else {}
        payload["items"] = derived_items
        return payload

    @staticmethod
    def _is_complete(payload: Mapping[str, Any]) -> bool:
        items = list(payload.get("items") or [])
        required_core = [
            item for item in items if item.get("required") and item.get("role") == "core"
        ]
        return (
            bool(required_core)
            and int((payload.get("progress") or {}).get("review_due") or 0) == 0
            and all(item.get("state") == "mastered" for item in required_core)
            and all(int(item.get("active_wrong_count") or 0) == 0 for item in items)
        )
