from __future__ import annotations

import importlib
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
_HOST_VALUE_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")


@dataclass(frozen=True)
class _SdkContext:
    app_id: str
    client_id: str
    session_id: str
    scope: str
    operation: str

    @classmethod
    def from_mapping(cls, value: dict[str, object]) -> "_SdkContext":
        required = {"app_id", "client_id", "session_id", "scope", "operation"}
        if set(value) != required:
            raise ValueError("invalid fields")
        fields: dict[str, str] = {}
        for name in required:
            raw = value[name]
            if not isinstance(raw, str) or _HOST_VALUE_PATTERN.fullmatch(raw) is None:
                raise ValueError(f"invalid {name}")
            fields[name] = raw
        return cls(**fields)


def _trusted_operation(operation: str):
    def decorator(function: Any) -> Any:
        setattr(function, "__neko_trusted_local_app_operation__", operation)
        return function

    return decorator


def _load_binding(monkeypatch: pytest.MonkeyPatch, *, with_new_sdk: bool) -> ModuleType:
    plugin = ModuleType("plugin")
    plugin.__path__ = []  # type: ignore[attr-defined]
    sdk = ModuleType("plugin.sdk")
    sdk.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "plugin", plugin)
    monkeypatch.setitem(sys.modules, "plugin.sdk", sdk)
    monkeypatch.delitem(sys.modules, "plugin.sdk.local_app", raising=False)
    if with_new_sdk:
        local_app = ModuleType("plugin.sdk.local_app")
        local_app.TrustedLocalAppPluginContext = _SdkContext  # type: ignore[attr-defined]
        local_app.trusted_local_app_operation = _trusted_operation  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "plugin.sdk.local_app", local_app)

    package_name = f"_study_local_app_binding_{with_new_sdk}_{id(monkeypatch)}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    return importlib.import_module(f"{package_name}.entry_knowledge_dungeon_local_app")


def _context(operation: str, **overrides: str) -> _SdkContext:
    values = {
        "app_id": "knowledge_dungeon",
        "client_id": "electron-client-test",
        "session_id": "session-001",
        "scope": "study_companion:dungeon",
        "operation": operation,
        **overrides,
    }
    return _SdkContext(**values)


def test_manifest_declares_only_the_four_frozen_local_app_operations() -> None:
    with (ROOT / "plugin.toml").open("rb") as stream:
        local_app = tomllib.load(stream)["plugin"]["local_app"]

    operations = {
        "knowledge_dungeon.bootstrap": "knowledge_dungeon.bootstrap",
        "knowledge_dungeon.create_run": "knowledge_dungeon.create_run",
        "knowledge_dungeon.get_run": "knowledge_dungeon.get_run",
        "knowledge_dungeon.perform_action": "knowledge_dungeon.perform_action",
    }
    assert local_app == {
        "app_id": "knowledge_dungeon",
        "scope": "study_companion:dungeon",
        "operations": operations,
    }


@pytest.mark.asyncio
async def test_new_sdk_binding_validates_context_and_forwards_four_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_binding(monkeypatch, with_new_sdk=True)
    calls: list[tuple[Path, Any, str, dict[str, object]]] = []

    class _Outcome:
        def __init__(self, operation: str) -> None:
            self.operation = operation

        def to_dict(self) -> dict[str, object]:
            return {"ok": True, "operation": self.operation}

    class _Adapter:
        def __init__(self, path: Path) -> None:
            self.path = path

        async def invoke(self, context: object, operation: str, payload: dict[str, object]) -> _Outcome:
            calls.append((self.path, context, operation, dict(payload)))
            return _Outcome(operation)

    monkeypatch.setattr(module, "KnowledgeDungeonHostAdapter", _Adapter)

    class _Owner(module._KnowledgeDungeonLocalAppMixin):
        def data_path(self, filename: str) -> Path:
            return tmp_path / filename

    owner = _Owner()
    methods = {
        "knowledge_dungeon.bootstrap": (owner.knowledge_dungeon_bootstrap, "bootstrap"),
        "knowledge_dungeon.create_run": (owner.knowledge_dungeon_create_run, "create_run"),
        "knowledge_dungeon.get_run": (owner.knowledge_dungeon_get_run, "get_run"),
        "knowledge_dungeon.perform_action": (
            owner.knowledge_dungeon_perform_action,
            "perform_action",
        ),
    }
    for trusted_operation, (method, adapter_operation) in methods.items():
        assert getattr(method, "__neko_trusted_local_app_operation__") == trusted_operation
        assert await method(_context(trusted_operation), {"bridge_protocol_version": 1}) == {
            "ok": True,
            "operation": adapter_operation,
        }

    assert [call[2] for call in calls] == [
        "bootstrap",
        "create_run",
        "get_run",
        "perform_action",
    ]
    assert all(call[0] == tmp_path / "knowledge_dungeon.sqlite3" for call in calls)
    assert all(call[1].client_id == "electron-client-test" for call in calls)
    assert all(not hasattr(call[1], "session_id") for call in calls)

    invalid_contexts = (
        _context("knowledge_dungeon.bootstrap", app_id="forged_app"),
        _context("knowledge_dungeon.bootstrap", client_id="bad client"),
        _context("knowledge_dungeon.bootstrap", session_id="bad session"),
        _context("knowledge_dungeon.bootstrap", scope="other:scope"),
        _context("knowledge_dungeon.get_run"),
    )
    for invalid_context in invalid_contexts:
        invalid = await owner.knowledge_dungeon_bootstrap(
            invalid_context,
            {"bridge_protocol_version": 1},
        )
        assert invalid["category"] == "protocol"
        assert invalid["error"]["code"] == "untrusted_local_app_context"  # type: ignore[index]
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_old_sdk_imports_fail_closed_without_exposing_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_binding(monkeypatch, with_new_sdk=False)

    assert module._TRUSTED_LOCAL_APP_SDK_AVAILABLE is False
    assert not hasattr(
        module._KnowledgeDungeonLocalAppMixin.knowledge_dungeon_bootstrap,
        "__neko_trusted_local_app_operation__",
    )

    class _Owner(module._KnowledgeDungeonLocalAppMixin):
        def data_path(self, filename: str) -> Path:
            return tmp_path / filename

    outcome = await _Owner().knowledge_dungeon_bootstrap(
        object(),
        {"bridge_protocol_version": 1},
    )
    assert outcome["category"] == "protocol"
    assert outcome["error"]["code"] == "local_app_sdk_unavailable"  # type: ignore[index]
