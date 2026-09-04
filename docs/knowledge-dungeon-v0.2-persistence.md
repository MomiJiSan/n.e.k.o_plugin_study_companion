# 知识副本 v0.2-A1：运行持久化与恢复

## 目标与边界

本阶段只把 v0.1 的确定性运行状态从进程内存扩展为可选的独立 SQLite 存储，验证异常退出后的恢复与命令幂等。默认不传 `DungeonRunStore` 时，引擎仍按 v0.1 的纯内存方式运行。

本阶段不接入真实 Mastery、FSRS 或 Cognitive 数据，不读取或修改插件用户数据库，不提供正式 Bridge、Electron 入口、复习跳转、安装器或账号能力。

## 存储契约

- `dungeon_runs` 保存每个运行的规范化状态 JSON、状态版本及状态哈希。
- `dungeon_command_receipts` 保存完整请求指纹和原始成功响应。
- 状态与成功回执在同一个 `BEGIN IMMEDIATE` 事务内提交。
- 完全相同的重试返回首次提交时的逐字段相同响应，不重新执行 reducer。
- 同一 `command_id` 对应不同完整请求时返回 `command_id_conflict`。
- 写入使用 `state_version` 乐观条件；并发写入失败后返回最新状态上的 `stale_state_version`。
- SQLite 使用 WAL、`synchronous=FULL`、外键和独立 schema 版本。

## 损坏处理

读取状态时会重新解析 `RunState`，并核对 `run_id`、`state_version` 和 `state_hash`。状态或命令回执损坏时，整轮运行会移动到 `dungeon_quarantine`，同时删除活动状态与回执。

隔离记录作为墓碑保留；同一 `run_id` 不会被静默重建。原型阶段没有自动修复，需由后续运维工具显式检查和处理。

## 恢复演示

请使用一个尚不存在的临时数据库路径：

```bash
uv run python -m knowledge_dungeon.persistence_simulator --database .tmp/knowledge-dungeon-v0.2-demo.sqlite3
```

演示会提交两个命令、关闭数据库、重新打开并比较恢复前后的状态哈希，同时验证首个命令的精确响应重放。

## 验收

```bash
uv run python -m pytest -q tests/knowledge_dungeon
uv run ruff check knowledge_dungeon tests/knowledge_dungeon
uv run pyright knowledge_dungeon tests/knowledge_dungeon
```

自动测试覆盖重启恢复、精确幂等、命令标识冲突、两处事务故障注入、双引擎并发写、状态损坏隔离、回执损坏隔离以及存储表边界。
