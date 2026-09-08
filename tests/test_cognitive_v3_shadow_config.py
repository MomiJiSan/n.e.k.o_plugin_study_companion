from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _models(monkeypatch: pytest.MonkeyPatch):
    package_name = "_cognitive_v3_shadow_config"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{package_name}.models")


def test_strategy_shadow_defaults_off_and_parses_explicit_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _models(monkeypatch)
    assert models.CognitiveConfig().strategy_shadow_enabled is False

    enabled = models.build_cognitive_config(
        {
            "cognitive": {
                "projection_enabled": True,
                "read_mode": "active",
                "intent_policy": "on",
                "strategy_shadow_enabled": True,
            }
        }
    )

    assert enabled.strategy_shadow_enabled is True
    assert enabled.to_dict()["strategy_shadow_enabled"] is True


def test_strategy_shadow_fails_closed_for_invalid_values_and_version_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _models(monkeypatch)
    invalid_boolean = models.CognitiveConfig(strategy_shadow_enabled="yes")  # type: ignore[arg-type]
    assert invalid_boolean.strategy_shadow_enabled is False

    invalid_version = models.CognitiveConfig(
        projection_enabled=True,
        read_mode="active",
        intent_policy="on",
        strategy_shadow_enabled=True,
        version_set="unknown-version-set",
    )
    assert invalid_version.strategy_shadow_enabled is False


def test_strategy_rotation_defaults_off_and_requires_a_strict_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _models(monkeypatch)
    assert models.CognitiveConfig().strategy_rotation_enabled is False

    enabled = models.build_cognitive_config(
        {"cognitive": {"strategy_rotation_enabled": True}}
    )
    invalid = models.CognitiveConfig(strategy_rotation_enabled="yes")  # type: ignore[arg-type]

    assert enabled.strategy_rotation_enabled is True
    assert invalid.strategy_rotation_enabled is False


def test_strategy_rotation_fails_closed_for_unknown_version_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _models(monkeypatch)
    invalid_version = models.CognitiveConfig(
        projection_enabled=True,
        read_mode="active",
        intent_policy="on",
        strategy_shadow_enabled=True,
        strategy_rotation_enabled=True,
        version_set="unknown-version-set",
    )

    assert invalid_version.strategy_rotation_enabled is False
