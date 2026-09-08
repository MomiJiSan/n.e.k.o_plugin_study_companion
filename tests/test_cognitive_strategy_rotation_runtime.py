from __future__ import annotations

from types import SimpleNamespace

import pytest

# isort: split
from test_cognitive_shadow_runtime import (
    _load_runtime,
    _Logger,
    _NeverCalledExtractor,
    _store,
)


def _config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "projection_enabled": True,
        "read_mode": "active",
        "intent_policy": "on",
        "ui_enabled": False,
        "strategy_shadow_enabled": True,
        "strategy_rotation_enabled": True,
        "version_set": "cognitive-v1",
        "model_version": "cognitive-v1",
        "supported_topics": ("calculus.chain_rule",),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_runtime_rotation_requires_active_delivery_and_shadow_collection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    Store, Tracker, _, _ = _load_runtime(
        monkeypatch, "_cognitive_strategy_rotation_runtime"
    )
    store = _store(tmp_path, Store)
    try:
        enabled = Tracker(
            store,
            logger=_Logger(),
            cognitive_config=_config(),
            cognitive_extractor=_NeverCalledExtractor(),
        )
        no_collection = Tracker(
            store,
            logger=_Logger(),
            cognitive_config=_config(strategy_shadow_enabled=False),
            cognitive_extractor=_NeverCalledExtractor(),
        )
        shadow_read = Tracker(
            store,
            logger=_Logger(),
            cognitive_config=_config(read_mode="shadow"),
            cognitive_extractor=_NeverCalledExtractor(),
        )
        disabled = Tracker(
            store,
            logger=_Logger(),
            cognitive_config=_config(strategy_rotation_enabled=False),
            cognitive_extractor=_NeverCalledExtractor(),
        )

        assert enabled.cognitive_strategy_rotation_enabled is True
        assert no_collection.cognitive_strategy_rotation_enabled is False
        assert shadow_read.cognitive_strategy_rotation_enabled is False
        assert disabled.cognitive_strategy_rotation_enabled is False
    finally:
        store.close()
