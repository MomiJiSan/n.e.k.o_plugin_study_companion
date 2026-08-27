from __future__ import annotations

import re
from collections import deque
from typing import Any

from ._graph_utils import (
    text as _text,
)
from ._graph_utils import (
    topic_id as _topic_id,
)
from ._graph_utils import (
    topic_label as _topic_label,
)
from .knowledge_graph_edges import (
    ALLOWED_RELATIONS as _ALLOWED_RELATIONS,
)
from .knowledge_graph_edges import (
    FOCUSED_RELATION_DIRECTION as _FOCUSED_RELATION_DIRECTION,
)
from .knowledge_graph_edges import (
    SYMMETRIC_RELATIONS as _SYMMETRIC_RELATIONS,
)
from .knowledge_graph_edges import (
    build_topic_edges as _build_topic_edges,
)
from .knowledge_graph_edges import (
    dedupe_edges as _dedupe_edges,
)
from .knowledge_graph_edges import (
    normalized_relation as _normalized_relation,
)

APPLICATION_RELATIONS = {"application", "procedure_step", "extends", "supports"}
CONFUSION_RELATIONS = {"confusable"}
NEXT_PRACTICE_RELATIONS = {"application", "procedure_step", "extends", "co_occurs", "next"}
RELATION_PRIORITY = {
    "prerequisite": 0,
    "procedure_step": 1,
    "application": 2,
    "supports": 3,
    "extends": 4,
    "co_occurs": 5,
    "next": 6,
    "confusable": 7,
}
_PUBLIC_LEARNING_PATH_LIMIT = 64
GENERIC_QUERY_TERMS = {
    "\u4e0d\u4f1a",
    "\u4e0d\u61c2",
    "\u600e\u4e48",
    "\u600e\u4e48\u5b66",
    "\u600e\u4e48\u505a",
    "\u600e\u4e48\u6c42",
    "\u600e\u4e48\u533a\u5206",
    "\u4e48\u5b66",
    "\u5982\u4f55",
    "\u5b66\u4e60",
    "\u4ec0\u4e48",
    "\u7406\u89e3",
    "\u5206\u6790",
    "\u8bf4\u660e",
    "\u89e3\u91ca",
    "\u8bb2\u89e3",
    "\u89e3\u7b54",
    "\u8c08\u8c08",
    "\u533a\u522b",
    "\u5173\u7cfb",
    "\u4e3a\u4ec0\u4e48",
    "\u7528\u6765",
    "\u4e00\u5b9a",
    "\u4e0d\u4e00\u5b9a",
    "\u51fa\u9898",
    "\u751f\u9898",
    "\u7ec3\u4e60",
    "\u9898\u76ee",
    "\u4e00\u9053",
    "\u4e00\u4e2a",
    "\u5185\u5bb9",
    "\u6839\u636e",
    "\u5e2e\u6211",
    "\u7ed9\u6211",
    "\u8fd9\u6bb5",
    "create",
    "generate",
    "question",
    "practice",
    "exercise",
}
SUBJECT_QUERY_HINTS = {
    "math": {
        "\u6570\u5b66",
        "\u51fd\u6570",
        "\u65b9\u7a0b",
        "\u51e0\u4f55",
        "\u6982\u7387",
        "\u4ee3\u6570",
        "math",
        "mathematics",
    },
    "physics": {
        "\u725b\u987f",
        "\u53d7\u529b",
        "\u901f\u5ea6",
        "\u52a0\u901f\u5ea6",
        "\u529f",
        "\u80fd\u91cf",
        "\u7535\u573a",
        "\u7535\u52bf",
    },
    "chemistry": {
        "\u5316\u5b66",
        "\u6c27\u5316",
        "\u8fd8\u539f",
        "\u914d\u5e73",
        "\u5e73\u8861",
        "ph",
        "\u7535\u79bb",
    },
    "biology": {
        "\u57fa\u56e0",
        "\u9057\u4f20",
        "\u8868\u73b0\u578b",
        "\u51cf\u6570\u5206\u88c2",
        "\u6709\u4e1d\u5206\u88c2",
    },
    "english": {
        "\u9605\u8bfb\u7406\u89e3",
        "\u4e3b\u65e8",
        "\u5b8c\u5f62",
        "\u957f\u96be\u53e5",
        "\u63a8\u65ad\u9898",
        "\u7ec6\u8282\u9898",
    },
    "computer_science": {
        "\u6570\u7ec4",
        "\u94fe\u8868",
        "\u6700\u77ed\u8def",
        "bfs",
        "dfs",
        "\u904d\u5386",
    },
    "politics": {
        "\u653f\u6cbb",
        "\u6cd5\u6cbb",
        "\u516c\u6c11",
        "\u6c11\u4e3b",
        "\u54f2\u5b66",
        "politics",
        "civics",
        "government",
    },
    "chinese": {
        "\u8bed\u6587",
        "\u4e2d\u6587",
        "\u9605\u8bfb",
        "\u4f5c\u6587",
        "\u6587\u8a00\u6587",
        "\u8bd7\u6b4c",
        "chinese",
        "literature",
    },
    "history": {
        "\u5386\u53f2",
        "\u671d\u4ee3",
        "\u9769\u547d",
        "\u6218\u4e89",
        "\u6587\u660e",
        "history",
        "historical",
    },
    "geography": {
        "\u5730\u7406",
        "\u6c14\u5019",
        "\u5730\u5f62",
        "\u7ecf\u7eac\u5ea6",
        "\u533a\u57df",
        "geography",
        "climate",
    },
    "economics": {
        "\u7ecf\u6d4e",
        "\u4f9b\u7ed9",
        "\u9700\u6c42",
        "\u5e02\u573a",
        "\u901a\u8d27\u81a8\u80c0",
        "economics",
        "economy",
        "market",
    },
}
RELATION_GROUP_TITLES = {
    "prerequisite": "\u5148\u8865\u4ec0\u4e48",
    "confusable": "\u5bb9\u6613\u6df7\u5728\u54ea\u91cc",
    "procedure_step": "\u89e3\u9898\u6d41\u7a0b\u4e0b\u4e00\u6b65",
    "application": "\u5178\u578b\u7528\u9014",
    "extends": "\u540e\u7eed\u62d3\u5c55",
    "co_occurs": "\u4e00\u8d77\u590d\u4e60",
    "supports": "\u652f\u6301\u7406\u89e3",
    "next": "\u5efa\u8bae\u4e0b\u4e00\u6b65",
    "nearby": "\u76f8\u90bb\u77e5\u8bc6",
}
RELATION_GROUP_ORDER = tuple(RELATION_GROUP_TITLES)


