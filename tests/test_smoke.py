import ast
import importlib
import json
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


def _relative_module_path(root: Path, module: str) -> Path | None:
    candidate = root.joinpath(*module.split("."))
    module_file = candidate.with_suffix(".py")
    if module_file.is_file():
        return module_file
    package_file = candidate / "__init__.py"
    return package_file if package_file.is_file() else None


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
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue

            if node.module is None:
                package_symbols = _module_level_symbols(root / "__init__.py")
                for alias in node.names:
                    if (
                        _relative_module_path(root, alias.name) is None
                        and alias.name not in package_symbols
                    ):
                        unresolved.append(
                            f"{consumer.name}:{node.lineno} imports missing .{alias.name}"
                        )
                continue

            provider = _relative_module_path(root, node.module)
            if provider is None:
                unresolved.append(
                    f"{consumer.name}:{node.lineno} imports missing module .{node.module}"
                )
                continue

            available = _module_level_symbols(provider)
            for alias in node.names:
                if alias.name != "*" and alias.name not in available:
                    unresolved.append(
                        f"{consumer.name}:{node.lineno} imports missing "
                        f"{node.module}.{alias.name}"
                    )

    assert not unresolved, "\n".join(unresolved)


def test_knowledge_map_payload_marks_edge_templates_for_ui_i18n(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    package_name = "_study_companion_ui_test"
    package = ModuleType(package_name)
    package.__path__ = [str(root)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    ui_api = importlib.import_module(f"{package_name}.ui_api")

    payload = ui_api.build_knowledge_map_payload(
        topics=[
            {
                "id": "func",
                "name": "函数概念",
                "subject": "math",
                "related": [
                    {
                        "id": "params",
                        "relation": "co_occurs",
                        "reason": '"函数与参数传递" are often practiced together in application problems.',
                    },
                    {
                        "id": "number_sense",
                        "relation": "supports",
                        "reason": (
                            "Number sense and place value anchor the later classification "
                            "of rational, irrational, and real numbers."
                        ),
                    },
                    {
                        "id": "foundation",
                        "relation": "related",
                        "reason": "Foundation concept",
                    },
                ],
            },
            {"id": "params", "name": "函数与参数传递", "subject": "math"},
            {"id": "number_sense", "name": "数感", "subject": "math"},
            {"id": "foundation", "name": "基础概念", "subject": "math"},
            {
                "id": "linear_func",
                "name": "一次函数",
                "subject": "math",
                "question_types": [
                    "concept_check",
                    None,
                    " applied calculation ",
                    "application",
                ],
                "prerequisites": [
                    {
                        "id": "func",
                        "relation": "prerequisite",
                        "reason": (
                            "Master 函数概念 before 一次函数; it supplies the "
                            "foundation for this math learning path."
                        ),
                    }
                ],
            },
            {
                "id": "college_cs_modular_design",
                "name": "模块化程序设计",
                "subject": "computer_science",
                "stage": "college",
                "course_family": "c_programming",
                "chapter": "计算机基础",
                "unit": "程序设计方法",
            },
        ],
    )

    linear_func = next(node for node in payload["nodes"] if node["id"] == "linear_func")
    assert linear_func["question_types"] == ["concept_check", "applied calculation", "application"]
    college_topic = next(
        node for node in payload["nodes"] if node["id"] == "college_cs_modular_design"
    )
    assert college_topic["course_family"] == "c_programming"

    edges_by_relation = {edge["relation"]: edge for edge in payload["edges"]}
    assert edges_by_relation["prerequisite"]["reason_template"] == "prerequisite"
    assert "reason" not in edges_by_relation["prerequisite"]
    assert edges_by_relation["co_occurs"]["reason_template"] == "co_occurs"
    assert "reason" not in edges_by_relation["co_occurs"]
    assert edges_by_relation["supports"]["reason_template"] == "supports"
    assert "reason" not in edges_by_relation["supports"]
    assert edges_by_relation["related"]["reason_template"] == "related"
    assert "reason" not in edges_by_relation["related"]


def test_knowledge_map_payload_keeps_chinese_reason_with_latin_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    package_name = "_study_companion_ui_latin_term_test"
    package = ModuleType(package_name)
    package.__path__ = [str(root)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    ui_api = importlib.import_module(f"{package_name}.ui_api")

    reason = "分析 DNA replication fork 的形成条件，并结合复制方向判断其稳定机制。"
    payload = ui_api.build_knowledge_map_payload(
        topics=[
            {
                "id": "dna",
                "name": "DNA 复制",
                "subject": "biology",
                "related": [{"id": "transcription", "relation": "application", "reason": reason}],
            },
            {"id": "transcription", "name": "转录与翻译", "subject": "biology"},
        ],
    )

    assert payload["edges"][0]["reason"] == reason
    assert "reason_template" not in payload["edges"][0]


def test_knowledge_map_i18n_keys_are_complete_for_all_locales() -> None:
    root = Path(__file__).resolve().parents[1]
    locales = ("en", "ja", "ko", "zh-CN", "zh-TW", "ru", "pt", "es")
    required_keys = {
        *(f"ui.knowledge.edge_priority.{value}" for value in ("core", "useful", "optional")),
        *(
            f"ui.knowledge.edge_context.{value}"
            for value in ("diagnosis", "explanation", "practice", "review")
        ),
        *(
            f"ui.knowledge.question_type.{value}"
            for value in ("concept_check", "calculation", "applied_calculation", "application")
        ),
        *(
            f"ui.knowledge.edge_reason.{value}"
            for value in (
                "prerequisite",
                "procedure_step",
                "application",
                "confusable",
                "extends",
                "co_occurs",
                "supports",
                "analogy",
                "related",
            )
        ),
        "ui.practice.attempt.correct",
        "ui.practice.attempt.partial",
        "ui.practice.attempt.wrong",
        "ui.practice.attempt.dont_know",
        "ui.practice.mastery.mastered",
        "ui.knowledge.mastery.unassessed",
        "ui.error.question_validation_failed",
        "ui.error.evaluation_inconsistent",
    }

    for locale in locales:
        translations = json.loads((root / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
        assert not required_keys - translations.keys(), locale
        for key in required_keys:
            assert translations[key].strip(), f"{locale}: {key}"


def test_both_knowledge_map_uis_localize_internal_edge_values() -> None:
    root = Path(__file__).resolve().parents[1]
    static_index = (root / "static" / "index.html").read_text(encoding="utf-8")
    static_source = (root / "static" / "knowledge-map.js").read_text(encoding="utf-8")
    hosted_source = (root / "surfaces" / "knowledge_map.tsx").read_text(encoding="utf-8")

    for source in (static_source, hosted_source):
        assert "ui.knowledge.edge_priority" in source
        assert "ui.knowledge.edge_context" in source
        assert "ui.knowledge.question_type" in source
        assert "ui.knowledge.edge_reason" in source
        assert "reason_template" in source
        assert "window.alert" not in source

    assert "knowledgeTopicPracticeScope(node)" in static_source
    assert "runKnowledgePracticeScopeAction(topicAction, topicScope)" in static_source
    assert "activatePracticeScope('explicit_topic')" in hosted_source
    assert "practiceComingSoonOpen" not in hosted_source
    assert "./style.css?v=study-settings-dialog-20260824" in static_index
    assert "./knowledge-map.js?v=study-knowledge-mastery-status-pr1-20260825" in static_index


def test_release_versions_stay_in_sync() -> None:
    root = Path(__file__).resolve().parents[1]
    plugin_version = next(
        line.split('"', 2)[1]
        for line in (root / "plugin.toml").read_text(encoding="utf-8").splitlines()
        if line.startswith("version = ")
    )
    project_version = next(
        line.split('"', 2)[1]
        for line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines()
        if line.startswith("version = ")
    )
    assert plugin_version == project_version == "0.2.1"
