# 项目地图

这份文档描述 Study Companion 的稳定模块边界，帮助维护者先找到正确入口，
再阅读具体实现。仓库当前采用扁平 Python 模块布局，这是 N.E.K.O 插件入口和打包路径的一部分；
不要仅为了目录美观移动这些模块。

## 总体结构

| 区域 | 主要文件 | 职责 |
| --- | --- | --- |
| 插件装配与生命周期 | `__init__.py`, `plugin.toml` | 组装所有 entry mixin，初始化存储、模型、OCR、事件与后台任务 |
| 对外能力入口 | `entry_*.py` | N.E.K.O entry、命令、状态、学习、OCR、记忆、目标等边界适配 |
| 辅导与推理 | `tutor_llm_agent*.py`, `study_model_gateway.py`, `study_inference_router.py`, `llm_prompts.py` | 构建提示词、选择运行时、调用模型并规范化结构化结果 |
| 学习状态与知识图谱 | `knowledge_tracker.py`, `knowledge_graph_*.py`, `knowledge_quality.py`, `knowledge_seed_validator.py` | 掌握度、题目范围、图谱引导、种子校验和质量评估 |
| 持久化 | `store.py`, `store_*.py`, `memory_*.py`, `fsrs_bridge.py` | SQLite schema、学习记录、笔记、卡组、FSRS 与迁移兼容 |
| 文字识别与文档 | `study_ocr_pipeline.py`, `document_*.py` | 使用内置文字识别读取截图和文档页面，并完成分块、异步分析与导出 |
| 习惯与监督 | `study_habit_store.py`, `pomodoro_timer.py`, `checkin_manager.py`, `supervision.py` | 番茄钟、打卡、学习习惯和提醒 |
| Hosted surfaces | `surfaces/*.tsx`, `surfaces/*.ts` | 宿主提供的现代 UI surface |
| 兼容静态 UI | `static/` | 传统工作台、控制器、样式、i18n 运行时及随包资源 |
| 本地化与种子数据 | `i18n/`, `static/knowledge_seeds/`, `local_models/`, `data/` | 八语言文本、知识种子、本地模型目录和导出样式 |
| 验证 | `tests/`, `tests/frontend/`, `tools/` | Python/DOM 回归测试、前端测试依赖和知识种子审计工具 |

## 生命周期主链

`plugin.toml` 将入口指向 `StudyCompanionPlugin`。该类在 `__init__.py` 中通过多个
`entry_*` mixin 组合能力；其中 `_TutorContextSupportMixin` 必须位于各 tutor entry mixin
之前，因为它拥有上下文构建、调用收尾和学习结果写回逻辑。

启动过程依次完成：

1. 读取并规范化插件配置。
2. 打开 `StudyStore`，加载持久化配置与状态。
3. 装配知识追踪、记忆卡组、习惯、打卡、番茄钟和监督组件。
4. 创建 OCR pipeline 与 `TutorLLMAgent`。
5. 注册静态 UI、动态导出 entry、事件订阅和后台任务。

关闭和启动失败回滚会按相反方向停止任务、事件总线、模型运行时、OCR 与存储。
新增后台任务时，必须同时覆盖正常关闭和启动失败清理。

## 核心学习链路

```text
Hosted surface / static UI / N.E.K.O command
                    |
                    v
               entry_*.py
                    |
                    v
       _TutorContextSupportMixin
        |           |             |
        v           v             v
  TutorLLMAgent  OCR/Document  KnowledgeTracker
        |                         |
        v                         v
  model gateway               StudyStore / FSRS
        |
        v
 structured result -> state/session summary -> public payload
```

边界原则：

- `entry_*.py` 负责校验公共输入、错误映射和响应契约，不承载底层存储细节。
- `TutorLLMAgent` 负责模型运行时与结构化结果，不直接决定公共 entry 的状态写回。
- `_TutorContextSupportMixin` 负责学习上下文和 tutor 调用收尾，是辅导链的汇合点。
- `KnowledgeTracker` 编排掌握度、题目参数与复习状态；数据库细节留在 `store*`/`memory*`。
- 文档原文遵守 README 中的隐私边界，默认不写入 SQLite、学习事件或导出记录。

## 前端边界

项目同时维护 Hosted surfaces 与兼容静态 UI。修改可见行为时，应检查两套实现是否共享：

- 状态字段和错误码；
- 本地化 key；
- 练习 scope、知识图谱和复习状态；
- OCR/文档任务的进度、取消和失败语义。

`tests/test_workspace_frontend.py`、`tests/test_notebook_frontend.py`、
`tests/test_practice_status_ui.py` 和扫描 PDF 测试覆盖这些跨前端契约。

## 修改导航

| 需求 | 首先查看 | 通常需要同步检查 |
| --- | --- | --- |
| 新增/修改公共操作 | 对应 `entry_*.py` | `__init__.py`, `service.py`, 两套前端及 i18n |
| 修改模型输入输出 | `tutor_llm_agent*.py`, `llm_prompts.py` | entry 响应契约、隐私测试、结果写回测试 |
| 修改知识引导 | `knowledge_graph_guidance.py`, `knowledge_tracker.py` | 种子数据、范围路由、payload budget 与生产等价测试 |
| 修改数据库 | `store_schema.py`, 对应 `store_*.py` | migration、兼容读取、事务与批量写入测试 |
| 修改记忆复习 | `memory_deck_store.py`, `fsrs_bridge.py` | `KnowledgeTracker`、习惯桥、Hosted/static UI |
| 修改 OCR/文档 | `study_ocr_pipeline.py`, `document_*.py` | 隐私边界、任务取消、依赖状态和双前端 |
| 修改插件启动 | `StudyCompanionPlugin.startup` | `shutdown` 与 `_cleanup_after_failed_startup` |

## 本地验证

```bash
uv sync --group dev
uv run python -m pytest -q
uvx ruff==0.12.4 check --ignore-noqa --config ruff.toml .
```

pytest 的临时文件统一写入仓库根目录下被忽略的 `.pytest-tmp/`，避免依赖系统临时目录权限，
也不要为单次运行创建新的 `.pytest-*` 目录名。

运行 DOM 测试前先在 `tests/frontend/` 执行 `npm ci`。发布前还应从 N.E.K.O 主仓库运行
README 中的 plugin sync/check/release 检查。
