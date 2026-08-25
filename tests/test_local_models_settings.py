from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_models(monkeypatch: pytest.MonkeyPatch, package_name: str):
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{package_name}.models"), package_name


def _load_status_entries(
    monkeypatch: pytest.MonkeyPatch, package_name: str, models
):
    event_bus = ModuleType(f"{package_name}._event_bus")
    event_bus.StudyEventBus = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, event_bus.__name__, event_bus)

    entry_common = ModuleType(f"{package_name}.entry_common")
    entry_common.StudyConfig = models.StudyConfig  # type: ignore[attr-defined]
    entry_common.StudyEventBus = object  # type: ignore[attr-defined]
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


def test_local_models_config_defaults_and_rejects_non_boolean_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, _package_name = _load_models(monkeypatch, "_local_models_defaults")

    assert models.build_config({}).local_models_enabled is False
    assert models.build_config({"llm": {"local_models_enabled": True}}).local_models_enabled is True
    assert models.build_config({"llm": {"local_models_enabled": "true"}}).local_models_enabled is False


def test_local_models_settings_payload_round_trips_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, package_name = _load_models(monkeypatch, "_local_models_status")
    entries = _load_status_entries(monkeypatch, package_name, models)
    current = models.StudyConfig()

    payload = entries._settings_config_payload(current)
    assert payload["llm"]["local_models_enabled"] is False

    enabled = entries._apply_settings_config(
        current, {"llm": {"local_models_enabled": True}}
    )
    assert enabled.local_models_enabled is True
    assert entries._settings_config_payload(enabled)["llm"]["local_models_enabled"] is True

    invalid = entries._apply_settings_config(
        current, {"llm": {"local_models_enabled": "not-a-boolean"}}
    )
    assert invalid.local_models_enabled is False


def test_local_models_setting_ui_is_opt_in_and_has_no_download_action() -> None:
    index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    main = (ROOT / "static" / "main.js").read_text(encoding="utf-8")
    manifest = (ROOT / "plugin.toml").read_text(encoding="utf-8")

    assert 'local_models_enabled = false' in manifest
    assert 'id="settingsLocalModelsEnabled"' in index_html
    assert 'id="settingsLocalModelsRuntime"' in index_html
    assert 'id="settingsLocalModelsNotInstalled"' in index_html
    assert "local_models_enabled" in main
    assert "renderLocalModelsRuntime" in main
    assert "/models/install" not in index_html + main
    section_start = index_html.index('<section class="local-models-setting"')
    local_models_section = index_html[
        section_start : index_html.index("</section>", section_start)
    ]
    assert "<button" not in local_models_section


def test_local_models_setting_is_localized_for_english_and_chinese() -> None:
    keys = {
        "ui.settings.local_models.title",
        "ui.settings.local_models.enabled.label",
        "ui.settings.local_models.enabled.help",
        "ui.settings.local_models.status.not_started",
        "ui.settings.local_models.status.starting",
        "ui.settings.local_models.status.ready",
        "ui.settings.local_models.status.error",
        "ui.settings.local_models.not_installed",
    }
    for locale in ("en", "zh-CN", "zh-TW"):
        messages = json.loads((ROOT / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
        assert all(str(messages.get(key, "")).strip() for key in keys), locale
