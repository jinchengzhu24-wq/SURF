# Multi-Agent

| Agent | 服务 | 作用 | 主要输出 |
|---|---|---|---|
| Draft首版理解助手 | 8000 | 理解四道 DG 问题，形成首版设计理解 | `dgContext` |
| 关卡蓝图规划助手 | 8000 | 将确认后的设计理解转换为关卡蓝图 | `LevelDesignPlan` |
| 共创助手 | 8010 | 基于 Stage 进行共创对话，在授权后提出修改 | `intentHypothesis`、`RevisionPlan` |

## 传递关系

```text
DG 回答
→ Draft首版理解助手
→ dgContext
→ 关卡蓝图规划助手
→ LevelDesignPlan
→ Unity 生成器与求解器
→ 验证地图
→ 共创助手
→ RevisionPlan
→ 确定性验证
→ 玩家确认
→ 新 Stage
```

系统通过 `audit_events` 或 8000 现有结构化运行日志记录 Agent 之间的交接：

```text
draft_understanding → blueprint_planning
blueprint_planning → co_creation
co_creation → deterministic_validator
```

每条交接包含 `schemaVersion`、来源、目标、产物、证据和状态。状态可为 `proposed`、`confirmed` 或 `rejected`。`intentHypothesis` 始终是 AI 的可修正推测，不自动成为玩家最终意图；所有地图修改必须通过确定性验证。

8000 只负责 DG 理解和蓝图规划；8010 只接收当前 Stage、地图事实、求解器结果和共创对话，不接收研究者目标、实验条件或未经确认的 DG 上下文。直访问示例会话直接由 8010 的算法地图生成器创建，不经过 8000；其交接记录随该 Demo 会话清理。

现有应用流程承担协调职责。独立的 Intent Agent、Layout Agent、Difficulty Agent、Evaluator Agent、Coordinator Agent：暂无。确定性生成器和求解器不是独立 Agent。

各 Prompt：

- [Draft首版理解助手（8000）.md](Draft首版理解助手（8000）.md)
- [关卡蓝图规划助手（8000）.md](关卡蓝图规划助手（8000）.md)
- [共创助手（8010）.md](共创助手（8010）.md)
