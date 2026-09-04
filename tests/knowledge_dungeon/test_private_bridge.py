from __future__ import annotations

import asyncio
import http.client
import json
import os
import re
import socket
import sqlite3
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest

# isort: split

from knowledge_dungeon import private_bridge as bridge_module
from knowledge_dungeon.private_bridge import (
    BridgeUnavailableError,
    KnowledgeDungeonPrivateBridge,
    RendezvousOwnershipError,
    RendezvousPublishError,
)

PAIR_FIELDS = {
    "protocol_version",
    "bridge_instance_id",
    "generation",
    "client_id",
    "launch_code",
}
RENDEZVOUS_FIELDS = {
    "protocol_version",
    "bridge_instance_id",
    "generation",
    "port",
    "launch_code",
    "expires_at",
    "owner_pid",
}
PAIR_SUCCESS_FIELDS = {
    "ok",
    "protocol_version",
    "bridge_instance_id",
    "generation",
    "client_id",
    "access_token",
    "access_token_expires_in",
    "session_idle_timeout",
    "session_absolute_timeout",
}


def _read_rendezvous(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _post(
    port: int,
    path: str,
    payload: object,
    *,
    token: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("POST", path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        decoded = json.loads(response.read().decode())
        assert isinstance(decoded, dict)
        return response.status, decoded
    finally:
        connection.close()


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def _raw_exchange(port: int, request: bytes, *, shutdown_write: bool = False) -> bytes:
    client = socket.create_connection(("127.0.0.1", port), timeout=3)
    client.settimeout(3)
    try:
        client.sendall(request)
        if shutdown_write:
            client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        client.close()


def _raw_status_and_json(response: bytes) -> tuple[int, dict[str, Any]]:
    head, body = response.split(b"\r\n\r\n", 1)
    status = int(head.split(b" ", 2)[1])
    decoded = json.loads(body)
    assert isinstance(decoded, dict)
    return status, decoded


def _pair_payload(
    record: dict[str, Any],
    client_id: str = "client-0123456789abcdef0123456789abcdef",
) -> dict[str, Any]:
    payload = {
        "protocol_version": record["protocol_version"],
        "bridge_instance_id": record["bridge_instance_id"],
        "generation": record["generation"],
        "client_id": client_id,
        "launch_code": record["launch_code"],
    }
    assert set(payload) == PAIR_FIELDS
    return payload


@pytest.fixture
def running_bridge(tmp_path: Path):
    bridge = KnowledgeDungeonPrivateBridge(
        tmp_path / "data" / "knowledge_dungeon.sqlite3",
        runtime_dir=tmp_path / "runtime",
    )
    bridge.start()
    try:
        yield bridge
    finally:
        bridge.stop()


def test_rendezvous_schema_pair_envelope_and_domain_envelope(running_bridge) -> None:
    bridge = running_bridge
    initial = _read_rendezvous(bridge.rendezvous_path)

    assert set(initial) == RENDEZVOUS_FIELDS
    assert initial["protocol_version"] == 1
    assert re.fullmatch(r"bridge-[0-9a-f]{32}", initial["bridge_instance_id"])
    assert initial["generation"] == 1
    assert 1 <= initial["port"] <= 65535
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", initial["launch_code"])
    assert isinstance(initial["expires_at"], int)
    assert initial["owner_pid"] == os.getpid()

    status, paired = _post(initial["port"], "/v1/pair", _pair_payload(initial))
    assert status == 200
    assert set(paired) == PAIR_SUCCESS_FIELDS
    assert paired == {
        "ok": True,
        "protocol_version": 1,
        "bridge_instance_id": initial["bridge_instance_id"],
        "generation": 1,
        "client_id": "client-0123456789abcdef0123456789abcdef",
        "access_token": paired["access_token"],
        "access_token_expires_in": 1800,
        "session_idle_timeout": 1800,
        "session_absolute_timeout": 43200,
    }
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", paired["access_token"])

    rotated = _read_rendezvous(bridge.rendezvous_path)
    assert rotated["bridge_instance_id"] == initial["bridge_instance_id"]
    assert rotated["generation"] == 2
    assert rotated["launch_code"] != initial["launch_code"]

    status, bootstrap = _post(
        initial["port"],
        "/v1/bootstrap",
        {"bridge_protocol_version": 1},
        token=paired["access_token"],
    )
    assert status == 200
    assert set(bootstrap) == {"ok", "protocol_version", "result"}
    assert bootstrap["ok"] is True
    assert bootstrap["protocol_version"] == 1
    assert bootstrap["result"]["category"] == "success"
    assert bootstrap["result"]["value"]["bridge_protocol_version"] == 1


def test_cross_repository_fixture_matches_frozen_contract() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "knowledge_dungeon"
        / "private_bridge_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert set(fixture) == {
        "rendezvous",
        "pair_request",
        "pair_success",
        "domain_requests",
        "domain_success",
    }
    assert set(fixture["rendezvous"]) == RENDEZVOUS_FIELDS
    assert set(fixture["pair_request"]) == PAIR_FIELDS
    assert set(fixture["pair_success"]) == PAIR_SUCCESS_FIELDS
    assert fixture["pair_success"]["access_token_expires_in"] == 1800
    assert fixture["pair_success"]["session_idle_timeout"] == 1800
    assert fixture["pair_success"]["session_absolute_timeout"] == 43200
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", fixture["pair_success"]["access_token"])
    assert {
        name: request["path"] for name, request in fixture["domain_requests"].items()
    } == {
        "bootstrap": "/v1/bootstrap",
        "create_run": "/v1/runs/create",
        "get_run": "/v1/runs/get",
        "perform_action": "/v1/runs/action",
    }


def test_all_five_post_endpoints_are_domain_specific(running_bridge) -> None:
    bridge = running_bridge
    initial = _read_rendezvous(bridge.rendezvous_path)
    _, paired = _post(initial["port"], "/v1/pair", _pair_payload(initial))
    token = paired["access_token"]

    status, created = _post(
        initial["port"],
        "/v1/runs/create",
        {
            "bridge_protocol_version": 1,
            "request_id": "create-run-bridge-001",
            "subject_id": "math",
            "scenario_id": "calculus_v0_1",
        },
        token=token,
    )
    assert status == 200
    assert created["result"]["ok"] is True
    run = created["result"]["value"]["run"]

    status, fetched = _post(
        initial["port"],
        "/v1/runs/get",
        {"bridge_protocol_version": 1, "run_id": run["run_id"]},
        token=token,
    )
    assert status == 200
    assert fetched["result"]["value"]["state_hash"] == created["result"]["value"]["state_hash"]

    status, acted = _post(
        initial["port"],
        "/v1/runs/action",
        {
            "bridge_protocol_version": 1,
            "run_id": run["run_id"],
            "request_id": "select-battle-bridge-001",
            "expected_state_version": created["result"]["value"]["state_version"],
            "action_id": "select_node:battle_1",
        },
        token=token,
    )
    assert status == 200
    assert acted["result"]["ok"] is True

    status, error = _post(
        initial["port"],
        "/v1/dispatch",
        {"app_id": "knowledge_dungeon", "scope": "study_companion:dungeon"},
        token=token,
    )
    assert status == 404
    assert error == {
        "ok": False,
        "error": {"code": "endpoint_not_found", "message": "endpoint was not found"},
    }


def test_transport_rejects_browser_origin_wrong_host_and_unexpected_fields(running_bridge) -> None:
    record = _read_rendezvous(running_bridge.rendezvous_path)

    status, error = _post(
        record["port"],
        "/v1/pair",
        _pair_payload(record),
        headers={"Origin": "http://127.0.0.1:9999"},
    )
    assert status == 403
    assert error["error"]["code"] == "browser_origin_rejected"

    status, error = _post(
        record["port"],
        "/v1/pair",
        _pair_payload(record),
        headers={"Host": "localhost:9999"},
    )
    assert status == 403
    assert error["error"]["code"] == "invalid_host"

    invalid = {**_pair_payload(record), "app_id": "knowledge_dungeon"}
    status, error = _post(record["port"], "/v1/pair", invalid)
    assert status == 400
    assert error == {
        "ok": False,
        "error": {"code": "invalid_request", "message": "pair request fields are invalid"},
    }


@pytest.mark.parametrize(
    "body",
    [
        b'{"protocol_version":1,"protocol_version":1}',
        b'{"protocol_version":NaN}',
        b'{"protocol_version":Infinity}',
        b'{"protocol_version":-Infinity}',
    ],
)
def test_strict_json_rejects_duplicate_keys_and_non_finite_constants(
    running_bridge,
    body: bytes,
) -> None:
    record = _read_rendezvous(running_bridge.rendezvous_path)
    status, response_body, _ = _request(
        record["port"],
        "POST",
        "/v1/pair",
        body=body,
        headers={"Content-Type": "application/json"},
    )
    assert status == 400
    assert json.loads(response_body)["error"]["code"] == "invalid_json"


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "OPTIONS", "PATCH"])
def test_non_post_methods_return_consistent_json_405(running_bridge, method: str) -> None:
    record = _read_rendezvous(running_bridge.rendezvous_path)
    status, response_body, headers = _request(record["port"], method, "/v1/pair")
    assert status == 405
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(response_body) == {
        "ok": False,
        "error": {"code": "method_not_allowed", "message": "POST is required"},
    }


