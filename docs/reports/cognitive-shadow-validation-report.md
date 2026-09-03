# Cognitive Evidence Engine Shadow 放行报告

生成时间：2026-09-03T02:09:29.853154Z

## 结论

- 工程模拟门禁：**PASS**。
- Read Only / Personal Beta / Active Local：**均未放行**。本报告没有把合成样本冒充真实用户数据。
- 真实配置模型：**EVALUATED；22/22 样本完全匹配，期望证据召回率 100.0%，意外证据率 0.0%**。
- 本轮只运行认知引擎定向测试，未运行全量测试。

## 样本与提取边界

- 样本共 22 条：标准 15 条，对抗 7 条。
- 覆盖 `omit_inner_derivative`、`differentiate_inner_incorrectly`、`confuse_product_and_chain` 三个 hypothesis。
- 审定标签通过真实提取器结构校验路径：22/22 完全匹配。
- 模型不可用安全降级：PASS；失败时证据数 0。

真实配置模型分 hypothesis 初评：

- `omit_inner_derivative`：precision 100.0%, recall 100.0%
- `differentiate_inner_incorrectly`：precision 100.0%, recall 100.0%
- `confuse_product_and_chain`：precision 100.0%, recall 100.0%

## 工程验证

定向测试结果：PASS，耗时 12.106 秒。

- PASS: 乱序、并发、lease 接管、版本重跑、全量重建一致性（test_cognitive_shadow_validation.py, test_cognitive_projection.py, test_cognitive_store.py, test_cognitive_v2_projection_store.py）
- PASS: 同题重试与同模板变体去重（test_cognitive_projection.py）
- PASS: probe → repair → transfer → monitored（test_cognitive_shadow_validation.py, test_cognitive_projection.py, test_cognitive_intervention_validation.py）
- PASS: 所有权边界与失效绑定 fail-closed（test_cognitive_state_policy.py, test_cognitive_intervention_validation.py, test_cognitive_answer_event_integration.py）

## 答题提交延迟

- 环境：合成本地 SQLite（临时、provisional）。
- 每组 200 次，预热 20 次。
- 关闭入队：p50 3.9889 ms，p95 4.9954 ms。
- 开启原子入队：p50 4.0025 ms，p95 6.9775 ms。
- p95 增量：1.9821 ms；≤5 ms 门槛：PASS。

这里只测答题事务里的原子入队，不包含异步 LLM 提取。

## 所有权边界

验证范围内认知引擎只拥有认知证据、假设和三个装饰字段；没有修改 Coach 选题、Mastery、FSRS、课程进度或错题状态。失效 topic/scope/plan revision/错题绑定均按定向测试 fail-closed。

## 尚未满足的真实放行门槛

- 7 天本地 Shadow：NOT_EVALUATED
- 30 次真实结构化答题：NOT_EVALUATED
- 10 个真实题目族：NOT_EVALUATED
- supported 假设人工精确率：NOT_EVALUATED
- 真实用户否认率：NOT_EVALUATED
- 5 个真实完整干预闭环：NOT_EVALUATED

因此当前结论只支持“工程 Shadow 验证通过/失败”，不能据此宣称 Personal Beta 或普遍有效。
