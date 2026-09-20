"""Exercise the mounted HTTP adapter across real listener and store lifetimes."""

import http.client
import json

from knowledge_dungeon.private_bridge import KnowledgeDungeonPrivateBridge


def post(record, path, payload, token=None):
    connection = http.client.HTTPConnection("127.0.0.1", record.port, timeout=3)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        connection.request("POST", path, json.dumps(payload), headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def pair(record):
    status, response = post(record, "/v1/pair", {
        "protocol_version": record.protocol_version,
        "bridge_instance_id": record.bridge_instance_id,
        "generation": record.generation,
        "launch_code": record.launch_code,
        "client_id": "client-0123456789abcdef0123456789abcdef",
    })
    assert status == 200
    return response["access_token"]


def test_http_restart_reconnect_preserves_create_and_action_receipts(tmp_path):
    store_path = tmp_path / "dungeon.sqlite3"
    runtime_dir = tmp_path / "runtime"
    create = {
        "bridge_protocol_version": 1,
        "request_id": "restart-create",
        "subject_id": "math",
        "scenario_id": "calculus_v0_1",
    }
    first = KnowledgeDungeonPrivateBridge(store_path, runtime_dir=runtime_dir)
    record = first.start()
    try:
        old_token = pair(record)
        status, created = post(record, "/v1/runs/create", create, old_token)
        assert status == 200 and created["result"]["ok"]
        run = created["result"]["value"]
        action = {
            "bridge_protocol_version": 1,
            "run_id": run["run"]["run_id"],
            "request_id": "restart-action",
            "expected_state_version": run["state_version"],
            "action_id": "select_node:battle_1",
        }
        status, acted = post(record, "/v1/runs/action", action, old_token)
        assert status == 200 and acted["result"]["ok"]
    finally:
        assert first.stop()

    second = KnowledgeDungeonPrivateBridge(store_path, runtime_dir=runtime_dir)
    replacement = second.start()
    try:
        assert replacement.bridge_instance_id != record.bridge_instance_id
        status, _ = post(replacement, "/v1/bootstrap", {"bridge_protocol_version": 1}, old_token)
        assert status == 401
        token = pair(replacement)
        status, recreated = post(replacement, "/v1/runs/create", create, token)
        assert status == 200 and recreated["result"]["ok"]
        assert recreated["result"]["value"]["run"]["run_id"] == action["run_id"]
        assert recreated["result"]["value"]["state_version"] == acted["result"]["value"]["state_version"]
        assert post(replacement, "/v1/runs/action", action, token) == (200, acted)
        status, fetched = post(replacement, "/v1/runs/get", {
            "bridge_protocol_version": 1, "run_id": action["run_id"],
        }, token)
        assert status == 200
        assert fetched["result"]["value"]["state_hash"] == acted["result"]["value"]["state_hash"]
    finally:
        assert second.stop()


def test_http_unknown_version_and_provider_failure_leave_bridge_usable(tmp_path):
    calls = []

    def unavailable():
        calls.append(True)
        raise RuntimeError("private provider details must not escape")

    bridge = KnowledgeDungeonPrivateBridge(
        tmp_path / "dungeon.sqlite3", runtime_dir=tmp_path / "runtime",
        learning_snapshot_provider=unavailable,
    )
    record = bridge.start()
    try:
        token = pair(record)
        status, rejected = post(record, "/v2/game-sessions/begin", {
            "bridge_protocol_version": 999, "game_session_id": "session-one",
        }, token)
        assert status == 200 and not rejected["result"]["ok"]
        assert rejected["result"]["category"] == "protocol"
        assert not calls
        status, unavailable_result = post(record, "/v2/game-sessions/begin", {
            "bridge_protocol_version": 2, "game_session_id": "session-one",
        }, token)
        assert status == 200 and not unavailable_result["result"]["ok"]
        assert unavailable_result["result"]["error"]["code"] == "learning_unavailable"
        assert "private provider details" not in json.dumps(unavailable_result)
        assert len(calls) == 1
        status, bootstrap = post(record, "/v2/bootstrap", {"bridge_protocol_version": 2}, token)
        assert status == 200 and bootstrap["result"]["ok"]
    finally:
        assert bridge.stop()
