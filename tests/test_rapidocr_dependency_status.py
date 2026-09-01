from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_models_and_service(monkeypatch: pytest.MonkeyPatch):
    package_name = f"_rapidocr_dependency_status_{time.time_ns()}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(  # type: ignore[attr-defined]
        value or "companion"
    )
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    models = importlib.import_module(f"{package_name}.models")
    service = importlib.import_module(f"{package_name}.service")
    return models, service


def _rapidocr_status(
    *, installed: bool, detail: str, can_download_models: bool = False
) -> dict[str, Any]:
    return {
        "installed": installed,
        "can_download_models": can_download_models,
        "detail": detail,
    }


def _dxcam_status(*, installed: bool = True) -> dict[str, Any]:
    return {"installed": installed, "can_install": False}


def test_dependency_status_is_ready_with_rapidocr_and_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, service = _load_models_and_service(monkeypatch)
    monkeypatch.setattr(
        service,
        "_inspect_rapidocr",
        lambda _config: _rapidocr_status(installed=True, detail="installed"),
    )
    monkeypatch.setattr(service, "_inspect_dxcam", _dxcam_status)

    status = service.build_dependency_status(models.StudyConfig())

    assert set(status) == {
        "rapidocr",
        "dxcam",
        "missing_installable",
        "ocr_readiness",
    }
    assert status["ocr_readiness"] == {
        "enabled": True,
        "selected_backend": "rapidocr",
        "selected_backend_ready": True,
        "capture_ready": True,
        "ready": True,
        "diagnostic": "ready",
    }


def test_dependency_status_reports_missing_rapidocr_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, service = _load_models_and_service(monkeypatch)
    monkeypatch.setattr(
        service,
        "_inspect_rapidocr",
        lambda _config: _rapidocr_status(
            installed=False,
            detail="missing_model_files",
            can_download_models=True,
        ),
    )
    monkeypatch.setattr(service, "_inspect_dxcam", _dxcam_status)

    status = service.build_dependency_status(models.StudyConfig())

    assert status["missing_installable"] == ["rapidocr_models"]
    assert status["ocr_readiness"]["diagnostic"] == "rapidocr_models_missing"
    assert status["ocr_readiness"]["ready"] is False


def test_dependency_status_reports_missing_rapidocr_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, service = _load_models_and_service(monkeypatch)
    monkeypatch.setattr(
        service,
        "_inspect_rapidocr",
        lambda _config: _rapidocr_status(
            installed=False,
            detail="runtime_missing",
        ),
    )
    monkeypatch.setattr(service, "_inspect_dxcam", _dxcam_status)

    status = service.build_dependency_status(models.StudyConfig())

    assert status["missing_installable"] == []
    assert status["ocr_readiness"]["diagnostic"] == "rapidocr_runtime_missing"


def test_invalid_rapidocr_language_never_reports_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, service = _load_models_and_service(monkeypatch)
    monkeypatch.setattr(service, "_inspect_dxcam", _dxcam_status)
    config = models.StudyConfig(rapidocr_lang_type="unsupported")

    status = service.build_dependency_status(config)

    assert config.rapidocr_lang_type == "ch"
    assert status["rapidocr"]["installed"] is False
    assert status["rapidocr"]["detail"] == "invalid_language"
    assert status["missing_installable"] == []
    assert (
        status["ocr_readiness"]["diagnostic"]
        == "rapidocr_language_invalid"
    )


def test_legacy_tesseract_backend_selection_does_not_change_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, service = _load_models_and_service(monkeypatch)
    monkeypatch.setattr(
        service,
        "_inspect_rapidocr",
        lambda _config: _rapidocr_status(installed=True, detail="installed"),
    )
    monkeypatch.setattr(service, "_inspect_dxcam", _dxcam_status)
    config = models.StudyConfig(ocr_backend_selection="tesseract")

    status = service.build_dependency_status(config)

    assert status["ocr_readiness"]["selected_backend"] == "rapidocr"
    assert status["ocr_readiness"]["ready"] is True
    assert config.to_dict()["ocr_backend_selection"] == "tesseract"
