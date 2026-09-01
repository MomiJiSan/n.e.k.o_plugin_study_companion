from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _manifest() -> dict:
    with (ROOT / "plugin.toml").open("rb") as manifest_file:
        return tomllib.load(manifest_file)


def test_install_manifest_declares_the_rapidocr_contract() -> None:
    install = _manifest()["plugin"]["install"]

    assert install["enabled"] is True
    assert install["ui_i18n_dir"] == "i18n"
    assert install["tutorial_enabled"] is True
    assert install["kinds"] == {
        "rapidocr_models": {
            "entry_id": "study_download_rapidocr_models",
            "label": "RapidOCR Models",
            "queued_message": "RapidOCR model download queued",
            "entry_timeout": 600.0,
        }
    }
    assert (ROOT / install["ui_i18n_dir"]).is_dir()


def test_declared_install_entry_matches_the_runtime_entry() -> None:
    install_kind = _manifest()["plugin"]["install"]["kinds"]["rapidocr_models"]
    module = ast.parse((ROOT / "entry_ocr_entries.py").read_text(encoding="utf-8"))

    matching_decorators = [
        decorator
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "plugin_entry"
        and any(
            keyword.arg == "id"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == install_kind["entry_id"]
            for keyword in decorator.keywords
        )
    ]

    assert len(matching_decorators) == 1
    metadata = {
        keyword.arg: keyword.value.value
        for keyword in matching_decorators[0].keywords
        if keyword.arg is not None and isinstance(keyword.value, ast.Constant)
    }
    assert metadata["timeout"] == install_kind["entry_timeout"] == 600.0


def test_package_has_no_host_install_registry_dependency() -> None:
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    imported_modules = {
        imported.name
        for node in ast.walk(module)
        if isinstance(node, ast.Import)
        for imported in node.names
    } | {
        node.module
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "plugin.server.install_registry" not in imported_modules
    assert "register_install_plugin" not in source
    assert "InstallKindRegistration" not in source
