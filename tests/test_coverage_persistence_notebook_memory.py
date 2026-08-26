from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_study_companion_coverage_persistence"
if PACKAGE_NAME not in sys.modules:
    package = ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = package
mode_manager = ModuleType(f"{PACKAGE_NAME}.mode_manager")
mode_manager.normalize_mode = lambda value: str(value or "companion")
sys.modules[mode_manager.__name__] = mode_manager

StudyStore = importlib.import_module(f"{PACKAGE_NAME}.store").StudyStore
MemoryDeckStore = importlib.import_module(
    f"{PACKAGE_NAME}.memory_deck_store"
).MemoryDeckStore
NotebookStore = importlib.import_module(f"{PACKAGE_NAME}.store_notebook").NotebookStore
fsrs_bridge = importlib.import_module(f"{PACKAGE_NAME}.fsrs_bridge")


class _Logger:
    def debug(self, *_args, **_kwargs) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


@pytest.fixture()
def study_store(tmp_path: Path):
    store = StudyStore(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    try:
        yield store
    finally:
        store.close()


def test_notebook_crud_search_and_reopen_preserve_content_and_provenance(
    study_store,
) -> None:
    notebooks = NotebookStore(study_store)
    notebook = notebooks.create_notebook(
        name="Persistence notes", description="durable", sort_order=3
    )
    note = notebooks.create_note(
        notebook_id=notebook.id,
        title="Atomic SQLite",
        content="# Transactions\n\nRollback keeps writes atomic.",
        source_type="session",
        source_ref="session-17",
        topic_ids=["sqlite", "transactions", "sqlite"],
        tags=["database", "database"],
    )

    listed = notebooks.list_notes(notebook_id=notebook.id)
    assert [item.id for item in listed] == [note.id]
    assert listed[0].content == ""
    assert listed[0].snippet == "Transactions Rollback keeps writes atomic."
    assert [item.id for item in notebooks.list_notes(search_query="Rollback")] == [
        note.id
    ]

    updated = notebooks.upsert_note(
        note_id=note.id,
        title="Atomic persistence",
        content="A committed note survives reopening.",
        source_type="manual",
        source_ref="replacement",
        topic_ids=["sqlite"],
        tags=["durable"],
    )
    assert updated.source_type == "session"
    assert updated.source_ref == "session-17"
    markdown = notebooks.build_notes_markdown([note.id], title="Selected")
    assert "# Selected" in markdown
    assert "## Atomic persistence" in markdown
    assert "A committed note survives reopening." in markdown

    study_store.close()
    study_store.open()
    reopened = NotebookStore(study_store).get_note(note.id)
    assert reopened is not None
    assert reopened.content == "A committed note survives reopening."
    assert reopened.topic_ids == ["sqlite"]
    assert reopened.tags == ["durable"]

    deleted = NotebookStore(study_store).delete_notebook(notebook.id)
    assert deleted == {"deleted": 1, "notes_unlinked": 1}
    assert NotebookStore(study_store).get_note(note.id).notebook_id is None


def test_memory_deck_review_state_and_cascades_survive_reopen(study_store) -> None:
    memory = MemoryDeckStore(study_store)
    deck = memory.create_deck(
        name="Vocabulary", deck_type="word", subject="English", language="en"
    )
    first = memory.add_word(
        deck_id=deck["id"],
        word="durable",
        meaning="lasting",
        example_sentence="The record is durable.",
        tags=["sqlite"],
    )
    duplicate = memory.add_word(
        deck_id=deck["id"], word="durable", meaning="able to withstand wear"
    )
    assert first["created"] is True
    assert duplicate["created"] is False
    assert duplicate["item"]["id"] == first["item"]["id"]

    imported = memory.import_words_csv(
        deck_id=deck["id"],
        content="word,meaning,example_sentence\natomic,all or nothing,One transaction\n",
    )
    assert imported["imported_count"] == 1
    assert memory.count_due_reviews(deck_id=deck["id"]) == 2

    reviewed = memory.review_item(
        item_id=first["item"]["id"],
        rating="good",
        correct=True,
        elapsed_ms=320,
        error_type="",
        session_id="memory-session",
    )
    assert reviewed["rating"] == int(fsrs_bridge.StudyFsrsRating.Good)
    assert reviewed["review_record"]["correct"] is True
    assert reviewed["_review_was_due_before"] is True
    assert reviewed["_review_is_due_after"] is False

    study_store.close()
    study_store.open()
    memory = MemoryDeckStore(study_store)
    persisted = memory.get_item(first["item"]["id"])
    assert persisted is not None
    assert persisted["answer"] == "able to withstand wear"
    assert persisted["fsrs_card"]["last_rating"] == int(
        fsrs_bridge.StudyFsrsRating.Good
    )
    exported = memory.export_deck_json(deck["id"])
    assert exported["deck"]["name"] == "Vocabulary"
    assert {item["prompt"] for item in exported["items"]} == {"atomic", "durable"}
    assert memory.status_summary()["review_count"] == 1

    deleted = memory.delete_deck(deck["id"])
    assert deleted["deleted"] == 1
    assert deleted["cascade"] == {
        "deck_count": 1,
        "item_count": 2,
        "card_count": 2,
        "review_count": 1,
    }
    conn = study_store._require_conn()
    for table in (
        "memory_items",
        "memory_fsrs_cards",
        "memory_review_log",
        "review_records",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_topic_fsrs_and_mastery_histories_trim_and_reopen(study_store) -> None:
    study_store.ensure_topic(topic_id="topic-fsrs", name="FSRS")
    for index, mastery in enumerate((0.2, 0.5, 0.8), start=1):
        study_store.append_mastery_snapshot(
            {
                "topic_id": "topic-fsrs",
                "mastery": mastery,
                "accuracy": mastery,
                "recency": 1.0,
                "consistency": 0.7,
                "confidence": 0.9,
                "level": f"level-{index}",
                "attempts": index,
                "flags": [f"snapshot-{index}"],
            },
            history_limit=2,
        )

    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    card = fsrs_bridge.create_card("topic-fsrs", now)
    updated, schedule = fsrs_bridge.rate_answer(
        card, fsrs_bridge.StudyFsrsRating.Good, now
    )
    study_store.upsert_fsrs_card(
        topic_id="topic-fsrs",
        card=updated.to_dict(),
        last_rating=int(fsrs_bridge.StudyFsrsRating.Good),
    )
    for scheduled_days in (1, 2, 3):
        study_store.append_review_log(
            topic_id="topic-fsrs",
            card_id=None,
            rating=int(fsrs_bridge.StudyFsrsRating.Good),
            scheduled_days=scheduled_days,
            actual_days=scheduled_days - 1,
            history_limit=2,
        )

    assert study_store.get_latest_mastery("topic-fsrs")["mastery"] == 0.8
    assert len(study_store.list_mastery_overview()) == 1
    assert study_store.list_latest_mastery_for_topics(
        ["topic-fsrs", "topic-fsrs", "missing"]
    )[0]["flags"] == ["snapshot-3"]
    assert len(study_store.list_review_log()) == 2
    assert [item["scheduled_days"] for item in study_store.list_review_log()] == [2, 3]

    study_store.close()
    study_store.open()
    persisted_card = study_store.get_fsrs_card("topic-fsrs")
    assert persisted_card["last_rating"] == int(fsrs_bridge.StudyFsrsRating.Good)
    assert persisted_card["card"]["due"] == schedule["due"]
    assert [item["topic_id"] for item in study_store.list_fsrs_cards(None)] == [
        "topic-fsrs"
    ]
    reopened_review_log = study_store.list_review_log()
    assert [item["scheduled_days"] for item in reopened_review_log] == [2, 3]
    assert all(item["topic_id"] == "topic-fsrs" for item in reopened_review_log)
    assert study_store.list_fsrs_cards(topic_ids=[]) == []
    mastery_count = study_store._require_conn().execute(
        "SELECT COUNT(*) FROM mastery_snapshots WHERE topic_id = ?",
        ("topic-fsrs",),
    ).fetchone()[0]
    assert mastery_count == 2
