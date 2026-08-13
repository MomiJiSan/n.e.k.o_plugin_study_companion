# 猫娘伴学 / Study Companion

N.E.K.O 的自适应学习插件，提供 OCR 识别、题目讲解与出题、答案评估、
TXT/Markdown 文档分析、知识图谱范围练习、FSRS 记忆卡组、学习目标、
番茄钟和学习会话总结。

## 主要功能

- 使用当前 N.E.K.O `agent` 模型组中的 Qwen 模型完成学习分析。
- 支持普通文本、图片题目以及 TXT/Markdown 文档分析。
- 支持长文档结构化分段、章节分析、进度查看与全局归并。
- 支持知识图谱引导练习、记忆卡组、笔记、目标、打卡和专注计时。
- Hosted 管理面板和兼容静态页面均提供八语言界面。

## 隐私边界

- 文档原文默认不写入 SQLite、学习事件或导出记录。
- 文档分析时，正文会发送到用户在 N.E.K.O 中配置的 Qwen 服务。
- 插件不包含 API Key；模型凭证由 N.E.K.O 的模型配置统一管理。

## 运行要求

- N.E.K.O Plugin SDK `>=0.1.0,<0.3.0`。
- OCR 为可选能力；RapidOCR/Tesseract 的可用性和安装提示会在插件设置中展示。
- 视觉解释需要在 N.E.K.O 中启用并配置视觉模型。

## Development

This repository is meant to live at:

```text
N.E.K.O/plugin/plugins/study_companion
```

When publishing to the plugin market, use this GitHub repository name:

```text
n.e.k.o_plugin_study_companion
```

From this plugin repository root:

```bash
uvx ruff==0.12.4 check --ignore-noqa --config ruff.toml .
```

From the N.E.K.O repository root:

```bash
uv run --with pip python -m plugin.neko_plugin_cli.cli sync study_companion --clean
uv run python -m plugin.neko_plugin_cli.cli check study_companion
uv run python -m plugin.neko_plugin_cli.cli check -r study_companion
```

Python runtime dependencies are declared in `pyproject.toml` and synced into
`vendor/` for packaging. The generated `vendor/` directory is not committed;
local builds and CI recreate it before release checks.

## Market release

Push a tag matching `plugin.toml` version to create a GitHub Release asset:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The generated `.github/workflows/release.yml` uploads `study_companion.neko-plugin`.
Use that GitHub Release URL when publishing a version in the plugin market.

## Entry

```toml
entry = "plugin.plugins.study_companion:StudyCompanionPlugin"
```
