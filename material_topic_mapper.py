from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from .knowledge_graph_guidance import match_topics

_CORE_ROLE = "core"
_PREREQUISITE_ROLE = "prerequisite"
_HIGH_CONFIDENCE = "high"
_MEDIUM_CONFIDENCE = "medium"
_MASTERED_THRESHOLD = 0.8
_MATERIAL_GENERIC_CONCEPTS = frozenset(
    {
        "chapter",
        "concept",
        "course",
        "example",
        "lesson",
        "plan",
        "practice",
        "study",
        "topic",
        "学习",
        "知识",
        "知识点",
        "课程",
        "章节",
        "练习",
        "例题",
    }
)


class _TopicStore(Protocol):
    def list_topics(self, limit: int | None = 100, **kwargs: object) -> list[dict[str, Any]]: ...

    def get_topic(self, topic_id: str) -> dict[str, Any] | None: ...

    def get_latest_mastery(self, topic_id: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class MaterialMappingInput:
    """Runtime-only text used to map one analyzed material to stored topics."""

    source_kind: str
    merged_summary: str
    chunk_memos: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MaterialTopicMapping:
    candidates: tuple[dict[str, object], ...]
    unmatched_count: int
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "candidates": [dict(candidate) for candidate in self.candidates],
            "unmatched_count": self.unmatched_count,
            "truncated": self.truncated,
        }


@dataclass(slots=True)
class _Evidence:
    topic_id: str
    max_score: float = 0.0
    exact_match: bool = False
    chunk_hits: int = 0
    meaningful_match: bool = False


TopicMatcher = Callable[..., list[dict[str, Any]]]


def _text(value: object) -> str:
    return str(value or "").strip()


def _normalized_phrase(value: object) -> str:
    return re.sub(r"\s+", " ", _text(value).casefold())


def _aliases(topic: dict[str, Any]) -> tuple[str, ...]:
    raw = topic.get("aliases")
    if not isinstance(raw, list):
        return ()
    return tuple(alias for item in raw if (alias := _text(item)))


def _contains_complete_concept(query: str, concept: str) -> bool:
    normalized_query = _normalized_phrase(query)
    normalized_concept = _normalized_phrase(concept)
    if (
        not normalized_query
        or len(normalized_concept) < 2
        or normalized_concept in _MATERIAL_GENERIC_CONCEPTS
    ):
        return False
    if re.fullmatch(r"[a-z0-9][a-z0-9 +.#/_-]*", normalized_concept):
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(normalized_concept)}(?![a-z0-9])",
                normalized_query,
            )
        )
    return normalized_concept in normalized_query


def _has_exact_topic_concept(query: str, topic: dict[str, Any]) -> bool:
    labels = (_text(topic.get("name") or topic.get("label") or topic.get("id")), *_aliases(topic))
    return any(_contains_complete_concept(query, label) for label in labels if label)


def _prerequisite_ids(topic: dict[str, Any]) -> tuple[str, ...]:
    raw = topic.get("prerequisites")
    if not isinstance(raw, list):
        return ()
    result: list[str] = []
    for item in raw:
        topic_id = _text(item.get("id") if isinstance(item, dict) else item)
        if topic_id and topic_id not in result:
            result.append(topic_id)
    return tuple(result)


