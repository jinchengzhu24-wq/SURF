# Matchmaking 路由说明

本文档单独记录双人匹配研究流程的入口和当前场景路由。

## 当前路由

```text
Menu
  → 点击黄色 Matchmaking 按钮
Online_Lobby
  → 创建房间或输入六位房间码加入房间
Match_Briefing
  → 双方 Ready
Competition_Mode
  → 选择 Competitive Mode 或 Supportive Mode
  → 点击 Confirm
AI_Asistant_Mode
  ├─ Partial-Level Completion
  │    → PC → PC_Design → PC_Level
  └─ Description-to-Level Generation
       → DG → DG_Level
  → 通关己方生成关卡
Challenge_Waiting
  → 提交己方 rows 并等待对手
Online_Level
  → 使用 Player2 游玩对手关卡并提交成绩
Match_Result
  → 等待并显示双方结果
Questionnaire(Online)
  → 完成并成功提交在线赛后问卷
Menu
```

完整场景路径见本文末尾的 Build Settings。上述联机场景均已创建并启用。

注意：项目当前场景文件名采用 `AI_Asistant_Mode`，其中 `Asistant` 保留现有拼写。代码和 Build Settings 必须使用相同名称。

## 路由实现

- `MenuController.OpenMatchmaking()` 加载 `Online_Lobby`。
- `Online_Lobby` 创建或加入内存房间，匹配成功后双方进入 `Match_Briefing`。
- `Match_Briefing` 每秒轮询 Ready 状态；双方 Ready 后进入 `Competition_Mode`。
- `CompetitionModeController` 管理 Competition Mode 的单选与 Confirm。
- Confirm 只有在选择一个模式后才可点击。
- Confirm 后将模式写入 PlayerPrefs，再加载 `AI_Asistant_Mode`。
- AI-Asistant Mode 选择 Partial-Level Completion 后加载 `PC`，选择 Description-to-Level Generation 后加载 `DG`。
- `PC` 的 Confirm 加载 `PC_Design`。
- `PC_Design` 的 Submit 会重新校验并保存固定 `12×10` 草图，然后加载 `PC_Level`。
- 草图中的箱子起点 `s` 不得与墙上下左右相邻；Submit 前还会验证全开放版本至少存在一个可解玩家出生区域。
- `PC_Level` 只读取 PC 草图上下文。后端先构造最多六个完整安全候选，按五墙、四墙、三墙降级；模型只返回 `layoutCandidateId`，不能跨候选混合坐标。Unity `LevelSolver` 仍执行最终可解性验证。
- PC 候选不得删除或移动玩家绘制的墙 `#`、箱子起点 `s` 和终点 `t`。
- 生成失败时可 Retry，或返回 `PC_Design` 并恢复上次提交的草图。
- 在线房间中，玩家亲自通关 `PC_Level` 或 `DG_Level` 后，最终 rows 和两种模式会暂存并进入 `Challenge_Waiting`；非联机调试继续执行原场景完成行为。
- `Challenge_Waiting` 幂等提交关卡并等待对手；双方提交后由玩家确认进入 `Online_Level`。
- `Online_Level` 使用 `Player2` 和方向键游玩对手 rows，累计耗时、有效移动数和理论最少移动数，通关后提交结果并进入 `Match_Result`。
- `Match_Result` 先显示己方成绩，再轮询并补齐对手成绩；点击 Continue 后 Leave 当前房间、清理联机上下文并进入 `Questionnaire(Online)`。
- `Questionnaire(Online)` 使用三条带 1～5 数字刻度的离散滑杆，默认分数均为 3；`/record-survey-response` 提交成功后才加载 `Menu`，提交失败时留在当前场景重试。

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

当前已完成匹配、双方 Ready、关卡创作、关卡交换、本地游玩、成绩提交和结果汇总链路。客户端统一连接
`http://111.231.136.4:8000`，使用六位房间码和每秒一次的 HTTP
轮询同步状态。房间暂存于单进程服务器内存中，30 分钟无活动后清理；
刷新网页或服务器重启后不恢复房间。当前阶段不判定胜负、不提供排行榜，也不做服务端移动复演或反作弊。

当前完整路由：

```text
Menu
  → Online_Lobby
  → Match_Briefing
  → Competition_Mode
  → AI_Asistant_Mode
      ├─ Partial Completion
      │    → PC → PC_Design → PC_Level
      └─ Description-to-Level Generation
           → DG → DG_Level
  → 通关己方关卡并提交最终 rows
  → Challenge_Waiting
  → 双方完成后确认游玩
  → Online_Level
  → 提交耗时、移动数和理论最小移动数
  → Match_Result
  → Questionnaire(Online)
  → 成功提交问卷
  → Menu
```

