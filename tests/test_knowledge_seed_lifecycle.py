from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_study_companion_knowledge_seed_lifecycle_test"
PACKAGE = ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
sys.modules[PACKAGE_NAME] = PACKAGE

plugin_package = ModuleType("plugin")
plugin_package.__path__ = []  # type: ignore[attr-defined]
sdk_package = ModuleType("plugin.sdk")
sdk_package.__path__ = []  # type: ignore[attr-defined]
shared_package = ModuleType("plugin.sdk.shared")
shared_package.__path__ = []  # type: ignore[attr-defined]
i18n_module = ModuleType("plugin.sdk.shared.i18n")
i18n_module.load_plugin_i18n_from_dir = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
sys.modules.update(
    {
        "plugin": plugin_package,
        "plugin.sdk": sdk_package,
        "plugin.sdk.shared": shared_package,
        "plugin.sdk.shared.i18n": i18n_module,
    }
)

StudyStore = importlib.import_module(f"{PACKAGE_NAME}.store").StudyStore
topics_module = importlib.import_module(f"{PACKAGE_NAME}.store_topics")


class _Logger:
    def warning(self, *_args, **_kwargs) -> None:
        return None

    def info(self, *_args, **_kwargs) -> None:
        return None


def _topic(topic_id: str) -> dict[str, object]:
    return {
        "id": topic_id,
        "name": topic_id,
        "subject": "math",
        "stage": "junior_high",
        "chapter": "geometry",
        "unit": "angles",
        "prerequisites": [],
        "related": [],
    }


def _write_manifest(
    root: Path,
    *,
    revision: str,
    topics: list[dict[str, object]],
    protocol: object = 1,
    content_revision: int | None = None,
    manifest_sha256: str | None = None,
) -> Path:
    seed = root / "topics.json"
    seed.write_text(json.dumps({"topics": topics}), encoding="utf-8")
    manifest = root / "manifest.json"
    payload: dict[str, object] = {
        "seed_protocol_version": protocol,
        "revision": revision,
        "files": [{"path": "topics.json", "topic_count": len(topics)}],
        "total_topics": len(topics),
    }
    if content_revision is not None:
        payload["content_revision"] = content_revision
    if manifest_sha256 is not None:
        payload["manifest_sha256"] = manifest_sha256
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def _store(tmp_path: Path, manifest: Path):
    store = StudyStore(tmp_path / "study.db", tmp_path / "config.json", _Logger(), manifest)
    store.open()
    return store


def test_seed_revision_is_idempotent_and_retires_without_deleting_history(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, revision="r1", topics=[_topic("old"), _topic("keep")])
    store = _store(tmp_path, manifest)
    try:
        assert {topic["id"] for topic in store.list_topics(limit=10)} == {"old", "keep"}
        first_membership = store._require_conn().execute(
            "SELECT COUNT(*) AS count FROM knowledge_seed_membership WHERE active = 1"
        ).fetchone()["count"]
        assert first_membership == 2
        state = store._require_conn().execute(
            "SELECT topic_count, edge_count, applied_at FROM knowledge_seed_state"
        ).fetchone()
        assert (state["topic_count"], state["edge_count"]) == (2, 0)
        assert state["applied_at"]
        old_hash = store._require_conn().execute(
            "SELECT content_hash FROM knowledge_seed_membership WHERE topic_id = 'old'"
        ).fetchone()["content_hash"]
        normalized_old = next(
            topic
            for topic in topics_module._read_knowledge_seed_bundle(manifest)[4]
            if topic["id"] == "old"
        )
        assert old_hash == topics_module._seed_topic_hash(normalized_old)
        conn = store._require_conn()
        conn.execute(
            "INSERT INTO mastery_snapshots (topic_id, mastery, flags) VALUES ('old', 0.25, '[]')"
        )
        conn.execute(
            """INSERT INTO wrong_questions (id, topic_id, question, user_answer, expected_answer, error_type, verdict)
            VALUES ('wrong-old', 'old', 'q', 'a', 'b', 'concept', 'wrong')"""
        )
        conn.execute(
            "INSERT INTO fsrs_cards (topic_id, card_data) VALUES ('old', '{}')"
        )
        conn.commit()
        assert store.load_knowledge_seed() == 2
        assert store._require_conn().execute(
            "SELECT COUNT(*) AS count FROM knowledge_seed_membership WHERE active = 1"
        ).fetchone()["count"] == 2

        manifest = _write_manifest(tmp_path, revision="r2", topics=[_topic("keep")])
        assert store.load_knowledge_seed(manifest) == 1
        assert [topic["id"] for topic in store.list_topics(limit=10)] == ["keep"]
        assert store.get_topic("old")["id"] == "old"
        retired = store._require_conn().execute(
            "SELECT active, retired_at FROM knowledge_seed_membership WHERE topic_id = 'old'"
        ).fetchone()["active"]
        assert retired == 0
        assert store._require_conn().execute(
            "SELECT retired_at FROM knowledge_seed_membership WHERE topic_id = 'old'"
        ).fetchone()["retired_at"]
        assert store.count_topics() == 1
        assert conn.execute(
            "SELECT mastery FROM mastery_snapshots WHERE topic_id = 'old'"
        ).fetchone()["mastery"] == 0.25
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM wrong_questions WHERE topic_id = 'old'"
        ).fetchone()["count"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM fsrs_cards WHERE topic_id = 'old'"
        ).fetchone()["count"] == 1

        manifest = _write_manifest(tmp_path, revision="r3", topics=[_topic("old"), _topic("keep")])
        assert store.load_knowledge_seed(manifest) == 2
        restored = conn.execute(
            "SELECT active, retired_at, content_hash FROM knowledge_seed_membership WHERE topic_id = 'old'"
        ).fetchone()
        assert restored["active"] == 1
        assert restored["retired_at"] is None
        normalized_old = next(
            topic
            for topic in topics_module._read_knowledge_seed_bundle(manifest)[4]
            if topic["id"] == "old"
        )
        assert restored["content_hash"] == topics_module._seed_topic_hash(normalized_old)
    finally:
        store.close()


