from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _package(monkeypatch: pytest.MonkeyPatch, name: str) -> str:
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    mode_manager = ModuleType(f"{name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return name


def _load_models(monkeypatch: pytest.MonkeyPatch, name: str):
    package = _package(monkeypatch, name)
    return importlib.import_module(f"{package}.models"), package


def test_state_serialization_keeps_ocr_runtime_only_and_sanitizes_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, _package_name = _load_models(monkeypatch, "_ocr_privacy_state")
    state = models.StudyState(
        last_vision_image_base64="private-image",
        last_captured_question_id="capture-runtime-only",
        last_screen_classification={
            "screen_type": "question",
            "text_excerpt": "private excerpt",
            "window_title": "private title",
            "signals": {"question_hits": ["question"]},
        },
        recent_screen_classifications=[
            {
                "screen_type": "reading",
                "text_excerpt": "private excerpt",
                "window_title": "private title",
            }
        ],
    )
    state.set_ocr_session_text("private OCR question")
    state.last_vision_image_base64 = "private-image"
    state.last_captured_question_id = "capture-runtime-only"

    persisted = state.to_dict()

    assert state.last_ocr_text == "private OCR question"
    assert "last_ocr_text" not in persisted
    assert "last_vision_image_base64" not in persisted
    assert "last_captured_question_id" not in persisted
    assert persisted["last_screen_classification"] == {
        "screen_type": "question",
        "signals": {"question_hits": ["question"]},
    }
    assert persisted["recent_screen_classifications"] == [
        {"screen_type": "reading"}
    ]


def test_state_initialization_clears_legacy_ocr_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, _package_name = _load_models(monkeypatch, "_ocr_privacy_legacy")

    state = models.StudyState(
        last_ocr_text="legacy private OCR text",
        last_vision_image_base64="legacy private image",
    )

    assert state.last_ocr_text == ""
    assert state.last_vision_image_base64 == ""


def test_status_payload_never_returns_ocr_text_or_classifier_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, package = _load_models(monkeypatch, "_ocr_privacy_service")
    service = importlib.import_module(f"{package}.service")
    state = models.StudyState(
        last_captured_question_id="captured-1",
        last_screen_classification={
            "screen_type": "question",
            "text_excerpt": "private excerpt",
            "window_title": "private title",
        },
    )
    captured_at = datetime.now(timezone.utc).isoformat()
    state.set_ocr_session_text(
        "private OCR question", captured_at=captured_at
    )
    state.last_captured_question_id = "captured-1"

    payload = service.build_status_payload(config=models.StudyConfig(), state=state)

    assert "last_ocr_text" not in payload
    assert payload["has_ocr_text"] is True
    assert payload["last_ocr_at"] == captured_at
    assert payload["last_captured_question_id"] == "captured-1"
    assert payload["screen_classification"] == {"screen_type": "question"}


def test_ocr_buffer_expires_after_thirty_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    models, _package_name = _load_models(monkeypatch, "_ocr_privacy_ttl")
    captured_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    state = models.StudyState()
    state.set_ocr_session_text(
        "private OCR question", captured_at=captured_at.isoformat()
    )

    assert state.clear_expired_ocr_session() is True
    assert state.last_ocr_text == ""
    assert state.last_ocr_at == captured_at.isoformat()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({}, "save_when_used"),
        ({"ocr_reader": {"question_persistence_mode": "auto_save_questions"}}, "auto_save_questions"),
        ({"ocr_question_persistence_mode": "not-a-mode"}, "save_when_used"),
    ],
)
def test_ocr_question_persistence_mode_is_validated(
    monkeypatch: pytest.MonkeyPatch, raw: dict[str, object], expected: str
) -> None:
    models, _package_name = _load_models(monkeypatch, f"_ocr_privacy_config_{expected}")

    config = models.build_config(raw)

    assert config.ocr_question_persistence_mode == expected


def test_screen_classifier_payload_omits_ocr_excerpt_and_window_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(monkeypatch, "_ocr_privacy_classifier")
    classifier = importlib.import_module(f"{package}.screen_classifier")

    payload = classifier.ScreenClassification(
        screen_type="question",
        text_excerpt="private OCR excerpt",
        window_title="private window title",
    ).to_payload()

    assert "text_excerpt" not in payload
    assert "window_title" not in payload
