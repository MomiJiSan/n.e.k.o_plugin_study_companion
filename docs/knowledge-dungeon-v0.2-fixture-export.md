# 知识副本 v0.2：权威夹具导出

v0.2 第一阶段把 Electron 演示所需的完整命令/响应序列交给 Python 权威引擎生成，删除表现层手写战斗数值这一条临时边界。

## 导出命令

在 Study Companion 仓库执行：

```powershell
uv run python -m knowledge_dungeon.fixture_exporter `
  --output ..\N.E.K.O-Knowledge-Dungeon\fixtures\demo-sequence.zh-CN.json
```

检查 Electron 仓库中的夹具是否仍与当前 Python 引擎一致：

```powershell
uv run python -m knowledge_dungeon.fixture_exporter `
  --output ..\N.E.K.O-Knowledge-Dungeon\fixtures\demo-sequence.zh-CN.json `
  --check
```

不提供 `--output` 时，导出器把 JSON 写到标准输出。

## 产物保证

- `projection` 由固定模拟学习快照投影，不由 Electron 计算卡牌伤害。
- 每个 `steps[].request` 都是完整的协议 v1 命令。
- 每个 `steps[].response` 都是对应 `KnowledgeDungeonEngine.dispatch()` 的原始接受响应。
- 命令链覆盖普通战斗、本轮奖励、休整、Boss 和 `finish_run`。
- `fixture_sha256` 标识整个导出产物，`final_state_hash` 等于最后一步的权威状态哈希。
- 当前权威链为 16 步；旧的 15 步 UI 夹具并不符合真实 Boss HP、能量和抽牌结果，已停止使用。
- 导出过程仍只使用模拟学习快照，不读取用户数据库，也不写入学习事实。

Electron 的 `MockDungeonBridge` 只验证顺序、版本和输入并回放响应。它可以附加卡牌展示数据和下一步按钮文案，但不得重算伤害、敌人 HP、抽牌、奖励或状态哈希。