def test_head_returns_json_metadata_without_a_forbidden_body(running_bridge) -> None:
    record = _read_rendezvous(running_bridge.rendezvous_path)
    status, response_body, headers = _request(record["port"], "HEAD", "/v1/pair")
    assert status == 405
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert int(headers["Content-Length"]) > 0
    assert response_body == b""


def test_content_type_length_header_and_body_limits(running_bridge) -> None:
    record = _read_rendezvous(running_bridge.rendezvous_path)
    status, error = _post(
        record["port"],
        "/v1/pair",
        _pair_payload(record),
        headers={"Content-Type": "text/plain"},
    )
    assert status == 415
    assert error["error"]["code"] == "invalid_content_type"

    missing_length = _raw_exchange(
        record["port"],
        (
            f"POST /v1/pair HTTP/1.1\r\nHost: 127.0.0.1:{record['port']}\r\n"
            "Content-Type: application/json\r\nConnection: close\r\n\r\n"
        ).encode(),
        shutdown_write=True,
    )
    status, error = _raw_status_and_json(missing_length)
    assert status == 411
    assert error["error"]["code"] == "content_length_required"

    oversized_body = _raw_exchange(
        record["port"],
        (
            f"POST /v1/pair HTTP/1.1\r\nHost: 127.0.0.1:{record['port']}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {bridge_module.MAX_BODY_BYTES + 1}\r\n"
            "Connection: close\r\n\r\n"
        ).encode(),
    )
    status, error = _raw_status_and_json(oversized_body)
    assert status == 413
    assert error["error"]["code"] == "body_too_large"

    oversized_header = _raw_exchange(
        record["port"],
        (
            f"POST /v1/pair HTTP/1.1\r\nHost: 127.0.0.1:{record['port']}\r\n"
            f"X-Oversized: {'a' * bridge_module.MAX_HEADER_BYTES}\r\n"
            "Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
        ).encode(),
    )
    status, error = _raw_status_and_json(oversized_header)
    assert status == 431
    assert error["error"]["code"] == "headers_too_large"