def _topic_aliases(topic: dict[str, Any]) -> list[str]:
    value = topic.get("aliases")
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)]


def _ref_id(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("id") or value.get("topic_id"))
    return _text(value)


def _edge_relation(field: str, value: Any) -> str:
    if isinstance(value, dict):
        relation = _normalized_relation(value.get("relation"))
        if relation:
            return relation
    return "prerequisite" if field == "prerequisites" else "co_occurs"


def _edge_reason(value: Any) -> str:
    return _text(value.get("reason")) if isinstance(value, dict) else ""


def _edge_use_cases(value: Any) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get("use_cases"), list):
        return []
    return [_text(item) for item in value["use_cases"] if _text(item)]


def _edge_priority_value(relation: str, ref: Any) -> str:
    if isinstance(ref, dict):
        priority = _text(ref.get("priority"))
        if priority in {"core", "useful", "optional"}:
            return priority
    if relation in {"prerequisite", "procedure_step", "confusable"}:
        return "core"
    if relation in {"application", "supports", "extends"}:
        return "useful"
    return "optional"


def _edge_context_value(relation: str, use_cases: list[str], ref: Any) -> str:
    if isinstance(ref, dict):
        context = _text(ref.get("context"))
        if context in {"diagnosis", "explanation", "practice", "review"}:
            return context
    if relation == "confusable":
        return "diagnosis"
    if relation in {"procedure_step", "application"}:
        return "practice"
    if relation in {"extends", "co_occurs"} or "review" in use_cases:
        return "review"
    return "explanation"


def _edge_confidence_value(ref: Any, *, reason: str, use_cases: list[str]) -> float:
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


def _relation_priority(edge: dict[str, Any]) -> int:
    return RELATION_PRIORITY.get(_normalized_relation(edge.get("relation")), 99)


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
        "from_label": _topic_label(source, source_id),
        "to_label": _topic_label(target, target_id),
        "relation": relation,
    }
    reason = _edge_reason(ref)
    if reason:
        payload["reason"] = reason
    use_cases = _edge_use_cases(ref)
    if use_cases:
        payload["use_cases"] = use_cases
    payload["priority"] = _edge_priority_value(relation, ref)
    payload["context"] = _edge_context_value(relation, use_cases, ref)
    payload["confidence"] = _edge_confidence_value(
        ref,
        reason=reason,
        use_cases=use_cases,
    )
    if isinstance(ref, dict) and ref.get("required_mastery") is not None:
        payload["required_mastery"] = ref.get("required_mastery")
    return payload


