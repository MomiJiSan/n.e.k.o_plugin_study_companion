from __future__ import annotations

import json
from pathlib import Path

from knowledge_dungeon.application_service import KnowledgeDungeonApplicationService
from knowledge_dungeon.bridge_contracts import TrustedInvocationContext

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "knowledge_dungeon" / "bridge_protocol_v1.json"


def test_protocol_compatibility_fixture_matches_authority_output() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    service = KnowledgeDungeonApplicationService()
    context = TrustedInvocationContext("electron-client-test", "study_companion:dungeon")

    assert fixture["fixture_version"] == 1
    assert service.bootstrap(context) == fixture["bootstrap"]
    create_request = fixture["requests"][0]
    created = service.create_run(
        context,
        {
            "bridge_protocol_version": create_request["bridge_protocol_version"],
            "request_id": create_request["request_id"],
            "subject_id": create_request["subject_id"],
            "scenario_id": create_request["scenario_id"],
        },
    )
    contract = fixture["create_run_contract"]

    assert created["run"]["run_id"] == contract["run_id"]
    assert created["state_version"] == contract["state_version"]
    assert created["state_hash"] == contract["state_hash"]
    assert [action["action_id"] for action in created["available_actions"]] == contract["available_action_ids"]
    assert [event["type"] for event in created["events"]] == contract["event_types"]
    assert fixture["requests"][1]["run_id"] == created["run"]["run_id"]
    assert fixture["requests"][2]["action_id"] == created["available_actions"][0]["action_id"]
