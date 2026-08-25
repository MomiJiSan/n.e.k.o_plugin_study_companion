from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

_audit_module = importlib.import_module("tools.audit_knowledge_seed")
audit_knowledge_seed = _audit_module.audit_knowledge_seed
audit_main = _audit_module.main
verify_runtime_relation_semantics = _audit_module.verify_runtime_relation_semantics
LIVE_PLUGIN_ROOT_ENV = _audit_module.LIVE_PLUGIN_ROOT_ENV

ROOT = Path(__file__).resolve().parents[1]


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


def test_audit_cli_reports_broken_workspace_runtime_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, live = tmp_path / "workspace", tmp_path / "live"
    _write_seed(workspace, [{"id": "topic", "label": "Topic"}])
    _write_seed(live, [{"id": "topic", "label": "Topic"}])
    (workspace / "knowledge_graph_edges.py").write_text("def build_topic_edges(topics):\n    return []\n", encoding="utf-8")

    def _raise_syntax_error(*_args, **_kwargs):
        raise SyntaxError("simulated broken runtime")

    monkeypatch.setattr(_audit_module.importlib, "import_module", _raise_syntax_error)

    assert audit_main(["--workspace-root", str(workspace), "--live-plugin-root", str(live)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "workspace_runtime_unavailable"


def test_audit_cli_requires_live_root_without_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LIVE_PLUGIN_ROOT_ENV, raising=False)

    with pytest.raises(SystemExit) as exc_info:
        audit_main(["--workspace-root", str(ROOT)])

    assert exc_info.value.code == 2


def test_live_seed_audit_does_not_modify_live_plugin_files() -> None:
    configured_root = os.environ.get(LIVE_PLUGIN_ROOT_ENV, "").strip()
    if not configured_root or not Path(configured_root).is_dir():
        pytest.skip(f"{LIVE_PLUGIN_ROOT_ENV} does not name a local live plugin checkout")
    manifest = Path(configured_root) / "static" / "knowledge_graph_seed.json"
    before = manifest.read_bytes()

    report = audit_knowledge_seed(ROOT, Path(configured_root))

    assert manifest.read_bytes() == before
    assert report["read_only"] is True
    assert report["merge_gate"]["automatic_copy_performed"] is False
