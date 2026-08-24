from __future__ import annotations

import asyncio
import importlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _Snapshot:
    text: str = ""
    status: str = "ok"
    captured_at: str = "now"
    diagnostic: str = ""
    backend: str = "test"


class _SdkError(RuntimeError):
    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class _Ok:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value


class _Err:
    def __init__(self, error: Exception) -> None:
        self.error = error


@pytest.fixture()
def ocr_entries(monkeypatch: pytest.MonkeyPatch):
    package_name = f"_ocr_question_intent_{time.time_ns()}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    common = ModuleType(f"{package_name}.entry_common")
    common.Err = _Err
    common.Ok = _Ok
    common.SdkError = _SdkError
    common._entry_exception_error = lambda _self, exc, **_kwargs: _Err(exc)
    common._normalize_submitted_image_payload = lambda value: value
    common.asyncio = asyncio
    common.base64 = __import__("base64")
    common.build_ocr_payload = lambda snapshot: {
        "text": snapshot.text,
        "status": snapshot.status,
    }
    common.plugin_entry = lambda **metadata: lambda function: function
    common.rapidocr_support = SimpleNamespace()
    common.tesseract_support = SimpleNamespace()
    common.tr = lambda _key, default="": default
    common.ui = SimpleNamespace(action=lambda: lambda function: function)
    common.update_install_task_state = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, common.__name__, common)

    models = ModuleType(f"{package_name}.models")
    models.OcrSnapshot = _Snapshot
    monkeypatch.setitem(sys.modules, models.__name__, models)

    screenshot = ModuleType(f"{package_name}.interactive_screenshot")
    screenshot.InteractiveCaptureError = RuntimeError
    screenshot.capture_interactive_region = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, screenshot.__name__, screenshot)
    return importlib.import_module(f"{package_name}.entry_ocr_entries")


class _State:
    def __init__(self) -> None:
        self.last_ocr_text = ""
        self.last_ocr_at = ""
        self.last_screen_classification: dict[str, Any] = {}
        self.last_captured_question_id = ""

    def clear_ocr_session(self, *, captured_at: str = "") -> None:
        self.last_ocr_text = ""
        self.last_ocr_at = captured_at
        self.last_captured_question_id = ""

    def clear_expired_ocr_session(self) -> bool:
        return False

    def set_ocr_session_text(self, text: str, *, captured_at: str = "") -> None:
        self.last_ocr_text = text
        self.last_ocr_at = captured_at


class _Store:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    def save_captured_question(self, **kwargs: Any) -> dict[str, Any]:
        self.saved.append(kwargs)
        return {"id": f"captured-{len(self.saved)}", "status": "active"}


class _Owner:
    def __init__(self, *, mode: str, classification: dict[str, Any]) -> None:
        self._lock = asyncio.Lock()
        self._state = _State()
        self._store = _Store()
        self._cfg = SimpleNamespace(ocr_question_persistence_mode=mode)
        self._ocr_pipeline = SimpleNamespace(
            capture_snapshot=lambda: _Snapshot(text="Solve x + 1 = 2?")
        )
        self._supervision = None
        self.logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
        self._classification = classification
        self.persist_calls = 0

    async def _update_screen_classification(self, *_args: Any, **_kwargs: Any):
        self._state.last_screen_classification = dict(self._classification)
        return dict(self._classification)

    async def _persist_state(self) -> None:
        self.persist_calls += 1


def test_ocr_snapshot_does_not_persist_in_default_mode(ocr_entries: Any) -> None:
    owner = _Owner(
        mode="save_when_used",
        classification={"screen_type": "question", "confidence": 0.99},
    )

    result = asyncio.run(
        ocr_entries._OcrEntriesMixin.study_ocr_snapshot(owner)
    )

    assert isinstance(result, _Ok)
    assert owner._store.saved == []
    assert owner._state.last_ocr_text == "Solve x + 1 = 2?"


def test_ocr_snapshot_auto_saves_only_confident_question_text(
    ocr_entries: Any,
) -> None:
    owner = _Owner(
        mode="auto_save_questions",
        classification={"screen_type": "question", "confidence": 0.80},
    )

    async def save_current_ocr_question(**kwargs: Any) -> dict[str, Any]:
        return await ocr_entries._OcrEntriesMixin._save_current_ocr_question(
            owner, **kwargs
        )

    owner._save_current_ocr_question = save_current_ocr_question

    result = asyncio.run(
        ocr_entries._OcrEntriesMixin.study_ocr_snapshot(owner)
    )

    assert isinstance(result, _Ok)
    assert result.value["captured_question_id"] == "captured-1"
    assert owner._store.saved == [
        {
            "text": "Solve x + 1 = 2?",
            "consent_origin": "auto_save",
            "source_type": "ocr",
            "topic_id": "",
            "classification": {"screen_type": "question", "confidence": 0.80},
        }
    ]
    assert owner._state.last_captured_question_id == "captured-1"


def test_ocr_snapshot_rejects_auto_save_below_confidence_gate(
    ocr_entries: Any,
) -> None:
    owner = _Owner(
        mode="auto_save_questions",
        classification={"screen_type": "question", "confidence": 0.79},
    )

    result = asyncio.run(
        ocr_entries._OcrEntriesMixin.study_ocr_snapshot(owner)
    )

    assert isinstance(result, _Ok)
    assert owner._store.saved == []
    assert "captured_question_id" not in result.value
