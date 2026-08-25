"""Read-only convergence audit for the bundled knowledge-graph seed.

The audit intentionally never copies, rewrites, or imports data from the live
plugin.  It is a gate for a later, separately approved data convergence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

LIVE_PLUGIN_ROOT_ENV = "STUDY_COMPANION_LIVE_PLUGIN_ROOT"


def _default_live_plugin_root() -> Path | None:
    value = os.environ.get(LIVE_PLUGIN_ROOT_ENV, "").strip()
    return Path(value) if value else None


def _seed_root(plugin_root: Path) -> Path:
    """Accept either a plugin root or its static seed directory."""

    candidate = plugin_root / "static"
    return candidate if (candidate / "knowledge_graph_seed.json").is_file() else plugin_root


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("knowledge_seed_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("knowledge_seed_invalid")
    return payload


def load_seed_topics(plugin_root: Path) -> dict[str, dict[str, Any]]:
    """Load all manifest-listed topics without writing to either source tree."""

    seed_root = _seed_root(Path(plugin_root))
    manifest = _read_json(seed_root / "knowledge_graph_seed.json")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("knowledge_seed_invalid")
    topics: dict[str, dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("knowledge_seed_invalid")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("knowledge_seed_invalid")
        payload = _read_json(seed_root / relative)
        raw_topics = payload.get("topics")
        if not isinstance(raw_topics, list):
            raise ValueError("knowledge_seed_invalid")
        for topic in raw_topics:
            if not isinstance(topic, dict) or not isinstance(topic.get("id"), str):
                raise ValueError("knowledge_seed_invalid")
            topic_id = topic["id"].strip()
            if not topic_id or topic_id in topics:
                raise ValueError("knowledge_seed_invalid")
            topics[topic_id] = topic
    return topics


def _runtime_edges(workspace_root: Path, topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build edges through the workspace runtime, not a parallel audit parser."""

    root = Path(workspace_root).resolve()
    module_file = root / "knowledge_graph_edges.py"
    if not module_file.is_file():
        raise ValueError("workspace_runtime_unavailable")
    package_name = f"_knowledge_seed_audit_{hashlib.sha256(str(root).encode()).hexdigest()[:12]}"
    package = ModuleType(package_name)
    package.__path__ = [str(root)]  # type: ignore[attr-defined]
    sys.modules.setdefault(package_name, package)
    try:
        module = importlib.import_module(f"{package_name}.knowledge_graph_edges")
    except (ImportError, SyntaxError) as exc:
        raise ValueError("workspace_runtime_unavailable") from exc
    if Path(module.__file__ or "").resolve() != module_file:
        raise ValueError("workspace_runtime_unavailable")
    edges = module.build_topic_edges(topics)
    return [dict(edge) for edge in edges if isinstance(edge, dict)]


def verify_runtime_relation_semantics(workspace_root: Path) -> dict[str, Any]:
    """Prove the runtime preserves the three legacy-sensitive relation names."""

    topics = [
        {
            "id": "source",
            "label": "Source",
            "related": [
                {"id": "supports_target", "relation": "supports"},
                {"id": "next_target", "relation": "next"},
                {"id": "nearby_target", "relation": "nearby"},
            ],
        },
        {"id": "supports_target", "label": "Supports"},
        {"id": "next_target", "label": "Next"},
        {"id": "nearby_target", "label": "Nearby"},
    ]
    observed = {str(edge.get("to")): str(edge.get("relation")) for edge in _runtime_edges(Path(workspace_root), topics)}
    expected = {
        "supports_target": "supports",
        "next_target": "next",
        "nearby_target": "nearby",
    }
    return {
        "edge_builder": "knowledge_graph_edges.py",
        "observed": observed,
        "expected": expected,
        "preserved": all(observed.get(key) == value for key, value in expected.items()),
    }


def _topic_payload(topic: dict[str, Any]) -> dict[str, Any]:
    """Separate node payload comparison from relationship comparison."""

    return {key: value for key, value in topic.items() if key not in {"prerequisites", "related"}}


def _changed_fields(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    return sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))


