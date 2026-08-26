"""Read-model helpers for bounded knowledge-map responses.

The helpers in this module intentionally have no database dependency.  The
store adapter supplies canonical topic and edge rows; this module applies the
public paging and boundary-closure contract consistently for every client.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping
from typing import Any

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 200
DEFAULT_BOUNDARY_LIMIT = 50

_SCOPE_MACHINE_FIELDS = frozenset({"stage", "subject", "course_family"})
_CURSOR_FIELDS = ("stage", "subject", "course_family", "chapter", "unit", "depth", "id")


class MapCursorError(ValueError):
    """Raised when a map cursor is malformed or belongs to another scope."""


def _text(value: object) -> str:
    return str(value or "").strip()


def _machine_text(value: object) -> str:
    return _text(value).lower().replace("-", "_").replace(" ", "_")


def _scope_value(field: str, value: object) -> str:
    return _machine_text(value) if field in _SCOPE_MACHINE_FIELDS else _text(value)


def normalize_scope(scope: Mapping[str, object] | None) -> dict[str, str]:
    source = dict(scope or {})
    return {
        field: _scope_value(field, source.get(field))
        for field in ("stage", "subject", "course_family", "chapter", "unit")
    }


def topic_matches_scope(topic: Mapping[str, object], scope: Mapping[str, object] | None) -> bool:
    normalized = normalize_scope(scope)
    return all(
        not expected or _scope_value(field, topic.get(field)) == expected
        for field, expected in normalized.items()
    )


def topic_sort_key(topic: Mapping[str, object]) -> tuple[str, str, str, str, str, int, str]:
    try:
        depth = int(topic.get("depth") or 1)
    except (TypeError, ValueError, OverflowError):
        depth = 1
    return (
        _machine_text(topic.get("stage")),
        _machine_text(topic.get("subject")),
        _machine_text(topic.get("course_family")),
        _text(topic.get("chapter")),
        _text(topic.get("unit")),
        depth,
        _text(topic.get("id")),
    )


def _scope_token(scope: Mapping[str, object] | None) -> str:
    return json.dumps(normalize_scope(scope), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encode_cursor(*, scope: Mapping[str, object] | None, topic: Mapping[str, object]) -> str:
    payload = {"v": 1, "scope": _scope_token(scope), "after": list(topic_sort_key(topic))}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def decode_cursor(cursor: object, *, scope: Mapping[str, object] | None) -> tuple[str, str, str, str, str, int, str] | None:
    raw = _text(cursor)
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        after = payload.get("after")
        if payload.get("v") != 1 or payload.get("scope") != _scope_token(scope) or not isinstance(after, list) or len(after) != len(_CURSOR_FIELDS):
            raise ValueError("invalid cursor")
        return (
            _machine_text(after[0]),
            _machine_text(after[1]),
            _machine_text(after[2]),
            _text(after[3]),
            _text(after[4]),
            int(after[5]),
            _text(after[6]),
        )
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MapCursorError("invalid knowledge-map cursor") from exc


def _public_scope_node(topic: Mapping[str, object]) -> dict[str, Any]:
    node = dict(topic)
    node["in_scope"] = True
    node["boundary"] = False
    return node


def _public_boundary_node(topic: Mapping[str, object]) -> dict[str, Any]:
    node = dict(topic)
    node["in_scope"] = False
    node["boundary"] = True
    return node


def query_map_page(
    *,
    topics: Iterable[Mapping[str, object]],
    edges: Iterable[Mapping[str, object]],
    scope: Mapping[str, object] | None = None,
    page_size: object = DEFAULT_PAGE_SIZE,
    cursor: object = "",
    include_boundary: bool = True,
    boundary_limit: object = DEFAULT_BOUNDARY_LIMIT,
    catalog_revision: str = "",
) -> dict[str, Any]:
    """Build one explicit, cursor-paged map response from canonical rows."""

    try:
        safe_page_size = max(1, min(MAX_PAGE_SIZE, int(page_size or DEFAULT_PAGE_SIZE)))
    except (TypeError, ValueError, OverflowError):
        safe_page_size = DEFAULT_PAGE_SIZE
    try:
        safe_boundary_limit = max(0, int(boundary_limit))
    except (TypeError, ValueError, OverflowError):
        safe_boundary_limit = DEFAULT_BOUNDARY_LIMIT

    all_topics = [dict(item) for item in topics if _text(item.get("id"))]
    topics_by_id = {_text(item.get("id")): item for item in all_topics}
    scoped = sorted((item for item in all_topics if topic_matches_scope(item, scope)), key=topic_sort_key)
    after = decode_cursor(cursor, scope=scope)
    eligible = [item for item in scoped if after is None or topic_sort_key(item) > after]
    page_topics = eligible[:safe_page_size]
    page_ids = {_text(item.get("id")) for item in page_topics}
    has_more = len(eligible) > len(page_topics)

    canonical_edges = [
        dict(edge)
        for edge in edges
        if _text(edge.get("from")) and _text(edge.get("to"))
    ]
    boundary_ids: list[str] = []
    if include_boundary and page_ids:
        candidates = sorted(
            {
                endpoint
                for edge in canonical_edges
                if _text(edge.get("from")) in page_ids or _text(edge.get("to")) in page_ids
                for endpoint in (_text(edge.get("from")), _text(edge.get("to")))
                if endpoint and endpoint not in page_ids and endpoint in topics_by_id
            }
        )
        boundary_ids = candidates[:safe_boundary_limit]
        boundary_truncated = len(candidates) > len(boundary_ids)
    else:
        boundary_truncated = False

    included_ids = page_ids | set(boundary_ids)
    selected_edges = [
        edge
        for edge in canonical_edges
        if _text(edge.get("from")) in included_ids and _text(edge.get("to")) in included_ids
        and (_text(edge.get("from")) in page_ids or _text(edge.get("to")) in page_ids)
    ]
    nodes = [_public_scope_node(topic) for topic in page_topics]
    nodes.extend(_public_boundary_node(topics_by_id[topic_id]) for topic_id in boundary_ids)
    next_cursor = encode_cursor(scope=scope, topic=page_topics[-1]) if has_more and page_topics else ""

    return {
        "schema_version": 2,
        "catalog_revision": _text(catalog_revision),
        "scope": normalize_scope(scope),
        "scope_total_count": len(scoped),
        "scope_returned_count": len(page_topics),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "nodes": nodes,
        "edges": selected_edges,
        "boundary": {
            "returned_count": len(boundary_ids),
            "truncated": boundary_truncated,
        },
    }