@pytest.mark.parametrize(
    ("extra_header", "expected_code"),
    [
        ("Host: 127.0.0.1:{port}\r\n", "ambiguous_header"),
        ("Content-Type: application/json\r\n", "ambiguous_header"),
        ("Content-Length: {length}\r\n", "ambiguous_header"),
        ("Transfer-Encoding: chunked\r\n", "transfer_encoding_rejected"),
    ],
)
def test_transport_rejects_duplicate_framing_headers_and_transfer_encoding(
    running_bridge,
    extra_header: str,
    expected_code: str,
) -> None:
    record = _read_rendezvous(running_bridge.rendezvous_path)
    body = json.dumps(_pair_payload(record), separators=(",", ":")).encode()
    request = (
        f"POST /v1/pair HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{record['port']}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        + extra_header.format(port=record["port"], length=len(body))
        + "Connection: close\r\n\r\n"
    ).encode() + body

    status, error = _raw_status_and_json(_raw_exchange(record["port"], request))

    assert status == 400
    assert error["error"]["code"] == expected_code


def test_transport_rejects_authorization_on_pair_and_duplicate_domain_authorization(
    running_bridge,
) -> None:
    record = _read_rendezvous(running_bridge.rendezvous_path)
    pair_body = json.dumps(_pair_payload(record), separators=(",", ":")).encode()
    pair_request = (
        f"POST /v1/pair HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{record['port']}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(pair_body)}\r\n"
        "Authorization: Bearer ignored\r\n"
        "Authorization: Bearer also-ignored\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + pair_body
    status, error = _raw_status_and_json(
        _raw_exchange(record["port"], pair_request)
    )
    assert status == 400
    assert error["error"]["code"] == "unexpected_authorization"

    _, paired = _post(record["port"], "/v1/pair", _pair_payload(record))
    domain_body = b'{"bridge_protocol_version":1}'
    domain_request = (
        f"POST /v1/bootstrap HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{record['port']}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(domain_body)}\r\n"
        f"Authorization: Bearer {paired['access_token']}\r\n"
        f"Authorization: Bearer {paired['access_token']}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + domain_body
    status, error = _raw_status_and_json(
        _raw_exchange(record["port"], domain_request)
    )
    assert status == 400
    assert error["error"]["code"] == "ambiguous_header"


def test_incomplete_and_slow_body_fail_with_bounded_read(running_bridge, monkeypatch) -> None:
    record = _read_rendezvous(running_bridge.rendezvous_path)
    incomplete = _raw_exchange(
        record["port"],
        (
            f"POST /v1/pair HTTP/1.1\r\nHost: 127.0.0.1:{record['port']}\r\n"
            "Content-Type: application/json\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{}"
        ).encode(),
        shutdown_write=True,
    )
    status, error = _raw_status_and_json(incomplete)
    assert status == 400
    assert error["error"]["code"] == "incomplete_body"

    monkeypatch.setattr(bridge_module, "REQUEST_TIMEOUT", 0.1)
    slow = _raw_exchange(
        record["port"],
        (
            f"POST /v1/pair HTTP/1.1\r\nHost: 127.0.0.1:{record['port']}\r\n"
            "Content-Type: application/json\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{}"
        ).encode(),
    )
    status, error = _raw_status_and_json(slow)
    assert status == 408
    assert error["error"]["code"] == "request_timeout"


def test_new_pair_revokes_every_previous_session(running_bridge) -> None:
    bridge = running_bridge
    first_record = _read_rendezvous(bridge.rendezvous_path)
    _, first = _post(first_record["port"], "/v1/pair", _pair_payload(first_record))
    second_record = _read_rendezvous(bridge.rendezvous_path)
    _, second = _post(
        second_record["port"],
        "/v1/pair",
        _pair_payload(second_record, "client-fedcba9876543210fedcba9876543210"),
    )

    status, error = _post(
        first_record["port"],
        "/v1/bootstrap",
        {"bridge_protocol_version": 1},
        token=first["access_token"],
    )
    assert status == 401
    assert error["error"]["code"] == "invalid_access_token"

    status, bootstrap = _post(
        first_record["port"],
        "/v1/bootstrap",
        {"bridge_protocol_version": 1},
        token=second["access_token"],
    )
    assert status == 200
    assert bootstrap["result"]["ok"] is True


def test_same_one_time_code_allows_only_one_concurrent_pair(running_bridge) -> None:
    record = _read_rendezvous(running_bridge.rendezvous_path)
    barrier = threading.Barrier(3)
    responses: list[tuple[int, dict[str, Any]]] = []

    def pair(client_id: str) -> None:
        barrier.wait()
        responses.append(
            _post(record["port"], "/v1/pair", _pair_payload(record, client_id))
        )

    threads = [
        threading.Thread(target=pair, args=(f"client-{index:032x}",))
        for index in (1, 2)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()

    assert sorted(status for status, _ in responses) == [200, 401]
    assert sum(payload.get("ok") is True for _, payload in responses) == 1


def test_pair_attempt_limit_is_bounded(running_bridge) -> None:
    record = _read_rendezvous(running_bridge.rendezvous_path)
    invalid = {**_pair_payload(record), "launch_code": "A" * 43}
    for _ in range(bridge_module.MAX_PAIR_FAILURES):
        status, error = _post(record["port"], "/v1/pair", invalid)
        assert status == 401
        assert error["error"]["code"] == "invalid_launch_code"
    status, error = _post(record["port"], "/v1/pair", invalid)
    assert status == 429
    assert error["error"]["code"] == "pairing_rate_limited"


def test_client_cannot_read_another_clients_run_over_http(running_bridge) -> None:
    record_a = _read_rendezvous(running_bridge.rendezvous_path)
    _, paired_a = _post(record_a["port"], "/v1/pair", _pair_payload(record_a))
    _, created = _post(
        record_a["port"],
        "/v1/runs/create",
        {
            "bridge_protocol_version": 1,
            "request_id": "create-private-run-001",
            "subject_id": "math",
            "scenario_id": "calculus_v0_1",
        },
        token=paired_a["access_token"],
    )
    run_id = created["result"]["value"]["run"]["run_id"]
    record_b = _read_rendezvous(running_bridge.rendezvous_path)
    _, paired_b = _post(
        record_b["port"],
        "/v1/pair",
        _pair_payload(record_b, "client-fedcba9876543210fedcba9876543210"),
    )
    status, fetched = _post(
        record_b["port"],
        "/v1/runs/get",
        {"bridge_protocol_version": 1, "run_id": run_id},
        token=paired_b["access_token"],
    )
    assert status == 200
    assert fetched["result"]["ok"] is False
    assert fetched["result"]["category"] == "domain"
    assert fetched["result"]["error"]["code"] == "run_not_found"


def test_pair_publish_failure_keeps_old_code_retryable(running_bridge, monkeypatch) -> None:
    bridge = running_bridge
    initial = _read_rendezvous(bridge.rendezvous_path)
    original_publish = bridge._publisher.publish  # noqa: SLF001 - verifies commit boundary
    calls = 0

    def fail_once(record) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RendezvousPublishError("injected publish failure")
        original_publish(record)

    monkeypatch.setattr(bridge._publisher, "publish", fail_once)  # noqa: SLF001
    status, error = _post(initial["port"], "/v1/pair", _pair_payload(initial))
    assert status == 503
    assert error["error"]["code"] == "rendezvous_unavailable"
    assert _read_rendezvous(bridge.rendezvous_path) == initial

    status, paired = _post(initial["port"], "/v1/pair", _pair_payload(initial))
    assert status == 200
    assert paired["generation"] == initial["generation"]


def test_expired_code_rotates_before_rejection(running_bridge) -> None:
    bridge = running_bridge
    expired = _read_rendezvous(bridge.rendezvous_path)
    bridge._state._clock = lambda: expired["expires_at"] + 1  # noqa: SLF001

    status, error = _post(expired["port"], "/v1/pair", _pair_payload(expired))
    assert status == 401
    assert error["error"]["code"] == "launch_code_expired"
    replacement = _read_rendezvous(bridge.rendezvous_path)
    assert replacement["generation"] == expired["generation"] + 1
    assert replacement["launch_code"] != expired["launch_code"]


def test_active_session_uses_idle_and_absolute_expiration(running_bridge) -> None:
    bridge = running_bridge
    state = bridge._state  # noqa: SLF001 - controls the session clock
    assert state is not None
    fake_now = [100.0]
    state._monotonic = lambda: fake_now[0]  # noqa: SLF001
    record = _read_rendezvous(bridge.rendezvous_path)
    _, paired = _post(record["port"], "/v1/pair", _pair_payload(record))

    fake_now[0] += bridge_module.ACCESS_TOKEN_EXPIRES_IN - 1
    status, first = _post(
        record["port"],
        "/v1/bootstrap",
        {"bridge_protocol_version": 1},
        token=paired["access_token"],
    )
    assert status == 200
    assert first["ok"] is True

    fake_now[0] += 2
    status, active = _post(
        record["port"],
        "/v1/bootstrap",
        {"bridge_protocol_version": 1},
        token=paired["access_token"],
    )
    assert status == 200
    assert active["ok"] is True

    fake_now[0] = 100.0 + bridge_module.SESSION_ABSOLUTE_TIMEOUT
    status, expired = _post(
        record["port"],
        "/v1/bootstrap",
        {"bridge_protocol_version": 1},
        token=paired["access_token"],
    )
    assert status == 401
    assert expired["error"]["code"] == "access_token_expired"


def test_background_renewal_rotates_before_expiry_without_revoking_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "LAUNCH_CODE_RENEWAL_INTERVAL", 0.01)
    bridge = KnowledgeDungeonPrivateBridge(
        tmp_path / "dungeon.sqlite3",
        runtime_dir=tmp_path / "runtime",
    )
    first = bridge.start().to_dict()
    state = bridge._state  # noqa: SLF001 - controls the renewal clock
    assert state is not None
    fake_now = [first["expires_at"] - bridge_module.LAUNCH_CODE_RENEWAL_MARGIN]
    state._clock = lambda: fake_now[0]  # noqa: SLF001
    try:
        deadline = time.monotonic() + 2
        while _read_rendezvous(bridge.rendezvous_path)["generation"] == first["generation"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        second = _read_rendezvous(bridge.rendezvous_path)
        assert second["generation"] == first["generation"] + 1

        _, paired = _post(second["port"], "/v1/pair", _pair_payload(second))
        third = _read_rendezvous(bridge.rendezvous_path)
        fake_now[0] = third["expires_at"] - bridge_module.LAUNCH_CODE_RENEWAL_MARGIN
        deadline = time.monotonic() + 2
        while _read_rendezvous(bridge.rendezvous_path)["generation"] == third["generation"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        status, result = _post(
            third["port"],
            "/v1/bootstrap",
            {"bridge_protocol_version": 1},
            token=paired["access_token"],
        )
        assert status == 200
        assert result["ok"] is True
    finally:
        renewal_thread = bridge._renewal_thread  # noqa: SLF001
        assert bridge.stop()
        assert renewal_thread is not None and not renewal_thread.is_alive()


@pytest.mark.parametrize("replacement", ["corrupt", "different_generation"])
def test_stop_preserves_replaced_or_corrupt_rendezvous(
    tmp_path: Path,
    replacement: str,
) -> None:
    bridge = KnowledgeDungeonPrivateBridge(
        tmp_path / "dungeon.sqlite3",
        runtime_dir=tmp_path / "runtime",
    )
    record = bridge.start().to_dict()
    if replacement == "corrupt":
        expected = "{not-json\n"
    else:
        record["generation"] += 1
        expected = json.dumps(record, separators=(",", ":")) + "\n"
    bridge.rendezvous_path.write_text(expected, encoding="utf-8")

    assert bridge.stop()
    assert bridge.rendezvous_path.read_text(encoding="utf-8") == expected


def test_storage_preflight_rejects_bad_schema_without_listener_or_rendezvous(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "dungeon.sqlite3"
    with sqlite3.connect(store_path) as connection:
        connection.execute("PRAGMA user_version = 999")
    bridge = KnowledgeDungeonPrivateBridge(
        store_path,
        runtime_dir=tmp_path / "runtime",
    )

    with pytest.raises(BridgeUnavailableError, match="storage preflight"):
        bridge.start()

    assert bridge._server is None  # noqa: SLF001
    assert bridge._thread is None  # noqa: SLF001
    assert not bridge.rendezvous_path.exists()


def test_storage_preflight_rejects_unopenable_path_without_publishing(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "is-a-directory"
    store_path.mkdir()
    bridge = KnowledgeDungeonPrivateBridge(
        store_path,
        runtime_dir=tmp_path / "runtime",
    )

    with pytest.raises(BridgeUnavailableError, match="storage preflight"):
        bridge.start()

    assert bridge._server is None  # noqa: SLF001
    assert bridge._thread is None  # noqa: SLF001
    assert not bridge.rendezvous_path.exists()


@pytest.mark.parametrize(
    "failed_thread_name",
    [
        "study-companion-knowledge-dungeon-bridge",
        "study-companion-knowledge-dungeon-rendezvous-renewal",
    ],
)
def test_thread_start_failure_releases_listener_rendezvous_and_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_thread_name: str,
) -> None:
    runtime_dir = tmp_path / "runtime"
    ports: list[int] = []
    original_server_init = bridge_module._PrivateBridgeHttpServer.__init__  # noqa: SLF001
    original_thread_start = threading.Thread.start
    failure_injected = False

    def capture_server_port(server, *args, **kwargs) -> None:
        original_server_init(server, *args, **kwargs)
        ports.append(int(server.server_address[1]))

    def fail_one_start(thread: threading.Thread) -> None:
        nonlocal failure_injected
        if thread.name == failed_thread_name and not failure_injected:
            failure_injected = True
            raise RuntimeError(f"injected {failed_thread_name} start failure")
        original_thread_start(thread)

    monkeypatch.setattr(
        bridge_module._PrivateBridgeHttpServer,  # noqa: SLF001
        "__init__",
        capture_server_port,
    )
    monkeypatch.setattr(threading.Thread, "start", fail_one_start)
    bridge = KnowledgeDungeonPrivateBridge(
        tmp_path / "failed.sqlite3",
        runtime_dir=runtime_dir,
    )

    with pytest.raises(RuntimeError, match="injected"):
        bridge.start()

    assert failure_injected is True
    assert len(ports) == 1
    assert not bridge.running
    assert bridge._server is None  # noqa: SLF001
    assert bridge._state is None  # noqa: SLF001
    assert not bridge.rendezvous_path.exists()
    port_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        port_probe.bind(("127.0.0.1", ports[0]))
    finally:
        port_probe.close()

    # The injected failure is one-shot. A second bridge acquiring the same
    # runtime proves the first ownership lock was released as well.
    successor = KnowledgeDungeonPrivateBridge(
        tmp_path / "successor.sqlite3",
        runtime_dir=runtime_dir,
    )
    successor.start()
    try:
        assert successor.running
        assert successor.rendezvous_path.exists()
    finally:
        successor.stop()


def test_runtime_lock_competition_and_stale_stop_cannot_delete_new_file(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    first = KnowledgeDungeonPrivateBridge(tmp_path / "first.sqlite3", runtime_dir=runtime_dir)
    competitor = KnowledgeDungeonPrivateBridge(tmp_path / "second.sqlite3", runtime_dir=runtime_dir)
    first.start()
    first_instance = first.record.bridge_instance_id
    with pytest.raises(RendezvousOwnershipError):
        competitor.start()

    assert first.stop()
    competitor.start()
    try:
        new_record = _read_rendezvous(competitor.rendezvous_path)
        assert new_record["bridge_instance_id"] != first_instance
        # The stopped object no longer owns the OS lock, so a repeated stale
        # cleanup must not unlink the new owner's rendezvous.
        assert first.stop()
        assert _read_rendezvous(competitor.rendezvous_path) == new_record
    finally:
        competitor.stop()


def test_stop_removes_rendezvous_revokes_sessions_and_closes_listener(tmp_path: Path) -> None:
    bridge = KnowledgeDungeonPrivateBridge(tmp_path / "dungeon.sqlite3", runtime_dir=tmp_path / "runtime")
    record = bridge.start().to_dict()
    _, paired = _post(record["port"], "/v1/pair", _pair_payload(record))
    assert paired["access_token"]

    listener = bridge._thread  # noqa: SLF001
    server = bridge._server  # noqa: SLF001
    assert listener is not None and server is not None
    assert bridge.stop()
    assert not bridge.rendezvous_path.exists()
    assert not listener.is_alive()
    assert server.active_worker_count == 0
    assert not any(
        thread.name == "study-companion-knowledge-dungeon-bridge" and thread.is_alive()
        for thread in threading.enumerate()
    )
    with pytest.raises((ConnectionRefusedError, TimeoutError, socket.timeout, OSError)):
        _post(
            record["port"],
            "/v1/bootstrap",
            {"bridge_protocol_version": 1},
            token=paired["access_token"],
        )


def test_runtime_hardener_is_injectable_and_failure_publishes_nothing(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    calls: list[Path] = []

    def reject(path: str | Path) -> None:
        calls.append(Path(path))
        raise BridgeUnavailableError("injected ACL failure")

    bridge = KnowledgeDungeonPrivateBridge(
        tmp_path / "dungeon.sqlite3",
        runtime_dir=runtime_dir,
        runtime_hardener=reject,
    )
    with pytest.raises(BridgeUnavailableError, match="injected ACL failure"):
        bridge.start()
    assert calls == [runtime_dir]
    assert not bridge.running
    assert not bridge.rendezvous_path.exists()


def test_platform_runtime_hardening_succeeds_or_fails_closed(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    bridge_module.harden_runtime_directory(runtime_dir)
    assert runtime_dir.is_dir()
    if os.name != "nt":
        assert runtime_dir.stat().st_mode & 0o777 == 0o700


def test_late_pair_cannot_publish_or_restore_session_after_stop_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = KnowledgeDungeonPrivateBridge(
        tmp_path / "dungeon.sqlite3",
        runtime_dir=tmp_path / "runtime",
    )
    initial = bridge.start().to_dict()
    state = bridge._state  # noqa: SLF001
    assert state is not None
    original_publish = bridge._publisher.publish  # noqa: SLF001
    publish_entered = threading.Event()
    release_publish = threading.Event()

    def blocked_publish(record) -> None:
        if record.generation == initial["generation"] + 1:
            publish_entered.set()
            assert release_publish.wait(2)
        original_publish(record)

    monkeypatch.setattr(bridge._publisher, "publish", blocked_publish)  # noqa: SLF001
    response: list[tuple[int, dict[str, Any]]] = []
    pair_thread = threading.Thread(
        target=lambda: response.append(
            _post(initial["port"], "/v1/pair", _pair_payload(initial))
        )
    )
    pair_thread.start()
    assert publish_entered.wait(2)

    assert bridge.stop(timeout=0.05) is False
    assert not bridge.rendezvous_path.exists()
    release_publish.set()
    pair_thread.join(2)
    assert not pair_thread.is_alive()
    assert response[0][0] == 503
    assert state._sessions == {}  # noqa: SLF001
    assert state._client_tokens == {}  # noqa: SLF001
    assert not bridge.rendezvous_path.exists()


def test_stop_timeout_revokes_existing_session_while_domain_request_is_blocked(
    tmp_path: Path,
) -> None:
    bridge = KnowledgeDungeonPrivateBridge(
        tmp_path / "dungeon.sqlite3",
        runtime_dir=tmp_path / "runtime",
    )
    initial = bridge.start().to_dict()
    _, paired = _post(initial["port"], "/v1/pair", _pair_payload(initial))
    state = bridge._state  # noqa: SLF001
    assert state is not None
    entered = threading.Event()
    release = threading.Event()

    class BlockingOutcome:
        def to_dict(self) -> dict[str, object]:
            return {"ok": True}

    class BlockingAdapter:
        async def invoke(self, *_args, **_kwargs):
            entered.set()
            await asyncio.to_thread(release.wait)
            return BlockingOutcome()

    state.adapter = BlockingAdapter()
    response: list[tuple[int, dict[str, Any]]] = []
    request_thread = threading.Thread(
        target=lambda: response.append(
            _post(
                initial["port"],
                "/v1/bootstrap",
                {"bridge_protocol_version": 1},
                token=paired["access_token"],
            )
        )
    )
    request_thread.start()
    assert entered.wait(2)
    assert bridge.stop(timeout=0.05) is False
    assert state._sessions == {}  # noqa: SLF001
    assert state._client_tokens == {}  # noqa: SLF001
    release.set()
    request_thread.join(2)
    assert not request_thread.is_alive()


def test_stop_preserves_renewal_join_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = KnowledgeDungeonPrivateBridge(
        tmp_path / "dungeon.sqlite3",
        runtime_dir=tmp_path / "runtime",
    )
    bridge.start()
    renewal_thread = bridge._renewal_thread  # noqa: SLF001
    original_join = bridge._join_if_started  # noqa: SLF001
    assert renewal_thread is not None

    def report_renewal_failure(thread: threading.Thread, timeout: float) -> bool:
        joined = original_join(thread, timeout)
        return False if thread is renewal_thread else joined

    monkeypatch.setattr(bridge, "_join_if_started", report_renewal_failure)

    assert bridge.stop() is False
    assert not renewal_thread.is_alive()


def test_concurrency_limit_rejects_excess_request_without_blocking(running_bridge) -> None:
    bridge = running_bridge
    record = _read_rendezvous(bridge.rendezvous_path)
    sockets: list[socket.socket] = []
    try:
        for _ in range(bridge_module.MAX_CONCURRENT_REQUESTS):
            client = socket.create_connection(("127.0.0.1", record["port"]), timeout=2)
            client.sendall(b"POST /v1/pair HTTP/1.1\r\n")
            sockets.append(client)
        deadline = time.monotonic() + 2
        server = bridge._server  # noqa: SLF001
        assert server is not None
        while server.active_worker_count != bridge_module.MAX_CONCURRENT_REQUESTS:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        overloaded = socket.create_connection(("127.0.0.1", record["port"]), timeout=2)
        overloaded.settimeout(2)
        try:
            response = overloaded.recv(65536)
        finally:
            overloaded.close()
        status, error = _raw_status_and_json(response)
        assert status == 503
        assert error["error"]["code"] == "bridge_busy"
    finally:
        for client in sockets:
            client.close()


def test_manifest_uses_private_bridge_switch_without_core_local_app_metadata() -> None:
    root = Path(__file__).resolve().parents[2]
    with (root / "plugin.toml").open("rb") as stream:
        manifest = tomllib.load(stream)
    assert manifest["knowledge_dungeon"] == {"bridge_enabled": True}
    assert "local_app" not in manifest["plugin"]
    source = (root / "__init__.py").read_text(encoding="utf-8")
    assert "plugin.sdk.local_app" not in source
    assert "trusted_local_app_operation" not in source
