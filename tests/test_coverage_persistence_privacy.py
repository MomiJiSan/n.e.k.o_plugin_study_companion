from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

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
StudyState = importlib.import_module(f"{PACKAGE_NAME}.models").StudyState


def _count_tokens(text: object) -> int:
    return max(1, len(str(text)) // 4)


created_utils_package = "utils" not in sys.modules
created_tokenize_module = "utils.tokenize" not in sys.modules
if created_utils_package:
    utils_package = ModuleType("utils")
    utils_package.__path__ = []  # type: ignore[attr-defined]
    sys.modules["utils"] = utils_package
if created_tokenize_module:
    tokenize_module = ModuleType("utils.tokenize")
    tokenize_module.count_tokens = _count_tokens
    sys.modules["utils.tokenize"] = tokenize_module
try:
    validate_document = importlib.import_module(
        f"{PACKAGE_NAME}.document_analysis"
    ).validate_document
finally:
    if created_tokenize_module:
        sys.modules.pop("utils.tokenize", None)
    if created_utils_package:
        sys.modules.pop("utils", None)


class _Logger:
    def debug(self, *_args, **_kwargs) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


def test_document_source_is_absent_from_sqlite_state_and_json_export(
    tmp_path: Path,
) -> None:
    source = (
        "PRIVATE-DOCUMENT-SENTINEL-7f91: This exact source paragraph must remain "
        "runtime-only and must never enter durable storage or exports."
    )
    document = validate_document(
        document_name="private.md",
        document_type="text/markdown",
        document_text=source,
        analysis_instruction="Summarize without quoting the source.",
        analysis_kind="general_notes",
        locale="en",
    )
    assert document.text == source
    assert source not in document.descriptor
    assert source not in json.dumps(document.public_metadata(), ensure_ascii=False)

    store = StudyStore(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    try:
        state = StudyState()
        state.set_ocr_session_text(source)
        store.save_state(state)
        store.append_interaction(
            kind="document_analyze",
            input_text=document.descriptor,
            output_text="A privacy-safe summary of the imported document.",
            metadata={
                "document": document.public_metadata(),
                "source_retained": False,
            },
        )

        persisted_state = store.get_raw("state")
        assert "last_ocr_text" not in persisted_state
        exported = store.export_json()
        exported_text = json.dumps(exported, ensure_ascii=False, sort_keys=True)
        assert source not in exported_text
        assert exported["interactions"][0]["input_text"] == document.descriptor
        assert exported["interactions"][0]["metadata"]["source_retained"] is False
    finally:
        store.close()

    sqlite_bytes = b"".join(
        path.read_bytes() for path in sorted(tmp_path.glob("study.db*"))
    )
    assert source.encode("utf-8") not in sqlite_bytes
