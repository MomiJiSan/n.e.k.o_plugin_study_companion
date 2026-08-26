from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

try:
    from .knowledge_graph_edges import build_topic_edges
    from .knowledge_graph_guidance import (
        _build_relationship_model_context,
        build_knowledge_guidance_payload,
    )
    from .knowledge_relation_retrieval import resolve_relationship_evidence
except ImportError:  # pragma: no cover - direct script execution
    package_name = "_study_companion_retrieval_eval"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parent)]  # type: ignore[attr-defined]
    sys.modules.setdefault(package_name, package)
    guidance_module = importlib.import_module(
        f"{package_name}.knowledge_graph_guidance"
    )
    edges_module = importlib.import_module(f"{package_name}.knowledge_graph_edges")
    relation_module = importlib.import_module(
        f"{package_name}.knowledge_relation_retrieval"
    )
    build_knowledge_guidance_payload = guidance_module.build_knowledge_guidance_payload
    _build_relationship_model_context = guidance_module._build_relationship_model_context
    build_topic_edges = edges_module.build_topic_edges
    resolve_relationship_evidence = relation_module.resolve_relationship_evidence


CORE_RELATIONS = ("prerequisite", "confusable", "procedure_step", "application")
COMPACT_CONTEXT_FIELDS = (
    "prerequisites",
    "procedure",
    "confusions",
    "applications",
    "extensions",
    "review_with",
    "practice_suggestions",
)
RAW_MODEL_CONTEXT_KEYS = ("topics", "nodes", "edges", "matches", "relation_groups")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {_text(item) for item in value if _text(item)}


def _compact_context_text(model_context: dict[str, Any]) -> str:
    pieces: list[str] = []
    for field in COMPACT_CONTEXT_FIELDS:
        value = model_context.get(field)
        if isinstance(value, list):
            pieces.extend(_text(item) for item in value if _text(item))
    return "\n".join(pieces)


def _has_raw_seed(model_context: dict[str, Any]) -> bool:
    summary = model_context.get("summary") if isinstance(model_context, dict) else {}
    return any(key in model_context for key in RAW_MODEL_CONTEXT_KEYS) or bool(
        isinstance(summary, dict) and summary.get("raw_seed_included")
    )


