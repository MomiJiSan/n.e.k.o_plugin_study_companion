from __future__ import annotations

import json
from pathlib import Path
from string import Formatter

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ("en", "ja", "ko", "zh-CN", "zh-TW", "ru", "pt", "es")
ERROR_KEYS = {
    "ui.error.document_pdf_ocr_disabled",
    "ui.error.document_pdf_ocr_unavailable",
    "ui.error.document_pdf_ocr_too_many_pages",
    "ui.error.document_pdf_render_failed",
    "ui.error.document_pdf_page_too_large",
    "ui.error.document_pdf_ocr_timeout",
    "ui.error.document_pdf_ocr_failed",
    "ui.error.document_pdf_ocr_busy",
}
DOCUMENT_KEYS = {
    "ui.document.scanned_pdf_ocr",
    "ui.document.progress_ocr_pages",
    "ui.document.ocr_truncated_warning",
    "ui.document.partial_ocr_skipped_warning",
}
NO_READABLE_TEXT = {
    "en": "No readable text was found.",
    "ja": "読み取り可能な文字が見つかりません。",
    "ko": "읽을 수 있는 텍스트가 없습니다.",
    "zh-CN": "未识别到可读文字。",
    "zh-TW": "未辨識到可讀文字。",
    "ru": "Читаемый текст не найден.",
    "pt": "Nenhum texto legível foi encontrado.",
    "es": "No se encontró texto legible.",
}


def _load(locale: str) -> dict[str, str]:
    path = ROOT / "i18n" / f"{locale}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _placeholders(value: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in Formatter().parse(value)
        if field_name is not None
    }


def test_all_locales_have_identical_keys() -> None:
    messages = {locale: _load(locale) for locale in LOCALES}
    expected = set(messages["en"])

    for locale, localized in messages.items():
        assert set(localized) == expected, locale


def test_scanned_pdf_messages_are_complete_and_nonempty() -> None:
    required = ERROR_KEYS | DOCUMENT_KEYS

    for locale in LOCALES:
        localized = _load(locale)
        assert required <= localized.keys(), locale
        assert all(localized[key].strip() for key in required), locale


def test_scanned_pdf_progress_placeholders_match() -> None:
    for locale in LOCALES:
        localized = _load(locale)
        assert _placeholders(localized["ui.document.progress_ocr_pages"]) == {
            "page",
            "total",
        }, locale


def test_no_readable_text_copy_is_capability_neutral() -> None:
    for locale, expected in NO_READABLE_TEXT.items():
        localized = _load(locale)
        assert localized["ui.error.document_no_readable_text"] == expected
