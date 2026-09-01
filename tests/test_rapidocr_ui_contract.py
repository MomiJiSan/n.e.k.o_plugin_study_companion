from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCALES = ("en", "ja", "ko", "zh-CN", "zh-TW", "ru", "pt", "es")
LANGUAGE_KEYS = {
    "ui.settings.recognition_language.label",
    "ui.settings.recognition_language.help",
    "ui.settings.recognition_language.ch",
    "ui.settings.recognition_language.japan",
    "ui.settings.recognition_language.korean",
    "ui.settings.recognition_language.en",
}


def test_static_ocr_settings_use_rapidocr_language_contract() -> None:
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    main = (ROOT / "static" / "main.js").read_text(encoding="utf-8")

    assert '<select id="settingsOcrLanguages"' in index
    for value in ("ch", "japan", "korean", "en"):
        assert f'<option value="{value}"' in index
    assert '<input id="settingsOcrLanguages"' not in index
    assert './dependency-controller.js?v=study-rapidocr-only-0.2.1' in index
    assert './main.js?v=study-rapidocr-only-0.2.1' in index
    assert "const rapidocr = config.rapidocr || {};" in main
    assert "rapidocr.lang_type" in main
    assert "ocr.languages =" not in main


def test_static_dependency_ui_has_no_tesseract_path() -> None:
    sources = {
        name: (ROOT / name).read_text(encoding="utf-8")
        for name in (
            "static/index.html",
            "static/main.js",
            "static/dependency-controller.js",
            "README.md",
            "docs/PROJECT_MAP.md",
        )
    }

    assert "['rapidocr', 'dxcam']" in sources["static/main.js"]
    assert 'data-dependency="rapidocr"' in sources["static/index.html"]
    assert 'data-dependency="dxcam"' in sources["static/index.html"]
    assert "/ui-api/rapidocr-models" in sources["static/dependency-controller.js"]
    for name, source in sources.items():
        assert "tesseract" not in source.lower(), name


def test_recognition_language_copy_is_complete_in_all_locales() -> None:
    for locale in LOCALES:
        messages = json.loads(
            (ROOT / "i18n" / f"{locale}.json").read_text(encoding="utf-8")
        )
        assert LANGUAGE_KEYS <= set(messages), locale
        for key in LANGUAGE_KEYS:
            assert str(messages[key]).strip(), f"{locale}: {key}"
        assert not any("tesseract" in str(value).lower() for value in messages.values()), locale
        assert "entries.install_tesseract.name" not in messages, locale
        assert "ui.settings.dependencies.name_tesseract" not in messages, locale
