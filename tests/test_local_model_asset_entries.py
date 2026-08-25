from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _SdkError(Exception):
    def __init__(self, message: str, *, code: str = "") -> None:
        self.code = code
        super().__init__(message)


def _load_entry_module(monkeypatch: pytest.MonkeyPatch, package_name: str):
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    models = importlib.import_module(f"{package_name}.models")

    entry_common = ModuleType(f"{package_name}.entry_common")
    entry_common.Err = lambda value: {"error": value}  # type: ignore[attr-defined]
    entry_common.Ok = lambda value: value  # type: ignore[attr-defined]
    entry_common.SdkError = _SdkError  # type: ignore[attr-defined]
    entry_common.plugin_entry = lambda **_kwargs: lambda function: function  # type: ignore[attr-defined]
    entry_common.tr = lambda _key, default="": default  # type: ignore[attr-defined]
    entry_common.ui = SimpleNamespace(action=lambda: lambda function: function)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, entry_common.__name__, entry_common)
    module = importlib.import_module(f"{package_name}.entry_local_model_entries")
    return module, models, package_name


def test_license_url_sanitizer_rejects_credentials_queries_and_invalid_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _models, _package_name = _load_entry_module(monkeypatch, "_local_assets_url_safety")

    assert module._safe_https_url("https://licenses.example/model")
    assert module._safe_https_url("https://licenses.example:443/model")
    assert module._safe_https_url("https://user@licenses.example/model") == ""
    assert module._safe_https_url("https://licenses.example/model?token=secret") == ""
    assert module._safe_https_url("https://licenses.example:invalid/model") == ""


class _Package:
    package_id = "math-basic"
    version = "1.0.0"
    role = "reasoner"
    total_size_bytes = 123
    license = SimpleNamespace(name="MIT", requires_acceptance=False)


class _Manager:
    instances: list["_Manager"] = []

    def __init__(self, directory=None, logger=None) -> None:
        self.directory = directory
        self.logger = logger
        self.catalog_calls = 0
        self.status_calls = 0
        self.install_calls: list[tuple[str, str]] = []
        self.actions: list[tuple[str, tuple[object, ...]]] = []
        self.shutdown_calls = 0
        _Manager.instances.append(self)

    async def catalog(self):
        self.catalog_calls += 1
        return SimpleNamespace(packages=(_Package(),))

    async def status(self):
        self.status_calls += 1
        return {
            "directory": str(self.directory or "default"),
            "installed": [],
            "downloads": [],
            "disk": {"free_bytes": 999},
        }

    async def install(self, package_id, version, *, license_accepted=False):
        self.install_calls.append((package_id, version))
        return {"state": "installing"}

    async def pause(self, package_id, version):
        self.actions.append(("pause", (package_id, version)))
        return {"state": "paused"}

    async def resume(self, package_id, version):
        self.actions.append(("resume", (package_id, version)))
        return {"state": "installing"}

    async def cancel(self, package_id, version):
        self.actions.append(("cancel", (package_id, version)))
        return {"state": "cancelled"}

    async def uninstall(self, package_id, version):
        self.actions.append(("uninstall", (package_id, version)))
        return {"state": "removed"}

    async def set_directory(self, directory):
        self.directory = directory
        self.actions.append(("set_directory", (directory,)))

    async def shutdown(self):
        self.shutdown_calls += 1


class _ForbiddenAgent:
    def __getattr__(self, name):
        raise AssertionError(f"asset management touched the inference agent: {name}")


class _PluginConfig:
    def __init__(self, owner) -> None:
        self._owner = owner

    async def set(self, path, value, **_kwargs) -> None:
        self._owner.plugin_config_updates.append((path, value))


class _Owner:
    def __init__(self, mixin, config) -> None:
        self.__class__ = type(
            "AssetOwner",
            (mixin,),
            {
                "_apply_runtime_settings_config": _Owner._apply_runtime_settings_config,
                "_persist_state": _Owner._persist_state,
            },
        )
        self._cfg = config
        self._agent = _ForbiddenAgent()
        self._local_model_manager = None
        self._local_model_catalog_cache = []
        self._local_model_manager_error = ""
        self.logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
        self.applied = []
        self.persisted = 0
        self.plugin_config_updates = []
        self.config = _PluginConfig(self)

    def _apply_runtime_settings_config(self, config) -> None:
        self._cfg = config
        self.applied.append(config)

    async def _persist_state(self) -> None:
        self.persisted += 1