一次对局中，玩家 A 和玩家 B 分别完成自己的 PC 或 DG 生成流程，并将最终确认的关卡和模式选择提交给服务器。服务器冻结两份关卡后，将 A 的关卡交给 B、将 B 的关卡交给 A。双方各自在自己的浏览器中游玩对手关卡，完成后上传耗时、移动数和本地求解器得到的理论最小移动数；结果页负责等待并汇总双方成绩。

### 联机场景

当前匹配阶段已经使用：

- `Online_Lobby`：创建或加入房间、等待匹配对手，并保存服务器返回的 `matchId` 和当前玩家身份。
- `Match_Briefing`：在匹配成功后展示双方 Ready 状态；双方准备完成后进入 Competition Mode。

当前交换与游玩阶段已经使用：

- `Challenge_Waiting`：幂等提交己方 rows、轮询对手创作状态；双方完成后启用确认游玩按钮。
- `Online_Level`：只加载服务器锁定的对手 rows，使用 `Player2` 和方向键游玩，并在本地格式与可解性验证通过后开始累计用时和移动数；不会再次调用 PC 或 DG 生成接口。
- `Match_Result`：先展示己方成绩并轮询对手状态；双方完成后展示各自耗时、实际移动数与理论最小移动数，以及双方选择的 Competition Mode 和 AI Assistant Mode。
- `Questionnaire(Online)`：通过三条 1～5 分离散滑杆记录在线赛后评分；圆形滑块只能停在整数刻度，右侧分数框实时显示当前数值，默认值为 3。提交成功后返回主菜单，失败时不跳转。

当前场景路径：

```text
Assets/Scenes/Matchmaking/Online/Online_Lobby.unity
Assets/Scenes/Matchmaking/Online/Challenge_Waiting.unity
Assets/Scenes/Matchmaking/Online/Online_Level.unity
Assets/Scenes/Matchmaking/Online/Match_Result.unity
Assets/Scenes/Matchmaking/Online/Match_Briefing.unity
Assets/Scenes/Matchmaking/Online/Questionnaire(Online).unity
```

`PC_Level` 和 `DG_Level` 继续使用 `Player`（WASD），负责生成、校验、预览并提交己方设计的关卡。`Online_Level` 使用 `Player2`（方向键）单独游玩对手关卡，避免 PC/DG 的生成控制器在加载时覆盖服务器下发的固定布局。

### 浏览器与服务器的数据流

当前使用 HTTP 提交关卡，并通过每秒一次的房间查询取得对手关卡：

