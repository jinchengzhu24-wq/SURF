# Sokoban 人机共创与在线挑战

本仓库包含 Unity 2D Sokoban 客户端、8000 端口的匹配服务与研究 dashboard，以及 8010 端口的独立 LLM 共创工作台。当前原型把“与 LLM 共创地图”和“在 Unity 中游玩地图”分开：Unity 先生成并验证第一版地图，网页负责持续聊天、版本管理和可选试玩。

## 当前目标路由

```text
Menu
  → Online_Lobby → Match_Briefing → Draft
      ├─ Partial-Level Completion
      │    → PC → PC_Design → PC_Level（生成并验证首版）
      └─ Description-to-Level Generation
           → DG → DG_Level（生成并验证首版）
  → CoCreation_Entry（上传首版并创建 8010 会话）
  → 8010 Co-Creation Lab
      → Stage 1 = Unity 首版 rows
      → LLM 评价、连续聊天、手工编辑或 LLM 修改提案
      → 每次明确保存/接受才创建不可覆盖的新 Stage
      → 可选择任意已保存 Stage 并点击 Play
          → 8000 WebGL → PC_Level 或 DG_Level 只读试玩
          → 通关动画与结果同步 → 自动返回同一 8010 会话
      → 明确确认最终 Stage
      → 填写设计意图
  → Unity 获得最终 rows
  → Challenge_Waiting → Online_Level → Match_Result
  → Questionnaire(Online) → Menu
```

旧 `Competition_Mode`、`AI_Asistant_Mode` 以及 Competitive / Supportive 生成语义已经移除。`Draft` 只区分初稿方法；系统不向设计者分配“困难、友好、竞争、支持”等预设目标，也不把这些历史定义注入 LLM。

8010 共创服务、8000 中立匹配后端和包含 Stage Play 的 WebGL 已于 2026-08-11 部署到 `http://111.231.136.4:8010/` 与 `http://111.231.136.4:8000/game/`；8010 的 60 秒单次调用、消息恢复和就地重试更新已于 2026-08-12 部署。后续修改仍须按本文验证流程重新构建和部署，不能仅凭本地源码判断线上版本。

## 共创规则

- `Stage 1` 必须与 PC/DG 在 Unity 中通过格式检查及 `LevelSolver` 验证后的 rows 完全一致。
- 会话围绕同一个持续演化的关卡进行。聊天历史、版本、差异、评价、提案决定、试玩证据、最终版本和设计意图均持久化。
- 手工修改只存在于浏览器草稿中，点击“保存为新 Stage”后才成为版本；历史 Stage 永不覆盖。
- LLM 修改先以提案和差异预览呈现。只有设计者接受且服务器求解验证通过后才创建 Stage。
- LLM 的难度和体验判断是主观意见；服务器与 Unity 求解器结果才是确定性证据。
- Play 只允许当前选中的已保存 Stage。未保存草稿、请求处理中或存在待决定提案时禁用。
- Play 不会修改地图、创建 Stage、确认最终版本或提交在线挑战。
- 最终确认后先收集设计者自然形成的意图；在此之前，集成接口不会向 Unity暴露最终 rows。
- 英文/中文切换影响新 UI、错误信息和后续 LLM 回复；历史消息保留原始语言。

是否把 PC/DG 作为正式研究条件、是否设置最少共创轮数、问卷指标和最终研究问题仍未决定，不应提前固化。

## PC_Level 与 DG_Level 的双重用途

### 首版生成模式

- `PC_Level` 读取 `PCDesignContext` 中的 `12×10` 草图，请求安全候选，并在 Unity 端重新校验及求解。
- `DG_Level` 根据玩家描述获取 LLM 方案，通过模板生成和回退策略形成完整地图，并在 Unity 端验证。
- 两条路径都保持中立，不传递或读取已废除的模式字段。
- 首版验证成功后不要求玩家先通关；rows 与 `partial_completion` / `description_generation` 写入 `CoCreationDraftContext`，随后加载 `CoCreation_Entry`。

### Stage 试玩模式

