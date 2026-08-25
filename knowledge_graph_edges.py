"""Canonical parsing and runtime normalization for knowledge-graph edges.

The graph represents ``from -> to`` consistently.  In particular, a
``prerequisite`` points from the prerequisite topic to the topic that depends
on it.  This module deliberately does not translate current relation names:
``supports``, ``next``, and ``nearby`` remain distinct graph semantics.
"""

from __future__ import annotations

from typing import Any, Iterable

try:  # Keep ``uv run python knowledge_seed_validator.py`` supported.
    from ._graph_utils import text, topic_id, topic_label
except ImportError:  # pragma: no cover - direct validator CLI execution
    from _graph_utils import text, topic_id, topic_label


# This is the one relationship contract used by seed validation, indexing and
# model guidance.  ``from -> to`` is directional unless the relation appears
# in ``SYMMETRIC_RELATIONS``.
ALLOWED_RELATIONS = frozenset(
    {
        "prerequisite",
        "application",
        "procedure_step",
        "confusable",
        "extends",
        "analogy",
        "co_occurs",
        "supports",
        "next",
        "nearby",
    }
)
SYMMETRIC_RELATIONS = frozenset({"analogy", "confusable", "co_occurs", "nearby"})
SEMANTIC_RELATIONS = frozenset(
    {
        "application",
        "procedure_step",
        "confusable",
        "co_occurs",
        "supports",
        "analogy",
        "next",
        "nearby",
    }
)

# Which endpoint a focused topic must occupy for a directional relation.  The
# relation names intentionally retain their seed semantics rather than being
# collapsed into an undirected "related" link.
FOCUSED_RELATION_DIRECTION = {
    "prerequisite": "incoming",
    "procedure_step": "incoming",
    "application": "outgoing",
    "extends": "outgoing",
    "supports": "incoming",
    "next": "outgoing",
}


def normalized_relation(relation: Any) -> str:
    """Return the persisted relation name without collapsing its semantics."""
    return text(relation)


def _reference_id(value: Any) -> str:
    if isinstance(value, dict):
        return text(value.get("id") or value.get("topic_id"))
    return text(value)


def _reference_relation(field: str, value: Any) -> str | None:
    """Resolve a seed reference relation, enforcing the prerequisite boundary.

    Malformed legacy ``related`` prerequisite entries are omitted at runtime.
    The seed validator reports them as hard errors; omitting them here keeps
    pre-existing persisted data from producing a reverse prerequisite edge.
    """
    relation = ""
    if isinstance(value, dict):
        relation = normalized_relation(value.get("relation"))
    if field == "prerequisites":
        return "prerequisite"
    relation = relation or "co_occurs"
    return None if relation == "prerequisite" else relation


def _edge_reason(value: Any) -> str:
    return text(value.get("reason")) if isinstance(value, dict) else ""


def _edge_use_cases(value: Any) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get("use_cases"), list):
        return []
    return [text(item) for item in value["use_cases"] if text(item)]


def _edge_priority(relation: str, ref: Any) -> str:
    if isinstance(ref, dict):
        priority = text(ref.get("priority"))
        if priority in {"core", "useful", "optional"}:
            return priority
    if relation in {"prerequisite", "procedure_step", "confusable"}:
        return "core"
    if relation in {"application", "supports", "extends"}:
        return "useful"
    return "optional"


def _edge_context(relation: str, use_cases: list[str], ref: Any) -> str:
    if isinstance(ref, dict):
        context = text(ref.get("context"))
        if context in {"diagnosis", "explanation", "practice", "review"}:
            return context
    if relation == "confusable":
        return "diagnosis"
    if relation in {"procedure_step", "application"}:
        return "practice"
    if relation in {"extends", "co_occurs", "nearby"} or "review" in use_cases:
        return "review"
    return "explanation"


def _edge_confidence(ref: Any, *, reason: str, use_cases: list[str]) -> float:
    if isinstance(ref, dict):
        try:
            confidence = float(ref.get("confidence"))
        except (TypeError, ValueError):
            confidence = -1.0
        if 0.0 <= confidence <= 1.0:
            return confidence
    if reason and use_cases:
        return 0.95
    if reason or use_cases:
        return 0.85
    return 0.7


def _edge_payload(
    *,
    source: dict[str, Any] | None,
    target: dict[str, Any] | None,
    source_id: str,
    target_id: str,
    relation: str,
    ref: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "from": source_id,
        "to": target_id,
        "from_label": topic_label(source, source_id),
        "to_label": topic_label(target, target_id),
        "relation": relation,
    }
    reason = _edge_reason(ref)
    if reason:
        payload["reason"] = reason
    use_cases = _edge_use_cases(ref)
    if use_cases:
        payload["use_cases"] = use_cases
    payload["priority"] = _edge_priority(relation, ref)
    payload["context"] = _edge_context(relation, use_cases, ref)
    payload["confidence"] = _edge_confidence(ref, reason=reason, use_cases=use_cases)
    if isinstance(ref, dict) and ref.get("required_mastery") is not None:
        payload["required_mastery"] = ref.get("required_mastery")
    return payload


def edge_key(edge: dict[str, Any]) -> tuple[str, str, str]:
    """Return the stable identity key for an edge after relation normalization."""
    source_id = text(edge.get("from"))
    target_id = text(edge.get("to"))
    relation = normalized_relation(edge.get("relation"))
    if relation in SYMMETRIC_RELATIONS:
        source_id, target_id = sorted((source_id, target_id))
    return source_id, target_id, relation


def dedupe_edges(edges: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministically retain the first valid edge of each semantic identity."""
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for edge in edges:
        key = edge_key(edge)
        if not key[0] or not key[1] or not key[2] or key in seen:
            continue
        seen.add(key)
        payload = dict(edge)
        payload["relation"] = key[2]
        unique.append(payload)
    return unique


def build_topic_edges(topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse seed topic references into consistently directed, unique edges."""
    by_id = {topic_id(topic): topic for topic in topics if topic_id(topic)}
    edges: list[dict[str, Any]] = []
    for topic in topics:
        target_id = topic_id(topic)
        if not target_id:
            continue
        for ref in topic.get("prerequisites") or []:
            source_id = _reference_id(ref)
            if source_id:
                edges.append(
                    _edge_payload(
                        source=by_id.get(source_id),
                        target=topic,
                        source_id=source_id,
                        target_id=target_id,
                        relation="prerequisite",
                        ref=ref,
                    )
                )
        for ref in topic.get("related") or []:
            related_id = _reference_id(ref)
            relation = _reference_relation("related", ref)
            if not related_id or not relation:
                continue
            edges.append(
                _edge_payload(
                    source=topic,
                    target=by_id.get(related_id),
                    source_id=target_id,
                    target_id=related_id,
                    relation=relation,
                    ref=ref,
                )
            )
    return dedupe_edges(edges)