def build_topic_edges(topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compatibility export for callers that previously imported this helper."""
    return _build_topic_edges(topics)


def _topic_search_text(topic: dict[str, Any]) -> str:
    parts: list[str] = [
        _topic_id(topic),
        _topic_label(topic),
        _text(topic.get("subject")),
        _text(topic.get("chapter")),
        _text(topic.get("unit")),
        _text(topic.get("course_family")),
    ]
    parts.extend(_topic_aliases(topic))
    for field in ("skills", "question_types", "typical_misconceptions"):
        value = topic.get(field)
        if isinstance(value, list):
            parts.extend(_text(item) for item in value if _text(item))
    for example in topic.get("examples") or []:
        if isinstance(example, dict):
            parts.append(_text(example.get("prompt")))
    return " ".join(part for part in parts if part).lower()


_CJK_CONNECTORS = ("\u548c", "\u4e0e", "\u8ddf", "\u53ca", "\u3001")


def _cjk_fragments(value: str, *, strip_generic: bool) -> list[str]:
    """Extract complete CJK concept fragments without crossing connectors.

    CJK has no whitespace word boundary.  Treating a connector as removable
    used to join the two sides and manufacture fragments such as ``\u5dee\u76f8\u6570``;
    removing every generic phrase also split ``\u76f8\u5173\u7cfb\u6570`` into one-character
    pieces.  Keep connectors as separators and only make a generic phrase
    disappear when processing a user query, never a stored topic label.
    """
    cjk = "".join(
        char if "\u4e00" <= char <= "\u9fff" else " "
        for char in _text(value).lower()
    )
    if strip_generic:
        for stopword in sorted(GENERIC_QUERY_TERMS, key=len, reverse=True):
            # "\u5173\u7cfb" is generic by itself but is part of the complete concept
            # "\u76f8\u5173\u7cfb\u6570".  Do not split a potential label inside a query.
            if stopword in {"\u5206\u6790", "\u5173\u7cfb", "\u6709"}:
                continue
            cjk = cjk.replace(stopword, " ")
        # A trailing "\u6709" belongs to prompts such as "\u6709\u4ec0\u4e48\u533a\u522b", while
        # an in-concept character (for example "\u6709\u4e1d\u5206\u88c2") must remain intact.
        cjk = cjk.replace("\u6709\u5173", " ")
        cjk = re.sub("\u6709(?=\\s|$)", " ", cjk)
    for connector in _CJK_CONNECTORS:
        cjk = cjk.replace(connector, " ")
    return [fragment for fragment in cjk.split() if len(fragment) >= 2]


def _query_terms(query: str) -> list[str]:
    normalized = _text(query).lower()
    if not normalized:
        return []
    terms = {
        token
        for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) >= 2 and token not in GENERIC_QUERY_TERMS
    }
    for fragment in _cjk_fragments(normalized, strip_generic=True):
        terms.add(fragment)
        # N-grams remain a recall fallback, but they are built inside each
        # complete concept only.  In particular, no connector-spanning token
        # and no one-character CJK term may enter the matcher.
        for size in (2, 3, 4):
            for index in range(0, len(fragment) - size + 1):
                terms.add(fragment[index : index + size])
    return sorted(terms, key=lambda item: (-len(item), item))


def _has_full_concept_coverage(
    *,
    label: str,
    aliases: list[str],
    query: str,
) -> bool:
    """Whether a complete stored label or alias is covered by query concepts.

    This is a rank category, not an accumulated substring bonus: a topic named
    ``\u534f\u65b9\u5dee\u4e0e\u76f8\u5173\u7cfb\u6570`` must outrank a topic that only shares the
    short substring ``\u65b9\u5dee``.  Connector-normalized token coverage also accepts
    user wording with a different connector ("\u548c" vs "\u4e0e").
    """
    query_fragments = set(_cjk_fragments(query, strip_generic=True))
    query_words = {
        token for token in re.findall(r"[a-z0-9]+", _text(query).lower()) if len(token) >= 2
    }
    for candidate in (label, *aliases):
        candidate_fragments = _cjk_fragments(candidate, strip_generic=False)
        # A two-character alias is often an ambiguous abbreviation (for
        # example "\u9012\u5f52").  It remains a valid regular match, but cannot by
        # itself promote a broader topic over a complete stored label.
        short_alias = candidate != label and len(candidate_fragments) == 1 and len(candidate_fragments[0]) < 3
        if (
            candidate_fragments
            and not short_alias
            and set(candidate_fragments).issubset(query_fragments)
        ):
            return True
        candidate_words = {
            token
            for token in re.findall(r"[a-z0-9]+", _text(candidate).lower())
            if len(token) >= 2
        }
        if candidate_words and candidate_words.issubset(query_words):
            return True
    return False


def _label_prefix_concepts(label: str, query: str) -> list[str]:
    """Find complete query concepts at the beginning of a stored label.

    A user naming "\u5bfc\u6570" is strong evidence for "\u5bfc\u6570\u5b9a\u4e49", whereas a
    mention of "\u5bfc\u6570" in an example or a short substring inside another word is
    only fallback evidence.  This keeps lexical recall without rewarding
    arbitrary metadata accumulation.
    """
    label_fragments = _cjk_fragments(label, strip_generic=False)
    if not label_fragments:
        return []
    first_label_fragment = label_fragments[0]
    return [
        fragment
        for fragment in _cjk_fragments(query, strip_generic=True)
        if first_label_fragment.startswith(fragment)
    ]


def _has_meaningful_plain_query_term(query: str) -> bool:
    normalized = _text(query).lower()
    if any(
        len(token) >= 2 and token not in GENERIC_QUERY_TERMS
        for token in re.findall(r"[a-z0-9]+", normalized)
    ):
        return True
    cjk = "".join(
        char if "\u4e00" <= char <= "\u9fff" else " " for char in normalized
    )
    for stopword in sorted(GENERIC_QUERY_TERMS, key=len, reverse=True):
        cjk = cjk.replace(stopword, " ")
    cjk = cjk.replace("\u6709", " ")
    for connector in ("\u548c", "\u4e0e", "\u8ddf", "\u53ca", "\u3001"):
        cjk = cjk.replace(connector, " ")
    return any(len(fragment) >= 2 for fragment in cjk.split())


def _subject_hints(query: str) -> set[str]:
    normalized = _text(query).lower()
    if not normalized:
        return set()
    hints: set[str] = set()
    for subject, tokens in SUBJECT_QUERY_HINTS.items():
        if any(token in normalized for token in tokens):
            hints.add(subject)
    return hints


def match_topics(
    topics: list[dict[str, Any]],
    *,
    topic_id: str = "",
    query: str = "",
    subject: str = "",
    limit: int = 5,
) -> list[dict[str, Any]]:
    by_id = {_topic_id(topic): topic for topic in topics if _topic_id(topic)}
    topic_key = _text(topic_id)
    if topic_key and topic_key in by_id:
        return [
            {
                "id": topic_key,
                "label": _topic_label(by_id[topic_key], topic_key),
                "subject": _text(by_id[topic_key].get("subject")),
                "score": 100,
                "match": "topic_id",
            }
        ]
    query_text = query or topic_id
    subject_scope = _text(subject).lower()
    plain_query = not subject_scope
    if plain_query and not _has_meaningful_plain_query_term(query_text):
        return []
    terms = _query_terms(query_text)
    if plain_query:
        terms = [term for term in terms if len(term) >= 2]
    subject_hints = _subject_hints(query_text)
    if not terms:
        return []
    if subject_scope == "unknown":
        return []
    scored: list[dict[str, Any]] = []
    for topic in topics:
        current_id = _topic_id(topic)
        if not current_id:
            continue
        topic_subject = _text(topic.get("subject"))
        if subject_scope and topic_subject.lower() != subject_scope:
            continue
        label = _topic_label(topic, current_id)
        label_lower = label.lower()
        aliases = [alias.lower() for alias in _topic_aliases(topic)]
        haystack = _topic_search_text(topic)
        score = 0
        matched_terms: list[str] = []
        label_prefix_concepts = _label_prefix_concepts(label_lower, query_text)
        if label_prefix_concepts:
            # Keep direct concept-to-label evidence separate from a term that
            # only happens to occur in examples, skills, or misconceptions.
            score += 5
            matched_terms.extend(label_prefix_concepts)
        if _has_full_concept_coverage(
            label=label_lower,
            aliases=aliases,
            query=query_text,
        ):
            # Complete stored concepts are a stronger kind of evidence than
            # several overlapping short substrings.  Keep the numeric score
            # for the existing response contract, but do not let a partial
            # term such as "\u65b9\u5dee" outrank the full paired label.
            score += 100
            matched_terms.append(label_lower)
        if label_lower and label_lower in terms:
            score += 40
            matched_terms.append(label_lower)
        elif len(label_lower) >= 2 and label_lower in " ".join(terms):
            score += 24
            matched_terms.append(label_lower)
        for alias in aliases:
            if alias and alias in terms:
                score += 36
                matched_terms.append(alias)
            elif len(alias) >= 2 and alias in " ".join(terms):
                score += 20
                matched_terms.append(alias)
        for term in terms:
            if not term:
                continue
            if term == current_id.lower() or term == label.lower():
                score += 20
            elif term in aliases:
                score += 18
            elif any(term in alias for alias in aliases):
                score += 8 if len(term) >= 3 else 5
            elif label.lower().startswith(term):
                score += 18
            elif term in label.lower():
                score += 10
            elif term in haystack:
                score += 3
            else:
                continue
            matched_terms.append(term)
        if score and subject_hints:
            if topic_subject in subject_hints:
                score += 18
            elif topic_subject:
                score -= 10
        if score:
            scored.append(
                {
                    "id": current_id,
                    "label": label,
                    "subject": topic_subject,
                    "score": score,
                    "match": "query",
                    "matched_terms": list(dict.fromkeys(matched_terms))[:6],
                }
            )
    return sorted(
        scored,
        key=lambda item: (
            -int(item["score"]),
            len(_text(item["label"])),
            item["label"],
        ),
    )[: max(1, int(limit or 5))]


def _learning_path_for_topic(
    *,
    topic_id: str,
    by_id: dict[str, dict[str, Any]],
    incoming: dict[str, list[dict[str, Any]]],
    max_depth: int,
) -> list[dict[str, Any]]:
    queue: deque[tuple[str, int]] = deque([(topic_id, 0)])
    seen = {topic_id}
    path: list[dict[str, Any]] = []
    while queue:
        current_id, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for edge in sorted(incoming.get(current_id, []), key=_relation_priority):
            parent_id = _text(edge.get("from"))
            if not parent_id or parent_id in seen:
                continue
            seen.add(parent_id)
            item = dict(edge)
            item["depth"] = depth + 1
            item["topic"] = by_id.get(parent_id, {})
            path.append(item)
            queue.append((parent_id, depth + 1))
    return path


def _public_learning_path_sort_key(edge: dict[str, Any]) -> tuple[int, int, str, str, str, str]:
    return (
        int(edge.get("depth") or 0),
        _relation_priority(edge),
        _text(edge.get("from_label")),
        _text(edge.get("to_label")),
        _text(edge.get("from")),
        _text(edge.get("to")),
    )


def _limit_public_learning_path(
    learning_path: list[dict[str, Any]], *, max_items: int = _PUBLIC_LEARNING_PATH_LIMIT
) -> list[dict[str, Any]]:
    """Bound public learning-path payloads without hiding direct prerequisites.

    Direct neighbours are retained before deeper context.  Each bucket has a
    canonical sort key, so its selection remains stable if graph construction
    changes iteration order.  Bundled graph nodes have fewer than 64 direct
    parents; the final slice is a defensive hard cap for externally supplied
    topic data.
    """
    bounded_limit = max(0, int(max_items))
    if not bounded_limit:
        return []
    direct = sorted(
        (edge for edge in learning_path if int(edge.get("depth") or 0) == 1),
        key=_public_learning_path_sort_key,
    )
    remaining = sorted(
        (edge for edge in learning_path if int(edge.get("depth") or 0) != 1),
        key=_public_learning_path_sort_key,
    )
    return [*direct, *remaining][:bounded_limit]


def _sort_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        _dedupe_edges(edges),
        key=lambda edge: (
            _relation_priority(edge),
            _text(edge.get("from_label")),
            _text(edge.get("to_label")),
            _text(edge.get("from")),
            _text(edge.get("to")),
        ),
    )


def _build_relation_groups(
    *,
    learning_path: list[dict[str, Any]],
    applications: list[dict[str, Any]],
    confusions: list[dict[str, Any]],
    next_practice: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    # These public groups are a view of ``learning_path``.  Keep their edge
    # set identical to the already-budgeted path; the dedicated application,
    # confusion, and next-practice fields retain their established contracts.
    candidates = _dedupe_edges(learning_path)
    grouped: dict[str, list[dict[str, Any]]] = {
        relation: [] for relation in RELATION_GROUP_ORDER
    }
    for edge in candidates:
        relation = _normalized_relation(edge.get("relation"))
        if relation in grouped:
            normalized_edge = dict(edge)
            normalized_edge["relation"] = relation
            grouped[relation].append(normalized_edge)
    return {
        relation: {
            "relation": relation,
            "title": RELATION_GROUP_TITLES[relation],
            "items": _sort_edges(items),
        }
        for relation, items in grouped.items()
    }


def _build_guidance_sections(
    relation_groups: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {
            "relation": relation,
            "title": group["title"],
            "items": group["items"],
        }
        for relation, group in relation_groups.items()
    ]


def _other_topic_for_edge(edge: dict[str, Any], selected_id: str) -> tuple[str, str]:
    if _text(edge.get("from")) == selected_id:
        return _text(edge.get("to")), _text(edge.get("to_label"))
    return _text(edge.get("from")), _text(edge.get("from_label"))


def _question_payload(
    *,
    kind: str,
    topic_id: str,
    topic_label: str,
    question: str,
    edge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": kind,
        "topic_id": topic_id,
        "topic_label": topic_label or topic_id,
        "question": question,
    }
    if edge:
        payload["relation"] = _text(edge.get("relation"))
        reason = _text(edge.get("reason"))
        if reason:
            payload["reason"] = reason
    return payload


def _direct_diagnosis_edges(
    *,
    selected_id: str,
    by_id: dict[str, dict[str, Any]],
    incoming: dict[str, list[dict[str, Any]]],
    outgoing: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return the canonical one-hop evidence allowed to drive diagnosis.

    Learning paths intentionally include ancestors several hops away.  Those
    edges remain useful public explanation, but asking a learner a diagnostic
    question about them implies a direct relationship that may not exist.
    """
    selected_id = _text(selected_id)
    if not selected_id or selected_id not in by_id:
        return []
    evidence: list[dict[str, Any]] = []
    for edge in [*(incoming.get(selected_id) or []), *(outgoing.get(selected_id) or [])]:
        if not isinstance(edge, dict):
            continue
        source_id = _text(edge.get("from"))
        target_id = _text(edge.get("to"))
        relation = _normalized_relation(edge.get("relation"))
        if selected_id not in {source_id, target_id}:
            continue
        if not source_id or not target_id or source_id == target_id:
            continue
        # Diagnostics only use a relation in the direction its wording claims.
        if relation in {"prerequisite", "procedure_step"} and target_id != selected_id:
            continue
        if relation in {"application", "extends", "next"} and source_id != selected_id:
            continue
        if relation not in {
            "prerequisite",
            "procedure_step",
            "application",
            "extends",
            "next",
            "confusable",
            "co_occurs",
        }:
            continue
        other_id = target_id if source_id == selected_id else source_id
        other_topic = by_id.get(other_id)
        source_topic = by_id.get(source_id)
        target_topic = by_id.get(target_id)
        if other_topic is None or source_topic is None or target_topic is None:
            continue
        normalized = dict(edge)
        normalized.update(
            {
                "from": source_id,
                "to": target_id,
                "from_label": _topic_label(source_topic, source_id),
                "to_label": _topic_label(target_topic, target_id),
                "relation": relation,
            }
        )
        evidence.append(normalized)
    return _sort_edges(evidence)


def _build_diagnosis_questions(
    *,
    selected_id: str,
    selected_label: str,
    direct_edges: list[dict[str, Any]],
    limit: int = 8,
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    kind_limits = {
        "prerequisite_probe": 3,
        "procedure_probe": 2,
        "confusion_check": 2,
        "application_practice": 2,
        "extension_suggestion": 1,
        "next_step": 1,
        "related_review": 1,
    }

    def add(payload: dict[str, Any]) -> None:
        key = (_text(payload.get("kind")), _text(payload.get("topic_id")))
        if not key[0] or not key[1] or key in seen:
            return
        if sum(1 for item in questions if item["kind"] == key[0]) >= kind_limits.get(
            key[0], 0
        ):
            return
        seen.add(key)
        questions.append(payload)

    # Reserve one signal for each supported direct relation.  In particular, a
    # dense prerequisite chain must not starve application, extension, or next
    # step actions under the total public output budget.
    reserved_relations = (
        "prerequisite",
        "procedure_step",
        "application",
        "extends",
        "next",
        "confusable",
        "co_occurs",
    )
    ordered_edges: list[dict[str, Any]] = []
    reserved_keys: set[tuple[str, str, str]] = set()
    for relation in reserved_relations:
        for edge in direct_edges:
            if _normalized_relation(edge.get("relation")) != relation:
                continue
            key = (
                _text(edge.get("from")),
                _text(edge.get("to")),
                relation,
            )
            if key not in reserved_keys:
                reserved_keys.add(key)
                ordered_edges.append(edge)
            break
    ordered_edges.extend(
        edge
        for edge in direct_edges
        if (
            _text(edge.get("from")),
            _text(edge.get("to")),
            _normalized_relation(edge.get("relation")),
        )
        not in reserved_keys
    )

    for edge in ordered_edges:
        relation = _normalized_relation(edge.get("relation"))
        topic_id, topic_label = _other_topic_for_edge(edge, selected_id)
        if not topic_id or topic_id == selected_id:
            continue
        if relation == "procedure_step":
            add(
                _question_payload(
                    kind="procedure_probe",
                    topic_id=topic_id,
                    topic_label=topic_label,
                    question=(
                        f"你是卡在“{topic_label or topic_id}”这一步，"
                        f"还是不知道它在“{selected_label}”里该放到哪里？"
                    ),
                    edge=edge,
                )
            )
        elif relation == "application":
            add(
                _question_payload(
                    kind="application_practice",
                    topic_id=topic_id,
                    topic_label=topic_label,
                    question=(
                        f"要不要用“{topic_label or topic_id}”做一道典型题，"
                        f"看看“{selected_label}”怎样落到题目里？"
                    ),
                    edge=edge,
                )
            )
        elif relation == "prerequisite":
            add(
                _question_payload(
                    kind="prerequisite_probe",
                    topic_id=topic_id,
                    topic_label=topic_label,
                    question=(
                        f"你是卡在“{topic_label or topic_id}”，"
                        f"还是不知道它怎样用于“{selected_label}”？"
                    ),
                    edge=edge,
                )
            )
        elif relation == "confusable":
            add(
                _question_payload(
                    kind="confusion_check",
                    topic_id=topic_id,
                    topic_label=topic_label,
                    question=f"你是不是把“{selected_label}”和“{topic_label or topic_id}”混在一起了？",
                    edge=edge,
                )
            )
        elif relation == "extends":
            add(
                _question_payload(
                    kind="extension_suggestion",
                    topic_id=topic_id,
                    topic_label=topic_label,
                    question=(
                        f"如果基础判断已经会了，要不要进阶到“{topic_label or topic_id}”？"
                    ),
                    edge=edge,
                )
            )
        elif relation == "co_occurs":
            add(
                _question_payload(
                    kind="related_review",
                    topic_id=topic_id,
                    topic_label=topic_label,
                    question=(
                        f"要不要顺手复习“{topic_label or topic_id}”，"
                        f"它经常和“{selected_label}”一起出现？"
                    ),
                    edge=edge,
                )
            )
        elif relation == "next":
            add(
                _question_payload(
                    kind="next_step",
                    topic_id=topic_id,
                    topic_label=topic_label,
                    question=(
                        f"要不要下一步练“{topic_label or topic_id}”，"
                        f"把“{selected_label}”用到具体题里？"
                    ),
                    edge=edge,
                )
            )
        if len(questions) >= limit:
            break

    return questions[:limit]


def _focused_context_summary(
    relevant_subgraph: dict[str, Any], *, diagnostics: dict[str, int]
) -> dict[str, Any]:
    """Return the non-relational portion of the compact model context.

    ``relevant_subgraph`` remains the public, retrieval-oriented explanation of
    a match.  The model context is deliberately stricter: it is evidence for one
    selected topic, rather than a summary of every edge in the retrieved
    neighbourhood.
    """
    summary = (
        dict(relevant_subgraph.get("summary") or {})
        if isinstance(relevant_subgraph, dict)
        else {}
    )
    return {
        "node_count": int(summary.get("node_count") or 0),
        "edge_count": int(summary.get("edge_count") or 0),
        "raw_seed_included": False,
        "diagnostics": dict(diagnostics),
    }


def _build_relationship_model_context(evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Reduce bounded relationship evidence to the generation contract.

    Relationship retrieval is deliberately separate from focused-topic context:
    its path may cross subjects and may include intermediary topics.  This
    adapter only accepts the resolver's bounded path, preserves stored edge
    direction, and never expands it with graph neighbours or seed records.
    """
    source = evidence if isinstance(evidence, dict) else {}
    if (
        not source
        or not bool(source.get("resolved"))
        or bool(source.get("relationship_unresolved"))
    ):
        return {"relationship_unresolved": True}

    endpoints: list[dict[str, str]] = []
    seen_endpoint_ids: set[str] = set()
    for endpoint in list(source.get("endpoints") or []):
        if not isinstance(endpoint, dict) or len(endpoints) >= 2:
            continue
        endpoint_id = _text(endpoint.get("id"))
        label = _text(endpoint.get("label"))
        subject = _text(endpoint.get("subject"))
        if not endpoint_id or not label or endpoint_id in seen_endpoint_ids:
            continue
        seen_endpoint_ids.add(endpoint_id)
        endpoints.append({"id": endpoint_id, "label": label, "subject": subject})
    if len(endpoints) != 2:
        return {"relationship_unresolved": True}

    path: list[dict[str, str]] = []
    for edge in list(source.get("path") or []):
        if not isinstance(edge, dict) or len(path) >= 3:
            continue
        from_id = _text(edge.get("from_id") or edge.get("from"))
        to_id = _text(edge.get("to_id") or edge.get("to"))
        relation = _normalized_relation(edge.get("relation"))
        if not from_id or not to_id or from_id == to_id or relation not in _ALLOWED_RELATIONS:
            continue
        path.append(
            {
                "from_id": from_id,
                "to_id": to_id,
                "relation": relation,
                "reason": _text(edge.get("reason")),
            }
        )
    if not path:
        return {"relationship_unresolved": True}

    return {
        "relationship": {
            "endpoints": endpoints,
            "path": path,
            "hop_count": len(path),
        }
    }


def _build_focused_model_context(
    *,
    selected_id: str,
    by_id: dict[str, dict[str, Any]],
    incoming_edges: dict[str, list[dict[str, Any]]],
    outgoing_edges: dict[str, list[dict[str, Any]]],
    relevant_subgraph: dict[str, Any],
    mode: str = "guidance",
) -> dict[str, Any]:
    """Build generation evidence from canonical, one-hop topic relations.

    The graph UI may include a multi-hop retrieved subgraph.  Reusing its
    relation groups here previously made edges between two neighbouring topics
    look like direct relations of ``selected_id``.  This function intentionally
    reads only the canonical incident-edge index and derives every label from
    the canonical topic map.
    """
    selected_id = _text(selected_id)
    diagnostics = {
        "guidance_self_relation_dropped": 0,
        "guidance_nonincident_edge_dropped": 0,
        "guidance_direction_mismatch_dropped": 0,
    }
    focus_topic = by_id.get(selected_id) if selected_id else None
    context: dict[str, Any] = {
        "mode": mode,
        "query": _text(relevant_subgraph.get("query")),
        "focus": {
            "id": selected_id if focus_topic else "",
            "label": _topic_label(focus_topic, selected_id) if focus_topic else "",
        },
        "prerequisites": [],
        "procedure": [],
        "confusions": [],
        "applications": [],
        "extensions": [],
        "supporting_concepts": [],
        "review_with": [],
        "analogies": [],
        "next_topics": [],
        "practice_suggestions": [],
        "summary": _focused_context_summary(
            relevant_subgraph, diagnostics=diagnostics
        ),
    }
    if not selected_id or focus_topic is None:
        return context

    relation_targets: dict[str, list[tuple[str, str]]] = {
        "prerequisites": [],
        "procedure": [],
        "confusions": [],
        "applications": [],
        "extensions": [],
        "supporting_concepts": [],
        "review_with": [],
        "analogies": [],
        "next_topics": [],
    }

    # Keep direction checks explicit rather than inferring semantics from an
    # arbitrary subgraph edge.  Symmetric relations are allowed from either
    # side; all other supported relations have a single canonical direction.
    supported_relations = _ALLOWED_RELATIONS
    indexed_edges = [
        *(incoming_edges.get(selected_id) or []),
        *(outgoing_edges.get(selected_id) or []),
    ]
    seen_edges: set[tuple[str, str, str]] = set()
    for edge in indexed_edges:
        if not isinstance(edge, dict):
            diagnostics["guidance_nonincident_edge_dropped"] += 1
            continue
        source_id = _text(edge.get("from"))
        target_id = _text(edge.get("to"))
        relation = _normalized_relation(edge.get("relation"))
        edge_key = (source_id, target_id, relation)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)
        if relation not in supported_relations:
            continue
        if source_id == selected_id and target_id == selected_id:
            diagnostics["guidance_self_relation_dropped"] += 1
            continue
        if selected_id not in {source_id, target_id}:
            diagnostics["guidance_nonincident_edge_dropped"] += 1
            continue
        other_id = target_id if source_id == selected_id else source_id
        if not other_id or other_id == selected_id:
            diagnostics["guidance_self_relation_dropped"] += 1
            continue
        other_topic = by_id.get(other_id)
        if other_topic is None:
            diagnostics["guidance_nonincident_edge_dropped"] += 1
            continue

        incoming = target_id == selected_id
        expected_direction = _FOCUSED_RELATION_DIRECTION.get(relation)
        if expected_direction == "incoming" and not incoming:
            diagnostics["guidance_direction_mismatch_dropped"] += 1
            continue
        if expected_direction == "outgoing" and incoming:
            diagnostics["guidance_direction_mismatch_dropped"] += 1
            continue

        if relation == "prerequisite":
            bucket = "prerequisites"
        elif relation == "procedure_step":
            bucket = "procedure"
        elif relation == "application":
            bucket = "applications"
        elif relation == "extends":
            bucket = "extensions"
        elif relation == "supports":
            bucket = "supporting_concepts"
        elif relation == "next":
            bucket = "next_topics"
        elif relation == "confusable":
            bucket = "confusions"
        elif relation == "analogy":
            bucket = "analogies"
        elif relation in {"co_occurs", "nearby"}:
            bucket = "review_with"
        else:  # Defensive assertion for a future relation-contract change.
            if relation not in _SYMMETRIC_RELATIONS:
                diagnostics["guidance_direction_mismatch_dropped"] += 1
            continue
        relation_targets[bucket].append(
            (other_id, _topic_label(other_topic, other_id))
        )

    def labels(items: list[tuple[str, str]], *, limit: int | None = None) -> list[str]:
        # Labels are what the prompt contract consumes.  Deduping them preserves
        # that contract while the topic-id sort makes output stable across index
        # construction order.
        ordered = sorted(items, key=lambda item: (item[1].casefold(), item[0]))
        values: list[str] = []
        seen_labels: set[str] = set()
        for _related_topic_id, label in ordered:
            normalized_label = _text(label)
            if not normalized_label or normalized_label in seen_labels:
                continue
            seen_labels.add(normalized_label)
            values.append(normalized_label)
            if limit is not None and len(values) >= limit:
                break
        return values

    for key in (
        "prerequisites",
        "procedure",
        "confusions",
        "applications",
        "extensions",
        "supporting_concepts",
        "review_with",
        "analogies",
        "next_topics",
    ):
        context[key] = labels(relation_targets[key])
    context["practice_suggestions"] = labels(
        [
            *relation_targets["procedure"],
            *relation_targets["applications"],
            *relation_targets["next_topics"],
        ],
        limit=6,
    )
    context["summary"]["diagnostics"] = dict(diagnostics)
    return context


def _canonical_necessary_relations(
    *, topics: list[dict[str, Any]], topic_id: str
) -> dict[str, list[str]]:
    """Rebuild semantic-validation evidence from server-side graph records."""
    from .knowledge_graph_index import KnowledgeGraphIndex  # lazy import avoids a cycle

    graph_index = KnowledgeGraphIndex(list(topics or []))
    focused = _build_focused_model_context(
        selected_id=topic_id,
        by_id=graph_index.by_id,
        incoming_edges=graph_index.incoming_edges,
        outgoing_edges=graph_index.outgoing_edges,
        relevant_subgraph={},
    )
    return {
        key: list(focused.get(key) or [])
        for key in (
            "prerequisites",
            "procedure",
            "confusions",
            "applications",
            "extensions",
            "supporting_concepts",
            "review_with",
            "analogies",
            "next_topics",
        )
        if focused.get(key)
    }


def build_knowledge_guidance_payload(
    *,
    topics: list[dict[str, Any]],
    topic_id: str = "",
    query: str = "",
    response_mode: str = "problem_solving",
    max_depth: int = 3,
    match_limit: int = 5,
) -> dict[str, Any]:
    topic_items = list(topics or [])
    from .knowledge_graph_index import (  # lazy import avoids a module import cycle
        KnowledgeGraphIndex,
        SubgraphBudget,
        build_relevant_subgraph,
    )
    graph_index = KnowledgeGraphIndex(topic_items)

    normalized_response_mode = _text(response_mode).lower() or "problem_solving"
    subgraph_budget = SubgraphBudget(
        focus_topics=max(1, min(3, int(match_limit or 3))),
        max_depth=(
            1
            if normalized_response_mode == "general_discussion"
            else max(1, min(2, int(max_depth or 2)))
        ),
        max_nodes=24,
    )
    relevant_subgraph = build_relevant_subgraph(
        graph_index,
        topic_id=topic_id,
        query=query,
        budget=subgraph_budget,
    )
    allowed_relations = {
        "general_explanation": {
            "prerequisite", "application", "supports", "extends", "confusable"
        },
        "general_discussion": {"application", "supports", "extends", "co_occurs"},
        "unknown": {
            "prerequisite", "application", "supports", "extends", "confusable"
        },
    }.get(normalized_response_mode)
    if allowed_relations is not None:
        relevant_subgraph = dict(relevant_subgraph)
        relation_groups = relevant_subgraph.get("relation_groups")
        relation_groups = relation_groups if isinstance(relation_groups, dict) else {}
        filtered_groups = {
            relation: group
            for relation, group in relation_groups.items()
            if relation in allowed_relations
        }
        edges = [
            edge for edge in list(relevant_subgraph.get("edges") or [])
            if isinstance(edge, dict)
            and _normalized_relation(edge.get("relation")) in allowed_relations
        ]
        referenced_ids = set()
        for edge in edges:
            referenced_ids.update({_text(edge.get("from")), _text(edge.get("to"))})
        focus_ids = {
            _text(topic.get("id"))
            for topic in list(relevant_subgraph.get("focus_topics") or [])
            if isinstance(topic, dict)
        }
        relevant_subgraph["relation_groups"] = filtered_groups
        relevant_subgraph["edges"] = edges
        relevant_subgraph["nodes"] = [
            node for node in list(relevant_subgraph.get("nodes") or [])
            if isinstance(node, dict)
            and _text(node.get("id")) in referenced_ids.union(focus_ids)
        ]
        summary = dict(relevant_subgraph.get("summary") or {})
        summary["node_count"] = len(relevant_subgraph["nodes"])
        summary["edge_count"] = len(edges)
        relevant_subgraph["summary"] = summary
    by_id = graph_index.by_id
    edges = graph_index.edges
    incoming = graph_index.incoming_edges
    outgoing = graph_index.outgoing_edges

    matches = graph_index.match(topic_id=topic_id, query=query, limit=match_limit)
    selected_id = _text(matches[0]["id"]) if matches else _text(topic_id)
    selected_topic = by_id.get(selected_id)
    model_context = _build_focused_model_context(
        selected_id=selected_id,
        by_id=by_id,
        incoming_edges=incoming,
        outgoing_edges=outgoing,
        relevant_subgraph=relevant_subgraph,
    )
    if normalized_response_mode != "problem_solving":
        model_context["practice_suggestions"] = []
    if not selected_topic:
        relation_groups = _build_relation_groups(
            learning_path=[],
            applications=[],
            confusions=[],
            next_practice=[],
        )
        return {
            "topic": {},
            "matches": matches,
            "learning_path": [],
            "applications": [],
            "confusions": [],
            "next_practice_topics": [],
            "relation_groups": relation_groups,
            "guidance_sections": _build_guidance_sections(relation_groups),
            "diagnosis_questions": [],
            "relevant_subgraph": relevant_subgraph,
            "model_context": model_context,
            "summary": {
                "matched": False,
                "topic_count": len(topic_items),
                "edge_count": len(edges),
                "learning_path_total_count": 0,
                "learning_path_returned_count": 0,
                "learning_path_truncated": False,
                "active_relation_group_count": 0,
                "diagnosis_question_count": 0,
                "subgraph_node_count": relevant_subgraph["summary"]["node_count"],
                "subgraph_edge_count": relevant_subgraph["summary"]["edge_count"],
                "raw_seed_included": False,
            },
        }

    learning_path = _learning_path_for_topic(
        topic_id=selected_id,
        by_id=by_id,
        incoming=incoming,
        max_depth=max(1, int(max_depth or 3)),
    )
    outgoing_edges = outgoing.get(selected_id, [])
    applications = [
        edge for edge in outgoing_edges if _normalized_relation(edge.get("relation")) in APPLICATION_RELATIONS
    ]
    incoming_edges = incoming.get(selected_id, [])
    confusions = _dedupe_edges(
        [
            edge
            for edge in [*outgoing_edges, *incoming_edges]
            if _normalized_relation(edge.get("relation")) in CONFUSION_RELATIONS
        ]
    )
    next_practice = [
        edge
        for edge in outgoing_edges
        if _normalized_relation(edge.get("relation")) in NEXT_PRACTICE_RELATIONS
    ]
    if normalized_response_mode == "general_explanation":
        next_practice = []
    elif normalized_response_mode == "general_discussion":
        learning_path = []
        confusions = []
        next_practice = []
    direct_diagnosis_edges = _direct_diagnosis_edges(
        selected_id=selected_id,
        by_id=by_id,
        incoming=incoming,
        outgoing=outgoing,
    )
    direct_public_path_items: list[dict[str, Any]] = []
    for edge in direct_diagnosis_edges:
        relation = _normalized_relation(edge.get("relation"))
        if relation not in {"application", "confusable", "co_occurs", "next"}:
            continue
        other_id, _other_label = _other_topic_for_edge(edge, selected_id)
        other_topic = by_id.get(other_id)
        if not other_topic:
            continue
        item = dict(edge)
        item["depth"] = 1
        item["topic"] = other_topic
        direct_public_path_items.append(item)
    public_learning_path_source = [*learning_path, *direct_public_path_items]
    public_learning_path = _limit_public_learning_path(
        public_learning_path_source
    )
    diagnosis_questions = _build_diagnosis_questions(
        selected_id=selected_id,
        selected_label=_topic_label(selected_topic, selected_id),
        direct_edges=direct_diagnosis_edges,
    )
    if normalized_response_mode != "problem_solving":
        diagnosis_questions = []
    relation_groups = _build_relation_groups(
        learning_path=public_learning_path,
        applications=applications,
        confusions=confusions,
        next_practice=next_practice,
    )
    active_relation_group_count = sum(
        1 for group in relation_groups.values() if group["items"]
    )
    return {
        "topic": {
            "id": selected_id,
            "label": _topic_label(selected_topic, selected_id),
            "subject": _text(selected_topic.get("subject")),
            "stage": _text(selected_topic.get("stage")),
            "chapter": _text(selected_topic.get("chapter")),
            "unit": _text(selected_topic.get("unit")),
            "course_family": _text(selected_topic.get("course_family")),
            "aliases": _topic_aliases(selected_topic),
            "typical_misconceptions": list(
                selected_topic.get("typical_misconceptions") or []
            ),
        },
        "matches": matches,
        "learning_path": public_learning_path,
        "applications": applications,
        "confusions": confusions,
        "next_practice_topics": next_practice,
        "relation_groups": relation_groups,
        "guidance_sections": _build_guidance_sections(relation_groups),
        "diagnosis_questions": diagnosis_questions,
        "relevant_subgraph": relevant_subgraph,
        "model_context": model_context,
        "summary": {
            "matched": True,
            "topic_count": len(topic_items),
            "edge_count": len(edges),
            "learning_path_count": len(learning_path),
            "learning_path_total_count": len(public_learning_path_source),
            "learning_path_returned_count": len(public_learning_path),
            "learning_path_truncated": len(public_learning_path) < len(public_learning_path_source),
            "application_count": len(applications),
            "confusion_count": len(confusions),
            "next_practice_count": len(next_practice),
            "active_relation_group_count": active_relation_group_count,
            "diagnosis_question_count": len(diagnosis_questions),
            "subgraph_node_count": relevant_subgraph["summary"]["node_count"],
            "subgraph_edge_count": relevant_subgraph["summary"]["edge_count"],
            "raw_seed_included": False,
        },
    }