- 网页创建五分钟、一次性 Play Ticket，URL 只携带 attempt ID 与票据，不携带 rows。
- 8000 WebGL 的 Menu 启动组件换取完整 rows、来源、语言、提交 token 和返回 URL，并立即从地址栏清除票据。
- `partial_completion` 加载 `PC_Level`，`description_generation` 加载 `DG_Level`。
- 试玩上下文存在时，PC/DG 的生成控制器停用；指定 rows 再经 Unity `LevelSolver(maxSearchStates=300000)` 后加载。
- WASD 移动；`R` 重开地图，但累计移动、推动、重开和首次有效移动后的耗时不清零。
- 通关后保留完成动画和淡出，提交 `completed` 指标后自动返回原 8010 会话；Unity 内不再提供主动提前返回按钮，浏览器返回、关闭或刷新按 `interrupted` 处理。
- 试玩不会进入 `Challenge_Waiting`、`Match_Result` 或问卷。

## 8010 工作台

目录：

```text
CoCreationPrototype/
├── Frontend/              # 独立 HTML/CSS/JS 三栏工作台
└── Backend/               # FastAPI、DeepSeek 客户端、SQLite 与测试
```

前端视觉复用 8000 MatchMaking/Train dashboard 当前生效的 Pixel-adventure 设计语言：草绿色网格背景、木质 sticky topbar、米白石质面板、蓝紫标题板、硬边像素阴影、Consolas 标题与像素地图框。桌面仍为 Stage / 聊天 / 地图编辑器三栏；低于 1200px 收为两栏，低于 900px 为单栏，680px 下进一步缩小阴影、间距与瓦片。8010 保留独立 CSS，不在运行时引用 8000 文件。

LLM 使用适应型共创策略。每个新 Stage 会收到一次基于真实地图的中性开场；之后 LLM 根据玩家最新表达选择澄清意图、提出观点、讨论权衡、结合试玩反思或建议修改方向。意图推测必须是可纠正的暂定假设，不会写入最终设计意图。LLM 可以主动提出修改方向，但只有玩家明确请求或同意后才能返回完整地图提案。普通聊天不再显示固定的 assessment 报告卡；结构化评价和每轮 guidance 元数据仍保存在 SQLite 中供研究分析。

8010 的同步 LLM 调用最多等待 60 秒且不自动重复调用上游。聊天输入区会就地显示等待或安全错误；失败、刷新或连接中断后使用原消息幂等键重试，确保同一请求最多保存一条 user turn 和一条 assistant turn。修改失败消息内容则视为一条新请求。

后端使用独立 SQLite/WAL。默认数据库为 `CoCreationPrototype/Backend/data/cocreation.sqlite3`，`.env` 和数据库文件均被本目录 `.gitignore` 排除。
生产 systemd 模板保存在 `CoCreationPrototype/Backend/sokoban-cocreation.service`，其中将写权限限制到独立数据目录，并同时读取主后端的 LLM 配置和 8010 自己的安全配置。

本地启动：

```powershell
python -m pip install -r CoCreationPrototype/Backend/requirements.txt
python CoCreationPrototype/Backend/app.py
```

访问 `http://127.0.0.1:8010/`。关键环境变量见 `CoCreationPrototype/Backend/.env.example`：

- `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`、`DEEPSEEK_BASE_URL`
- `COCREATION_PUBLIC_BASE_URL`
- `COCREATION_WEBGL_BASE_URL`
- `COCREATION_TOKEN_SECRET`
- `COCREATION_DATABASE_PATH`
- `COCREATION_ALLOWED_ORIGINS`

## 8010 API

