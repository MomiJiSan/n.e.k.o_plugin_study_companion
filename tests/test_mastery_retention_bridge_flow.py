"""Learning commits through the real tracker/provider and authenticated HTTP bridge."""
from __future__ import annotations

import http.client
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from knowledge_dungeon.private_bridge import KnowledgeDungeonPrivateBridge


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = exception = debug


def _post(port, path, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("POST", path, json.dumps(payload), headers)
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200, body
        return body
    finally:
        connection.close()


def test_learning_commit_session_freeze_expiry_and_reacquisition(monkeypatch, tmp_path):
    package_name = "_retention_bridge_integration"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode = ModuleType(package_name + ".mode_manager")
    mode.normalize_mode = lambda value: str(value or "companion")
    monkeypatch.setitem(sys.modules, mode.__name__, mode)
    Store = importlib.import_module(package_name + ".store").StudyStore
    Tracker = importlib.import_module(package_name + ".knowledge_tracker").KnowledgeTracker
    retention = importlib.import_module(package_name + ".store_mastery_retention")
    now = [1_800_000_000.0]
    monkeypatch.setattr(retention, "utc_timestamp", lambda: now[0])
    store = Store(tmp_path / "study.sqlite3", tmp_path / "seed.json", _Logger())
    store.open()
    bridge = None
    try:
        store.ensure_topic(topic_id="biology-cell", name="细胞", subject="biology")
        tracker = Tracker(store, logger=_Logger())

        def learn(attempt):
            result = store.batch_write_answer_data(
                session_id="study-" + attempt, mode="companion", topic_id="biology-cell",
                question={"question_id": "cell-question", "question": "细胞的基本结构？"},
                user_answer="细胞膜、细胞质等", response_time_ms=None, attempt_id=attempt,
                eval_result={"verdict": "correct", "score": 100,
                             "evaluator_type": "llm_rubric", "confidence": 1.0},
                used_hint=False,
            )
            assert result["ok"]

        calls = []

        def provider():
            calls.append(now[0])
            return tracker.get_learning_card_snapshot()

        bridge = KnowledgeDungeonPrivateBridge(
            tmp_path / "dungeon.sqlite3", runtime_dir=tmp_path / "runtime",
            learning_snapshot_provider=provider,
        )
        bridge.start()
        record = json.loads(bridge.rendezvous_path.read_text())
        paired = _post(record["port"], "/v1/pair", {
            **{key: record[key] for key in ("protocol_version", "bridge_instance_id", "generation", "launch_code")},
            "client_id": "client-0123456789abcdef0123456789abcdef",
        })

        def invoke(path, **payload):
            body = _post(record["port"], path, {"bridge_protocol_version": 2, **payload}, paired["access_token"])
            assert body["result"]["ok"], body
            return body["result"]["value"]

        def begin(sid):
            return invoke("/v2/game-sessions/begin", game_session_id=sid)

        empty = begin("launch-empty")
        assert [c["card_id"] for c in empty["cards"]] == ["neutral.momiji_mercy"]
        learn("answer-1")
        assert begin("launch-empty") == empty
        assert len(calls) == 1
        learned = begin("launch-learned")
        card = next(c for c in learned["cards"] if c["topic_id"] == "biology-cell")
        assert card["subject_id"] == "biology"
        assert card["mastery"] > 0.01
        assert card["generation"] == 1

        now[0] += 7 * 86400
        assert begin("launch-learned") == learned
        decayed = begin("launch-decayed")
        next_card = next(c for c in decayed["cards"] if c["topic_id"] == "biology-cell")
        assert next_card["mastery"] == pytest.approx(card["mastery"] / 2)
        assert next_card["generation"] == 1

        now[0] += 100 * 86400
        forgotten = begin("launch-forgotten")
        assert len(forgotten["cards"]) == 1
        learn("answer-2")
        assert begin("launch-forgotten") == forgotten
        reacquired = begin("launch-reacquired")
        restored = next(c for c in reacquired["cards"] if c["topic_id"] == "biology-cell")
        assert restored["generation"] == 2
        assert len(calls) == 5
    finally:
        if bridge is not None:
            bridge.stop()
        store.close()
