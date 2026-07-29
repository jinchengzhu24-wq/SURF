# Matchmaking 路由说明

本文档单独记录双人匹配研究流程的入口和当前场景路由。

## 当前路由

```text
Menu
  → 点击黄色 Matchmaking 按钮
Competition_Mode
  → 选择 Competitive Mode 或 Supportive Mode
  → 点击 Confirm
AI_Asistant_Mode
  → 选择 Partial Completion
PC
  → 点击 Confirm
PC_Design
  → 绘制并通过 Check
  → 点击 Submit
PC_Level
```

场景文件：

- `Assets/Scenes/Menu.unity`
- `Assets/Scenes/Matchmaking/Competition_Mode.unity`
- `Assets/Scenes/Matchmaking/AI_Asistant_Mode.unity`
- `Assets/Scenes/Matchmaking/PC.unity`
- `Assets/Scenes/Matchmaking/PC_Design.unity`
- `Assets/Scenes/Matchmaking/PC_Level.unity`

注意：项目当前场景文件名采用 `AI_Asistant_Mode`，其中 `Asistant` 保留现有拼写。代码和 Build Settings 必须使用相同名称。

## 路由实现

- `MenuController.OpenMatchmaking()` 加载 `Competition_Mode`。
- `CompetitionModeController` 管理 Competition Mode 的单选与 Confirm。
- Confirm 只有在选择一个模式后才可点击。
- Confirm 后将模式写入 PlayerPrefs，再加载 `AI_Asistant_Mode`。
- AI-Asistant Mode 选择 Partial Completion 后加载 `PC`。
- `PC` 的 Confirm 加载 `PC_Design`。
- `PC_Design` 的 Submit 会重新校验并保存固定 `12×10` 草图，然后加载 `PC_Level`。
- 草图中的箱子起点 `s` 不得与墙上下左右相邻；Submit 前还会验证全开放版本至少存在一个可解玩家出生区域。
- `PC_Level` 只读取 PC 草图上下文。`/generate-pc-level` 让模型选择玩家、内部墙和水面的坐标，由后端确定性组装并预检完整候选地图；Unity `LevelSolver` 仍执行最终可解性验证。
- PC 候选不得删除或移动玩家绘制的墙 `#`、箱子起点 `s` 和终点 `t`。
- 生成失败时可 Retry，或返回 `PC_Design` 并恢复上次提交的草图。

当前选择保存键：

```text
SokobanMatchmakingCompetitionMode
```

当前值：

```text
competitive
supportive
```

## Build Settings

以下场景必须启用：

```text
Assets/Scenes/Menu.unity
Assets/Scenes/Matchmaking/Competition_Mode.unity
Assets/Scenes/Matchmaking/AI_Asistant_Mode.unity
Assets/Scenes/Matchmaking/PC.unity
Assets/Scenes/Matchmaking/PC_Design.unity
Assets/Scenes/Matchmaking/PC_Level.unity
```

## 手动验证

1. 从 Menu 点击黄色 `Matchmaking`，确认进入 Competition Mode。
2. 未选择选项时，Confirm 应保持禁用。
3. 选择任一模式后，选中样式应更新，Confirm 应启用。
4. 点击 Confirm，确认进入 AI-Asistant Mode。
5. 返回后选择另一模式，确认 PlayerPrefs 中保存的值随选择更新。
6. 在 AI-Asistant Mode 选择 Partial Completion，确认进入 `PC`。
7. 从 `PC` Confirm 后进入 `PC_Design`，零组或数量不匹配的 `s`/`t` 不得通过 Check。
8. 合法草图 Submit 后进入 `PC_Level`，确认请求中不包含 DG 或 Creative Workshop 参数。
9. 合法且可解的候选应被加载；无解或格式错误候选不得覆盖当前关卡。
10. 生成失败后点击 Back to Design，确认原草图完整恢复。
