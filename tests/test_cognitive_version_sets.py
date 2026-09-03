from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

# isort: off
from adaptive_learning.cognitive_versions import (
    DEFAULT_COGNITIVE_VERSION_SET,
    get_cognitive_version_set,
)
# isort: on

ROOT = Path(__file__).resolve().parents[1]


def _models(monkeypatch: pytest.MonkeyPatch):
    name = "_cognitive_version_set_models"
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    mode_manager = ModuleType(f"{name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{name}.models")


def test_default_version_set_is_one_frozen_combination() -> None:
    versions = get_cognitive_version_set(DEFAULT_COGNITIVE_VERSION_SET)
    assert versions is not None
    assert versions.projection_version == "cognitive-v2.1-1"
    assert versions.reducer_version == "cognitive-reducer-v2.1-1"


def test_unknown_version_set_disables_every_cognitive_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _models(monkeypatch)
    config = models.CognitiveConfig(
        projection_enabled=True,
        read_mode="active",
        intent_policy="on",
        ui_enabled=True,
        retention_enabled=True,
        version_set="unknown-combination",
    )
    assert config.version_set == "unknown-combination"
    assert config.model_version == ""
    assert config.projection_enabled is False
    assert config.read_mode == "off"
    assert config.intent_policy == "off"
    assert config.ui_enabled is False
    assert config.retention_enabled is False


def test_old_model_version_only_config_keeps_legacy_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _models(monkeypatch)
    config = models.build_cognitive_config(
        {"cognitive": {"model_version": "cognitive-v1"}}
    )
    assert config.version_set == "cognitive-v1"
    assert config.model_version == "cognitive-v1"
