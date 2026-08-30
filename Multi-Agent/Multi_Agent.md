# Multi-Agent

| Agent | 服务 | 作用 | 输入 → 输出 |
|---|---|---|---|
| Draft首版理解助手 | 8000 | 理解四道 DG 问题，形成首版设计理解 | DG 回答 → `dgContext` |
| 关卡蓝图规划助手 | 8000 | 将确认后的设计理解转换为可生成蓝图 | `dgContext` → `LevelDesignPlan` |
| 共创聊天助手 | 8010 | 理解共创要求、澄清意图，并生成已授权的语义计划 | 对话、Stage、地图事实 → `RevisionPlan`、`executionContract` |
| 共创关卡修改助手 | 8010 | 严格按计划输出局部格子操作，不重新解释意图 | `RevisionPlan`、`executionContract`、Stage、求解器事实 → 操作候选 |

## 传递关系

```text
DG 回答
→ Draft首版理解助手
→ dgContext
→ 关卡蓝图规划助手
→ LevelDesignPlan
→ Unity 生成器与求解器
→ 验证地图
→ 共创聊天助手
→ RevisionPlan + executionContract
→ 共创关卡修改助手
→ 具体操作候选
→ 确定性执行与 validate_and_solve()
→ 玩家确认
→ 新 Stage
```

8010 的交接记录为：

```text
co_creation_chat → co_creation_revision → deterministic_validator
```

交接记录包含产物、证据、重试、操作、变化数量、验证结果和 `proposed`、`confirmed` 或 `rejected` 状态。`intentHypothesis` 始终是可拒绝的 AI 推测，不自动成为玩家最终意图。未经明确授权、契约校验和确定性验证，不保存地图修改。

8000 的 `dgContext`、研究者目标和实验条件不得进入 8010。直访问示例会话直接进入 8010，不经过 8000；其交接记录随旧 Demo 会话清理。确定性执行器、地图生成器和求解器不是独立 Agent。独立的 Intent、Layout、Difficulty、Evaluator、Coordinator Agent：暂无。

正式流程 Prompt：

- [Draft首版理解助手（8000）.md](Draft首版理解助手（8000）.md)
- [关卡蓝图规划助手（8000）.md](关卡蓝图规划助手（8000）.md)
- [共创聊天助手（8010）.md](共创聊天助手（8010）.md)
- [共创关卡修改助手（8010）.md](共创关卡修改助手（8010）.md)
