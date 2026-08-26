"""Version-gated learner-state reads with per-topic V1 fallback.

The reader is deliberately storage-agnostic: it wraps the existing StudyStore
read methods and does not decide how either mastery model is calculated.  V1 is
the safe default.  A V2 read overlays available shadow snapshots on the same
topic set while retaining V1 rows wherever the projection is missing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

DEFAULT_MASTERY_V2_MODEL_VERSION = "mastery-v2-shadow-1"
_SUPPORTED_READ_MODELS = frozenset({"v1", "v2"})


class MasteryReadStore(Protocol):
    """Store surface required by :class:`LearnerStateReader`."""

    def get_latest_mastery(self, topic_id: str) -> dict[str, Any] | None: ...

    def list_latest_mastery_for_topics(
        self,
        topic_ids: Sequence[str],
    ) -> list[dict[str, Any]]: ...

    def list_mastery_overview(self, limit: int = 20) -> list[dict[str, Any]]: ...

    def count_tracked_mastery_topics(self) -> int: ...

    def average_latest_mastery(self) -> float: ...

    def get_latest_mastery_v2(
        self,
        *,
        topic_id: str,
        mastery_model_version: str,
    ) -> dict[str, Any] | None: ...

    def list_latest_mastery_v2_for_topics(
        self,
        topic_ids: Sequence[str],
        *,
        mastery_model_version: str,
    ) -> list[dict[str, Any]]: ...


class LearnerStateReader:
    """Read mastery through a reversible V1/V2 model boundary.

    ``read_model`` is intentionally fail-closed: any unrecognised value selects
    V1.  V2 is an overlay rather than an all-or-nothing switch, so a missing
    projection for one topic never hides its existing V1 state.
    """

    def __init__(
        self,
        store: MasteryReadStore,
        *,
        read_model: str = "v1",
        model_version: str = DEFAULT_MASTERY_V2_MODEL_VERSION,
    ) -> None:
        normalized_model = str(read_model or "").strip().lower()
        self._read_model = (
            normalized_model if normalized_model in _SUPPORTED_READ_MODELS else "v1"
        )
        self._model_version = (
            str(model_version or "").strip() or DEFAULT_MASTERY_V2_MODEL_VERSION
        )
        self._store = store

    @property
    def read_model(self) -> str:
        return self._read_model

    @property
    def model_version(self) -> str:
        return self._model_version

    def get_mastery(self, topic_id: str) -> dict[str, Any] | None:
        """Return one topic's selected snapshot, falling back to V1."""

        if self._read_model == "v1":
            return self._store.get_latest_mastery(topic_id)

        v2_row = self._store.get_latest_mastery_v2(
            topic_id=topic_id,
            mastery_model_version=self._model_version,
        )
        v1_row = self._store.get_latest_mastery(topic_id)
        if isinstance(v2_row, Mapping):
            return _adapt_v2_mastery(v2_row, fallback=v1_row)
        return v1_row

    def list_mastery(self, topic_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Return latest snapshots for a bounded set of topics."""

        if self._read_model == "v1":
            return self._store.list_latest_mastery_for_topics(topic_ids)

        topic_keys = list(topic_ids)
        v1_rows = self._store.list_latest_mastery_for_topics(topic_keys)
        v2_rows = self._store.list_latest_mastery_v2_for_topics(
            topic_keys,
            mastery_model_version=self._model_version,
        )
        return _overlay_v2_rows(v1_rows, v2_rows)

    def list_overview(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the existing V1 overview with available V2 rows overlaid.

        V1 defines the overview's topic membership and ordering.  Every V2
        attempt is also committed through the existing V1 write path, so this
        keeps overview limits stable and permits per-topic projection fallback.
        """

        v1_rows = self._store.list_mastery_overview(limit)
        if self._read_model == "v1" or not v1_rows:
            return v1_rows

        topic_ids = [
            str(row.get("topic_id") or "")
            for row in v1_rows
            if isinstance(row, Mapping) and str(row.get("topic_id") or "")
        ]
        if not topic_ids:
            return v1_rows
        v2_rows = self._store.list_latest_mastery_v2_for_topics(
            topic_ids,
            mastery_model_version=self._model_version,
        )
        return _overlay_v2_rows(v1_rows, v2_rows, append_v2_only=False)

    def count_tracked_topics(self) -> int:
        """Count tracked topics through the selected read model."""

        if self._read_model == "v1":
            return int(self._store.count_tracked_mastery_topics())
        return len(self.list_overview(2_147_483_647))

    def average_mastery(self) -> float:
        """Return the selected model's average without changing V1 semantics."""

        if self._read_model == "v1":
            return float(self._store.average_latest_mastery())
        rows = self.list_overview(2_147_483_647)
        if not rows:
            return 0.0
        return sum(float(row.get("mastery") or 0.0) for row in rows) / len(rows)


def tracker_list_mastery(
    tracker: Any,
    topic_ids: Sequence[str],
    *,
    store: Any | None = None,
) -> list[dict[str, Any]]:
    """Use a tracker's reader boundary, with a V1 adapter for legacy callers."""

    read = getattr(tracker, "list_mastery", None)
    if callable(read):
        return read(topic_ids)
    fallback_store = store or getattr(tracker, "store", None)
    if fallback_store is None:
        raise TypeError("tracker has no learner-state reader or store adapter")
    return LearnerStateReader(fallback_store).list_mastery(topic_ids)


def tracker_list_mastery_overview(
    tracker: Any,
    *,
    limit: int,
    store: Any | None = None,
) -> list[dict[str, Any]]:
    """Read an overview while preserving old duck-typed tracker fixtures."""

    read = getattr(tracker, "list_mastery_overview", None)
    if callable(read):
        return read(limit=limit)
    fallback_store = store or getattr(tracker, "store", None)
    if fallback_store is None:
        raise TypeError("tracker has no learner-state reader or store adapter")
    return LearnerStateReader(fallback_store).list_overview(limit)


def _overlay_v2_rows(
    v1_rows: Sequence[dict[str, Any]],
    v2_rows: Sequence[dict[str, Any]],
    *,
    append_v2_only: bool = True,
) -> list[dict[str, Any]]:
    v2_by_topic = {
        str(row.get("topic_id") or ""): row
        for row in v2_rows
        if isinstance(row, Mapping) and str(row.get("topic_id") or "")
    }
    result: list[dict[str, Any]] = []
    seen_topics: set[str] = set()
    for v1_row in v1_rows:
        topic_id = str(v1_row.get("topic_id") or "")
        v2_row = v2_by_topic.get(topic_id)
        if v2_row is None:
            result.append(v1_row)
        else:
            result.append(_adapt_v2_mastery(v2_row, fallback=v1_row))
            seen_topics.add(topic_id)

    if append_v2_only:
        for v2_row in v2_rows:
            topic_id = str(v2_row.get("topic_id") or "")
            if topic_id and topic_id not in seen_topics:
                result.append(_adapt_v2_mastery(v2_row))
                seen_topics.add(topic_id)
    return result


def _adapt_v2_mastery(
    row: Mapping[str, Any],
    *,
    fallback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Add the legacy mastery shape without dropping V2 provenance fields."""

    adapted = dict(row)
    legacy = fallback or {}
    topic_id = str(adapted.get("topic_id") or legacy.get("topic_id") or "")

    adapted["id"] = _as_int(adapted.get("id"), default=0)
    adapted["topic_id"] = topic_id
    adapted["topic_name"] = str(
        adapted.get("topic_name") or legacy.get("topic_name") or topic_id
    )
    adapted["chapter"] = str(
        adapted.get("chapter") or legacy.get("chapter") or ""
    )
    adapted["subject"] = str(
        adapted.get("subject") or legacy.get("subject") or ""
    )
    for field in ("mastery", "accuracy", "recency", "consistency", "confidence"):
        adapted[field] = _as_float(adapted.get(field), default=0.0)
    adapted["level"] = str(adapted.get("level") or "")
    adapted["attempts"] = _as_int(
        adapted.get("attempts"),
        default=_as_int(adapted.get("evidence_count"), default=0),
    )
    flags = adapted.get("flags")
    adapted["flags"] = flags if isinstance(flags, list) else []
    adapted["updated_at"] = str(
        adapted.get("updated_at") or adapted.get("computed_at") or ""
    )
    return adapted


def _as_float(value: Any, *, default: float) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return converted


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default
