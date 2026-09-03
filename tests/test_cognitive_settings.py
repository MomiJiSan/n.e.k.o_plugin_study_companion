from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ("en", "ja", "ko", "zh-CN", "zh-TW", "ru", "pt", "es")


def _load_models(monkeypatch: pytest.MonkeyPatch, package_name: str):
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{package_name}.models"), package_name


def _load_status_entries(
    monkeypatch: pytest.MonkeyPatch,
    package_name: str,
    models,
):
    event_bus = ModuleType(f"{package_name}._event_bus")
    event_bus.StudyEventBus = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, event_bus.__name__, event_bus)

    entry_common = ModuleType(f"{package_name}.entry_common")
    entry_common.StudyConfig = models.StudyConfig  # type: ignore[attr-defined]
    entry_common.Ok = lambda value: value  # type: ignore[attr-defined]
    entry_common._entry_exception_error = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    entry_common.asyncio = importlib.import_module("asyncio")  # type: ignore[attr-defined]
    entry_common.build_open_ui_payload = lambda **_kwargs: {}  # type: ignore[attr-defined]
    entry_common.plugin_entry = lambda **_kwargs: lambda function: function  # type: ignore[attr-defined]
    entry_common.tr = lambda _key, default="": default  # type: ignore[attr-defined]
    entry_common.ui = SimpleNamespace(  # type: ignore[attr-defined]
        action=lambda: lambda function: function,
        context=lambda **_kwargs: lambda function: function,
    )
    monkeypatch.setitem(sys.modules, entry_common.__name__, entry_common)
    return importlib.import_module(f"{package_name}.entry_status_entries")


def test_cognitive_settings_round_trip_all_runtime_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, package_name = _load_models(monkeypatch, "_cognitive_settings_modes")
    entries = _load_status_entries(monkeypatch, package_name, models)
    current = models.StudyConfig()

    shadow = entries._apply_settings_config(
        current,
        {
            "cognitive": {
                "projection_enabled": True,
                "read_mode": "shadow",
                "intent_policy": "shadow",
                "ui_enabled": False,
            }
        },
    )
    assert entries._settings_config_payload(shadow)["cognitive"] == {
        "projection_enabled": True,
        "read_mode": "shadow",
        "intent_policy": "shadow",
        "ui_enabled": False,
        "model_version": "cognitive-v1",
        "supported_topics": ("calculus.chain_rule", "college_chain_rule"),
    }

    active = entries._apply_settings_config(
        shadow,
        {
            "cognitive": {
                "projection_enabled": True,
                "read_mode": "active",
                "intent_policy": "on",
                "ui_enabled": True,
            }
        },
    )
    assert active.cognitive.projection_enabled is True
    assert active.cognitive.read_mode == "active"
    assert active.cognitive.intent_policy == "on"
    assert active.cognitive.ui_enabled is True

    disabled = entries._apply_settings_config(
        active,
        {
            "cognitive": {
                "projection_enabled": False,
                "read_mode": "off",
                "intent_policy": "off",
                "ui_enabled": False,
            }
        },
    )
    assert disabled.cognitive == models.CognitiveConfig()


def test_cognitive_settings_replace_the_running_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, package_name = _load_models(monkeypatch, "_cognitive_settings_runtime")
    entries = _load_status_entries(monkeypatch, package_name, models)

    class Tracker:
        def __init__(
            self,
            store,
            *,
            retention_target,
            logger,
            mastery_config,
            cognitive_config,
        ) -> None:
            self.store = store
            self.cognitive_config = cognitive_config
            self.summary_provider = None

        def set_memory_deck_summary_provider(self, provider) -> None:
            self.summary_provider = provider

    class Owner(entries._StatusEntriesMixin):
        def __init__(self) -> None:
            self._cfg = models.StudyConfig()
            self._store = object()
            self.logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
            self._memory_deck_store = SimpleNamespace(status_summary=lambda: {})
            self._knowledge_tracker = Tracker(
                self._store,
                retention_target=self._cfg.fsrs_retention_target,
                logger=self.logger,
                mastery_config=self._cfg.mastery,
                cognitive_config=self._cfg.cognitive,
            )
            self._ocr_pipeline = None
            self._agent = None
            self._pomodoro_timer = None
            self._supervision = None
            self._checkin_manager = None

    owner = Owner()
    previous_tracker = owner._knowledge_tracker
    active = models.StudyConfig(
        cognitive=models.CognitiveConfig(
            projection_enabled=True,
            read_mode="active",
            intent_policy="on",
            ui_enabled=True,
        )
    )

    owner._apply_runtime_settings_config(active)

    assert owner._knowledge_tracker is not previous_tracker
    assert owner._knowledge_tracker.cognitive_config == active.cognitive
    assert callable(owner._knowledge_tracker.summary_provider)


def test_cognitive_setting_is_in_the_right_data_column_and_localized() -> None:
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    main = (ROOT / "static" / "main.js").read_text(encoding="utf-8")
    data_panel = index[index.index('id="panel-data"') : index.index('id="panel-runtime"')]

    assert data_panel.index('id="settingsDocExportEnabled"') < data_panel.index('id="settingsCognitiveMode"')
    assert data_panel.count('class="settings-card"') == 2
    assert '<option value="off"' in data_panel
    assert '<option value="shadow"' in data_panel
    assert '<option value="active"' in data_panel
    for token in (
        "cognitive.projection_enabled",
        "cognitive.read_mode",
        "cognitive.intent_policy",
        "cognitive.ui_enabled",
    ):
        assert token in main

    keys = {
        "ui.settings.cognitive.title",
        "ui.settings.cognitive.mode.label",
        "ui.settings.cognitive.mode.off",
        "ui.settings.cognitive.mode.shadow",
        "ui.settings.cognitive.mode.active",
        "ui.settings.cognitive.help",
    }
    for locale in LOCALES:
        messages = json.loads((ROOT / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
        assert keys <= messages.keys(), locale
