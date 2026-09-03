from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_service(monkeypatch: pytest.MonkeyPatch):
    for name in ("plugin", "plugin.sdk", "plugin.sdk.shared"):
        namespace = ModuleType(name)
        namespace.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, namespace)
    i18n_module = ModuleType("plugin.sdk.shared.i18n")
    i18n_module.load_plugin_i18n_from_dir = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, i18n_module.__name__, i18n_module)

    package_name = f"_service_payloads_{id(monkeypatch)}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    return importlib.import_module(f"{package_name}.service")


def test_build_tutor_payload_accepts_runtime_namespace_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _load_service(monkeypatch)
    reply = SimpleNamespace(
        operation="answer_evaluate",
        input_text="2π/3",
        reply="回答正确",
        payload={"verdict": "correct", "score": 100},
        degraded=False,
        diagnostic="",
        created_at="2026-09-03T00:00:00Z",
    )

    payload = service.build_tutor_payload(reply)

    assert payload["summary"] == "回答正确"
    assert payload["verdict"] == "correct"
    assert payload["score"] == 100
    assert payload["created_at"] == "2026-09-03T00:00:00Z"
