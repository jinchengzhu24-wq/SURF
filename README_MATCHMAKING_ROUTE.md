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
```

场景文件：

- `Assets/Scenes/Menu.unity`
- `Assets/Scenes/Matchmaking/Competition_Mode.unity`
- `Assets/Scenes/Matchmaking/AI_Asistant_Mode.unity`

注意：项目当前场景文件名采用 `AI_Asistant_Mode`，其中 `Asistant` 保留现有拼写。代码和 Build Settings 必须使用相同名称。

## 路由实现

- `MenuController.OpenMatchmaking()` 加载 `Competition_Mode`。
- `CompetitionModeController` 管理 Competition Mode 的单选与 Confirm。
- Confirm 只有在选择一个模式后才可点击。
- Confirm 后将模式写入 PlayerPrefs，再加载 `AI_Asistant_Mode`。

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
```

## 手动验证

1. 从 Menu 点击黄色 `Matchmaking`，确认进入 Competition Mode。
2. 未选择选项时，Confirm 应保持禁用。
3. 选择任一模式后，选中样式应更新，Confirm 应启用。
4. 点击 Confirm，确认进入 AI-Asistant Mode。
5. 返回后选择另一模式，确认 PlayerPrefs 中保存的值随选择更新。
