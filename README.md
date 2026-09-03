# 猫娘伴学 / Study Companion

N.E.K.O 的自适应学习插件，提供 OCR 识别、题目讲解与出题、答案评估、
TXT/Markdown 文档分析、知识图谱范围练习、FSRS 记忆卡组、学习目标、
番茄钟和学习会话总结。

## 主要功能

- 使用当前 N.E.K.O `agent` 模型组完成文本与文档学习分析，支持
  OpenAI-compatible 和 Anthropic 协议；Qwen、OpenAI、OpenRouter
  及兼容接口的自定义模型均可使用。
- 支持普通文本、图片题目以及 TXT/Markdown 文档分析。
- 支持长文档结构化分段、章节分析、进度查看与全局归并。
- 支持知识图谱引导练习、记忆卡组、笔记、目标、打卡和专注计时。
- Hosted 管理面板和兼容静态页面均提供八语言界面。

## 隐私边界

- 文档原文默认不写入 SQLite、学习事件或导出记录。
- 文档分析时，正文会发送到用户在 N.E.K.O `agent` 模型组中配置的模型服务。
- 插件不包含 API Key；模型凭证由 N.E.K.O 的模型配置统一管理。

## 运行要求

- N.E.K.O Plugin SDK `>=0.1.0,<0.3.0`。
- 伴学使用内置文字识别读取截图和文档页面，无需额外安装识别软件。
- 可在插件设置中选择中文、日文、韩文或英文；需要补充语言支持时，界面会直接提示下载。
- 视觉解释使用 N.E.K.O `vision` 模型组，需要启用并配置支持图片输入的视觉模型。
- 当前支持 OpenAI-compatible 和 Anthropic 提供商协议；其他原生协议需通过兼容接口接入。

## Development

项目模块边界、主要执行链路和修改导航见
[`docs/PROJECT_MAP.md`](docs/PROJECT_MAP.md)。

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
uv sync --group dev
uv run python -m pytest -q
uvx ruff==0.12.4 check --ignore-noqa --config ruff.toml .
```

Frontend DOM tests additionally require Node.js 22 and the locked test dependencies:

```bash
cd tests/frontend
npm ci
cd ../..
uv run python -m pytest \
  tests/test_workspace_frontend.py \
  tests/test_notebook_frontend.py \
  tests/test_scanned_pdf_frontend.py \
  tests/test_scanned_pdf_surface.py \
  -q
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
git tag v0.2.1
git push origin v0.2.1
```

The generated `.github/workflows/release.yml` uploads `study_companion.neko-plugin`.
Use that GitHub Release URL when publishing a version in the plugin market.

## Entry

```toml
entry = "plugin.plugins.study_companion:StudyCompanionPlugin"
```
