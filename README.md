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

## 规划中的网页联机流程

联机采用“交换关卡并分别在本地游玩”的异步挑战模式。这里的“下载对手关卡”只表示浏览器从服务器接收一份很小的关卡 JSON 数据，不是下载文件、安装新客户端或重新下载 WebGL 游戏。

目标路由：

```text
Menu
  → Online_Lobby
  → Match_Briefing（可选，可合并进 Online_Lobby）
  → Competition_Mode
  → AI_Asistant_Mode
      ├─ Partial Completion
      │    → PC → PC_Design → PC_Level
      └─ Direction Generation
           → DG → DG_Level
  → 将最终关卡 rows 提交给服务器
  → Challenge_Waiting
  → 收到对手的关卡 rows
  → Online_Level
  → Match_Result
```

一次对局中，玩家 A 和玩家 B 分别完成自己的 PC 或 DG 生成流程，并将最终确认的关卡提交给服务器。服务器冻结两份关卡后，将 A 的关卡交给 B、将 B 的关卡交给 A。双方各自在自己的浏览器中游玩对手关卡，完成后上传移动序列和结果，服务器负责复核并汇总比赛结果。

### 需要新增的场景

最小实现需要四个新场景：

- `Online_Lobby`：创建或加入房间、匹配对手、显示双方准备状态，并保存服务器返回的 `matchId` 和当前玩家身份。
- `Challenge_Waiting`：当前玩家提交关卡后等待对手；收到 `opponent_challenge_ready` 后保存对手关卡并进入 `Online_Level`。
- `Online_Level`：只加载服务器锁定的对手关卡，记录移动序列和完成结果；不得再次调用 PC 或 DG 生成接口。
- `Match_Result`：等待双方完成，展示双方用时、步数、推动次数和胜负结果，并提供返回大厅或再次匹配入口。

可选增加：

- `Match_Briefing`：在匹配成功后展示对手、比赛规则和准备确认。首版可以将这些内容直接放进 `Online_Lobby`，不单独建场景。

建议场景路径：

```text
Assets/Scenes/Matchmaking/Online/Online_Lobby.unity
Assets/Scenes/Matchmaking/Online/Challenge_Waiting.unity
Assets/Scenes/Matchmaking/Online/Online_Level.unity
Assets/Scenes/Matchmaking/Online/Match_Result.unity
Assets/Scenes/Matchmaking/Online/Match_Briefing.unity   # 可选
```

`PC_Level` 和 `DG_Level` 继续负责生成、校验、预览并提交己方设计的关卡。`Online_Level` 单独负责游玩对手关卡，避免 PC/DG 的生成控制器在加载时覆盖服务器下发的固定布局。

### 浏览器与服务器的数据流

推荐使用 WebSocket 推送匹配状态和对手关卡，HTTP 作为首次连接、提交关卡和断线恢复的基础接口：

```text
玩家 A 浏览器                     服务器                     玩家 B 浏览器
生成并确认关卡 rows
        ── submit challenge ──→  保存并冻结
                                  保存并冻结  ←── submit challenge ──
        ← opponent rows JSON ──  交换关卡  ── opponent rows JSON →
本地游玩 B 的关卡                                          本地游玩 A 的关卡
        ── moves/result ─────→  复演与校验  ←──── moves/result ──
        ←─────────────── 双方最终结果 ─────────────────────→
```

关卡仍使用现有字符行结构，例如：

```json
{
  "rows": [
    "############",
    "#          #",
    "# ...      #",
    "############"
  ]
}
```

实际联机消息还应携带 `matchId`、玩家凭证、关卡版本或哈希等元数据。服务器保存的关卡一经双方进入挑战阶段便不可由客户端修改。客户端完成后应上传 `U/D/L/R` 移动序列；服务器通过复演移动验证完成状态、步数和推动次数，不能只信任浏览器上报的分数。

### 跨场景上下文

计划新增一个常驻的 `OnlineMatchContext`，至少保存：

- 当前 `matchId`、玩家身份和断线恢复凭证。
- Competition Mode 与 AI Assistant Mode 的选择。
- 己方已提交关卡的 ID 或哈希。
- 服务器下发的对手 `rows`、关卡 ID 和版本。
- 当前挑战状态以及进入 `Match_Result` 所需的结果数据。

`OnlineMatchContext` 只保存运行时状态，不把对手关卡写成磁盘文件。刷新网页或连接中断后，通过 `matchId` 和恢复凭证从服务器重新取得同一份 JSON。

### 场景搭建约定

- `Online_Lobby` 进入后建立联机连接；离开匹配流程时显式断开或切换为后台保活。
- `Competition_Mode`、`AI_Asistant_Mode`、PC 和 DG 现有场景继续复用，不复制联机专用版本。
- `PC_Level` 或 `DG_Level` 只有在生成结果通过本地可解性检查后才允许提交。
- 提交成功后进入 `Challenge_Waiting`；重复点击提交必须使用同一个请求 ID，避免生成两份挑战。
- `Challenge_Waiting` 同时支持 WebSocket 实时通知和 HTTP 查询恢复，刷新页面后仍能继续当前比赛。
- `Online_Level` 仅从 `OnlineMatchContext` 加载对手 `rows`，关卡开始后不得编辑布局。
- `Online_Level` 需要记录完整移动序列，而不仅是最终步数和用时。
- `Match_Result` 以服务器确认结果为准；本地结果可以先显示为“等待验证”。
- 生产环境的 WebGL 页面、HTTP API 和 WebSocket 应统一使用 HTTPS/WSS，避免浏览器阻止混合内容。

上述场景完成后，需要将四个必需场景（以及采用时的 `Match_Briefing`）加入 Unity Build Settings；在场景尚未创建前，不应先写入无效的 Build Settings 条目。

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
