from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ("en", "ja", "ko", "zh-CN", "zh-TW", "ru", "pt", "es")
KEY = "ui.onboarding.step.ocr.body"


def test_ocr_tutorial_explains_text_only_boundary_in_every_locale() -> None:
    for locale in LOCALES:
        messages = json.loads((ROOT / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
        body = messages[KEY]

        assert "OCR" in body, locale
        assert body.strip(), locale


def test_ocr_tutorial_english_fallback_matches_locale_copy() -> None:
    english = json.loads((ROOT / "i18n" / "en.json").read_text(encoding="utf-8"))[KEY]
    main_js = (ROOT / "static" / "main.js").read_text(encoding="utf-8")

    assert english in main_js
    assert "only extracts text" in english
    assert "does not automatically detect or split questions" in english
