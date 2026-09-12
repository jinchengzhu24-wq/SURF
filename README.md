# Sokoban 人机共创与在线挑战

本仓库包含 Unity 2D Sokoban 客户端、8000 端口的在线匹配与研究 Dashboard，以及独立的 8010 LLM 共创工作台。

当前产品把“地图共创”和“Unity 游玩”分开：Unity 负责生成并验证首版地图，8010 网页负责聊天、版本管理、手工编辑、AI 提案和 Stage 试玩；最终确认后，Unity 再取得最终地图进入在线挑战。

## 当前在线路线

```text
Menu
  → Questionnaire(Online1)
  → Online_Lobby
  → Match_Briefing
  → DG
  → DG_Level（生成并验证首版）
  → CoCreation_Entry
  → 8010 Co-Creation Lab
      → Stage 1 = Unity 首版 rows
      → 聊天、手工编辑、AI 提案和 Stage 试玩
      → 明确确认最终 Stage
      → 填写设计意图
  → Challenge_Waiting
  → Online_Level
  → Match_Result
  → Questionnaire(Online2)
  → Menu
```

Online1 是共创前的匹配问卷，Online2 是比赛后的问卷；两者都是每轮在线匹配的一部分。Tutorial 按钮打开浏览器 PDF：
`http://111.231.136.4/frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf`。

## 系统边界与当前实现

- 公网入口为 `http://111.231.136.4/game/`、`http://111.231.136.4/frontend/` 和 `http://111.231.136.4/cocreation/`。用户访问时使用 Nginx 的 80 端口，不在公开链接中使用 `:8000` 或 `:8010`。
- DG 使用四道中立地图设计问题：首步检查、推箱依赖、空间分布和路线结构。Q1–Q2 只用于 8000 的难度建议，Q3–Q4 只用于 8000 的布局建议；DG context 不会传入 8010 或其 LLM 上下文。
- 8000 的两个 Agent 使用 `deepseek-v4-flash`；8010 的聊天助手、关卡修改助手、Stage 开场、翻译、Revision 和意图反馈审查使用 Kimi `kimi-k2.6`。8010 不读取或回退到 8000 的 DeepSeek 环境变量。
- `Draft` 场景已退役。`PC`、`PC_Design` 和 `PC_Level` 仅作为历史实现资产保留，不在当前 Build Settings 或在线导航中。
- 8010 正式 Unity 会话在首次浏览器访问后启动服务端 deadline，当前为 20 分钟。到期后聊天、编辑、保存、恢复、试玩和提案锁定，只保留最终 Stage 提交；提交时可将当前可解的本地草稿原子保存为最终 `human_edit` Stage。
- 直接访问 `/cocreation/` 创建的是独立演示会话，不启动 deadline、不同步 8000，也不写入正式匹配记录。

## 8010 工作台规则

- 一个会话始终只包含同一个关卡；`Stage 1`、`Stage 2` 及后续 Stage 都是该关卡的不可变版本，不是不同关卡或关卡 progression。
- Stage 1 必须与 Unity DG 验证后的 rows 一致。手工编辑和 AI 提案都必须通过尺寸、符号、实体数量、外墙和 Sokoban 可解性校验。
- 手工草稿只有保存为新 Stage 后才持久化。AI 提案先保存为待审查 proposal，只有用户明确接受并再次通过后端验证，才创建新 Stage。
- Play 只针对已保存 Stage，不会修改地图、创建 Stage、确认最终版本或提交在线挑战。试玩会保存到对应 Stage 的 `play_attempts`。
- 地图事实以当前 StageSnapshot 为唯一来源。服务器会重新校验当前坐标、实体、路线和可点击链接；历史 Stage、旧助手文本和用户错误坐标不能作为当前地图事实。
- 普通聊天只返回经过校验的分析文本；proposal、disagreement、intent hypothesis 等内部字段经过服务端投影后才可供前端显示。研究者目标、实验条件和 8000 DG context 不进入 8010。

## 8010 后端数据保留

8010 使用独立 SQLite/WAL 数据库，默认路径为 `CoCreationPrototype/Backend/data/cocreation.sqlite3`。数据库和 `.env` 不提交到 Git。正式 Unity 会话的历史记录不会因新会话创建而清理；独立演示会话只保留最新一轮。