def test_unsupported_protocol_rejects_before_any_database_change(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, revision="r1", topics=[_topic("valid")])
    store = _store(tmp_path, manifest)
    try:
        invalid = _write_manifest(tmp_path, revision="r2", topics=[_topic("new")], protocol=2)
        assert store.load_knowledge_seed(invalid) == 0
        assert store.get_topic("valid") is not None
        assert store.get_topic("new") is None
    finally:
        store.close()


def test_same_revision_with_different_hash_is_rejected_without_changes(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, revision="r1", topics=[_topic("same")])
    store = _store(tmp_path, manifest)
    try:
        changed = _topic("same")
        changed["name"] = "changed"
        conflict = _write_manifest(tmp_path, revision="r1", topics=[changed])
        assert store.load_knowledge_seed(conflict) == 0
        assert store.get_topic("same")["name"] == "same"
        assert store._require_conn().execute(
            "SELECT COUNT(*) AS count FROM knowledge_seed_state"
        ).fetchone()["count"] == 1
    finally:
        store.close()


def test_content_revision_hash_and_downgrade_guards(tmp_path: Path) -> None:
    initial = _write_manifest(
        tmp_path, revision="legacy", topics=[_topic("high")], content_revision=2
    )
    _key, _protocol, _revision, expected_hash, _topics, _number = topics_module._read_knowledge_seed_bundle(
        initial
    )
    manifest = _write_manifest(
        tmp_path,
        revision="legacy",
        topics=[_topic("high")],
        content_revision=2,
        manifest_sha256=expected_hash,
    )
    store = _store(tmp_path, manifest)
    try:
        lower = _write_manifest(
            tmp_path, revision="legacy", topics=[_topic("low")], content_revision=1
        )
        assert store.load_knowledge_seed(lower) == 0
        assert store.get_topic("high") is not None
        assert store.get_topic("low") is None

        mismatched = _write_manifest(
            tmp_path,
            revision="legacy",
            topics=[_topic("bad-hash")],
            content_revision=3,
            manifest_sha256="0" * 64,
        )
        assert store.load_knowledge_seed(mismatched) == 0
        assert store.get_topic("bad-hash") is None
    finally:
        store.close()


def test_state_edge_count_uses_runtime_graph_normalization(tmp_path: Path) -> None:
    prerequisite = _topic("base")
    dependent = _topic("dependent")
    dependent["prerequisites"] = ["base", "base"]
    manifest = _write_manifest(tmp_path, revision="r1", topics=[prerequisite, dependent])
    store = _store(tmp_path, manifest)
    try:
        state = store._require_conn().execute(
            "SELECT topic_count, edge_count FROM knowledge_seed_state"
        ).fetchone()
        assert (state["topic_count"], state["edge_count"]) == (2, 1)
    finally:
        store.close()


def test_existing_production_manifest_remains_protocol_compatible() -> None:
    _key, protocol, _revision, content_hash, topics, _number = topics_module._read_knowledge_seed_bundle(
        ROOT / "static" / "knowledge_graph_seed.json"
    )
    assert protocol == 1
    assert len(content_hash) == 64
    assert len(topics) == 892
