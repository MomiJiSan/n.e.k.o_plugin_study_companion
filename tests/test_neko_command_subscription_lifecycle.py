from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_commands(monkeypatch: pytest.MonkeyPatch, package_name: str):
    plugin = ModuleType("plugin")
    plugin.__path__ = []  # type: ignore[attr-defined]
    sdk = ModuleType("plugin.sdk")
    sdk.__path__ = []  # type: ignore[attr-defined]
    sdk_plugin = ModuleType("plugin.sdk.plugin")

    class Err:
        def __init__(self, error: object) -> None:
            self.error = error

    class Ok:
        def __init__(self, value: object) -> None:
            self.value = value

    class SdkError(Exception):
        pass

    sdk_plugin.Err = Err  # type: ignore[attr-defined]
    sdk_plugin.Ok = Ok  # type: ignore[attr-defined]
    sdk_plugin.SdkError = SdkError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "plugin", plugin)
    monkeypatch.setitem(sys.modules, "plugin.sdk", sdk)
    monkeypatch.setitem(sys.modules, "plugin.sdk.plugin", sdk_plugin)

    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    adaptive = ModuleType(f"{package_name}.adaptive_learning")
    adaptive.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, adaptive.__name__, adaptive)
    learner_state = ModuleType(f"{package_name}.adaptive_learning.learner_state")
    learner_state.tracker_list_mastery_overview = lambda *_args, **_kwargs: []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, learner_state.__name__, learner_state)

    constants = ModuleType(f"{package_name}.constants")
    constants.MODE_COMPANION = "companion"  # type: ignore[attr-defined]
    constants.MODE_INTERACTIVE = "interactive"  # type: ignore[attr-defined]
    constants.MODE_TEACHING = "teaching"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, constants.__name__, constants)

    entry_common = ModuleType(f"{package_name}.entry_common")
    entry_common._plugin_lock = lambda _owner: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, entry_common.__name__, entry_common)
    return importlib.import_module(f"{package_name}.entry_neko_commands")


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[tuple[object, ...]] = []

    def warning(self, *args: object) -> None:
        self.warnings.append(args)


class _Watcher:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def subscribe(self, **_kwargs):
        return lambda callback: callback

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _Messages:
    def __init__(self, watcher: _Watcher) -> None:
        self.watcher = watcher

    def watch(self, *_args, **_kwargs) -> _Watcher:
        return self.watcher


@pytest.mark.asyncio
async def test_subscription_is_deferred_and_reports_messages_bus_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_commands(monkeypatch, "_deferred_neko_subscription")
    monkeypatch.setattr(module, "_NEKO_COMMAND_SUBSCRIBE_DELAY_SECONDS", 0)
    watcher = _Watcher()
    get_called = asyncio.Event()

    class Bus:
        async def get(self, **_kwargs) -> _Messages:
            get_called.set()
            return _Messages(watcher)

    class Owner(module._NekoCommandsMixin):
        async def _on_neko_command(self, _payload):
            return None

    owner = Owner()
    owner.ctx = SimpleNamespace(bus=SimpleNamespace(messages=Bus()))
    owner.logger = _Logger()
    owner._neko_command_transport = None
    owner._neko_command_handler = None
    owner._neko_command_watcher = None
    owner._neko_command_subscription_task = None

    owner._schedule_neko_command_subscription()
    task = owner._neko_command_subscription_task

    assert task is not None
    assert not get_called.is_set()
    assert owner._neko_command_subscription_status == "pending"
    await task
    await asyncio.sleep(0)

    assert get_called.is_set()
    assert watcher.started is True
    assert owner._neko_command_subscription_status == "messages_bus"
    assert owner._neko_command_subscription_error == ""
    assert owner._neko_command_subscription_task is None


@pytest.mark.asyncio
async def test_pending_subscription_is_cancelled_before_shutdown_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_commands(monkeypatch, "_cancelled_neko_subscription")
    monkeypatch.setattr(module, "_NEKO_COMMAND_SUBSCRIBE_DELAY_SECONDS", 0)
    get_called = asyncio.Event()

    class Bus:
        async def get(self, **_kwargs):
            get_called.set()
            await asyncio.Event().wait()

    class Owner(module._NekoCommandsMixin):
        pass

    owner = Owner()
    owner.ctx = SimpleNamespace(bus=SimpleNamespace(messages=Bus()))
    owner.logger = _Logger()
    owner._neko_command_transport = None
    owner._neko_command_handler = None
    owner._neko_command_watcher = None
    owner._neko_command_subscription_task = None

    owner._schedule_neko_command_subscription()
    task = owner._neko_command_subscription_task
    assert task is not None
    await asyncio.wait_for(get_called.wait(), timeout=1)

    await owner._cancel_neko_command_subscription_task()

    assert task.cancelled()
    assert owner._neko_command_subscription_task is None
    assert owner._neko_command_subscription_status == "inactive"
    assert owner._neko_command_subscription_error == ""
