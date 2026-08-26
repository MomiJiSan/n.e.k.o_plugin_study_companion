from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def debug(self, *_args, **_kwargs):
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


def _load_store(monkeypatch: pytest.MonkeyPatch, name: str):
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    mode_manager = ModuleType(f"{name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    store_module = importlib.import_module(f"{name}.store")
    edges_module = importlib.import_module(f"{name}.store_knowledge_edges")
    return store_module.StudyStore, edges_module


def _store(tmp_path: Path, Store):
    store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    return store


def _write_topics(store) -> None:
    store.upsert_topic(
        {
            "id": "base",
            "name": "Base",
            "subject": "math",
            "stage": "senior_high",
            "chapter": "algebra",
            "unit": "one",
            "prerequisites": [],
            "related": [{"id": "application", "relation": "application", "reason": "uses base"}],
        }
    )
    store.upsert_topic(
        {
            "id": "dependent",
            "name": "Dependent",
            "subject": "math",
            "stage": "senior_high",
            "chapter": "algebra",
            "unit": "one",
            "prerequisites": [{"id": "base", "reason": "needed first"}],
            "related": [],
        }
    )
    store.upsert_topic(
        {
            "id": "application",
            "name": "Application",
            "subject": "math",
            "stage": "senior_high",
            "chapter": "algebra",
            "unit": "two",
            "prerequisites": [],
            "related": [],
        }
    )


def _projection_is_dirty(store) -> bool:
    row = store._require_conn().execute(
        "SELECT dirty FROM knowledge_edge_projection_state WHERE projection_key = 'active'"
    ).fetchone()
    return bool(row and int(row["dirty"] or 0))


def test_projection_rebuild_persists_canonical_edges_and_active_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, _ = _load_store(monkeypatch, "_knowledge_edges_projection")
    store = _store(tmp_path, Store)
    try:
        _write_topics(store)
        revision = store.rebuild_knowledge_edge_projection()
        assert revision["available"] is True
        assert revision["edge_count"] == 2
        assert revision["catalog_revision"].startswith("knowledge-edges-v1:")
        assert store.get_knowledge_edge_revision() == revision
        assert store.list_knowledge_edges() == [
            {
                "from": "base",
                "to": "application",
                "relation": "application",
                "priority": "useful",
                "confidence": 0.85,
                "context": "practice",
                "reason": "uses base",
                "catalog_revision": revision["catalog_revision"],
            },
            {
                "from": "base",
                "to": "dependent",
                "relation": "prerequisite",
                "priority": "core",
                "confidence": 0.85,
                "context": "explanation",
                "reason": "needed first",
                "catalog_revision": revision["catalog_revision"],
            },
        ]
        assert store.list_knowledge_edges(topic_ids={"dependent"}) == [
            store.list_knowledge_edges()[1]
        ]
        assert store.list_knowledge_edges(relation_types={"application"}) == [
            store.list_knowledge_edges()[0]
        ]
        assert store.rebuild_knowledge_edge_projection() == revision
    finally:
        store.close()


def test_failed_rebuild_keeps_previous_active_revision_and_edges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, edges_module = _load_store(monkeypatch, "_knowledge_edges_rollback")
    store = _store(tmp_path, Store)
    try:
        _write_topics(store)
        previous = store.rebuild_knowledge_edge_projection()
        previous_edges = store.list_knowledge_edges()
        store.upsert_topic(
            {
                "id": "base",
                "name": "Changed Base",
                "subject": "math",
                "stage": "senior_high",
                "chapter": "algebra",
                "unit": "one",
                "prerequisites": [],
                "related": [],
            }
        )

        def fail_build(_topics):
            raise RuntimeError("injected projection failure")

        monkeypatch.setattr(edges_module, "build_topic_edges", fail_build)
        with pytest.raises(RuntimeError, match="injected projection failure"):
            store.rebuild_knowledge_edge_projection()
        assert store.get_knowledge_edge_revision() == previous
        assert store.list_knowledge_edges() == previous_edges
    finally:
        store.close()


def test_dirty_runtime_topic_rebuilds_before_map_read_and_stales_old_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, edges_module = _load_store(monkeypatch, "_knowledge_edges_dirty_runtime")
    store = _store(tmp_path, Store)
    try:
        _write_topics(store)
        store.rebuild_knowledge_edge_projection()
        first = store.query_knowledge_map_page(stage="senior_high", subject="math", page_size=1)
        assert first["next_cursor"]
        prior_revision = first["catalog_revision"]

        store.upsert_topic(
            {
                "id": "runtime-dependent",
                "name": "Runtime dependent",
                "subject": "math",
                "stage": "senior_high",
                "chapter": "algebra",
                "unit": "one",
                "prerequisites": [{"id": "base", "reason": "needs base"}],
                "related": [{"id": "application", "relation": "application"}],
            }
        )
        assert _projection_is_dirty(store) is True

        rebuild_calls = 0
        original_build = edges_module.build_topic_edges

        def count_build(topics):
            nonlocal rebuild_calls
            rebuild_calls += 1
            return original_build(topics)

        monkeypatch.setattr(edges_module, "build_topic_edges", count_build)
        with pytest.raises(ValueError, match="knowledge_map_cursor_stale"):
            store.query_knowledge_map_page(
                stage="senior_high",
                subject="math",
                page_size=1,
                cursor=first["next_cursor"],
            )
        assert rebuild_calls == 1
        assert _projection_is_dirty(store) is False
        assert store.get_knowledge_edge_revision()["catalog_revision"] != prior_revision

        complete = store.query_knowledge_map_page(stage="senior_high", subject="math", page_size=100)
        assert rebuild_calls == 1
        assert {
            (edge["from"], edge["to"], edge["relation"])
            for edge in complete["edges"]
        } >= {
            ("base", "runtime-dependent", "prerequisite"),
            ("runtime-dependent", "application", "application"),
        }
    finally:
        store.close()


def test_dirty_projection_failure_keeps_active_edges_and_rejects_map_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, edges_module = _load_store(monkeypatch, "_knowledge_edges_dirty_failure")
    store = _store(tmp_path, Store)
    try:
        _write_topics(store)
        previous = store.rebuild_knowledge_edge_projection()
        previous_edges = store.list_knowledge_edges()
        store.upsert_topic(
            {
                "id": "runtime-dependent",
                "name": "Runtime dependent",
                "subject": "math",
                "stage": "senior_high",
                "chapter": "algebra",
                "prerequisites": [{"id": "base"}],
            }
        )
        monkeypatch.setattr(
            edges_module,
            "build_topic_edges",
            lambda _topics: (_ for _ in ()).throw(RuntimeError("injected projection failure")),
        )
        with pytest.raises(ValueError, match="knowledge_edge_projection_rebuild_failed"):
            store.query_knowledge_map_page(stage="senior_high", subject="math")
        current = store.get_knowledge_edge_revision()
        assert current["catalog_revision"] == previous["catalog_revision"]
        assert _projection_is_dirty(store) is True
        assert store.list_knowledge_edges() == previous_edges
    finally:
        store.close()


def test_many_batch_topic_writes_trigger_one_lazy_projection_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, edges_module = _load_store(monkeypatch, "_knowledge_edges_batch_dirty")
    store = _store(tmp_path, Store)
    try:
        _write_topics(store)
        store.rebuild_knowledge_edge_projection()
        conn = store._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for index in range(3):
                store._batch_upsert_topic(
                    conn,
                    {
                        "id": f"batch-{index}",
                        "name": f"Batch {index}",
                        "subject": "math",
                        "stage": "senior_high",
                        "chapter": "algebra",
                        "prerequisites": [{"id": "base"}],
                    },
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        rebuild_calls = 0
        original_build = edges_module.build_topic_edges

        def count_build(topics):
            nonlocal rebuild_calls
            rebuild_calls += 1
            return original_build(topics)

        monkeypatch.setattr(edges_module, "build_topic_edges", count_build)
        store.query_knowledge_map_page(stage="senior_high", subject="math")
        store.query_knowledge_map_page(stage="senior_high", subject="math")
        assert rebuild_calls == 1
    finally:
        store.close()


def test_open_migrates_old_database_and_rebuilds_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, _ = _load_store(monkeypatch, "_knowledge_edges_old_database")
    first = _store(tmp_path, Store)
    try:
        _write_topics(first)
    finally:
        first.close()

    connection = sqlite3.connect(tmp_path / "study.db")
    try:
        connection.execute("DROP TABLE knowledge_edges")
        connection.execute("DROP TABLE knowledge_edge_projection_state")
        connection.commit()
    finally:
        connection.close()

    reopened = _store(tmp_path, Store)
    try:
        revision = reopened.get_knowledge_edge_revision()
        assert revision["available"] is True
        assert revision["edge_count"] == 2
        assert len(reopened.list_knowledge_edges()) == 2
    finally:
        reopened.close()


def test_map_page_uses_sql_keyset_scope_and_one_hop_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Store, edges_module = _load_store(monkeypatch, "_knowledge_edges_map_page")
    store = _store(tmp_path, Store)
    try:
        _write_topics(store)
        for index in range(205):
            store.upsert_topic(
                {
                    "id": f"page-{index:03d}",
                    "name": f"Page {index}",
                    "subject": "physics",
                    "stage": "senior_high",
                    "chapter": "mechanics",
                    "unit": "pagination",
                    "prerequisites": [],
                    "related": [],
                }
            )
        store.rebuild_knowledge_edge_projection()

        def must_not_rebuild(_topics):
            raise AssertionError("map page must use the persisted edge projection")

        monkeypatch.setattr(edges_module, "build_topic_edges", must_not_rebuild)
        first = store.query_knowledge_map_page(
            stage="senior-high", subject="physics", page_size=999
        )
        assert first["scope_total_count"] == 205
        assert first["scope_returned_count"] == 200
        assert first["has_more"] is True
        assert first["next_cursor"]
        second = store.query_knowledge_map_page(
            stage="senior_high", subject="physics", page_size=200, cursor=first["next_cursor"]
        )
        assert second["scope_returned_count"] == 5
        assert second["has_more"] is False
        assert second["next_cursor"] == ""

        base_page = store.query_knowledge_map_page(
            stage="senior_high", subject="math", unit="one", page_size=1
        )
        assert base_page["scope_total_count"] == 2
        assert base_page["scope_returned_count"] == 1
        assert base_page["nodes"][0]["id"] == "base"
        assert {node["id"] for node in base_page["boundary"]["nodes"]} == {
            "application",
            "dependent",
        }
        assert {
            (edge["from"], edge["to"], edge["relation"])
            for edge in base_page["edges"]
        } == {
            ("base", "application", "application"),
            ("base", "dependent", "prerequisite"),
        }
        assert base_page["boundary"]["truncated"] is False
    finally:
        store.close()
