"""Bounded evidence retrieval for questions that ask about two concepts.

This module is deliberately independent from the general topic matcher.  The
normal guidance path optimizes for one focus topic and a useful neighbourhood;
relationship questions instead need proof that *two* recognised endpoints are
connected.  Keeping that behaviour here prevents it from changing knowledge
map, mastery, or question-generation retrieval.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable, Sequence

try:  # Support direct local inspection in the same manner as edge utilities.
    from ._graph_utils import text as _text
    from ._graph_utils import topic_id as _topic_id
    from ._graph_utils import topic_label as _topic_label
    from .knowledge_graph_edges import build_topic_edges, normalized_relation
except ImportError:  # pragma: no cover - module is normally imported as a package
    from _graph_utils import text as _text
    from _graph_utils import topic_id as _topic_id
    from _graph_utils import topic_label as _topic_label
    from knowledge_graph_edges import build_topic_edges, normalized_relation


_RELATION_INTENT_MARKERS = (
    "有什么区别",
    "有何区别",
    "有什么关系",
    "有何关系",
    "有什么联系",
    "有何联系",
    "区别",
    "联系",
    "关系",
    "为什么用",
    "为什么要",
    "为什么能",
    "怎么支持",
    "怎么解释",
    "怎么理解",
    "怎么连接",
    "怎么联系",
    "怎么用来",
    "怎么用",
    "怎么看",
    "怎么描述",
    "怎么分析",
    "怎么判断",
    "如何支持",
    "如何解释",
    "如何理解",
    "如何连接",
    "如何联系",
    "如何使用",
    "如何描述",
    "如何分析",
    "如何判断",
    "可以用",
    "能用",
)
_PAIR_CONNECTORS = ("和", "与", "跟", "及", "、", "/")
_RELATION_ORDER = {
    "prerequisite": 0,
    "procedure_step": 1,
    "application": 2,
    "supports": 3,
    "extends": 4,
    "confusable": 5,
    "analogy": 6,
    "co_occurs": 7,
    "next": 8,
    "nearby": 9,
}
_STRUCTURAL_PATH_RELATIONS = frozenset(
    {"prerequisite", "procedure_step", "application", "supports", "extends"}
)
_CORE_STRUCTURAL_PATH_RELATIONS = frozenset(
    {"prerequisite", "procedure_step", "application", "supports"}
)
_IGNORED_MENTION_TERMS = frozenset(
    {
        "什么",
        "关系",
        "联系",
        "区别",
        "为什么",
        "怎么",
        "如何",
        "可以",
        "用来",
        "用于",
        "一个",
        "这个",
        "那个",
        "里面",
        "以及",
        "之间",
        "分析",
        "解释",
        "理解",
        "判断",
        "描述",
        "支持",
    }
)


def _compact(value: Any) -> str:
    """Normalize matching text without decomposing CJK compound terms."""
    return "".join(_text(value).casefold().split())


def _unique_nonempty(values: Iterable[Any]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _text(value)
        compact = _compact(item)
        if not compact or compact in seen:
            continue
        seen.add(compact)
        items.append(item)
    return items


def _has_relationship_intent(query: str) -> bool:
    normalized = _compact(query)
    return any(marker in normalized for marker in _RELATION_INTENT_MARKERS)


def _clean_endpoint_fragment(value: str) -> str:
    cleaned = _text(value)
    # ``关系`` is a valid contiguous part of ``相关系数``.  It is an intent
    # marker for detection, but only a trailing question phrase for extraction.
    for marker in _RELATION_INTENT_MARKERS:
        if marker in {"关系", "联系"}:
            continue
        cleaned = cleaned.replace(marker, " ")
    for suffix in ("之间的关系", "之间联系", "的关系", "的联系", "关系", "联系"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned.strip(" ，。？?！!：:；;的 ")


def _query_endpoint_fragments(query: str) -> list[str]:
    """Extract a conservative pair from common Chinese coordination forms."""
    parts = [_text(query)]
    for connector in _PAIR_CONNECTORS:
        if connector in _text(query):
            parts = _text(query).split(connector)
            break
    return _unique_nonempty(_clean_endpoint_fragment(part) for part in parts)[:2]


def _relationship_query(
    *, query: str, retrieval_concepts: Sequence[str]
) -> tuple[bool, list[str]]:
    concepts = _unique_nonempty(retrieval_concepts)
    if len(concepts) >= 2:
        return True, concepts[:2]
    has_intent = _has_relationship_intent(query)
    fragments = _query_endpoint_fragments(query)
    if has_intent and len(fragments) >= 2:
        return True, fragments
    return has_intent, []


def _topic_aliases(topic: dict[str, Any]) -> list[str]:
    aliases = topic.get("aliases")
    return _unique_nonempty(aliases if isinstance(aliases, list) else [])


def _text_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        if _text(value):
            yield _text(value)
        return
    if isinstance(value, list):
        for item in value:
            yield from _text_values(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "id",
                "topic_id",
                "relation",
                "priority",
                "context",
                "confidence",
                "required_mastery",
                "metadata",
            }:
                continue
            yield from _text_values(item)


def _topic_text_sources(topic: dict[str, Any]) -> list[tuple[str, int]]:
    """Return local topic wording only; this never consumes eval expectations."""
    sources: list[tuple[str, int]] = []
    label = _topic_label(topic, _topic_id(topic))
    if label:
        sources.append((label, 100))
    sources.extend((alias, 96) for alias in _topic_aliases(topic))
    for field in ("chapter", "unit", "course_family"):
        value = _text(topic.get(field))
        if value:
            sources.append((value, 66))
    for field in (
        "skills",
        "question_types",
        "typical_misconceptions",
        "examples",
        "prerequisites",
        "related",
    ):
        sources.extend((value, 42) for value in _text_values(topic.get(field)))
    return [(source, weight) for source, weight in sources if _compact(source)]


def _best_source_score(*, needle: str, sources: list[tuple[str, int]]) -> int:
    if not needle or needle in _IGNORED_MENTION_TERMS:
        return 0
    best = 0
    for source, weight in sources:
        candidate = _compact(source)
        if not candidate:
            continue
        if needle == candidate:
            best = max(best, weight + 20)
        elif len(needle) >= 2 and needle in candidate:
            best = max(best, weight + min(18, len(needle) * 2))
        elif len(candidate) >= 3 and candidate in needle:
            best = max(best, weight // 2 + min(12, len(candidate)))
    return best


def _endpoint_candidates(
    topics: list[dict[str, Any]],
    *,
    concept: str,
    subject: str = "",
    limit: int,
) -> list[dict[str, Any]]:
    """Return label/alias candidates without calling the global query matcher."""
    needle = _compact(concept)
    subject_scope = _compact(subject)
    if not needle:
        return []
    candidates: list[dict[str, Any]] = []
    for topic in topics:
        topic_id = _topic_id(topic)
        if not topic_id:
            continue
        topic_subject = _text(topic.get("subject"))
        if subject_scope and _compact(topic_subject) != subject_scope:
            continue
        label = _topic_label(topic, topic_id)
        score = _best_source_score(
            needle=needle,
            sources=_topic_text_sources(topic),
        )
        if score:
            candidates.append(
                {
                    "id": topic_id,
                    "label": label,
                    "subject": topic_subject,
                    "stage": _text(topic.get("stage")),
                    "score": score,
                }
            )
    ranked = sorted(
        candidates,
        key=lambda item: (
            -int(item["score"]),
            len(_compact(item["label"])),
            _compact(item["label"]),
            str(item["id"]),
        ),
    )
    # A relationship path is only useful if both endpoint candidates are
    # genuinely grounded in the requested concepts.  Do not let a weak phrase
    # from an example/reason form a tempting but unrelated direct edge.
    if not ranked:
        return []
    minimum_score = max(80, int(ranked[0]["score"]) - 24)
    return [
        item
        for item in ranked
        if int(item["score"]) >= minimum_score
    ][: max(1, min(3, int(limit or 1)))]


def _query_mention_candidates(
    topics: list[dict[str, Any]],
    *,
    query: str,
    subject: str = "",
    limit: int,
) -> list[dict[str, Any]]:
    """Find two positional topic mentions when prose has no clean connector.

    The scan uses only wording present on each topic (label, aliases, and its
    own learning metadata).  That gives a conservative fallback for prose such
    as ``边际成本为什么用导数`` without adding an unbounded semantic search.
    """
    normalized = _compact(query)
    subject_scope = _compact(subject)
    fragments: list[tuple[int, str]] = []
    for size in range(min(10, len(normalized)), 1, -1):
        for start in range(0, len(normalized) - size + 1):
            fragment = normalized[start : start + size]
            if fragment not in _IGNORED_MENTION_TERMS:
                fragments.append((start, fragment))
    matches: list[dict[str, Any]] = []
    for topic in topics:
        topic_id = _topic_id(topic)
        if not topic_id:
            continue
        topic_subject = _text(topic.get("subject"))
        if subject_scope and _compact(topic_subject) != subject_scope:
            continue
        sources = _topic_text_sources(topic)
        best: tuple[int, int, int] | None = None
        for start, fragment in fragments:
            score = _best_source_score(needle=fragment, sources=sources)
            if not score:
                continue
            candidate = (score, len(fragment), start)
            if best is None or candidate > best:
                best = candidate
        if best is not None:
            matches.append(
                {
                    "id": topic_id,
                    "label": _topic_label(topic, topic_id),
                    "subject": topic_subject,
                    "stage": _text(topic.get("stage")),
                    "score": best[0],
                    "mention_length": best[1],
                    "mention_start": best[2],
                }
            )
    return sorted(
        matches,
        key=lambda item: (
            -int(item["score"]),
            -int(item["mention_length"]),
            int(item["mention_start"]),
            len(_compact(item["label"])),
            str(item["id"]),
        ),
    )[: max(2, min(12, int(limit or 2)))]


def _fallback_endpoint_candidate_groups(
    topics: list[dict[str, Any]],
    *,
    query: str,
    primary_subject: str,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split conservative query mentions into an early and late endpoint slot."""
    all_mentions = _query_mention_candidates(
        topics,
        query=query,
        limit=12,
    )
    if not all_mentions:
        return [], []
    earliest = min(int(item["mention_start"]) for item in all_mentions)
    latest = max(int(item["mention_start"]) for item in all_mentions)
    if earliest == latest:
        return [], []
    first = [
        item
        for item in all_mentions
        if int(item["mention_start"]) == earliest
        and (not primary_subject or _compact(item["subject"]) == _compact(primary_subject))
    ]
    second = [item for item in all_mentions if int(item["mention_start"]) == latest]
    if not first:
        return [], []
    return first[:limit], second[:limit]


