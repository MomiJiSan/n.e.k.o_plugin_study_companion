# 知识副本 v0.1 原型验收报告

验收日期：2026-09-04

## 结论

v0.1 的代码与本机开发运行验收通过。实现范围保持为“模拟学习快照 + Python 权威引擎 + 独立 Electron 表现层”，没有接入真实 Mastery、FSRS、Cognitive、用户数据库或 N.E.K.O 启动链路。

## 交付物

- Study Companion 新增独立 `knowledge_dungeon` Python 包及 JSON Schema。
- 新增固定数学卡牌目录、新用户/已学习用户快照和 `english_stub` 测试投影。
- 新增确定性地图、战斗、奖励、幂等、状态版本、重放、序列化和状态哈希。
- 新增 CLI 场景 `calculus_v0_1`。
- 新增独立 Electron 工程（单独仓库，不在本仓库内）。
- Electron 端包含安全 Main/Preload/Renderer 分层、React/PixiJS 界面、严格 MockBridge 回放、中文演示夹具和 Python 权威响应兼容样本。

## 验收结果

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 新用户只有“红枼的怜悯” | 通过 | `test_new_learner_has_only_the_starter_as_owned_and_playable` |
| 掌握知识点解锁永久卡牌 | 通过 | `test_mastered_topic_unlocks_card_but_ownership_is_independent_of_usability` |
| 轻度褪色 80%、重度褪色 50% | 通过 | 投影边界与战斗伤害测试 |
| 休眠卡保留所有权但不进入抽牌堆 | 通过 | 投影与引擎抽牌测试 |
| 数学卡进入英语测试地图按 50% | 通过 | `test_same_subject_is_full_strength_and_cross_subject_is_half_strength` |
| 出牌不答题、不包含 `attempt_id` | 通过 | 命令校验和学习隔离测试 |
| 战斗、奖励和 Boss 不产生学习事实或永久奖励 | 通过 | `test_boss_finish_explicitly_emits_no_learning_or_permanent_reward` |
| 本轮卡牌状态冻结 | 通过 | `deck_frozen` 事件与引擎状态测试 |
| 重复 `command_id` 不重复结算 | 通过 | 幂等测试 |
| 旧 `state_version` 被拒绝 | 通过 | 版本冲突测试 |
| 相同输入产生相同哈希并可重放 | 通过 | 状态哈希、序列化和重放测试 |
| Python 与 TypeScript 验证同一权威响应样本 | 通过 | 固定哈希 `b596868b83997e870690793a0877833a616a4ea7cb9427594d9b03ef2e361034` |
| Renderer 无 Node、文件系统、Shell、令牌和通用网络能力 | 通过 | Electron 安全配置与源码边界测试 |
| Electron 从入口走到 Boss 的演示序列 | 通过 | MockBridge 15 步完整回放测试 |
| Electron 开发模式启动与退出 | 通过 | Forge 构建 Main/Preload/Renderer 后窗口启动/退出冒烟 |
| 不修改现有学习域、N.E.K.O 和 N.E.K.O-PC | 通过 | Study Companion 仅新增目录；另外两个仓库无本次变更 |

## 自动化结果

- `uv run python -m pytest -q tests/knowledge_dungeon`：37 passed。
- `uv run ruff check knowledge_dungeon tests/knowledge_dungeon`：通过。
- Study Companion 全量回归：1042 passed、2 skipped、1 xfailed。
- `uv run python -m knowledge_dungeon.simulator --scenario calculus_v0_1`：完整运行至 Boss，最终哈希 `956c6814ddb47b19e737f847573b08f75b163945d3fb7a4e1334d3afd1a13def`。
- Electron `npm run lint`：通过。
- Electron `npm run typecheck`：通过。
- Electron `npm test`：4 files、12 tests passed。
- Electron Renderer production build：通过，748 modules transformed。
- Electron Forge 开发启动：Main、Preload、Renderer 构建成功，窗口正常响应并退出。

## 冻结边界

- v0.1 的所有学习信息均来自固定 fixture。
- UI 的 15 步中文演示是表现层走查夹具，不冒充 Python 战斗结果；另有真实 Python 响应样本用于跨端契约校验。
- 不存在真实 Bridge、插件入口、令牌、联网、学习数据写入、复习跳转、正式多学科内容、安装器或更新器。
- `node_modules`、`.vite`、`dist` 等均为本机验证产物，不属于源码交付。

## v0.2 接口入口

下一阶段应先实现受限本地 Bridge，并由 Python 引擎批量导出完整演示命令/响应序列，再接入只读学习快照。真实学习域仍通过适配层提供事实，副本不得反向写入掌握度。
