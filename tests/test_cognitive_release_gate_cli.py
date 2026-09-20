from __future__ import annotations

import json
from pathlib import Path

from tools.cognitive_release_gate import main


def test_release_gate_cli_writes_snapshot_and_passes(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    evidence = tmp_path / "evidence.json"
    output = tmp_path / "gate.json"
    config.write_text(json.dumps({"projection_enabled": True, "strategy_shadow_enabled": True}), encoding="utf-8")
    evidence.write_text(json.dumps({"ordinary_answer_failures": 0, "duplicate_delivery": 0, "error_rate_bps": 0}), encoding="utf-8")

    assert main([
        "--profile", "B", "--config", str(config), "--evidence", str(evidence),
        "--report-hash", "report", "--plugin-version", "0.3.0",
        "--model-version", "cognitive-v2.1-1", "--database-schema-version", "7",
        "--output", str(output),
    ]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["allowed"] is True
    assert payload["snapshot"]["snapshot_hash"]


def test_release_gate_cli_returns_rollback_code(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    evidence = tmp_path / "evidence.json"
    output = tmp_path / "gate.json"
    config.write_text(json.dumps({"projection_enabled": True, "read_mode": "active", "intent_policy": "on", "ui_enabled": True, "retention_enabled": True}), encoding="utf-8")
    evidence.write_text(json.dumps({"ordinary_answer_failures": 0, "duplicate_delivery": 0, "error_rate_bps": 501}), encoding="utf-8")

    assert main([
        "--profile", "C", "--config", str(config), "--evidence", str(evidence),
        "--report-hash", "report", "--plugin-version", "0.3.0",
        "--model-version", "cognitive-v2.1-1", "--database-schema-version", "7",
        "--output", str(output),
    ]) == 10
    assert json.loads(output.read_text(encoding="utf-8"))["rollback_to"] == "A"


def test_release_gate_cli_rejects_missing_measured_evidence(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    evidence = tmp_path / "evidence.json"
    output = tmp_path / "gate.json"
    config.write_text(json.dumps({"projection_enabled": False, "read_mode": "off", "intent_policy": "off"}), encoding="utf-8")
    evidence.write_text(json.dumps({}), encoding="utf-8")
    assert main([
        "--profile", "A", "--config", str(config), "--evidence", str(evidence),
        "--report-hash", "report", "--plugin-version", "0.3.0",
        "--model-version", "cognitive-v2.1-1", "--database-schema-version", "7",
        "--output", str(output),
    ]) == 20