class MaterialTopicMapper:
    """Map in-memory summaries to existing topics without retaining source text.

    ``match_topics`` remains the only lexical scorer. This adapter aggregates its
    results, applies material-level confidence gates, validates every identifier
    against the store, and adds a bounded prerequisite frontier.
    """

    def __init__(
        self,
        store: _TopicStore,
        *,
        matcher: TopicMatcher = match_topics,
        max_core_topics: int = 12,
        max_prerequisite_topics: int = 5,
        max_prerequisite_depth: int = 2,
    ) -> None:
        self._store = store
        self._matcher = matcher
        self._max_core_topics = max(1, int(max_core_topics))
        self._max_prerequisite_topics = max(0, int(max_prerequisite_topics))
        self._max_prerequisite_depth = max(0, int(max_prerequisite_depth))

    def map(self, material: MaterialMappingInput) -> MaterialTopicMapping:
        # Neither the input object nor its text is retained on this mapper.
        topics = self._store.list_topics(limit=None)
        topics_by_id = {
            topic_id: topic
            for topic in topics
            if isinstance(topic, dict) and (topic_id := _text(topic.get("id")))
        }
        if not topics_by_id:
            return MaterialTopicMapping(candidates=(), unmatched_count=0, truncated=False)

        evidence: dict[str, _Evidence] = {}
        rejected_ids: set[str] = set()
        sources: list[tuple[str, bool]] = []
        if _text(material.merged_summary):
            sources.append((material.merged_summary, False))
        sources.extend(
            (memo, True) for memo in material.chunk_memos if _text(memo)
        )

        for query, is_chunk in sources:
            matches = self._matcher(topics, query=query, limit=5)
            seen_in_chunk: set[str] = set()
            for match in matches:
                topic_id = _text(match.get("id"))
                topic = topics_by_id.get(topic_id)
                if topic is None:
                    continue
                try:
                    score = float(match.get("score") or 0.0)
                except (TypeError, ValueError):
                    score = 0.0
                item = evidence.setdefault(topic_id, _Evidence(topic_id=topic_id))
                item.max_score = max(item.max_score, score)
                matched_terms = match.get("matched_terms")
                item.meaningful_match = item.meaningful_match or not isinstance(
                    matched_terms, list
                ) or any(
                    _normalized_phrase(term) not in _MATERIAL_GENERIC_CONCEPTS
                    for term in matched_terms
                    if _normalized_phrase(term)
                )
                item.exact_match = item.exact_match or _has_exact_topic_concept(
                    query, topic
                )
                if is_chunk and topic_id not in seen_in_chunk:
                    item.chunk_hits += 1
                    seen_in_chunk.add(topic_id)

        qualified: list[tuple[_Evidence, str, str]] = []
        for item in evidence.values():
            if item.meaningful_match and item.exact_match and item.max_score >= 40:
                qualified.append((item, _HIGH_CONFIDENCE, "material_exact_match"))
            elif (
                item.meaningful_match
                and item.max_score >= 10
                and item.chunk_hits >= 2
            ):
                qualified.append((item, _MEDIUM_CONFIDENCE, "material_repeated_match"))
            else:
                rejected_ids.add(item.topic_id)

        qualified.sort(
            key=lambda value: (
                0 if value[1] == _HIGH_CONFIDENCE else 1,
                -value[0].max_score,
                -value[0].chunk_hits,
                value[0].topic_id,
            )
        )
        validated: list[tuple[_Evidence, str, str]] = []
        for item, confidence, reason_code in qualified:
            if self._store.get_topic(item.topic_id) is None:
                rejected_ids.add(item.topic_id)
                continue
            validated.append((item, confidence, reason_code))
        truncated = len(validated) > self._max_core_topics

        core_candidates: list[dict[str, object]] = []
        validated_core_ids: set[str] = set()
        for item, confidence, reason_code in validated[: self._max_core_topics]:
            validated_core_ids.add(item.topic_id)
            core_candidates.append(
                {
                    "topic_id": item.topic_id,
                    "role": _CORE_ROLE,
                    "mapping_score": item.max_score,
                    "mapping_confidence": confidence,
                    "reason_code": reason_code,
                    "required": True,
                }
            )

        prerequisite_limit = min(
            self._max_prerequisite_topics, len(core_candidates)
        )
        prerequisites = self._expand_prerequisites(
            validated_core_ids,
            topics_by_id=topics_by_id,
            limit=prerequisite_limit,
        )
        return MaterialTopicMapping(
            candidates=tuple([*core_candidates, *prerequisites]),
            unmatched_count=len(rejected_ids),
            truncated=truncated,
        )

    def _expand_prerequisites(
        self,
        core_ids: Iterable[str],
        *,
        topics_by_id: dict[str, dict[str, Any]],
        limit: int,
    ) -> list[dict[str, object]]:
        if limit <= 0 or self._max_prerequisite_depth <= 0:
            return []
        core_set = set(core_ids)
        queue: deque[tuple[str, int]] = deque(
            (topic_id, 0) for topic_id in sorted(core_set)
        )
        visited = set(core_set)
        result: list[dict[str, object]] = []
        while queue and len(result) < limit:
            current_id, depth = queue.popleft()
            if depth >= self._max_prerequisite_depth:
                continue
            current = topics_by_id.get(current_id)
            if current is None:
                continue
            for prerequisite_id in _prerequisite_ids(current):
                if prerequisite_id in visited:
                    continue
                visited.add(prerequisite_id)
                prerequisite = topics_by_id.get(prerequisite_id)
                if prerequisite is None:
                    continue
                if self._store.get_topic(prerequisite_id) is None:
                    continue
                next_depth = depth + 1
                if not self._is_mastered(prerequisite_id):
                    result.append(
                        {
                            "topic_id": prerequisite_id,
                            "role": _PREREQUISITE_ROLE,
                            "mapping_score": 0.0,
                            "mapping_confidence": _HIGH_CONFIDENCE,
                            "reason_code": "material_prerequisite",
                            "required": True,
                        }
                    )
                    if len(result) >= limit:
                        break
                if next_depth < self._max_prerequisite_depth:
                    queue.append((prerequisite_id, next_depth))
        return result

    def _is_mastered(self, topic_id: str) -> bool:
        getter = getattr(self._store, "get_latest_mastery", None)
        if not callable(getter):
            return False
        snapshot = getter(topic_id)
        if not isinstance(snapshot, dict):
            return False
        try:
            return float(snapshot.get("mastery") or 0.0) >= _MASTERED_THRESHOLD
        except (TypeError, ValueError):
            return False


__all__ = [
    "MaterialMappingInput",
    "MaterialTopicMapper",
    "MaterialTopicMapping",
]
