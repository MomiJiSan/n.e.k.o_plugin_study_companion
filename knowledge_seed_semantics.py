"""Shared semantic validation for normalized knowledge-seed topics.

Filesystem manifests and runtime compatibility parsing intentionally remain in
their owning modules.  This module owns the graph invariants that must be
identical for the validator CLI and the store loader.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

try:  # Keep the standalone validator command importable.
    from .knowledge_graph_edges import ALLOWED_RELATIONS, SYMMETRIC_RELATIONS
except ImportError:  # pragma: no cover - direct script execution
    from knowledge_graph_edges import ALLOWED_RELATIONS, SYMMETRIC_RELATIONS


EDGE_RELATION_ALIASES = frozenset({"related", "similar", "compare"})
DEFAULT_PREREQUISITE_REQUIRED_MASTERY = 0.55
STAGE_ORDER = {
    "primary": 0,
    "junior_high": 1,
    "senior_high": 2,
    "college": 3,
}


@dataclass(frozen=True)
class KnowledgeSeedSemanticIssue:
    code: str
    message: str
    topic_id: str = ""


def _text(value: object) -> str:
    return str(value or "").strip()


def _ref_id(value: object) -> str:
    if isinstance(value, dict):
        return _text(value.get("id") or value.get("topic_id"))
    return _text(value)


def _relation(field: str, value: object) -> str:
    if isinstance(value, dict):
        relation = _text(value.get("relation"))
        if relation:
            return relation
    return "prerequisite" if field == "prerequisites" else "co_occurs"


def normalize_runtime_topic_relations(topic: dict[str, Any]) -> dict[str, Any]:
    """Upgrade legacy scalar references without weakening structured edges.

    Old runtime seeds used prerequisite ids directly and relied on the runtime
    mastery fallback.  Preserve that compatibility by making the fallback
    explicit before semantic validation.  Duplicate legacy scalar references
    are also collapsed, matching the historical runtime edge builder.
    """

    normalized = dict(topic)
    prerequisites: list[object] = []
    seen_legacy_prerequisites: set[str] = set()
    for ref in topic.get("prerequisites") or []:
        if isinstance(ref, str):
            topic_id = _text(ref)
            if not topic_id or topic_id in seen_legacy_prerequisites:
                continue
            seen_legacy_prerequisites.add(topic_id)
            prerequisites.append(
                {
                    "id": topic_id,
                    "required_mastery": DEFAULT_PREREQUISITE_REQUIRED_MASTERY,
                }
            )
        else:
            prerequisites.append(ref)
    normalized["prerequisites"] = prerequisites
    return normalized


def _find_cycle_nodes(edges: dict[str, set[str]]) -> set[str]:
    cycle_nodes: set[str] = set()
    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []

    def visit(node: str) -> None:
        if node in visiting:
            cycle_nodes.update(path[path.index(node) :])
            return
        if node in visited:
            return
        visiting.add(node)
        path.append(node)
        for target in edges.get(node, set()):
            visit(target)
        path.pop()
        visiting.discard(node)
        visited.add(node)

    for topic_id in edges:
        visit(topic_id)
    return cycle_nodes


def validate_normalized_knowledge_topics(
    topics: Iterable[dict[str, Any]],
) -> tuple[KnowledgeSeedSemanticIssue, ...]:
    """Validate graph invariants after manifest compatibility normalization."""

    topic_items = [dict(topic or {}) for topic in topics]
    topic_ids = {_text(topic.get("id")) for topic in topic_items}
    topic_ids.discard("")
    topic_stage_by_id = {
        _text(topic.get("id")): _text(topic.get("stage"))
        for topic in topic_items
        if _text(topic.get("id"))
    }
    issues: list[KnowledgeSeedSemanticIssue] = []
    seen_edges: dict[tuple[str, str, str], str] = {}
    prerequisite_edges: dict[str, set[str]] = {}

    for topic in topic_items:
        owner_id = _text(topic.get("id"))
        for field in ("prerequisites", "related"):
            refs = topic.get(field)
            if not isinstance(refs, list):
                continue
            for ref in refs:
                target_id = _ref_id(ref)
                relation = _relation(field, ref)
                if relation in EDGE_RELATION_ALIASES:
                    issues.append(
                        KnowledgeSeedSemanticIssue(
                            "edge_relation_alias",
                            f"{field} contains unsupported relation alias: {relation}",
                            owner_id,
                        )
                    )
                elif relation not in ALLOWED_RELATIONS:
                    issues.append(
                        KnowledgeSeedSemanticIssue(
                            "unknown_edge_relation",
                            f"{field} contains unknown relation: {relation}",
                            owner_id,
                        )
                    )
                if field == "prerequisites" and relation != "prerequisite":
                    issues.append(
                        KnowledgeSeedSemanticIssue(
                            "invalid_edge_relation_placement",
                            "prerequisites may only contain prerequisite relations",
                            owner_id,
                        )
                    )
                if field == "related" and relation == "prerequisite":
                    issues.append(
                        KnowledgeSeedSemanticIssue(
                            "invalid_edge_relation_placement",
                            "related must not contain prerequisite relations",
                            owner_id,
                        )
                    )
                if field == "prerequisites":
                    if not isinstance(ref, dict) or ref.get("required_mastery") is None:
                        issues.append(
                            KnowledgeSeedSemanticIssue(
                                "missing_required_mastery",
                                "prerequisite edge must declare required_mastery",
                                owner_id,
                            )
                        )
                    else:
                        raw_mastery = ref.get("required_mastery")
                        try:
                            required_mastery = float(raw_mastery)
                        except (TypeError, ValueError, OverflowError):
                            required_mastery = math.nan
                        if (
                            isinstance(raw_mastery, bool)
                            or not math.isfinite(required_mastery)
                            or not 0.0 <= required_mastery <= 1.0
                        ):
                            issues.append(
                                KnowledgeSeedSemanticIssue(
                                    "invalid_required_mastery",
                                    "prerequisite edge required_mastery must be between 0.0 and 1.0",
                                    owner_id,
                                )
                            )
                    if relation == "prerequisite":
                        prerequisite_rank = STAGE_ORDER.get(
                            topic_stage_by_id.get(target_id, "")
                        )
                        target_rank = STAGE_ORDER.get(
                            topic_stage_by_id.get(owner_id, "")
                        )
                        if (
                            prerequisite_rank is not None
                            and target_rank is not None
                            and prerequisite_rank > target_rank
                        ):
                            issues.append(
                                KnowledgeSeedSemanticIssue(
                                    "reverse_stage_prerequisite",
                                    (
                                        "prerequisite source stage must not be higher "
                                        f"than target stage: {target_id} "
                                        f"({topic_stage_by_id[target_id]}) -> {owner_id} "
                                        f"({topic_stage_by_id[owner_id]})"
                                    ),
                                    owner_id,
                                )
                            )
                if not target_id:
                    issues.append(
                        KnowledgeSeedSemanticIssue(
                            "invalid_reference",
                            f"{field} contains an empty reference",
                            owner_id,
                        )
                    )
                    continue
                if owner_id == target_id:
                    issues.append(
                        KnowledgeSeedSemanticIssue(
                            "self_reference",
                            f"{field} must not reference its own topic",
                            owner_id,
                        )
                    )
                if target_id not in topic_ids:
                    issues.append(
                        KnowledgeSeedSemanticIssue(
                            "missing_reference",
                            f"{field} references missing topic: {target_id}",
                            owner_id,
                        )
                    )

                if field == "prerequisites":
                    source_id, destination_id = target_id, owner_id
                else:
                    source_id, destination_id = owner_id, target_id
                if relation in SYMMETRIC_RELATIONS:
                    edge_key = (*sorted((source_id, destination_id)), relation)
                else:
                    edge_key = (source_id, destination_id, relation)
                previous_topic = seen_edges.get(edge_key)
                if previous_topic is not None:
                    issues.append(
                        KnowledgeSeedSemanticIssue(
                            "duplicate_edge",
                            f"duplicate {relation} edge; first declared by {previous_topic}",
                            owner_id,
                        )
                    )
                else:
                    seen_edges[edge_key] = owner_id

                if field == "prerequisites" and relation == "prerequisite":
                    prerequisite_edges.setdefault(source_id, set()).add(destination_id)

    for topic_id in sorted(_find_cycle_nodes(prerequisite_edges)):
        issues.append(
            KnowledgeSeedSemanticIssue(
                "prerequisite_cycle",
                "prerequisites must not form a cycle",
                topic_id,
            )
        )
    return tuple(issues)


__all__ = [
    "DEFAULT_PREREQUISITE_REQUIRED_MASTERY",
    "EDGE_RELATION_ALIASES",
    "KnowledgeSeedSemanticIssue",
    "STAGE_ORDER",
    "normalize_runtime_topic_relations",
    "validate_normalized_knowledge_topics",
]
