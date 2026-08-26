from __future__ import annotations

import importlib
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


def _load_status_entries(monkeypatch: pytest.MonkeyPatch, package_name: str, models):
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


def _load_store(monkeypatch: pytest.MonkeyPatch, package_name: str):
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{package_name}.store").StudyStore


class _Logger:
    def debug(self, *_args, **_kwargs) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


def _settings_owner(entries, config):
    class Owner(entries._StatusEntriesMixin):
        def __init__(self) -> None:
            self._cfg = config
            self._event_bus = None
            self._ocr_pipeline = None
            self._agent = None
            self._pomodoro_timer = None
            self._supervision = None
            self._checkin_manager = None
            self.logger = _Logger()
            self.persisted = 0

        async def _refresh_dependency_status(self) -> None:
            return None

        async def _persist_state(self) -> None:
            self.persisted += 1

    return Owner()


def test_relationship_retrieval_v2_config_uses_canonical_and_legacy_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, _package_name = _load_models(monkeypatch, "_relationship_v2_config")

    assert models.build_config({}).knowledge_relation_retrieval_v2_enabled is False
    assert models.build_config(
        {"knowledge_retrieval": {"relationship_v2_enabled": True}}
    ).knowledge_relation_retrieval_v2_enabled is True
    assert models.build_config(
        {"knowledge_relation_retrieval_v2_enabled": True}
    ).knowledge_relation_retrieval_v2_enabled is True
    assert models.build_config(
        {"knowledge_retrieval": {"relationship_v2_enabled": "true"}}
    ).knowledge_relation_retrieval_v2_enabled is False


def test_relationship_retrieval_v2_settings_payload_and_persistence_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, package_name = _load_models(monkeypatch, "_relationship_v2_settings")
    entries = _load_status_entries(monkeypatch, package_name, models)
    current = models.StudyConfig()

    assert entries._settings_config_payload(current)["knowledge_retrieval"] == {
        "relationship_v2_enabled": False
    }
    enabled = entries._apply_settings_config(
        current,
        {"knowledge_retrieval": {"relationship_v2_enabled": True}},
    )
    assert enabled.knowledge_relation_retrieval_v2_enabled is True
    assert entries._settings_config_payload(enabled)["knowledge_retrieval"] == {
        "relationship_v2_enabled": True
    }
    reloaded = models.build_config(enabled.to_dict())
    assert reloaded.knowledge_relation_retrieval_v2_enabled is True

    invalid = entries._apply_settings_config(
        current,
        {"knowledge_retrieval": {"relationship_v2_enabled": "not-a-boolean"}},
    )
    assert invalid.knowledge_relation_retrieval_v2_enabled is False


def test_relationship_retrieval_v2_setting_persists_through_store_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    models, _package_name = _load_models(monkeypatch, "_relationship_v2_store_models")
    Store = _load_store(monkeypatch, "_relationship_v2_store")
    store = Store(tmp_path / "study.db", tmp_path / "seed.json", _Logger())
    store.open()
    try:
        store.save_config(
            models.StudyConfig(knowledge_relation_retrieval_v2_enabled=True)
        )
        reloaded = store.load_config(models.StudyConfig())
        assert reloaded.knowledge_relation_retrieval_v2_enabled is True
    finally:
        store.close()


@pytest.mark.asyncio
async def test_relationship_retrieval_v2_setting_updates_through_settings_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, package_name = _load_models(monkeypatch, "_relationship_v2_settings_api")
    entries = _load_status_entries(monkeypatch, package_name, models)
    owner = _settings_owner(entries, models.StudyConfig())

    response = await owner.study_update_settings_config(
        config={"knowledge_retrieval": {"relationship_v2_enabled": True}}
    )

    assert owner._cfg.knowledge_relation_retrieval_v2_enabled is True
    assert owner.persisted == 1
    assert response["config"]["knowledge_retrieval"] == {
        "relationship_v2_enabled": True
    }