def _edge_map(edges: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in edges:
        key = (str(edge.get("from") or ""), str(edge.get("to") or ""), str(edge.get("relation") or ""))
        if all(key):
            result[key] = edge
    return result


def _edge_payload(edge: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in edge.items() if key not in {"from", "to", "relation"}}


def audit_knowledge_seed(
    workspace_root: Path,
    live_plugin_root: Path,
    *,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    """Return a deterministic, JSON-serializable read-only convergence report."""

    runtime_root = Path(runtime_root or workspace_root)
    workspace_topics = load_seed_topics(workspace_root)
    live_topics = load_seed_topics(live_plugin_root)
    workspace_ids, live_ids = set(workspace_topics), set(live_topics)
    common_ids = workspace_ids & live_ids
    payload_changes = [
        {
            "topic_id": topic_id,
            "changed_fields": _changed_fields(
                _topic_payload(workspace_topics[topic_id]), _topic_payload(live_topics[topic_id])
            ),
        }
        for topic_id in sorted(common_ids)
        if _canonical(_topic_payload(workspace_topics[topic_id])) != _canonical(_topic_payload(live_topics[topic_id]))
    ]

    workspace_edges = _edge_map(_runtime_edges(runtime_root, list(workspace_topics.values())))
    live_edges = _edge_map(_runtime_edges(runtime_root, list(live_topics.values())))
    workspace_keys, live_keys = set(workspace_edges), set(live_edges)
    common_edges = workspace_keys & live_keys
    edge_payload_changes = [
        {
            "from": key[0],
            "to": key[1],
            "relation": key[2],
            "changed_fields": _changed_fields(_edge_payload(workspace_edges[key]), _edge_payload(live_edges[key])),
        }
        for key in sorted(common_edges)
        if _canonical(_edge_payload(workspace_edges[key])) != _canonical(_edge_payload(live_edges[key]))
    ]

    workspace_only_edges = [
        {"from": key[0], "to": key[1], "relation": key[2]} for key in sorted(workspace_keys - live_keys)
    ]
    live_only_edges = [{"from": key[0], "to": key[1], "relation": key[2]} for key in sorted(live_keys - workspace_keys)]
    runtime = verify_runtime_relation_semantics(runtime_root)
    converged = not any(
        (
            workspace_ids - live_ids,
            live_ids - workspace_ids,
            payload_changes,
            workspace_only_edges,
            live_only_edges,
            edge_payload_changes,
        )
    )
    return {
        "audit_version": 1,
        "read_only": True,
        "topics": {
            "workspace_count": len(workspace_topics),
            "live_count": len(live_topics),
            "workspace_only_ids": sorted(workspace_ids - live_ids),
            "live_only_ids": sorted(live_ids - workspace_ids),
            "payload_changed": payload_changes,
        },
        "relationships": {
            "workspace_count": len(workspace_edges),
            "live_count": len(live_edges),
            "workspace_only": workspace_only_edges,
            "live_only": live_only_edges,
            "payload_changed": edge_payload_changes,
            "relation_type_counts": {
                "workspace": dict(sorted(Counter(key[2] for key in workspace_keys).items())),
                "live": dict(sorted(Counter(key[2] for key in live_keys).items())),
            },
        },
        "runtime_relation_semantics": runtime,
        "merge_gate": {
            "safe_to_merge": converged and bool(runtime["preserved"]),
            "reason": "converged" if converged and runtime["preserved"] else "seed_or_runtime_differences_detected",
            "automatic_copy_performed": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only knowledge seed convergence audit")
    parser.add_argument("--workspace-root", type=Path, default=Path(__file__).resolve().parents[1])
    default_live_plugin_root = _default_live_plugin_root()
    parser.add_argument(
        "--live-plugin-root",
        type=Path,
        default=default_live_plugin_root,
        required=default_live_plugin_root is None,
        help=f"live plugin root (or set {LIVE_PLUGIN_ROOT_ENV})",
    )
    args = parser.parse_args(argv)
    try:
        report = audit_knowledge_seed(args.workspace_root, args.live_plugin_root)
    except ValueError as exc:
        print(json.dumps({"audit_version": 1, "read_only": True, "error": str(exc)}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["merge_gate"]["safe_to_merge"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