def _topic_maps(topics: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    subjects: dict[str, str] = {}
    labels: dict[str, str] = {}
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        topic_id = _text(topic.get("id") or topic.get("topic_id"))
        if not topic_id:
            continue
        subjects[topic_id] = _text(topic.get("subject"))
        labels[topic_id] = _text(topic.get("name") or topic.get("label") or topic_id)
    return subjects, labels


def _subgraph_node_maps(
    subgraph: dict[str, Any],
    *,
    fallback_subjects: dict[str, str],
    fallback_labels: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    subjects: dict[str, str] = dict(fallback_subjects)
    labels: dict[str, str] = dict(fallback_labels)
    nodes = subgraph.get("nodes") if isinstance(subgraph, dict) else []
    if not isinstance(nodes, list):
        return subjects, labels
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = _text(node.get("id"))
        if not node_id:
            continue
        subjects[node_id] = _text(node.get("subject"))
        labels[node_id] = _text(node.get("label") or node_id)
    return subjects, labels


def _cross_subject_edges(
    subgraph: dict[str, Any],
    *,
    topic_subjects: dict[str, str],
    topic_labels: dict[str, str],
) -> list[dict[str, Any]]:
    subjects, labels = _subgraph_node_maps(
        subgraph,
        fallback_subjects=topic_subjects,
        fallback_labels=topic_labels,
    )
    result: list[dict[str, Any]] = []
    edges = subgraph.get("edges") if isinstance(subgraph, dict) else []
    if not isinstance(edges, list):
        return result
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        from_id = _text(edge.get("from"))
        to_id = _text(edge.get("to"))
        from_subject = subjects.get(from_id, "")
        to_subject = subjects.get(to_id, "")
        if not from_subject or not to_subject or from_subject == to_subject:
            continue
        result.append(
            {
                "from": from_id,
                "to": to_id,
                "from_label": labels.get(from_id, from_id),
                "to_label": labels.get(to_id, to_id),
                "from_subject": from_subject,
                "to_subject": to_subject,
                "relation": _text(edge.get("relation")),
            }
        )
    return result


def _subgraph_edge_tuples(subgraph: dict[str, Any]) -> set[tuple[str, str, str]]:
    edges = subgraph.get("edges") if isinstance(subgraph, dict) else []
    if not isinstance(edges, list):
        return set()
    return {
        (
            _text(edge.get("from")),
            _text(edge.get("to")),
            _text(edge.get("relation")),
        )
        for edge in edges
        if isinstance(edge, dict)
        and _text(edge.get("from"))
        and _text(edge.get("to"))
    }


def _expected_links(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    expected: list[dict[str, Any]] = []
    for link in value:
        if not isinstance(link, dict):
            continue
        from_id = _text(link.get("from"))
        to_id = _text(link.get("to"))
        relations = _string_set(link.get("relations"))
        if from_id and to_id and relations:
            expected.append(
                {
                    "from": from_id,
                    "to": to_id,
                    "relations": sorted(relations),
                }
            )
    return expected


def _expected_paths(value: Any) -> list[dict[str, Any]]:
    """Normalize bounded endpoint-to-endpoint path expectations.

    Paths deliberately identify only endpoints and permitted persisted relation
    kinds.  They do not prescribe a traversal direction: a relationship query
    may name either endpoint first, while returned path edges retain their real
    server-side direction.
    """
    if not isinstance(value, list):
        return []
    expected: list[dict[str, Any]] = []
    for path in value:
        if not isinstance(path, dict):
            continue
        from_id = _text(path.get("from"))
        to_id = _text(path.get("to"))
        try:
            max_hops = int(path.get("max_hops") or 0)
        except (TypeError, ValueError):
            max_hops = 0
        relations = _string_set(path.get("allowed_relations"))
        if from_id and to_id and 1 <= max_hops <= 3 and relations:
            expected.append(
                {
                    "from": from_id,
                    "to": to_id,
                    "max_hops": max_hops,
                    "allowed_relations": sorted(relations),
                }
            )
    return expected


def _no_path_expectation(value: Any) -> dict[str, Any] | None:
    """Normalize an explicit bounded gap in the canonical graph."""
    if not isinstance(value, dict):
        return None
    paths = _expected_paths([value])
    return paths[0] if paths else None


def _expected_endpoint_ids(
    *,
    expected_topic_ids: set[str],
    expected_links: Iterable[dict[str, Any]],
    expected_paths: Iterable[dict[str, Any]] = (),
) -> set[str]:
    endpoint_ids = set(expected_topic_ids)
    for link in expected_links:
        endpoint_ids.add(_text(link.get("from")))
        endpoint_ids.add(_text(link.get("to")))
    for path in expected_paths:
        endpoint_ids.add(_text(path.get("from")))
        endpoint_ids.add(_text(path.get("to")))
    endpoint_ids.discard("")
    return endpoint_ids


def _expected_links_hit(
    *,
    expected_links: list[dict[str, Any]],
    subgraph_edges: set[tuple[str, str, str]],
) -> bool | None:
    if not expected_links:
        return None
    return all(
        any(
            actual_from == expected["from"]
            and actual_to == expected["to"]
            and actual_relation in set(expected["relations"])
            for actual_from, actual_to, actual_relation in subgraph_edges
        )
        for expected in expected_links
    )


def _path_exists(
    *,
    edges: Iterable[dict[str, Any]],
    from_id: str,
    to_id: str,
    max_hops: int,
    allowed_relations: set[str],
) -> bool:
    """Check a bounded, endpoint-focused path without an unbounded BFS."""
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        edge_from = _text(edge.get("from") or edge.get("from_id"))
        edge_to = _text(edge.get("to") or edge.get("to_id"))
        relation = _text(edge.get("relation"))
        if (
            not edge_from
            or not edge_to
            or not relation
            or relation not in allowed_relations
        ):
            continue
        adjacency.setdefault(edge_from, []).append(edge_to)
        adjacency.setdefault(edge_to, []).append(edge_from)
    pending: list[tuple[str, int]] = [(from_id, 0)]
    seen = {from_id}
    while pending:
        node_id, hops = pending.pop(0)
        if node_id == to_id:
            return True
        if hops >= max_hops:
            continue
        for adjacent_id in adjacency.get(node_id, []):
            if adjacent_id not in seen:
                seen.add(adjacent_id)
                pending.append((adjacent_id, hops + 1))
    return False


def _expected_paths_hit(
    *,
    expected_paths: list[dict[str, Any]],
    edges: Iterable[dict[str, Any]],
) -> bool | None:
    if not expected_paths:
        return None
    return all(
        _path_exists(
            edges=edges,
            from_id=path["from"],
            to_id=path["to"],
            max_hops=path["max_hops"],
            allowed_relations=set(path["allowed_relations"]),
        )
        for path in expected_paths
    )


def _context_has_expected_paths(
    *,
    expected_paths: list[dict[str, Any]],
    model_context: dict[str, Any],
) -> bool | None:
    """Verify the future V2 relationship block contains only the expected path."""
    if not expected_paths:
        return None
    relationship = model_context.get("relationship")
    if not isinstance(relationship, dict):
        return False
    endpoints = relationship.get("endpoints")
    path_edges = relationship.get("path")
    if not isinstance(endpoints, list) or not isinstance(path_edges, list):
        return False
    endpoint_ids = {
        _text(endpoint.get("id"))
        for endpoint in endpoints
        if isinstance(endpoint, dict) and _text(endpoint.get("id"))
    }
    relationship_edges = [edge for edge in path_edges if isinstance(edge, dict)]
    return all(
        {path["from"], path["to"]}.issubset(endpoint_ids)
        and _path_exists(
            edges=relationship_edges,
            from_id=path["from"],
            to_id=path["to"],
            max_hops=path["max_hops"],
            allowed_relations=set(path["allowed_relations"]),
        )
        for path in expected_paths
    )


def _relationship_evidence_edges(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt resolver edge keys to the common bounded-path evaluator."""
    return [
        {
            "from": _text(edge.get("from_id") or edge.get("from")),
            "to": _text(edge.get("to_id") or edge.get("to")),
            "relation": _text(edge.get("relation")),
            "reason": _text(edge.get("reason")),
        }
        for edge in evidence.get("path", [])
        if isinstance(edge, dict)
    ]


def _relationship_endpoint_ids(evidence: dict[str, Any]) -> set[str]:
    return {
        _text(endpoint.get("id"))
        for endpoint in evidence.get("endpoints", [])
        if isinstance(endpoint, dict) and _text(endpoint.get("id"))
    }


def _context_has_expected_endpoint_cue(
    *,
    endpoint_ids: set[str],
    topic_labels: dict[str, str],
    model_context: dict[str, Any],
) -> bool:
    compact_text = _compact_context_text(model_context)
    return bool(compact_text) and any(
        label and label in compact_text
        for topic_id in endpoint_ids
        if (label := _text(topic_labels.get(topic_id)))
    )


def _context_has_cross_subject_cue(
    *,
    cross_subject_edges: list[dict[str, Any]],
    model_context: dict[str, Any],
) -> bool:
    compact_text = _compact_context_text(model_context)
    if not compact_text:
        return False
    for edge in cross_subject_edges:
        for field in ("from_label", "to_label"):
            label = _text(edge.get(field))
            if label and label in compact_text:
                return True
    return False


def _thin_relation_groups(
    relation_groups: dict[str, Any],
    *,
    min_active_core_groups: int,
) -> list[str]:
    active = {
        relation
        for relation in CORE_RELATIONS
        if isinstance(relation_groups.get(relation), dict)
        and relation_groups[relation].get("items")
    }
    if len(active) >= min_active_core_groups:
        return []
    return [relation for relation in CORE_RELATIONS if relation not in active]


def _compact_matches(matches: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    if not isinstance(matches, list):
        return []
    compact: list[dict[str, Any]] = []
    for match in matches[:limit]:
        if not isinstance(match, dict):
            continue
        compact.append(
            {
                "id": _text(match.get("id")),
                "label": _text(match.get("label")),
                "score": match.get("score"),
                "match": _text(match.get("match")),
            }
        )
    return compact


def evaluate_knowledge_retrieval_queries(
    *,
    topics: list[dict[str, Any]],
    cases: Iterable[dict[str, Any]],
    min_active_core_groups: int = 2,
    strict: bool = False,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    topic_subjects, topic_labels = _topic_maps(topics)
    summary = {
        "case_count": 0,
        "passed_count": 0,
        "failed_count": 0,
        "focus_hit_count": 0,
        "cross_subject_case_count": 0,
        "cross_subject_edge_count": 0,
        "model_context_cross_subject_cue_count": 0,
        "thin_relation_group_count": 0,
        "raw_seed_included_count": 0,
        "bad_hub_absorption_count": 0,
        "expected_link_case_count": 0,
        "expected_link_hit_count": 0,
        "expected_endpoint_case_count": 0,
        "expected_endpoint_returned_count": 0,
        "model_context_cue_required_count": 0,
        "model_context_cue_hit_count": 0,
        "unrelated_cross_edge_only_count": 0,
        "relationship_case_count": 0,
        "endpoint_pair_hit_count": 0,
        "expected_direct_link_case_count": 0,
        "expected_direct_link_hit_count": 0,
        "expected_path_case_count": 0,
        "expected_path_hit_count": 0,
        "canonical_expected_path_hit_count": 0,
        "canonical_expected_direct_link_hit_count": 0,
        "canonical_relationship_explainable_count": 0,
        "context_path_case_count": 0,
        "context_path_hit_count": 0,
        "relationship_unresolved_count": 0,
        "known_no_path_case_count": 0,
        "known_no_path_confirmed_count": 0,
        "strict_passed_count": 0,
        "strict_failed_count": 0,
    }
    canonical_edges = build_topic_edges(topics)

    for case in cases:
        query = _text(case.get("query") if isinstance(case, dict) else "")
        topic_id = _text(case.get("topic_id") if isinstance(case, dict) else "")
        expected_topic_ids = _string_set(case.get("expected_topic_ids"))
        expected_links = _expected_links(case.get("expected_links"))
        expected_paths = _expected_paths(case.get("expected_paths"))
        query_mode = _text(case.get("query_mode"))
        relationship_query = query_mode == "relationship"
        retrieval_concepts = [
            _text(concept)
            for concept in case.get("retrieval_concepts", [])
            if _text(concept)
        ] if isinstance(case.get("retrieval_concepts"), list) else []
        primary_subject = _text(case.get("primary_subject"))
        no_path_expectation = _no_path_expectation(case.get("known_no_path"))
        known_no_path = no_path_expectation is not None
        require_model_context_cue = bool(case.get("require_model_context_cue"))
        expect_cross_subject = bool(case.get("expect_cross_subject"))
        payload = build_knowledge_guidance_payload(
            topics=topics,
            topic_id=topic_id,
            query=query,
        )
        focus_topic = payload.get("topic") if isinstance(payload, dict) else {}
        focus_topic_id = (
            _text(focus_topic.get("id")) if isinstance(focus_topic, dict) else ""
        )
        subgraph = payload.get("relevant_subgraph") if isinstance(payload, dict) else {}
        if not isinstance(subgraph, dict):
            subgraph = {}
        model_context = payload.get("model_context") if isinstance(payload, dict) else {}
        if not isinstance(model_context, dict):
            model_context = {}
        relation_groups = payload.get("relation_groups") if isinstance(payload, dict) else {}
        if not isinstance(relation_groups, dict):
            relation_groups = {}
        relationship_evidence = (
            resolve_relationship_evidence(
                topics=topics,
                query=query,
                retrieval_concepts=retrieval_concepts,
                primary_subject=primary_subject,
            )
            if relationship_query
            else {}
        )
        if not isinstance(relationship_evidence, dict):
            relationship_evidence = {}
        relationship_context = _build_relationship_model_context(
            relationship_evidence
        )

        cross_edges = _cross_subject_edges(
            subgraph,
            topic_subjects=topic_subjects,
            topic_labels=topic_labels,
        )
        has_cross_edge = bool(cross_edges)
        subgraph_edges = _subgraph_edge_tuples(subgraph)
        expected_endpoint_ids = _expected_endpoint_ids(
            expected_topic_ids=expected_topic_ids,
            expected_links=expected_links,
            expected_paths=expected_paths,
        )
        relationship_endpoint_ids = _expected_endpoint_ids(
            expected_topic_ids=set(),
            expected_links=expected_links,
            expected_paths=expected_paths,
        )
        if no_path_expectation is not None:
            relationship_endpoint_ids = {
                no_path_expectation["from"],
                no_path_expectation["to"],
            }
        nodes = subgraph.get("nodes")
        if not isinstance(nodes, list):
            nodes = []
        returned_node_ids = {
            _text(node.get("id"))
            for node in nodes
            if isinstance(node, dict) and _text(node.get("id"))
        }
        expected_endpoint_returned = (
            expected_endpoint_ids.issubset(returned_node_ids)
            if expected_endpoint_ids
            else None
        )
        expected_link_hit = _expected_links_hit(
            expected_links=expected_links,
            subgraph_edges=subgraph_edges,
        )
        canonical_expected_link_hit = _expected_links_hit(
            expected_links=expected_links,
            subgraph_edges=_subgraph_edge_tuples({"edges": canonical_edges}),
        )
        canonical_expected_path_hit = _expected_paths_hit(
            expected_paths=expected_paths,
            edges=canonical_edges,
        )
        evidence_edges = _relationship_evidence_edges(relationship_evidence)
        evidence_endpoint_ids = _relationship_endpoint_ids(relationship_evidence)
        v2_direct_link_hit = _expected_links_hit(
            expected_links=expected_links,
            subgraph_edges=_subgraph_edge_tuples({"edges": evidence_edges}),
        )
        v2_path_hit = _expected_paths_hit(
            expected_paths=expected_paths,
            edges=evidence_edges,
        )
        v2_context_path_hit = _context_has_expected_paths(
            expected_paths=expected_paths,
            model_context=relationship_context,
        )
        context_path_hit = v2_context_path_hit
        model_context_cue_hit = (
            _context_has_expected_endpoint_cue(
                endpoint_ids=expected_endpoint_ids,
                topic_labels=topic_labels,
                model_context=model_context,
            )
            if require_model_context_cue
            else None
        )
        cross_edge_incident_to_expected_endpoint = any(
            edge["from"] in expected_endpoint_ids
            or edge["to"] in expected_endpoint_ids
            for edge in cross_edges
        )
        legacy_unrelated_cross_edge_only = bool(
            expect_cross_subject
            and has_cross_edge
            and expected_endpoint_ids
            and not cross_edge_incident_to_expected_endpoint
        )
        has_cross_cue = _context_has_cross_subject_cue(
            cross_subject_edges=cross_edges,
            model_context=model_context,
        )
        thin_groups = _thin_relation_groups(
            relation_groups,
            min_active_core_groups=min_active_core_groups,
        )
        has_raw_seed = _has_raw_seed(model_context)
        focus_hit = not expected_topic_ids or focus_topic_id in expected_topic_ids
        bad_hub_absorbed = bool(expected_topic_ids) and not focus_hit
        endpoint_pair_hit = (
            relationship_endpoint_ids.issubset(evidence_endpoint_ids)
            if relationship_endpoint_ids
            else None
        )
        canonical_relationship_explainable = bool(
            canonical_expected_link_hit is True
            or canonical_expected_path_hit is True
        )
        relationship_unresolved = bool(
            relationship_query
            and relationship_evidence.get("relationship_unresolved", True)
        )
        unrelated_cross_edge_only = bool(
            relationship_query
            and bool(evidence_edges)
            and bool(relationship_endpoint_ids)
            and not relationship_endpoint_ids.issubset(evidence_endpoint_ids)
        )
        known_no_path_confirmed = bool(
            no_path_expectation is not None
            and not _path_exists(
                edges=canonical_edges,
                from_id=no_path_expectation["from"],
                to_id=no_path_expectation["to"],
                max_hops=no_path_expectation["max_hops"],
                allowed_relations=set(no_path_expectation["allowed_relations"]),
            )
        )
        cross_subject_ok = not expect_cross_subject or has_cross_edge
        case_passed = focus_hit and cross_subject_ok
        strict_relationship_ok = (
            known_no_path_confirmed and relationship_unresolved
            if known_no_path
            else (
                endpoint_pair_hit is True
                and (v2_direct_link_hit is not False)
                and (v2_path_hit is not False)
            )
        )
        strict_passed = (
            focus_hit and strict_relationship_ok
            if relationship_query
            else case_passed
        )
        failure_reasons: list[str] = []
        if not focus_hit:
            failure_reasons.append("expected focus topic was not selected")
        if not cross_subject_ok:
            failure_reasons.append("expected cross-subject edge was not returned")
        if strict and relationship_query and not strict_relationship_ok:
            failure_reasons.append("expected relationship evidence was not returned")

        summary["case_count"] += 1
        summary["passed_count"] += int(case_passed)
        summary["failed_count"] += int(not case_passed)
        summary["focus_hit_count"] += int(focus_hit)
        summary["cross_subject_case_count"] += int(expect_cross_subject)
        summary["cross_subject_edge_count"] += int(expect_cross_subject and has_cross_edge)
        summary["model_context_cross_subject_cue_count"] += int(
            expect_cross_subject and has_cross_cue
        )
        summary["thin_relation_group_count"] += int(bool(thin_groups))
        summary["raw_seed_included_count"] += int(has_raw_seed)
        summary["bad_hub_absorption_count"] += int(bad_hub_absorbed)
        summary["expected_link_case_count"] += int(bool(expected_links))
        summary["expected_link_hit_count"] += int(expected_link_hit is True)
        summary["expected_endpoint_case_count"] += int(bool(expected_endpoint_ids))
        summary["expected_endpoint_returned_count"] += int(
            expected_endpoint_returned is True
        )
        summary["model_context_cue_required_count"] += int(
            require_model_context_cue
        )
        summary["model_context_cue_hit_count"] += int(model_context_cue_hit is True)
        summary["unrelated_cross_edge_only_count"] += int(unrelated_cross_edge_only)
        summary["relationship_case_count"] += int(relationship_query)
        summary["endpoint_pair_hit_count"] += int(endpoint_pair_hit is True)
        summary["expected_direct_link_case_count"] += int(bool(expected_links))
        summary["expected_direct_link_hit_count"] += int(v2_direct_link_hit is True)
        summary["expected_path_case_count"] += int(bool(expected_paths))
        summary["expected_path_hit_count"] += int(v2_path_hit is True)
        summary["canonical_expected_path_hit_count"] += int(
            canonical_expected_path_hit is True
        )
        summary["canonical_expected_direct_link_hit_count"] += int(
            canonical_expected_link_hit is True
        )
        summary["canonical_relationship_explainable_count"] += int(
            canonical_relationship_explainable
        )
        summary["context_path_case_count"] += int(bool(expected_paths))
        summary["context_path_hit_count"] += int(context_path_hit is True)
        summary["relationship_unresolved_count"] += int(relationship_unresolved)
        summary["known_no_path_case_count"] += int(known_no_path)
        summary["known_no_path_confirmed_count"] += int(known_no_path_confirmed)
        summary["strict_passed_count"] += int(strict_passed)
        summary["strict_failed_count"] += int(not strict_passed)

        results.append(
            {
                "query": query,
                "expected_topic_ids": sorted(expected_topic_ids),
                "query_mode": query_mode,
                "retrieval_concepts": retrieval_concepts,
                "primary_subject": primary_subject,
                "expected_links": expected_links,
                "expected_paths": expected_paths,
                "known_no_path": known_no_path,
                "require_model_context_cue": require_model_context_cue,
                "expect_cross_subject": expect_cross_subject,
                "focus_topic_id": focus_topic_id,
                "focus_hit": focus_hit,
                "expected_link_hit": expected_link_hit,
                "expected_direct_link_hit": v2_direct_link_hit,
                "expected_path_hit": v2_path_hit,
                "canonical_expected_direct_link_hit": canonical_expected_link_hit,
                "canonical_expected_path_hit": canonical_expected_path_hit,
                "canonical_relationship_explainable": canonical_relationship_explainable,
                "context_path_hit": context_path_hit,
                "endpoint_pair_hit": endpoint_pair_hit,
                "relationship_unresolved": relationship_unresolved,
                "known_no_path_confirmed": known_no_path_confirmed,
                "expected_endpoint_returned": expected_endpoint_returned,
                "model_context_cue_hit": model_context_cue_hit,
                "unrelated_cross_edge_only": unrelated_cross_edge_only,
                "legacy_unrelated_cross_edge_only": legacy_unrelated_cross_edge_only,
                "relationship_evidence": relationship_evidence,
                "relationship_context": relationship_context,
                "passed": case_passed,
                "strict_passed": strict_passed,
                "failure_reasons": failure_reasons,
                "bad_hub_absorbed": bad_hub_absorbed,
                "top_matches": _compact_matches(payload.get("matches")),
                "subgraph_node_ids": [
                    _text(node.get("id"))
                    for node in nodes
                    if isinstance(node, dict) and _text(node.get("id"))
                ],
                "cross_subject_edges": cross_edges,
                "has_cross_subject_edge": has_cross_edge,
                "model_context_has_cross_subject_cue": has_cross_cue,
                "model_context": model_context,
                "thin_relation_groups": thin_groups,
                "has_raw_seed": has_raw_seed,
            }
        )

    return {"summary": summary, "results": results}


def _load_manifest_topics(manifest_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    seed_root = manifest_path.parent
    files = payload.get("files")
    if not isinstance(files, list):
        topics = payload.get("topics")
        if not isinstance(topics, list):
            return []
        return [topic for topic in topics if isinstance(topic, dict)]
    topics: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        relative_path = _text(item.get("path"))
        if not relative_path:
            continue
        seed_payload = json.loads(
            (seed_root / relative_path).read_text(encoding="utf-8-sig")
        )
        seed_topics = seed_payload.get("topics")
        if isinstance(seed_topics, list):
            topics.extend(topic for topic in seed_topics if isinstance(topic, dict))
    return topics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate compact Study Companion knowledge retrieval quality."
    )
    parser.add_argument("cases", help="JSON file containing retrieval eval cases.")
    parser.add_argument(
        "--seed",
        default=Path(__file__).resolve().parent / "static" / "knowledge_graph_seed.json",
        help="Path to knowledge_graph_seed.json or a single seed JSON file.",
    )
    parser.add_argument("--min-active-core-groups", type=int, default=2)
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Require relationship cases to return their declared endpoint pair "
            "and direct link or bounded path. The default retains the legacy gate."
        ),
    )
    args = parser.parse_args(argv)

    cases_payload = json.loads(Path(args.cases).read_text(encoding="utf-8-sig"))
    if isinstance(cases_payload, dict):
        cases = cases_payload.get("cases")
    else:
        cases = cases_payload
    if not isinstance(cases, list):
        raise SystemExit("cases JSON must be a list or an object with a cases list")
    topics = _load_manifest_topics(Path(args.seed))
    report = evaluate_knowledge_retrieval_queries(
        topics=topics,
        cases=[case for case in cases if isinstance(case, dict)],
        min_active_core_groups=max(1, int(args.min_active_core_groups)),
        strict=bool(args.strict),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    failures_key = "strict_failed_count" if args.strict else "failed_count"
    return 0 if int(report["summary"].get(failures_key) or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