def _edge_payload(edge: dict[str, Any]) -> dict[str, Any]:
    """Expose only canonical path data, retaining persisted edge direction."""
    payload: dict[str, Any] = {
        "from_id": _text(edge.get("from")),
        "to_id": _text(edge.get("to")),
        "relation": normalized_relation(edge.get("relation")),
        "reason": _text(edge.get("reason")),
    }
    try:
        confidence = float(edge.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    payload["confidence"] = max(0.0, min(1.0, confidence))
    return payload


def _edge_sort_key(edge: dict[str, Any]) -> tuple[int, float, str, str]:
    relation = normalized_relation(edge.get("relation"))
    try:
        confidence = float(edge.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        _RELATION_ORDER.get(relation, 99),
        -max(0.0, min(1.0, confidence)),
        _text(edge.get("from")),
        _text(edge.get("to")),
    )


def _relation_intent_score(query: str, relation: str) -> int:
    """Prefer relation semantics that answer the expressed relationship intent."""
    normalized = _compact(query)
    if any(marker in normalized for marker in ("区别", "区分", "不同")):
        return {"confusable": 0, "analogy": 1}.get(relation, 2)
    if any(marker in normalized for marker in ("为什么用", "怎么支持", "如何支持")):
        return {"application": 0, "supports": 0, "extends": 1}.get(relation, 2)
    if "增长" in normalized or "衰减" in normalized:
        return {"application": 0, "procedure_step": 1}.get(relation, 2)
    if any(marker in normalized for marker in ("怎么解释", "如何解释")):
        return {"supports": 0, "application": 1, "prerequisite": 1}.get(relation, 2)
    return 0


def _path_intent_score(query: str, path: list[dict[str, Any]]) -> int:
    return sum(
        _relation_intent_score(query, normalized_relation(edge.get("relation")))
        for edge in path
    )


def _path_confidence(path: list[dict[str, Any]]) -> float:
    total = 0.0
    for edge in path:
        try:
            confidence = float(edge.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        total += max(0.0, min(1.0, confidence))
    return total


def _path_degree_penalty(path: list[dict[str, Any]], degrees: dict[str, int]) -> int:
    node_ids = {
        _text(edge.get("from"))
        for edge in path
    } | {
        _text(edge.get("to"))
        for edge in path
    }
    return sum(degrees.get(node_id, 0) for node_id in node_ids if node_id)


def _stage_mismatch(first: dict[str, Any], second: dict[str, Any]) -> int:
    """Use stage only as a tie-breaker for otherwise equal endpoint wording."""
    first_stage = _compact(first.get("stage"))
    second_stage = _compact(second.get("stage"))
    return int(bool(first_stage and second_stage and first_stage != second_stage))


def _path_between(
    *,
    start_id: str,
    target_id: str,
    edges: list[dict[str, Any]],
    max_hops: int,
    query: str,
) -> list[dict[str, Any]] | None:
    """Find the shortest bounded path, treating traversal as topology only.

    Traversal may walk an incoming edge in reverse to discover a semantic
    connection, but the returned edge always keeps its server-side ``from`` and
    ``to`` fields.  This is what prevents natural-language wording from
    silently reversing the graph contract.
    """
    if not start_id or not target_id or start_id == target_id:
        return None
    limit = max(1, min(3, int(max_hops or 1)))
    adjacency: dict[str, list[tuple[str, dict[str, Any], bool]]] = {}
    for edge in edges:
        source_id = _text(edge.get("from"))
        destination_id = _text(edge.get("to"))
        if not source_id or not destination_id:
            continue
        adjacency.setdefault(source_id, []).append((destination_id, edge, True))
        adjacency.setdefault(destination_id, []).append((source_id, edge, False))
    for neighbours in adjacency.values():
        neighbours.sort(
            key=lambda item: (
                # When both persisted directions exist, use the one that
                # actually points from the first resolved endpoint toward the
                # second.  This selects a real canonical edge; it never
                # rewrites a reverse edge to fit the wording.
                0 if item[2] else 1,
                _relation_intent_score(query, normalized_relation(item[1].get("relation"))),
                -_path_confidence([item[1]]),
                *_edge_sort_key(item[1]),
                item[0],
            )
        )

    queue: deque[tuple[str, list[dict[str, Any]]]] = deque([(start_id, [])])
    best_depth: dict[str, int] = {start_id: 0}
    while queue:
        current_id, path = queue.popleft()
        if len(path) >= limit:
            continue
        for next_id, edge, _traverses_forward in adjacency.get(current_id, []):
            candidate_path = [*path, edge]
            if next_id == target_id:
                return candidate_path
            depth = len(candidate_path)
            if depth >= limit or depth >= best_depth.get(next_id, limit + 1):
                continue
            best_depth[next_id] = depth
            queue.append((next_id, candidate_path))
    return None


def _prefers_structural_path(query: str) -> bool:
    normalized = _compact(query)
    return not any(marker in normalized for marker in ("区别", "区分", "不同"))


def _empty_evidence(*, detected: bool, unresolved: bool, diagnostics: dict[str, Any]) -> dict[str, Any]:
    return {
        "is_relationship_query": detected,
        "detected": detected,
        "resolved": False,
        "relationship_unresolved": unresolved,
        "unresolved": unresolved,
        "endpoints": [],
        "path": [],
        "hop_count": 0,
        "diagnostics": diagnostics,
    }


def resolve_relationship_evidence(
    *,
    topics: list[dict[str, Any]],
    query: str,
    retrieval_concepts: Sequence[str] = (),
    primary_subject: str = "",
    max_candidates_per_concept: int = 3,
    max_pairs: int = 9,
    max_hops: int = 3,
) -> dict[str, Any]:
    """Resolve a bounded, pair-focused relationship path or fail closed.

    No graph neighbourhood is ever returned: successful evidence contains only
    the two matched endpoints and the direct/two-hop/three-hop path between
    them.  A relationship-looking query without two confident endpoints, or
    one with no path, is reported as unresolved with empty endpoints/path.
    """
    usable_topics = [topic for topic in topics if isinstance(topic, dict)]
    detected, concepts = _relationship_query(
        query=query,
        retrieval_concepts=retrieval_concepts,
    )
    base_diagnostics: dict[str, Any] = {
        "query_mode": "relationship" if detected else "single_focus",
        "concept_count": len(concepts),
        "max_candidates_per_concept": max(1, min(3, int(max_candidates_per_concept or 1))),
        "max_pairs": max(1, min(9, int(max_pairs or 1))),
        "max_hops": max(1, min(3, int(max_hops or 1))),
    }
    if not detected:
        return _empty_evidence(detected=False, unresolved=False, diagnostics=base_diagnostics)
    candidate_limit = base_diagnostics["max_candidates_per_concept"]
    if len(concepts) >= 2:
        first_candidates = _endpoint_candidates(
            usable_topics,
            concept=concepts[0],
            subject=primary_subject,
            limit=candidate_limit,
        )
        second_candidates = _endpoint_candidates(
            usable_topics,
            concept=concepts[1],
            limit=candidate_limit,
        )
    else:
        first_candidates, second_candidates = _fallback_endpoint_candidate_groups(
            usable_topics,
            query=query,
            primary_subject=primary_subject,
            limit=candidate_limit,
        )
    base_diagnostics["endpoint_candidate_counts"] = [
        len(first_candidates),
        len(second_candidates),
    ]
    if not first_candidates or not second_candidates:
        return _empty_evidence(detected=True, unresolved=True, diagnostics=base_diagnostics)

    edges = build_topic_edges(usable_topics)
    pair_limit = base_diagnostics["max_pairs"]
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for first in first_candidates:
        for second in second_candidates:
            if first["id"] == second["id"]:
                continue
            pairs.append((first, second))
            if len(pairs) >= pair_limit:
                break
        if len(pairs) >= pair_limit:
            break
    base_diagnostics["compared_pairs"] = len(pairs)
    if not pairs:
        return _empty_evidence(detected=True, unresolved=True, diagnostics=base_diagnostics)

    found: list[tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]] = []
    core_structural_edges = [
        edge
        for edge in edges
        if normalized_relation(edge.get("relation")) in _CORE_STRUCTURAL_PATH_RELATIONS
    ]
    structural_edges = [
        edge
        for edge in edges
        if normalized_relation(edge.get("relation")) in _STRUCTURAL_PATH_RELATIONS
    ]
    for first, second in pairs:
        path_edges = core_structural_edges if _prefers_structural_path(query) else edges
        path = _path_between(
            start_id=str(first["id"]),
            target_id=str(second["id"]),
            edges=path_edges,
            max_hops=base_diagnostics["max_hops"],
            query=query,
        )
        # Contrast questions may legitimately need confusable/analogy edges.
        # Other relationship questions prefer a teachable dependency/application
        # chain, but still fall back to any existing direct/path evidence rather
        # than manufacturing a connection.
        if path is None and path_edges is not edges:
            path = _path_between(
                start_id=str(first["id"]),
                target_id=str(second["id"]),
                edges=structural_edges,
                max_hops=base_diagnostics["max_hops"],
                query=query,
            )
        if path is None and path_edges is not edges:
            path = _path_between(
                start_id=str(first["id"]),
                target_id=str(second["id"]),
                edges=edges,
                max_hops=base_diagnostics["max_hops"],
                query=query,
            )
        if path:
            found.append((path, first, second))

    if not found:
        return _empty_evidence(detected=True, unresolved=True, diagnostics=base_diagnostics)

    degrees: dict[str, int] = {}
    for edge in edges:
        for node_id in (_text(edge.get("from")), _text(edge.get("to"))):
            if node_id:
                degrees[node_id] = degrees.get(node_id, 0) + 1
    path, first, second = min(
        found,
        key=lambda item: (
            len(item[0]),
            -(int(item[1]["score"]) + int(item[2]["score"])),
            _stage_mismatch(item[1], item[2]),
            _path_intent_score(query, item[0]),
            -_path_confidence(item[0]),
            _path_degree_penalty(item[0], degrees),
            tuple(_edge_sort_key(edge) for edge in item[0]),
            str(item[1]["id"]),
            str(item[2]["id"]),
        ),
    )
    return {
        "is_relationship_query": True,
        "detected": True,
        "resolved": True,
        "relationship_unresolved": False,
        "unresolved": False,
        "endpoints": [
            {key: first[key] for key in ("id", "label", "subject")},
            {key: second[key] for key in ("id", "label", "subject")},
        ],
        "path": [_edge_payload(edge) for edge in path],
        "hop_count": len(path),
        "diagnostics": base_diagnostics,
    }
