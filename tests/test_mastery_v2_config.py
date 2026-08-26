from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_models(monkeypatch: pytest.MonkeyPatch, name: str):
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    mode_manager = ModuleType(f"{name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{name}.models")


def test_mastery_v2_config_defaults_to_disabled_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _load_models(monkeypatch, "_mastery_v2_config_defaults")

    config = models.build_config({}).mastery

    assert config.to_dict() == {
        "v2_shadow_enabled": False,
        "read_model": "v1",
        "model_version": "mastery-v2-shadow-1",
    }


def test_mastery_v2_config_is_strict_and_version_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _load_models(monkeypatch, "_mastery_v2_config_strict")

    enabled = models.build_config(
        {
            "mastery": {
                "v2_shadow_enabled": True,
                "read_model": "v2",
                "model_version": "mastery-v2-shadow-1",
            }
        }
    ).mastery
    invalid = models.build_config(
        {
            "mastery": {
                "v2_shadow_enabled": "true",
                "read_model": "future",
                "model_version": "unversioned-experiment",
            }
        }
    ).mastery

    assert enabled.to_dict() == {
        "v2_shadow_enabled": True,
        "read_model": "v2",
        "model_version": "mastery-v2-shadow-1",
    }
    assert invalid.to_dict() == {
        "v2_shadow_enabled": False,
        "read_model": "v1",
        "model_version": "mastery-v2-shadow-1",
    }
