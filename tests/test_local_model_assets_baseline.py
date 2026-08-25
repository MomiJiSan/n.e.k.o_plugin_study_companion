"""Regression baselines that guard pre-local-assets behaviour.

These tests intentionally use a configuration written before the local model
directory setting existed.  Adding an optional local-assets setting must not
change hosted API, OCR, or document-export configuration.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_models(monkeypatch: pytest.MonkeyPatch, package_name: str):
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{package_name}.models")


@pytest.fixture
def legacy_raw_config() -> dict[str, object]:
    return {
        "llm": {
            "llm_call_timeout_seconds": 97,
            "llm_vision_enabled": True,
            "llm_vision_max_image_px": 1024,
        },
        "ocr_reader": {
            "enabled": False,
            "languages": "eng",
            "question_persistence_mode": "auto_save_questions",
        },
        "doc_export": {"enabled": True, "pdf_backend": "reportlab"},
    }


def test_legacy_config_keeps_hosted_api_settings(
    monkeypatch: pytest.MonkeyPatch, legacy_raw_config: dict[str, object]
) -> None:
    models = _load_models(monkeypatch, "_local_assets_api_baseline")

    config = models.build_config(legacy_raw_config)

    assert config.llm_call_timeout_seconds == 97
    assert config.llm_vision_enabled is True
    assert config.llm_vision_max_image_px == 1024
    assert config.local_models_enabled is False
    assert config.local_models_directory == ""


def test_legacy_config_keeps_ocr_settings(
    monkeypatch: pytest.MonkeyPatch, legacy_raw_config: dict[str, object]
) -> None:
    models = _load_models(monkeypatch, "_local_assets_ocr_baseline")

    config = models.build_config(legacy_raw_config)

    assert config.ocr_enabled is False
    assert config.ocr_languages == "eng"
    assert config.ocr_question_persistence_mode == "auto_save_questions"


def test_legacy_config_keeps_document_export_settings(
    monkeypatch: pytest.MonkeyPatch, legacy_raw_config: dict[str, object]
) -> None:
    models = _load_models(monkeypatch, "_local_assets_document_baseline")

    config = models.build_config(legacy_raw_config)

    assert config.doc_export.enabled is True
    assert config.doc_export.pdf_backend == "reportlab"


@pytest.mark.parametrize("value", (None, 7, "   ", "bad\x00path", "relative/models"))
def test_local_models_directory_invalid_values_fall_back_to_default(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    models = _load_models(monkeypatch, "_local_assets_directory_validation")

    config = models.build_config({"llm": {"local_models_directory": value}})

    assert config.local_models_directory == ""