| 表 | 当前保留的数据 |
| --- | --- |
| `design_sessions` | 会话身份、demo 标记、匹配 ID/玩家编号、初稿方法、语言与锁定时间、deadline、当前/最终版本、状态和时间戳。访问、集成和 bootstrap token 只保存哈希，不保存明文 token。 |
| `level_versions` | 每个不可变 Stage 的编号、`parent_version_id`、来源、完整地图 rows、摘要、diff、验证结果、实体绑定和该 Stage 的 `design_context_json` 快照。 |
| `conversation_turns` | 用户与助手的完整消息、角色、序号、所属 Stage、语言、请求 ID，以及必要的模型尝试次数、延迟、引导信息和 proposal binding。 |
| `turn_translations` | 已翻译的助手正文、翻译后的引导/提案摘要、语言、模型元数据和时间戳。 |
| `llm_assessments` | 每个 Stage 的开场/评估归档及其关联助手 turn。Stage assessment 只作为后端记录，不渲染为独立评价卡。 |
| `change_proposals` | AI 提案的基础 Stage、候选地图、摘要、真实 diff、确定性验证结果、状态、来源助手 turn 和决定时间。 |
| `designer_decisions` | 用户对提案的接受/拒绝等决定、理由、关联 proposal/version、幂等键和时间。 |
| `play_attempts` | 某个已保存 Stage 的试玩票据和状态，以及加载、首次移动、结束时间、耗时、移动数、推动数、重开数和求解基准。 |
| `designer_intentions` | 最终 Stage 确认后的用户自报告意图、语言和提交时间；它不与 AI 的后台意图记忆混用。 |
| `audit_events` | 会话生命周期、版本、问题、提案、决定、分歧、手工编辑、试玩证据、意图反馈、deadline 和集成同步等追加式审计事件。 |
| `revision_challenges` | 针对 proposal 的结构化质疑状态、基础 Stage、来源 turn、当前理由 turn 和更新时间。 |
| `challenge_reason_reviews` / `challenge_review_requests` | 质疑理由审查结果、状态、失败原因、尝试次数、幂等请求和可重试记录。 |

`GET /api/sessions/{sessionId}` 返回的是服务端投影，包括 `versions`（含每个 Stage 的 `playAttempts`）、`progressContexts`、`turns`（含公开 `guidance` 和 `translations`）、`assessments`、`proposals`、`challengeReviewRecords` 和 `intention`。原始 DesignContext、内部审计字段、隐藏 token 和 prompt-only 字段不会直接公开。

## 跨 Stage 数据与 DesignContext

每个 Stage 是同一关卡的版本快照。新 Stage 通过 `parentVersionId` 连接父 Stage，保存父 Stage 的最新地图和 DesignContext 副本，再追加本次事件；父快照保持不变。历史 Stage 只读取自己的 lineage，不会看到后续 Stage 的回答、记忆、忽略/恢复状态或证据。

每个 `level_versions.design_context_json` 快照可包含：

- 用户目标 `userGoals` 和设计约束 `designConstraints`；
- 已确认决定 `confirmedDecisions` 和被拒绝决定 `rejectedDecisions`；
- 开放问题 `openQuestions`，包括 open、answered、ignored 等状态；
- `activeDisagreement`、已处理证据 ID 和来源 Stage/turn；
- 跨 Stage 继承的 `intentHypotheses`。

不同调用场景使用同一快照的不同投影：Chat 可读取完整语义快照；Revision 只读取 active 的 explicit/confirmed 目标、约束、决定、相关问题和执行 brief；手工编辑复核可读取带来源的完整投影。它们不会重新从跨 Stage 原始聊天猜测当前意图。

## 用户意图推测与证据

8010 会保留模型对用户设计方向的可纠正理解，但不会把推测当成事实：

- `explicit` 只表示用户自己明确表达的目标或约束。
- `confirmed` 只由用户确认、接受提案、接受折中或正式保留现状形成。
- `inferred` / `tentative` 是 AI 的暂定假设，必须保持可纠正，不能直接成为地图执行的硬约束。
- `intentHypotheses` 保存主题、陈述、状态、置信度、支持/反驳证据 ID、来源 Stage/turn、展示文本和反馈动作。历史假设会保留为 `rejected`、`superseded` 或 `legacy_unverified`，而不是被静默删除。
- 意图证据会以 `intent_evidence_recorded` 追加到 `audit_events`。证据可来自用户表达、提案接受/拒绝、分歧解决、确定性手工 diff、Stage 恢复和试玩结果；单个行为证据本身不会自动等同于用户意图。
- 橙色 TENTATIVE INTENT 卡只能通过专用反馈确认、修订或否定。每个普通非方案轮次都由主 Kimi 输出 `intentDecision`，再由独立 Kimi `intent_candidate_review` 检查误报、漏报、原话证据、正文/卡片一致性和与确认意图的关系；复核得到的 claims 是本轮锁定语义，服务器不得用关键词补写或改写。正文、卡片和冲突说明按组件验收：已经合格的组件必须保留，卡片或正文表达不完整时由 `intent_component_repair` 只修对应组件，不能重新生成并丢弃整轮内容。句数、字数、影响词和边界词命中仅作软性质量信号；原文证据、方向、冲突 ID、StageSnapshot 地图事实、截断和语义越界仍是硬约束。旧关键词抽取结果一律视为 `legacy_unverified`，不得触发确定性冲突。通过复核的同对象同属性明确反向可由服务器确定性进入新旧双卡选择，其余范围、作用面、体验和优先级冲突采用复核结论。只有锁定语义或地图事实无法可靠恢复时才整轮返回可重试错误，且不写入不完整助手轮次。
- “设计倾向”只投影 confirmed hypothesis，并显示最多 12 条逐 Stage 的证据轨迹；pending、rejected、tentative 和未经确认的 inferred 内容不会进入 Revision 硬约束。

