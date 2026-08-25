from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.audit_knowledge_seed import audit_knowledge_seed, verify_runtime_relation_semantics

ROOT = Path(__file__).resolve().parents[1]
LIVE_PLUGIN_ROOT = Path(r"C:\Users\ALEXGREENO\Desktop\CODE\N.E.K.O\plugin\plugins\study_companion")


def _write_seed(root: Path, topics: list[dict[str, object]]) -> None:
    static = root / "static"
    seeds = static / "knowledge_seeds"
    seeds.mkdir(parents=True)
    (static / "knowledge_graph_seed.json").write_text(
        json.dumps(
            {
                "version": "1.0",
                "type": "knowledge_seed_manifest",
                "files": [{"path": "knowledge_seeds/sample.json", "topic_count": len(topics)}],
            }
        ),
        encoding="utf-8",
    )
    (seeds / "sample.json").write_text(json.dumps({"topics": topics}), encoding="utf-8")


def test_audit_reports_topic_and_runtime_edge_differences(tmp_path: Path) -> None:
    workspace, live = tmp_path / "workspace", tmp_path / "live"
    _write_seed(
        workspace,
        [
            {"id": "common", "label": "Workspace title", "related": [{"id": "target", "relation": "supports"}]},
            {"id": "target", "label": "Target"},
            {"id": "workspace_only", "label": "Workspace only"},
        ],
    )
    _write_seed(
        live,
        [
            {"id": "common", "label": "Live title", "related": [{"id": "target", "relation": "next"}]},
            {"id": "target", "label": "Target"},
            {"id": "live_only", "label": "Live only"},
        ],
    )

    report = audit_knowledge_seed(workspace, live, runtime_root=ROOT)

    assert report["read_only"] is True
    assert report["topics"]["workspace_only_ids"] == ["workspace_only"]
    assert report["topics"]["live_only_ids"] == ["live_only"]
    assert report["topics"]["payload_changed"] == [{"topic_id": "common", "changed_fields": ["label"]}]
    assert report["relationships"]["workspace_only"] == [{"from": "common", "to": "target", "relation": "supports"}]
    assert report["relationships"]["live_only"] == [{"from": "common", "to": "target", "relation": "next"}]
    assert report["relationships"]["relation_type_counts"] == {
        "workspace": {"supports": 1},
        "live": {"next": 1},
    }
    assert report["merge_gate"]["safe_to_merge"] is False
    assert report["merge_gate"]["automatic_copy_performed"] is False


def test_workspace_runtime_keeps_sensitive_relation_names_distinct() -> None:
    semantics = verify_runtime_relation_semantics(ROOT)

    assert semantics["edge_builder"] == "knowledge_graph_edges.py"
    assert semantics["preserved"] is True
    assert (
        semantics["observed"]
        == semantics["expected"]
        == {
            "supports_target": "supports",
            "next_target": "next",
            "nearby_target": "nearby",
        }
    )


def test_live_seed_audit_does_not_modify_live_plugin_files() -> None:
    if not LIVE_PLUGIN_ROOT.is_dir():
        pytest.skip("local live plugin checkout is unavailable")
    manifest = LIVE_PLUGIN_ROOT / "static" / "knowledge_graph_seed.json"
    before = manifest.read_bytes()

    report = audit_knowledge_seed(ROOT, LIVE_PLUGIN_ROOT)

    assert manifest.read_bytes() == before
    assert report["read_only"] is True
    assert report["merge_gate"]["automatic_copy_performed"] is False
