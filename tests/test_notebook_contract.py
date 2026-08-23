from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ("en", "ja", "ko", "zh-CN", "zh-TW", "ru", "pt", "es")


def test_notebook_static_assets_are_loaded_in_dependency_order() -> None:
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    main = (ROOT / "static" / "main.js").read_text(encoding="utf-8")
    controller = (ROOT / "static" / "notebook-controller.js").read_text(
        encoding="utf-8"
    )
    panels = (ROOT / "static" / "surface-panels.js").read_text(encoding="utf-8")

    assert 'data-open-surface="notebook-panel"' in index
    assert "./notebook.css" in index
    assert index.index("./notebook-controller.js") < index.index(
        "./surface-panels.js"
    )
    assert index.index("./scanned-pdf-ocr.js") < index.index(
        "./document-controller.js"
    )
    assert "openSurface: openSurfaceDrawer" in main
    assert "study_note_ai_expand: 90000" in main
    assert "selectedNoteIds" in controller
    assert "note_ids: notebookNoteIds" in panels


def test_notebook_ci_does_not_persist_checkout_credentials() -> None:
    workflow = (ROOT / ".github" / "workflows" / "frontend-tests.yml").read_text(
        encoding="utf-8"
    )

    assert "permissions:\n  contents: read" in workflow
    assert "persist-credentials: false" in workflow


def test_notebook_i18n_contract_is_complete_in_all_locales() -> None:
    bundles = {
        locale: json.loads(
            (ROOT / "i18n" / f"{locale}.json").read_text(encoding="utf-8")
        )
        for locale in LOCALES
    }
    expected_keys = set(bundles["en"])
    required = {
        "ui.feature.notebook.title",
        "ui.feature.notebook.body",
        "ui.notebook.discard_draft_confirm",
        "ui.notebook.selected_notes",
        "ui.notebook.load_more",
    }

    for locale, bundle in bundles.items():
        assert set(bundle) == expected_keys, locale
        assert required <= set(bundle), locale
        for key in required:
            assert str(bundle[key]).strip(), f"{locale}: {key}"


def test_legacy_recitation_runtime_is_removed_but_schema_is_retained() -> None:
    removed_surfaces = (
        "note_card.tsx",
        "note_editor.tsx",
        "note_exporter.tsx",
        "note_search.tsx",
        "notebook_panel.tsx",
        "passage_recitation.tsx",
        "word_review.tsx",
    )
    for name in removed_surfaces:
        assert not (ROOT / "surfaces" / name).exists(), name

    runtime_sources = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in (
            "entry_memory_import_entries.py",
            "entry_memory_review_entries.py",
            "memory_deck_store.py",
            "memory_habit_bridge.py",
            "memory_ratings.py",
            "memory_rows.py",
            "memory_text.py",
        )
    )
    for symbol in (
        "study_memory_recitation_attempt",
        "add_recitation_attempt",
        "create_recitation_error_draft",
        "rating_from_recitation_score",
        "diff_recitation",
        "recitation_from_row",
    ):
        assert symbol not in runtime_sources

    schema = (ROOT / "memory_schema.py").read_text(encoding="utf-8")
    maintenance = (ROOT / "store_maintenance.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS recitation_attempts" in schema
    assert '"recitation_attempts"' in maintenance
