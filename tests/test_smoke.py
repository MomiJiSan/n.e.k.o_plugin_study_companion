import ast
import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _module_level_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    symbols: set[str] = set()

    def collect(statements: list[ast.stmt]) -> None:
        for node in statements:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                symbols.update(
                    alias.asname or alias.name.split(".")[0]
                    for alias in node.names
                    if alias.name != "*"
                )
            elif isinstance(node, ast.Assign):
                symbols.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                symbols.add(node.target.id)
            elif isinstance(node, ast.If):
                collect(node.body)
                collect(node.orelse)
            elif isinstance(node, ast.Try):
                collect(node.body)
                collect(node.orelse)
                collect(node.finalbody)
                for handler in node.handlers:
                    collect(handler.body)

    collect(tree.body)
    return symbols


def test_plugin_manifest_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = root / "plugin.toml"
    assert manifest.is_file()
    text = manifest.read_text(encoding="utf-8")
    assert 'id = "study_companion"' in text
    assert 'entry = "plugin.plugins.study_companion:StudyCompanionPlugin"' in text


def test_store_common_imports_memory_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]

    plugin_package = ModuleType("plugin")
    plugin_package.__path__ = []  # type: ignore[attr-defined]
    sdk_package = ModuleType("plugin.sdk")
    sdk_package.__path__ = []  # type: ignore[attr-defined]
    shared_package = ModuleType("plugin.sdk.shared")
    shared_package.__path__ = []  # type: ignore[attr-defined]
    i18n_module = ModuleType("plugin.sdk.shared.i18n")
    i18n_module.load_plugin_i18n_from_dir = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "plugin", plugin_package)
    monkeypatch.setitem(sys.modules, "plugin.sdk", sdk_package)
    monkeypatch.setitem(sys.modules, "plugin.sdk.shared", shared_package)
    monkeypatch.setitem(sys.modules, "plugin.sdk.shared.i18n", i18n_module)

    package_name = "_study_companion_import_test"
    package = ModuleType(package_name)
    package.__path__ = [str(root)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    store_common = importlib.import_module(f"{package_name}.store_common")
    memory_schema = importlib.import_module(f"{package_name}.memory_schema")

    assert store_common.ensure_memory_schema is memory_schema.ensure_memory_schema


def test_local_relative_imports_reference_exported_names() -> None:
    root = Path(__file__).resolve().parents[1]
    unresolved: list[str] = []

    for consumer in root.glob("*.py"):
        tree = ast.parse(consumer.read_text(encoding="utf-8"), filename=str(consumer))
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
                continue
            provider = root.joinpath(*node.module.split(".")).with_suffix(".py")
            if not provider.is_file():
                continue
            available = _module_level_symbols(provider)
            for alias in node.names:
                if alias.name != "*" and alias.name not in available:
                    unresolved.append(
                        f"{consumer.name}:{node.lineno} imports missing "
                        f"{node.module}.{alias.name}"
                    )

    assert not unresolved, "\n".join(unresolved)