## 8010 与 8000 的数据边界

8010 是完整共创记录的权威来源。正式会话只向 8000 同步必要的研究投影：首版 `first_stage`、已保存的人工/AI Stage、Stage opening/turn 以及最终 `final` 事件。最终事件中的 `coCreationDurationSeconds` 由 8010 服务端计算，当前范围为 0–1200 秒；对手游玩时长仍使用 8000 的 `result_submitted.durationSeconds`。

Unity 的集成接口只有在会话完成后返回最终 rows 和用户最终自报告意图。8010 不把 DesignContext、intentHypotheses、完整聊天、研究者目标或实验条件发送给 8000 的 LLM 或 Unity 执行流程。

## 8010 API 分类

8010 API 按功能分为以下几类，具体请求模型和校验以 `CoCreationPrototype/Backend/app.py` 为准：

- 会话创建、浏览器访问、语言锁定和状态读取；
- Stage 创建、恢复、评估和进度读取；
- 普通聊天、翻译、问题反馈和意图假设反馈；
- 提案决定、Revision challenge、理由审查和幂等重试；
- Stage Play ticket、试玩启动、进度、完成和中断；
- 最终 Stage 提交、最终意图和 Unity 集成读取；
- `/health`、`/ready` 和独立演示会话入口。

浏览器使用 HttpOnly cookie；Unity 创建正式会话后持有只读 integration token。Stage 保存、恢复、消息、提案决定和 Play 创建均使用幂等键；过期 Stage 写入返回版本冲突，改换同一消息键内容返回幂等冲突。

## Build Settings

当前在线路线启用的 Unity 场景：

```text
Assets/Scenes/Menu.unity
Assets/Scenes/Matchmaking/Online/Online_Lobby.unity
Assets/Scenes/Matchmaking/Online/Match_Briefing.unity
Assets/Scenes/Matchmaking/DG.unity
Assets/Scenes/Matchmaking/DG_Level.unity
Assets/Scenes/Matchmaking/Online/CoCreation_Entry.unity
Assets/Scenes/Matchmaking/Online/Challenge_Waiting.unity
Assets/Scenes/Matchmaking/Online/Online_Level.unity
Assets/Scenes/Matchmaking/Online/Match_Result.unity
Assets/Scenes/Matchmaking/Online/Questionnaire(Online1).unity
Assets/Scenes/Matchmaking/Online/Questionnaire(Online2).unity
```

## 演示模式

直接访问 `/cocreation/` 时，页面显示“创建示例会话”。8010 后端生成 `algorithm_demo` 的 10×12、两箱、两目标可解地图并开始 Stage 1 开场。演示数据只写入 8010，不创建正式 deadline、不同步 8000，也不记录正式匹配的 `coCreationDurationSeconds`。

创建新的演示会话成功后，只清理上一轮演示会话及其关联的聊天、版本、试玩、提案和审计记录；正式 Unity 会话不会被清理。如果新地图或新会话创建失败，上一轮演示记录保持不变。

## 验证

```powershell
python -m unittest discover -s Backend -p "test_*.py"
python -m unittest discover -s CoCreationPrototype/Backend/tests -p "test_*.py"
node --check CoCreationPrototype/Frontend/app.js
dotnet build Assembly-CSharp.csproj -v:minimal
```

完整手动回归应使用 Unity `2022.3.62f2c1`，覆盖：Stage 1 rows 一致性、连续创建和恢复多个 Stage、最新/历史 Stage 试玩、完成/中断指标、意图确认、最终 rows 返回 Unity，以及在线挑战和两次问卷。

8010-only 部署应先备份 SQLite，再只上传变更的 `CoCreationPrototype` 文件并重启独立服务；不要构建或上传 WebGL。WebGL 只有在用户提供或明确要求构建时才更新。不要把 `.env`、API Key、SQLite、研究日志、Unity 缓存或 `WebGLBuild/` 提交到 Git。部署细节见 [SERVER.md](SERVER.md)。