```text
玩家 A 浏览器                     服务器                     玩家 B 浏览器
生成并确认关卡 rows
        ── submit challenge ──→  保存并冻结
                                  保存并冻结  ←── submit challenge ──
        ← opponent rows JSON ──  交换关卡  ── opponent rows JSON →
本地游玩 B 的关卡                                          本地游玩 A 的关卡
        ── time/moves/min ───→  保存并冻结  ←─── time/moves/min ──
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

挑战通过 `POST /online/rooms/{matchId}/challenge` 提交，内容包含 `rows`、
`competitionMode` 和 `aiAssistantMode`，使用 `X-Player-Token` 识别玩家。
同一玩家重复提交完全相同的挑战幂等成功，修改 rows 或模式会被拒绝；双方
提交后，房间查询按当前玩家返回 `opponentChallengeRows` 和双方模式元数据。

通关结果通过 `POST /online/rooms/{matchId}/result` 提交：

```json
{
  "durationSeconds": 42.37,
  "moveCount": 31,
  "minimumMoves": 24
}
```

首位完成后房间进入 `waiting_for_results`，双方完成后进入 `results_ready`。
结果提交同样幂等冻结；当前阶段不上传移动序列，也不做服务器复演或反作弊。

### 跨场景上下文

常驻的 `OnlineMatchContext` 当前保存：

- 当前 `matchId`、六位房间码、玩家 token 和玩家编号。
- Competition Mode 与 AI Assistant Mode 的选择。
- 等待提交的己方 `rows`。
- 服务器下发的对手 `rows`。
- 当前房间、Ready 和挑战提交状态。
- 双方挑战模式元数据和已经提交的成绩。

`OnlineMatchContext` 只保存运行时状态，不把对手关卡写成磁盘文件。当前版本刷新网页后不恢复比赛。

### 场景搭建约定

- `Online_Lobby` 进入后建立联机上下文；离开匹配流程时调用 Leave 并清理运行时状态。当前没有后台保活或刷新恢复。
- `Competition_Mode`、`AI_Asistant_Mode`、PC 和 DG 现有场景继续复用，不复制联机专用版本。
- `PC_Level` 或 `DG_Level` 只有在玩家亲自通关生成结果后，才会暂存最终 rows 并进入 `Challenge_Waiting`。
- `Challenge_Waiting` 自动幂等提交，并通过 HTTP 查询等待对手完成；不会自动跳过确认按钮。
- `Online_Level` 仅从 `OnlineMatchContext` 加载对手 `rows`，关卡开始后不得编辑布局。
- `Online_Level` 从首次可操作到通关累计耗时与有效移动；按 `R` 重开不会清零比赛统计。
- `Online_Level` 通关后幂等提交结果并进入 `Match_Result`；网络失败时留在完成页自动重试。
- `Match_Result` 在对手尚未完成时每秒轮询，双方完成后停止轮询；Continue 会 Leave、清理运行时上下文并进入在线问卷。项目当前 `runInBackground` 为关闭状态，WebGL 标签页失去焦点时轮询可能暂停，重新聚焦后才会继续。
- `Questionnaire(Online)` 使用独立的 `online_post_match_survey` ID；三个默认 3 分可直接提交，分数继续写入现有答案结构的 `optionIndex`、`optionId` 和 `optionText`。只有后端确认提交成功后才进入 `Menu`。
- 生产环境的 WebGL 页面、HTTP API 和 WebSocket 应统一使用 HTTPS/WSS，避免浏览器阻止混合内容。

上述联机场景已经创建并加入 Unity Build Settings。新增或改名场景时必须同步更新场景跳转常量和 Build Settings，保留现有 `AI_Asistant_Mode` 拼写。

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
Assets/Scenes/Matchmaking/DG.unity
Assets/Scenes/Matchmaking/DG_Level.unity
Assets/Scenes/Matchmaking/Online/Challenge_Waiting.unity
Assets/Scenes/Matchmaking/Online/Online_Level.unity
Assets/Scenes/Matchmaking/Online/Match_Result.unity
Assets/Scenes/Matchmaking/Online/Questionnaire(Online).unity
```

## 手动验证

1. 用两个浏览器打开远程 `/game/`，从 Menu 点击黄色 `Matchmaking`，确认进入 `Online_Lobby`。
2. 玩家 A 创建房间并显示六位房间码；玩家 B 输入该码后，双方均进入 `Match_Briefing`。
3. 单方 Ready 时另一方应在一次轮询后看到状态；双方 Ready 后均进入 Competition Mode。
4. 未选择模式时，Confirm 应保持禁用；选择任一模式后选中样式应更新，Confirm 应启用。
5. 点击 Confirm，确认进入 AI-Asistant Mode；返回后选择另一模式，确认 PlayerPrefs 中保存的值随选择更新。
6. 在 AI-Asistant Mode 分别覆盖两个分支：Partial-Level Completion 应进入 `PC`，Description-to-Level Generation 应进入 `DG`。
7. 从 `PC` Confirm 后进入 `PC_Design`，零组或数量不匹配的 `s`/`t` 不得通过 Check。
8. 合法草图 Submit 后进入 `PC_Level`，确认请求中不包含 DG 或 Creative Workshop 参数。
9. 合法且可解的候选应被加载；无解或格式错误候选不得覆盖当前关卡。
10. 生成失败后点击 Back to Design，确认原草图完整恢复。
11. PC/DG 中确认生成的是 `Player` 且 WASD 有效；Online_Level 中确认生成的是 `Player2` 且方向键有效。
12. 一方通关对手关卡后应进入 `Match_Result` 并显示己方成绩；按 `R` 重开前的耗时和移动数仍计入成绩。
13. 第二方通关后，第一方结果页应在一次轮询后补齐对手成绩和双方模式；测试两个浏览器时需重新聚焦第一方页面并等待至少一次轮询，因为后台 WebGL 当前会暂停运行。
14. `CONTINUE` 应调用 Leave、清理房间上下文并进入 `Questionnaire(Online)`，不能直接返回大厅。
15. 在线问卷初始应显示三条五档滑杆和三个 `3` 分；拖动圆形滑块时只能落在整数刻度，右侧分数同步更新。默认值或调整后的分数只有在服务器返回成功后才进入 `Menu`，提交失败时不得离开。
