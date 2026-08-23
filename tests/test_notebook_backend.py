from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

try:
    from plugin.sdk.plugin import Ok
except ModuleNotFoundError:
    pytest.skip(
        "backend integration tests require the plugin mounted in an N.E.K.O checkout",
        allow_module_level=True,
    )


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_study_companion_notebook_backend_test"
PACKAGE = ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
sys.modules[PACKAGE_NAME] = PACKAGE

DocExporter = importlib.import_module(f"{PACKAGE_NAME}.doc_exporter").DocExporter
_NotebookEntriesMixin = importlib.import_module(
    f"{PACKAGE_NAME}.entry_notebook"
)._NotebookEntriesMixin
DocExportConfig = importlib.import_module(f"{PACKAGE_NAME}.models").DocExportConfig
StudyStore = importlib.import_module(f"{PACKAGE_NAME}.store").StudyStore
NotebookStore = importlib.import_module(f"{PACKAGE_NAME}.store_notebook").NotebookStore


class _Logger:
    def warning(self, *_args, **_kwargs):
        return None

    def info(self, *_args, **_kwargs):
        return None

    def debug(self, *_args, **_kwargs):
        return None

    def error(self, *_args, **_kwargs):
        return None


class _AsyncNoopLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _EntryHarness(_NotebookEntriesMixin):
    def __init__(self, notebook_store: NotebookStore) -> None:
        self._notebook_store = notebook_store
        self._agent = None
        self._state = SimpleNamespace(active_mode="companion", last_ocr_text="")
        self._lock = _AsyncNoopLock()
        self.logger = _Logger()


def _make_store(tmp_path):
    logger = _Logger()
    store = StudyStore(tmp_path / "study.db", tmp_path / "seed.json", logger)
    store.open()
    return store, NotebookStore(store)


def test_selected_note_export_is_not_truncated_to_default_limit(tmp_path) -> None:
    store, notebooks = _make_store(tmp_path)
    try:
        note_ids = [
            notebooks.create_note(
                title=f"Note {index:03d}",
                content=f"body {index:03d}",
            ).id
            for index in range(205)
        ]

        exported = DocExporter(
            store, config=DocExportConfig(enabled=True)
        ).export(fmt="markdown", note_ids=note_ids, title="Selected Notes")

        exported_titles = re.findall(
            r"^## (Note \d{3})$", exported.markdown, re.MULTILINE
        )
        assert exported_titles == [f"Note {index:03d}" for index in range(205)]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_note_list_paginates_without_omitting_notes(tmp_path) -> None:
    store, notebooks = _make_store(tmp_path)
    harness = _EntryHarness(notebooks)
    try:
        created_ids = []
        for index in range(3):
            saved = await harness.study_note_upsert(
                title=f"Paged note {index}",
                content=f"Page body {index}",
            )
            assert isinstance(saved, Ok)
            created_ids.append(saved.value["note"]["id"])

        first = await harness.study_note_list(limit=2, offset=0)
        second = await harness.study_note_list(
            limit=2, offset=first.value["next_offset"]
        )

        assert first.value["has_more"] is True
        assert first.value["next_offset"] == 2
        assert second.value["has_more"] is False
        assert second.value["next_offset"] is None
        assert {
            note["id"] for note in [*first.value["notes"], *second.value["notes"]]
        } == set(created_ids)

        search_first = await harness.study_note_list(
            search_query="Paged", limit=2, offset=0
        )
        search_second = await harness.study_note_list(
            search_query="Paged",
            limit=2,
            offset=search_first.value["next_offset"],
        )
        assert {
            note["id"]
            for note in [*search_first.value["notes"], *search_second.value["notes"]]
        } == set(created_ids)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_notebook_list_paginates_without_omitting_notebooks(tmp_path) -> None:
    store, notebooks = _make_store(tmp_path)
    harness = _EntryHarness(notebooks)
    try:
        created_ids = []
        for index in range(3):
            created = await harness.study_notebook_create(
                name=f"Paged notebook {index}"
            )
            assert isinstance(created, Ok)
            created_ids.append(created.value["notebook"]["id"])

        first = await harness.study_notebook_list(limit=2, offset=0)
        second = await harness.study_notebook_list(
            limit=2, offset=first.value["next_offset"]
        )

        assert first.value["has_more"] is True
        assert first.value["next_offset"] == 2
        assert second.value["has_more"] is False
        assert second.value["next_offset"] is None
        assert {
            notebook["id"]
            for notebook in [
                *first.value["notebooks"],
                *second.value["notebooks"],
            ]
        } == set(created_ids)
    finally:
        store.close()