@pytest.mark.asyncio
async def test_asset_catalog_and_status_never_touch_inference_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, models, package_name = _load_entry_module(monkeypatch, "_local_assets_entries")
    manager_module = ModuleType(f"{package_name}.local_model_download_manager")
    manager_module.LocalModelDownloadManager = _Manager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, manager_module.__name__, manager_module)
    _Manager.instances.clear()
    owner = _Owner(module._LocalModelEntriesMixin, models.StudyConfig())

    await owner._initialize_local_model_manager()
    catalog = await owner.study_local_models_catalog()
    status = await owner.study_local_models_status()

    manager = _Manager.instances[-1]
    assert catalog["packages"] == [
        {
            "id": "math-basic",
            "version": "1.0.0",
            "role": "reasoner",
            "size_bytes": 123,
            "requires_license_acceptance": False,
            "license": "MIT",
            "license_url": "",
        }
    ]
    assert status["state"] == "ready"
    assert status["error_code"] == ""
    assert "directory" not in status
    assert manager.catalog_calls >= 2
    # Initialization performs exactly one local recovery/status check; the
    # explicit status entry is the second check. Neither touches inference.
    assert manager.status_calls == 2
    assert manager.install_calls == []
    assert manager.actions == []


@pytest.mark.asyncio
async def test_startup_status_failure_is_safe_and_does_not_block_asset_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, models, package_name = _load_entry_module(monkeypatch, "_local_assets_status_failure")

    class _StatusFailureManager(_Manager):
        async def status(self):
            self.status_calls += 1
            error = _SdkError("C:/private/models", code="local_model_store_unavailable")
            raise error

    manager_module = ModuleType(f"{package_name}.local_model_download_manager")
    manager_module.LocalModelDownloadManager = _StatusFailureManager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, manager_module.__name__, manager_module)
    _Manager.instances.clear()
    owner = _Owner(module._LocalModelEntriesMixin, models.StudyConfig())

    await owner._initialize_local_model_manager()

    manager = _Manager.instances[-1]
    assert owner._local_model_manager is manager
    assert owner._local_model_manager_error == "local_model_store_unavailable"
    assert manager.catalog_calls == 1
    assert manager.status_calls == 1
    assert manager.install_calls == []
    assert manager.actions == []


@pytest.mark.asyncio
async def test_install_requires_confirmation_and_directory_updates_without_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, models, package_name = _load_entry_module(monkeypatch, "_local_assets_actions")
    manager_module = ModuleType(f"{package_name}.local_model_download_manager")
    manager_module.LocalModelDownloadManager = _Manager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, manager_module.__name__, manager_module)
    _Manager.instances.clear()
    owner = _Owner(module._LocalModelEntriesMixin, models.StudyConfig())
    await owner._initialize_local_model_manager()
    manager = _Manager.instances[-1]

    denied = await owner.study_local_model_install("math-basic", "1.0.0")
    assert denied["error"].code == "local_model_install_failed"
    assert manager.install_calls == []

    installed = await owner.study_local_model_install("math-basic", "1.0.0", confirmed=True, license_accepted=False)
    assert installed["result"] == {"state": "installing"}
    assert manager.install_calls == [("math-basic", "1.0.0")]

    directory = str(tmp_path / "local-models")
    updated = await owner.study_local_models_set_directory(directory)
    assert updated["config"]["local_models_directory"] == directory
    assert owner._cfg.local_models_directory == directory
    assert owner.persisted == 1
    assert owner.plugin_config_updates == [("llm.local_models_directory", directory)]
    assert ("set_directory", (Path(directory),)) in manager.actions


@pytest.mark.asyncio
async def test_asset_manager_shutdown_is_explicit_and_does_not_shutdown_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, models, package_name = _load_entry_module(monkeypatch, "_local_assets_shutdown")
    manager_module = ModuleType(f"{package_name}.local_model_download_manager")
    manager_module.LocalModelDownloadManager = _Manager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, manager_module.__name__, manager_module)
    _Manager.instances.clear()
    owner = _Owner(module._LocalModelEntriesMixin, models.StudyConfig())
    await owner._initialize_local_model_manager()
    manager = _Manager.instances[-1]

    await owner._shutdown_local_model_manager()

    assert manager.shutdown_calls == 1
    assert owner._local_model_manager is None
