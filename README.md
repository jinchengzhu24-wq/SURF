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

## 网页联机流程

联机采用“交换关卡并分别在本地游玩”的异步挑战模式。这里的“下载对手关卡”只表示浏览器从服务器接收一份很小的关卡 JSON 数据，不是下载文件、安装新客户端或重新下载 WebGL 游戏。

当前已完成匹配、双方 Ready、关卡创作、关卡交换和本地游玩链路。客户端统一连接
`http://111.231.136.4:8000`，使用六位房间码和每秒一次的 HTTP
轮询同步状态。房间暂存于单进程服务器内存中，30 分钟无活动后清理；
刷新网页或服务器重启后不恢复房间。比赛结果上传与结算仍属于后续阶段。

目标路由：

```text
Menu
  → Online_Lobby
  → Match_Briefing
  → Competition_Mode
  → AI_Asistant_Mode
      ├─ Partial Completion
      │    → PC → PC_Design → PC_Level
      └─ Direction Generation
           → DG → DG_Level
  → 通关己方关卡并提交最终 rows
  → Challenge_Waiting
  → 双方完成后确认游玩
  → Online_Level
  → Challenge Complete
  → Match_Result（后续）
```

一次对局中，玩家 A 和玩家 B 分别完成自己的 PC 或 DG 生成流程，并将最终确认的关卡提交给服务器。服务器冻结两份关卡后，将 A 的关卡交给 B、将 B 的关卡交给 A。双方各自在自己的浏览器中游玩对手关卡，完成后上传移动序列和结果，服务器负责复核并汇总比赛结果。

### 联机场景

当前匹配阶段已经使用：

- `Online_Lobby`：创建或加入房间、匹配对手、显示双方准备状态，并保存服务器返回的 `matchId` 和当前玩家身份。
- `Match_Briefing`：在匹配成功后展示双方 Ready 状态；双方准备完成后进入 Competition Mode。

当前交换与游玩阶段已经使用：

- `Challenge_Waiting`：幂等提交己方 rows、轮询对手创作状态；双方完成后启用确认游玩按钮。
- `Online_Level`：只加载服务器锁定的对手 rows，并在本地格式与可解性验证通过后开始游玩；不会再次调用 PC 或 DG 生成接口。

后续仍需增加 `Match_Result`：等待双方完成，展示双方用时、步数、推动次数和胜负结果，并提供返回大厅或再次匹配入口。

建议场景路径：

```text
Assets/Scenes/Matchmaking/Online/Online_Lobby.unity
Assets/Scenes/Matchmaking/Online/Challenge_Waiting.unity
Assets/Scenes/Matchmaking/Online/Online_Level.unity
Assets/Scenes/Matchmaking/Online/Match_Result.unity
Assets/Scenes/Matchmaking/Online/Match_Briefing.unity
```

`PC_Level` 和 `DG_Level` 继续负责生成、校验、预览并提交己方设计的关卡。`Online_Level` 单独负责游玩对手关卡，避免 PC/DG 的生成控制器在加载时覆盖服务器下发的固定布局。

### 浏览器与服务器的数据流

当前使用 HTTP 提交关卡，并通过每秒一次的房间查询取得对手关卡：

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

挑战通过 `POST /online/rooms/{matchId}/challenge` 提交，使用
`X-Player-Token` 识别玩家。同一玩家重复提交相同 rows 幂等成功，提交不同
rows 会被拒绝；双方提交后，房间查询只向当前玩家返回对手的
`opponentChallengeRows`。客户端完成后的移动序列与结果复核尚未接入。

### 跨场景上下文

常驻的 `OnlineMatchContext` 当前保存：

- 当前 `matchId`、玩家身份和断线恢复凭证。
- Competition Mode 与 AI Assistant Mode 的选择。
- 等待提交的己方 `rows`。
- 服务器下发的对手 `rows`。
- 当前房间、Ready 和挑战提交状态。

`OnlineMatchContext` 只保存运行时状态，不把对手关卡写成磁盘文件。当前版本刷新网页后不恢复比赛。

### 场景搭建约定

- `Online_Lobby` 进入后建立联机连接；离开匹配流程时显式断开或切换为后台保活。
- `Competition_Mode`、`AI_Asistant_Mode`、PC 和 DG 现有场景继续复用，不复制联机专用版本。
- `PC_Level` 或 `DG_Level` 只有在玩家亲自通关生成结果后，才会暂存最终 rows 并进入 `Challenge_Waiting`。
- `Challenge_Waiting` 自动幂等提交，并通过 HTTP 查询等待对手完成；不会自动跳过确认按钮。
- `Online_Level` 仅从 `OnlineMatchContext` 加载对手 `rows`，关卡开始后不得编辑布局。
- `Online_Level` 通关后停留在完成页；移动序列和比赛结果上传留待下一阶段。
- `Match_Result` 以服务器确认结果为准；本地结果可以先显示为“等待验证”。
- 生产环境的 WebGL 页面、HTTP API 和 WebSocket 应统一使用 HTTPS/WSS，避免浏览器阻止混合内容。

上述场景完成后，需要将四个必需场景（以及采用时的 `Match_Briefing`）加入 Unity Build Settings；在场景尚未创建前，不应先写入无效的 Build Settings 条目。

## Build Settings

以下场景必须启用：

```text
Assets/Scenes/Menu.unity
Assets/Scenes/Matchmaking/Online/Online_Lobby.unity
Assets/Scenes/Matchmaking/Online/Match_Briefing.unity
Assets/Scenes/Matchmaking/Competition_Mode.unity
Assets/Scenes/Matchmaking/AI_Asistant_Mode.unity
Assets/Scenes/Matchmaking/PC.unity
Assets/Scenes/Matchmaking/PC_Design.unity
Assets/Scenes/Matchmaking/PC_Level.unity
```

## 手动验证

1. 用两个浏览器打开远程 `/game/`，从 Menu 点击黄色 `Matchmaking`，确认进入 `Online_Lobby`。
2. 玩家 A 创建房间并显示六位房间码；玩家 B 输入该码后，双方均进入 `Match_Briefing`。
3. 单方 Ready 时另一方应在一次轮询后看到状态；双方 Ready 后均进入 Competition Mode。
4. 未选择模式时，Confirm 应保持禁用；选择任一模式后选中样式应更新，Confirm 应启用。
5. 点击 Confirm，确认进入 AI-Asistant Mode；返回后选择另一模式，确认 PlayerPrefs 中保存的值随选择更新。
6. 在 AI-Asistant Mode 选择 Partial Completion，确认进入 `PC`。
7. 从 `PC` Confirm 后进入 `PC_Design`，零组或数量不匹配的 `s`/`t` 不得通过 Check。
8. 合法草图 Submit 后进入 `PC_Level`，确认请求中不包含 DG 或 Creative Workshop 参数。
9. 合法且可解的候选应被加载；无解或格式错误候选不得覆盖当前关卡。
10. 生成失败后点击 Back to Design，确认原草图完整恢复。
