# 猫娘伴学 / Study Companion

[![Python Tests](https://github.com/MomiJiSan/n.e.k.o_plugin_study_companion/actions/workflows/python-tests.yml/badge.svg)](https://github.com/MomiJiSan/n.e.k.o_plugin_study_companion/actions/workflows/python-tests.yml)
[![Frontend Tests](https://github.com/MomiJiSan/n.e.k.o_plugin_study_companion/actions/workflows/frontend-tests.yml/badge.svg)](https://github.com/MomiJiSan/n.e.k.o_plugin_study_companion/actions/workflows/frontend-tests.yml)
[![Plugin Verify](https://github.com/MomiJiSan/n.e.k.o_plugin_study_companion/actions/workflows/verify.yml/badge.svg)](https://github.com/MomiJiSan/n.e.k.o_plugin_study_companion/actions/workflows/verify.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

猫娘伴学是 [N.E.K.O](https://github.com/Project-N-E-K-O/N.E.K.O) 的自适应学习插件。它把材料导入、AI 讲解、练习批改、知识图谱、间隔复习、学习计划和专注习惯整合在同一个学习工作台中。

当前源码版本：`0.3.0` · Python `>=3.11` · N.E.K.O Plugin SDK `>=0.1.0,<0.3.0`

## 功能概览

| 能力 | 说明 |
| --- | --- |
| 多种学习输入 | 支持粘贴文本、屏幕选区 OCR、JPG/PNG 图片，以及 TXT、Markdown、PDF、DOCX 文档 |
| AI 讲解与练习 | 提供伴学、互动、教学三种模式，可生成题目、提示、批改答案并总结会话 |
| 长文档分析 | 支持结构化分段、章节分析、异步进度、取消任务、扫描 PDF OCR 和全局归并 |
| 自适应学习 | 根据学习记录、薄弱点、到期复习和知识图谱范围安排下一步练习 |
| 知识与记忆 | 提供知识图谱、笔记本、FSRS 记忆卡组、材料导入和到期复习 |
| 目标与专注 | 支持学习目标、打卡、番茄钟、习惯联动和学习会话回顾 |
| 多语言界面 | Hosted surface 与兼容静态页面支持简体中文、繁体中文、英语、日语、韩语、西班牙语、葡萄牙语和俄语 |

模型调用统一使用 N.E.K.O 的 `agent` 模型组，支持 OpenAI-compatible 与 Anthropic 协议；Qwen、OpenAI、OpenRouter 及其他兼容接口均可接入。图片理解使用单独的 `vision` 模型组。

## 安装与快速开始

### 从 Release 安装

1. 在 [Releases](https://github.com/MomiJiSan/n.e.k.o_plugin_study_companion/releases) 下载对应版本的 `study_companion.neko-plugin`。
2. 在 N.E.K.O 的插件管理界面导入或安装该文件。
3. 打开猫娘伴学，在首次引导中选择学习阶段和学习模式。
4. 在“高级设置 > 学习”确认“文本与文档模型”已就绪；需要识别截图或扫描版 PDF 时，再按页面提示准备 OCR。

### 开发目录安装

仓库应位于 N.E.K.O 源码树中的以下位置：

```text
N.E.K.O/plugin/plugins/study_companion
```

在 N.E.K.O 仓库根目录同步并检查插件：

```bash
uv run --with pip python -m plugin.neko_plugin_cli.cli sync study_companion --clean
uv run python -m plugin.neko_plugin_cli.cli check study_companion
uv run python -m plugin.neko_plugin_cli.cli check -r study_companion
```

第一次使用时，推荐按以下路线体验完整闭环：

```text
学习 > 导入或粘贴材料 > 解释 > 练习 > 批改答案 > 记忆 / 知识 / 会话总结
```

更完整的界面说明见 [`onboarding.md`](onboarding.md)，英文版见 [`onboarding.en.md`](onboarding.en.md)。

## 隐私与模型边界

- 导入文件的原文件不会由插件保留；文档原文默认不写入 SQLite、学习事件或导出记录。
- 为完成讲解或分析，必要的文本或图片会发送到用户在 N.E.K.O 中选择的模型服务。
- 插件不包含、复制或单独保存 API Key；模型与凭证由 N.E.K.O 统一管理。
- OCR 在本地读取截图和文档页面；界面会在缺少运行组件或语言模型时给出安装提示。
- 本地大语言模型功能仍在开发中。当前学习请求使用 N.E.K.O 管理的模型接口，不会由本插件启动本地推理服务。

## 认知引擎 V2

`0.3.0` 加入了可审计、可回滚的认知证据与保持检查闭环：它可以从结构化答题记录中识别受支持的错误模式，在 Shadow 模式下仅观察，或在 Active 模式下参与下一题规划。用户可以查看证据、否认、暂时忽略、删除或恢复相关判断。

该能力默认关闭，并采用独立开关控制投影、读取、题目意图、界面和保持检查。当前主动干预范围仍严格限定在已验证的链式法则场景；未知版本、积压或冲突都会回退到普通学习流程。配置示例见 [`config.example.toml`](config.example.toml)，设计与升级边界见 [`docs/releases/v0.3.0-cognitive-v2.md`](docs/releases/v0.3.0-cognitive-v2.md)。

## 知识副本原型

仓库包含 `knowledge_dungeon/` 下的 v0.1 确定性卡牌原型，可将模拟学习快照投影为卡牌并完成地图、战斗、奖励、状态哈希与重放演示：

```bash
uv run python -m knowledge_dungeon.simulator --scenario calculus_v0_1
uv run python -m knowledge_dungeon.fixture_exporter --output ../N.E.K.O-Knowledge-Dungeon/fixtures/demo-sequence.zh-CN.json
uv run python -m pytest -q tests/knowledge_dungeon
```

Electron 演示夹具现由 Python 权威引擎完整导出；格式和同步检查见 [`docs/knowledge-dungeon-v0.2-fixture-export.md`](docs/knowledge-dungeon-v0.2-fixture-export.md)。

> **注意：** 该原型尚未接入正式插件入口、真实 Mastery/FSRS/Cognitive 数据或用户数据库，也不会写回学习事实。详细验收范围见 [`docs/knowledge-dungeon-v0.1-acceptance.md`](docs/knowledge-dungeon-v0.1-acceptance.md)。

## 配置要点

主要默认配置位于 [`plugin.toml`](plugin.toml)，可选配置示例位于 [`config.example.toml`](config.example.toml)。需要特别注意：

- `study.adaptive_loop` 控制学习计划预览、材料学习计划与自动出题。
- `cognitive` 的所有行为面默认关闭，启用前请先阅读 v0.3.0 发布边界。
- `ocr_reader` 与 `rapidocr` 控制截图/文档页面识别及模型资源。
- `fsrs.retention_target` 控制记忆卡目标保持率。
- `doc_export.enabled` 默认关闭，启用后才开放笔记导出能力。

## 开发与验证

项目采用扁平 Python 插件布局，并同时维护 Hosted surfaces 与兼容静态 UI。模块边界、生命周期和修改导航见 [`docs/PROJECT_MAP.md`](docs/PROJECT_MAP.md)。

在本仓库根目录安装依赖并运行 Python 测试、类型检查和代码检查：

```bash
uv sync --locked --group dev
uv run --locked python -m pytest -q
uv run --locked python -m pyright
uv run --locked python -m pyright --project pyrightconfig.services.json
uvx ruff==0.12.4 check --ignore-noqa --config ruff.toml .
```

认知引擎发布验收：

```bash
uv run --locked python -m tools.cognitive_v2_acceptance --profile ci
```

Hosted surface 的 DOM 合同测试需要 Node.js 22：

```bash
cd tests/frontend
npm ci
cd ../..
uv run python tools/sync_study_ui_contracts.py --check
uv run python -m pytest \
  tests/test_workspace_frontend.py \
  tests/test_notebook_frontend.py \
  tests/test_scanned_pdf_frontend.py \
  tests/test_scanned_pdf_surface.py \
  -q
```

Python 运行时依赖由 `pyproject.toml` 声明，发布时会同步到 `vendor/`。生成的 `vendor/` 不提交到仓库，由本地构建或 CI 在发布检查前重新创建。

## 发布

1. 同步 `plugin.toml` 与 `pyproject.toml` 中的版本号。
2. 完成本地测试、N.E.K.O 挂载检查和 release check。
3. 推送与插件版本一致的标签，例如：

```bash
git tag v0.3.0
git push origin v0.3.0
```

`.github/workflows/release.yml` 会调用 N.E.K.O 的插件市场发布工作流，并把 `study_companion.neko-plugin` 上传到 GitHub Release。插件市场发布时应填写该 Release 资源的下载地址。

插件入口：

```toml
entry = "plugin.plugins.study_companion:StudyCompanionPlugin"
```

## License

本项目采用 [Apache License 2.0](LICENSE)。