```text
POST  /api/sessions
POST  /api/sessions/{sessionId}/browser-access
GET   /api/sessions/{sessionId}
PATCH /api/sessions/{sessionId}/language

POST  /api/sessions/{sessionId}/versions
POST  /api/sessions/{sessionId}/versions/{versionId}/restore
POST  /api/sessions/{sessionId}/versions/{versionId}/assessments
POST  /api/sessions/{sessionId}/messages
POST  /api/sessions/{sessionId}/proposals/{proposalId}/decision

POST  /api/sessions/{sessionId}/versions/{versionId}/play-attempts
POST  /api/play-attempts/{attemptId}/bootstrap
POST  /api/play-attempts/{attemptId}/start
POST  /api/play-attempts/{attemptId}/progress
POST  /api/play-attempts/{attemptId}/complete
POST  /api/play-attempts/{attemptId}/abandon

POST  /api/sessions/{sessionId}/finalize
POST  /api/sessions/{sessionId}/intention
GET   /api/integrations/sessions/{sessionId}
```

浏览器会话使用 HttpOnly cookie；Unity 创建会话后持有只读集成 token。Play Ticket 只能换取一次地图，随后使用仅限该 attempt 的 token 提交指标。Stage 保存、恢复、消息、提案决定和 Play 创建均使用幂等键；同一消息键若改换内容或 Stage 返回 `409 IDEMPOTENCY_CONFLICT`，基于过期 Stage 的新写入返回 `409 VERSION_CONFLICT`。会话中的 assistant turn 可额外返回 `guidance`，记录本轮引导动作、暂定意图推测、核心问题和不含地图的修改方向；旧消息中的该字段为 `null`。

## 8000 在线匹配

8000 后端继续提供匿名两人房间、Ready、挑战交换、结果提交、问卷和 dashboard。房间位于单进程内存中，使用六位房间码、一秒轮询和 30 分钟惰性清理。

挑战请求只包含：

```json
{
  "rows": ["..."],
  "aiAssistantMode": "partial_completion"
}
```

旧客户端多传的 `competitionMode` 被兼容忽略，不验证、不存储、不返回。历史 JSONL 原件保留，但新事件和派生 dashboard 不暴露该字段。

## Build Settings

共创与匹配所需场景：

```text
Assets/Scenes/Menu.unity
Assets/Scenes/Matchmaking/Online/Online_Lobby.unity
Assets/Scenes/Matchmaking/Online/Match_Briefing.unity
Assets/Scenes/Matchmaking/Draft.unity
Assets/Scenes/Matchmaking/PC.unity
Assets/Scenes/Matchmaking/PC_Design.unity
Assets/Scenes/Matchmaking/PC_Level.unity
Assets/Scenes/Matchmaking/DG.unity
Assets/Scenes/Matchmaking/DG_Level.unity
Assets/Scenes/Matchmaking/Online/CoCreation_Entry.unity
Assets/Scenes/Matchmaking/Online/Challenge_Waiting.unity
Assets/Scenes/Matchmaking/Online/Online_Level.unity
Assets/Scenes/Matchmaking/Online/Match_Result.unity
Assets/Scenes/Matchmaking/Online/Questionnaire(Online).unity
```

## 验证

```powershell
python -m unittest discover -s Backend -p "test_*.py"
python -m unittest discover -s CoCreationPrototype/Backend/tests -p "test_*.py"
node --check CoCreationPrototype/Frontend/app.js
dotnet build Assembly-CSharp.csproj -v:minimal
```

完整手动回归应使用 Unity `2022.3.62f2c1`，分别走 PC 和 DG：

1. 生成首版后进入 `CoCreation_Entry`，确认 8010 Stage 1 rows 完全一致。
2. 连续创建至少三个 Stage，检查聊天、差异、历史恢复及中英文切换。
3. 试玩最新与历史 Stage，确认分别进入正确 PC/DG 场景且没有重新调用生成接口。
4. 覆盖通关自动返回、`R` 重开、完成提交重试和浏览器异常中断，确认指标累计且 Stage 数量不变。
5. 最终确认后填写意图；确认 Unity 只在这一步之后获得最终 rows 并进入 `Challenge_Waiting`。
6. 双端继续完成挑战交换、`Online_Level`、`Match_Result` 和在线问卷。

部署时先备份 8010 SQLite，再更新并重启独立服务；随后用指定 Unity 版本重建 WebGL、更新缓存键并上传 8000 静态构建。不要把 `.env`、API Key、SQLite、研究日志或 `WebGLBuild/` 提交到 Git。
