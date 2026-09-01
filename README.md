# Sokoban 人机共创与在线挑战

本仓库包含 Unity 2D Sokoban 客户端、8000 端口的匹配服务与研究 dashboard，以及 8010 端口的独立 LLM 共创工作台。当前原型把“与 LLM 共创地图”和“在 Unity 中游玩地图”分开：Unity 先生成并验证第一版地图，网页负责持续聊天、版本管理和可选试玩。

## 当前目标路由

```text
Menu
  → Questionnaire(Online1，可选择打开外部问卷或直接 Continue) → Online_Lobby → Match_Briefing → DG → DG_Level（生成并验证首版）
  → CoCreation_Entry（上传首版并创建 8010 会话）
  → 8010 Co-Creation Lab
      → Stage 1 = Unity 首版 rows
      → LLM 评价、连续聊天、手工编辑或 LLM 修改提案
      → 每次明确保存/接受才创建不可覆盖的新 Stage
      → 可选择任意已保存 Stage 并点击 Play
          → 8000 WebGL → DG_Level 只读试玩（PC_Level 为保留路径，当前未启用）
          → 通关动画与结果同步 → 自动返回同一 8010 会话
      → 明确确认最终 Stage
      → 填写设计意图
  → Unity 获得最终 rows
  → Challenge_Waiting → Online_Level → Match_Result
  → Questionnaire(Online2) → Menu
```

旧 `Competition_Mode`、`AI_Asistant_Mode` 以及 Competitive / Supportive 生成语义已经移除。匹配双方 Ready 后直接进入 DG 首版流程；PC 保留为暂未接入的实现资产。DG 当前询问四个中立的地图设计问题（首步检查、推箱依赖、空间分布、路线结构），前两题用于推断难度，后两题用于推断布局。AI 输出温暖的 AI reflection 以及难度/布局建议；四道答案只指导 8000 的首版生成，并在 Draft 研究节点中与 AI 推荐和用户最终确认一起保存，不会传入 8010 共创服务或其 LLM 上下文。

8010 共创服务、8000 中立匹配后端和包含 Stage Play 的 WebGL 部署通过 Nginx 暴露为 `http://111.231.136.4/cocreation/` 与 `http://111.231.136.4/game/`。8010 继续使用三栏 Pixel-adventure 工作台、五色引导卡、Stage 版本历史、试玩同步和最终意图流程；在线匹配会话提交最终意图后显示“返回 Unity 继续”按钮，由保留房间身份的原 Unity 标签页进入 `Challenge_Waiting`。当前 8010 前端脚本与样式缓存键为 `cocreation-continuous-progress-20260901-1`。

正式 Unity 共创会话在浏览器首次访问授权后开始 10 分钟倒计时。直访问示例会话不创建 deadline、不倒计时。截止后服务端锁定聊天、编辑、保存、恢复、试玩、提案与语言切换；网页只保留最终 Stage 提交。该提交可携带当前可解的本地草稿，并原子保存为最终人工 Stage 后进入意图填写。

8010 当前采用“聊天助手理解与协商 → 关卡修改助手执行局部候选 → 后端确定性校验与求解”的双角色交接。普通聊天只产生正文或概念引导，紫卡动作才可能授权地图提案；所有提案仍需用户明确接受后才创建新 Stage。风险质疑必须有具体地图、求解器、试玩或用户目标证据，不能因为意见不同就自动质疑。

## 共创规则

- `Stage 1` 必须与 DG 在 Unity 中通过格式检查及 `LevelSolver` 验证后的 rows 完全一致。
- 会话围绕同一个持续演化的关卡进行。聊天历史、版本、差异、评价、提案决定、试玩证据、最终版本和设计意图均持久化。
- 手工修改只存在于浏览器草稿中，点击“保存为新 Stage”后才成为版本；历史 Stage 永不覆盖。
- LLM 修改先以提案和差异预览呈现。提案必须相对基础 Stage 至少真实改变一个瓦片，且只有设计者接受并再次通过服务器求解与非空差异校验后才创建 Stage；零改动提案不能保存或接受。
- LLM 的难度和体验判断是主观意见；服务器与 Unity 求解器结果才是确定性证据。
- Play 只允许当前选中的已保存 Stage。未保存草稿、请求处理中或存在待决定提案时禁用。
- Play 不会修改地图、创建 Stage、确认最终版本或提交在线挑战。
- 最终确认后先收集设计者自然形成的意图；在此之前，集成接口不会向 Unity暴露最终 rows。
- 英文/中文切换影响新 UI、错误信息和后续 LLM 回复；历史消息保留原始语言。

## 当前 DG 与保留的 PC 资产

### 当前首版生成模式

- `DG_Level` 根据玩家描述获取 LLM 方案，通过模板生成和回退策略形成完整地图，并在 Unity 端验证。
- DG 四道问题见 [Draft_question.md](Draft_question.md)，reflection 与 Draft 记录规范见 [Draft首版理解助手（8000）.md](Multi-Agent/Draft首版理解助手（8000）.md)。
- 当前在线流程只启用 DG；PC/PC_Design/PC_Level 作为保留资产，不在 Build Settings 或导航中开放。
- 首版验证成功后不要求玩家先通关；DG rows 与 `description_generation` 写入 `CoCreationDraftContext`，随后加载 `CoCreation_Entry`。

### Stage 试玩模式

- 网页创建五分钟、一次性 Play Ticket，URL 只携带 attempt ID 与票据，不携带 rows。
- 8000 WebGL 的 Menu 启动组件换取完整 rows、来源、语言、提交 token 和返回 URL，并立即从地址栏清除票据。
- 当前 `description_generation` 加载 `DG_Level`；保留的 `partial_completion` 不在当前构建流程中调用。
- 试玩上下文存在时，生成控制器停用；指定 rows 再经 Unity `LevelSolver(maxSearchStates=300000)` 后加载。
- WASD 移动；`R` 重开地图，但累计移动、推动、重开和首次有效移动后的耗时不清零。
- 通关后保留完成动画和淡出，提交 `completed` 指标后自动返回原 8010 会话；Unity 内不再提供主动提前返回按钮，浏览器返回、关闭或刷新按 `interrupted` 处理。
- 试玩不会进入 `Challenge_Waiting`、`Match_Result` 或问卷。

## 8010 工作台

普通聊天会对明显的求助请求做确定性卡片路由：已有具体体验目标或修改方向、并请求建议、方案或修改方法时，优先出现 `REVISION / 修改建议` 卡；明确要求“帮我改”但没有具体方向时，先在正文中说明当前理解并显示可纠正的 `TENTATIVE INTENT / 暂定意图`，不直接形成修改方案；普通事实问题和开放式求思路留在正文。修改建议卡只是概念性方向，点击“请助手生成这个方案”后才进入授权提案流程，不会自动修改地图。

### 8010 双向质疑状态机

完整流程为：用户表达需求 → AI 理解意图 → 判断意图是否清楚 → AI 分析方案 → `REVISION` 或 `MANUAL EDIT`。意图不清时只在正文解释当前理解并显示 `TENTATIVE INTENT`，随后回到意图澄清；意图清楚后两条路线都先做风险检查。普通聊天的直接修改请求只生成概念紫卡，不能直接执行地图变更。

紫卡现在提供三个结构化动作：

- `execute_revision`（请助手生成这个方案）：校验当前 Stage 和来源 turn 后才启动 `RevisionPlan → executionContract → operation candidates → atomic deterministic execution → structure checks → validate_and_solve()`。通过后仍是待审查 proposal，只有用户接受才创建 `llm_accepted` Stage。
- `challenge_revision`（质疑这个方案）：先保存可见用户 turn，首轮只返回普通正文，重述方案、解释理由和目标游玩时刻并邀请用户说明异议；首轮不产生新卡或地图提案。
- `alternative_revision`（换一个方案）：不执行、不返回 `proposedRows`，要求模型提出与原紫卡核心处理不同的概念 `proposalOffer`；有限重试仍不能区分时只返回说明。

蓝色 `LET'S DISCUSS` 只表示尚未解决的真实分歧，不再作为普通提问或普通解释的容器。新 turn 使用可选的 `guidance.disagreement`：

```json
{
  "status": "active | resolved",
  "subject": "ai_revision | human_edit | user_request",
  "userPosition": "...",
  "aiPosition": "...",
  "coreDisagreement": "...",
  "nextQuestion": "...",
  "resolution": "user | ai | compromise | retain_current | null"
}
```

active 分歧不能生成紫卡；普通问题留在正文。AI 接受用户、用户接受 AI 或双方折中后才重新形成紫卡；`retain_current` 只结束分歧且不创建版本；没有共识则持续更新蓝卡并保留当前地图。红色 `WARNING` 只说明有具体地图、求解器、试玩或目标冲突证据支持的风险，蓝卡说明双方到底在哪个决策上有分歧，因此两者可以连续出现。

AI 的主动质疑适用于直接请求和已保存的 `human_edit`，但只能基于具体箱子、目标、墙体、水域、路径、推箱顺序、可达区域、求解器指标、试玩证据或此前明确目标。方案不同、用户拒绝 AI 或用户采用手动编辑本身都不是质疑依据。手动编辑仍先确定性验证并创建 `human_edit` Stage；失败保留草稿且不调用质疑流程，成功后 AI 使用前后 rows、完整 verified diff、changed components、map facts 和祖先 Stage 中用户明确目标进行复核。AI 发现具体风险时输出正文 + `WARNING` + `LET'S DISCUSS`，不得撤销或覆盖用户编辑；安全编辑只做普通确认和分析。

动作、分歧和手动编辑复核沿用 `audit_events`，记录 `card_action_requested`、三种卡片动作、`disagreement_started/updated/resolved` 和 `human_edit_reviewed`。`sourceTurnId` 必须引用当前 Stage 内带 `proposalOffer` 的 assistant turn，旧 turn 缺少 `disagreement` 时仍按旧 `followUpQuestion` 显示蓝卡。

目录：

```text
CoCreationPrototype/
├── Frontend/              # 独立 HTML/CSS/JS 三栏工作台
└── Backend/               # FastAPI、DeepSeek 客户端、SQLite 与测试
```

前端视觉复用 8000 MatchMaking/Train dashboard 当前生效的 Pixel-adventure 设计语言：草绿色网格背景、木质 sticky topbar、米白石质面板、蓝紫标题板、硬边像素阴影、Consolas 标题与像素地图框。8010 所有可见文字以 700 粗体为基础，按钮和状态标签使用 800，页面标题保留 900。桌面仍为 Stage / 聊天 / 地图编辑器三栏；低于 1200px 收为两栏，低于 900px 为单栏，680px 下进一步缩小阴影、间距与瓦片。8010 保留独立 CSS，不在运行时引用 8000 文件。

LLM 使用理性、亲切、以第一人称为主的朋友式共创策略，并明确避免机械复述、固定开头、客服话术及“回应—评价—提问”模板。普通设计回复通常保留两至四段紧凑交流，给具体观察、个人判断、原因和可想象的游玩瞬间更多展开空间，但不写成报告。Stage 1 的首条开场不提问，并会明确说明：LLM 只能协助小范围、可审查的改动、建议与思路梳理；大幅重做应先由设计者在右侧编辑器完成。之后，普通问题留在正文；蓝色 `LET'S DISCUSS / 一起聊聊` 只表示具体且尚未解决的分歧。手动保存的 `human_edit` Stage 会先确认真实改动和可解性并正常分析，只有有具体机械证据时才生成 `WARNING + LET'S DISCUSS`，不能为了制造互动强行质疑。用户的明确立场可以形成可纠正的橙色意图卡，但紫卡或 active 蓝卡场景按对应卡片优先级处理，不能重复制造卡片。玩家说“你帮我改”“你来改吧”“按这个思路改”等自然请求时只会先形成紫卡，必须点击紫卡动作才进入严格地图提案流程；“你觉得怎么改”只讨论思路。普通聊天不得声称“改好了”或邀请试玩尚未生成的地图，已验证提案也必须等玩家明确接受才会成为新 Stage。执行授权本身不再被误写进橙色意图卡。整个共创会话始终只有一关，所有 Stage 都只是这同一关的保存版本；提示词与保存前规范化会拦截错误进度表述。其余意图、五色卡片、强证据风险门槛与严格地图验证流程保持不变。

蓝色与紫色卡片不会再把正文原句直接搬进去。紫卡使用短标题总结实际修改方向，并在下方独立展开预期的游玩影响和验证重点；蓝卡固定总结用户立场、AI 立场、核心分歧和下一问题。普通问题不生成新蓝卡。完整地图提案的模型阶段先编译 `RevisionPlan`，再由关卡修改助手返回局部操作候选；计划结构不合法或候选非法时使用安全的结构反馈重试，候选仍失败则调用生产中的确定性 `search_revision_plan()` 兜底。地图构造、结构校验和求解全部由后端完成，不再把失败地图交给模型重画。

LLM 每轮还会接收初稿方法与当前 Stage 来源，避免把生成器产物误说成玩家亲手设计。DG 首版只把上游描述和参数选择归因给玩家，具体墙体、水域、箱子、目标和玩家位置均作为生成结果讨论；由于 8010 当前不保存具体参数值，LLM 不得猜测参数。保留的 PC 首版路径只把箱子起点、目标和整体草图约束视为玩家输入，不能询问玩家为何放置生成水域、某个内部墙或玩家出生点。玩家在工作台保存的手工修改可以依据确定性 diff 归因；接受 LLM 提案和恢复历史版本则分别表述为共同接受的方向和重新查看旧版本。这些规则同时作用于首轮和后续对话。完整地图提案还会逐格对照当前 Stage：后端拒绝空 diff，prompt 要求修改摘要只能陈述 rows 中实际发生的更改，保存提案和接受提案时各有一次独立校验。

当玩家适合直接实践自己的想法时，LLM 可以简短提示使用右侧地图编辑器并保存为新 Stage，但不把手工编辑说成必选步骤。手工 Stage 保存前必须通过统一的尺寸、符号、玩家/箱子/目标数量、封闭外墙与 Sokoban 可解性校验：地图可以是不规则轮廓并在外围保留空白 void，但外部只能被连续的 `#` 墙阻断，水域 `@` 不能替代外墙。通过后，后端会相对父版本确定性识别外壳、水域、内部墙体、箱子位置、目标点、玩家位置及可用地面的变化；新 Stage 的回复先准确确认这些改动和可解状态，再由 LLM评价其设计影响并继续对话。

对话以保存的 Stage 为边界：每条 turn 仍完整持久化并带有 `versionId`，但页面选择某个 Stage 时只显示该版本相关的对话，LLM 也只接收当前 Stage 的最近对话。唯一的关系型承接是已接受提案的 assistant turn：它不复制、不改写原记录，而通过 proposal/decision/version 关联同时显示为新 Stage 的第一条上下文。历史 Stage 的对话只读；返回当前 Stage 后才可继续发送，并恢复当前 Stage 自己的草稿或未完成请求。

8010 的一次业务请求使用真正的 60 秒总时长硬上限。普通聊天和 Stage 开场优先使用 `deepseek-v4-flash`，并沿用最多 40 秒的首选模型窗口；明确执行紫卡完整地图提案时优先使用 `deepseek-v4-pro` 编译计划，首次最多 18 秒，结构纠正或 Flash 传输后备最多 8 秒，模型阶段合计不超过 26 秒。确定性候选搜索在请求第 55 秒停止，为持久化和响应预留五秒。上游显式关闭思考输出，避免推理 token 耗尽后只返回空 JSON。普通问题留在正文；新蓝卡必须由结构化 active `disagreement` 驱动，旧 turn 才沿用 `followUpQuestion`。服务日志记录安全结构原因和搜索摘要，两次计划尝试共用同一 requestId 和消息幂等键；只有显式 `execute_revision` 且验证成功的提案才会入库为 proposal，搜索耗尽则保存说明而不创建 proposal。聊天输入区显示实时秒数与主/备用阶段，并在 65 秒执行浏览器安全中止；失败、刷新或连接中断后使用原消息幂等键重试，确保同一请求最多保存一条 user turn 和一条 assistant turn。

后端使用独立 SQLite/WAL。默认数据库为 `CoCreationPrototype/Backend/data/cocreation.sqlite3`，`.env` 和数据库文件均被本目录 `.gitignore` 排除。
完整地图修改采用“LLM 意图编译器 → 结构化 execution brief → RevisionPlan/executionContract → 关卡修改助手候选 → 确定性原子校验与求解 → 确定性局部搜索兜底 → 待审查提案”。`proposalOffer.summary/rationale` 继续负责用户可见内容；可选的 `proposalOffer.executionBrief` 保存 effect、锚点、focus、精确 `requiredTransitions`、允许算子、保护组件和游玩目标。明确坐标及 `from → to` 是硬约束，服务端会用当前 Stage 的 `tileAt` 事实再次核对；冲突时只要求澄清，不猜邻近格，也不生成错误紫卡。`RevisionStrategy` 仍描述效果、焦点区域、允许算子、保护项、修改预算和客观指标方向；后端负责扩展 `add/remove wall`、`move player/box/target` 与 `add/remove water`。精确的一格结构修改允许 1 个真实变更格，实体移动必须成对清除旧位置和写入新位置，多格修改按实际预算计算，最大 12 格；外壳与 void 从候选空间排除。候选必须真正使用能够实现其声明效果的算子、满足声明的指标变化，并遵守“不要改水/墙/玩家/箱子/目标”等明确保留项；否则会被拒绝而非作为低分方案选出。修改助手候选全部非法或不可解时，生产链会在 focus、锚点、effect、requiredTransitions 和保护规则内调用 `search_revision_plan()`；只有真实 tile 变化、结构合法且可解的候选才能进入 proposal。搜索最多构造 64 张地图、保存最多八张可解候选，并按明确要求、指标方向、区域/组件吻合度、最小改动和稳定签名选择一份。失败按坐标冲突、契约冲突、模型候选非法或确实没有可解候选分类；不会放宽要求、覆盖当前 Stage 或撤销手动编辑。旧 `proposalOffer` 没有 execution brief 时继续读取，但无法可靠恢复精确意图时优先要求澄清。

模型/传输错误（超时、连接失败、空响应、非法 JSON 等）第一次返回可重试错误，前端保留原消息幂等键并显示一次 Retry；Retry 再次失败后保存一条手动编辑/继续讨论的说明，不再继续提供 Retry。若模型已经生成有效 `RevisionPlan`，但确定性搜索找不到同时满足要求且可解的地图，系统将该结果作为已处理的业务结果返回：显示说明和风险提示，不自动放宽要求、不创建提案、不修改当前 Stage，也不显示 Retry。说明会明确邀请设计者亲自在右侧编辑器调整，或继续与 AI 商讨如何缩小、重新表述修改目标；只有新的明确授权和可行提案才会进入地图提案流程。合法计划在构造零候选时仍会执行一次内部结构修正；历史会话中已经存在的 `relaxationOffer` 仍按旧流程兼容读取和确认，但新失败请求不会创建新的放宽流程。

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

### REVISION 卡片的最新方案限制

同一个当前 Stage 中可以保留多条紫色 `REVISION` 建议，但只有按 `conversation_turns.sequence_number` 判定的最近一条有效 `proposalOffer` 可以执行、质疑或换方案。较早紫卡仍显示在对话历史中，但三个按钮禁用并提示仅最新方案可操作。普通 assistant 正文不会使紫卡失效；后端会再次校验 `sourceTurnId`，因此旧标签页或直接请求也不能绕过该限制。历史 Stage 仍只读，地图提案的接受/拒绝动作不受此规则影响。

## 8010 API

```text
POST  /api/sessions
POST  /api/demo-sessions
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

浏览器会话使用 HttpOnly cookie；Unity 创建会话后持有只读集成 token。Play Ticket 只能换取一次地图，随后使用仅限该 attempt 的 token 提交指标。Stage 保存、恢复、消息、提案决定和 Play 创建均使用幂等键；同一消息键若改换内容或 Stage 返回 `409 IDEMPOTENCY_CONFLICT`，基于过期 Stage 的新写入返回 `409 VERSION_CONFLICT`。会话中的 assistant turn 可额外返回 `guidance`，记录本轮引导动作、暂定意图、`proposalOffer`、可选 `disagreement` 和至多两个 `manual_edit` / `warning` UI cues；新 turn 只有 active `disagreement` 显示蓝卡，历史 `tradeoff` cue 继续兼容读取并按红色警告展示。

## 8000 在线匹配

8000 后端继续提供匿名两人房间、Ready、挑战交换、结果提交、问卷和 dashboard。房间位于单进程内存中，使用六位房间码、一秒轮询和 30 分钟惰性清理。

Online1 是问卷星中的双语匹配前筛选问卷：收集性别、年龄区间、既往推箱子经验及是否与另一组两位参与者均为陌生关系；隐藏的第 5 题 `studySessionId` 由 Unity 以 `q5` 参数写入。Online2 是独立的 21 题双语赛后问卷，隐藏的第 22 题 `studySessionId` 由 Unity 以 `q22` 参数写入。

Online Lobby 的生成房间码显示在静态浏览器只读输入框中，可以选中后手动复制；加入房间输入框支持粘贴并规范化为六位字母数字码。两个输入框通过 WebGL 模板与 `BrowserNavigation.jslib` 同步到 Unity，不再提供 `COPY CODE` 按钮。

WebGL 页面底部的 `DATA DASHBOARD` 按钮会先在游戏页面内显示 Dashboard 访问密码框，密码通过 8000 的校验接口后才打开 Dashboard；取消或校验失败不会跳转。Dashboard 内的删除操作仍单独使用同一密码校验。

在线共创研究记录按 `matchId` 分为 Player 1 和 Player 2 两条追加式 JSONL 流程。只有从 `Menu` 在线入口启动的玩家在 8010 成功产生 `first_stage` 后才正式落盘：DG 确认初稿设置时，Draft 只暂存在当前房间内存中，不写流程 JSONL；收到 `first_stage` 后才按 Draft → First Stage 顺序追加。未从 Menu 启动的玩家不会写入匹配生命周期、Draft、Stage、Message 或 Final 记录；其 8010 同步会成功返回但被 8000 直接丢弃，以免中断共创网页。Dashboard 默认只显示至少一位玩家同时具有 Menu 起点标记与 `first_stage` 的比赛；旧记录缺少该标记时不会显示。达到门槛但尚未完整结束的比赛仍显示实际的 `In progress`、`Expired` 或 `Cancelled` 状态，只有两位玩家都提交结果才标记为 `Completed`。Draft 节点保存四道中立题的内部答案、AI reflection、难度/布局理由、AI 推荐值、推荐来源及用户最终 `finalDifficulty` 与 `finalLayout`。后续可见节点为 `first_stage`、`stage`、`turn`、`final` 与 `message`；每个 Stage 的自动 AI 首评以内部 `opening` 记录追加保存，不另占时间线节点。Dashboard 会把未被后续首轮问答使用的首评附在对应 Stage；若紧接着出现该 Stage 的首轮主动问答，则合并展示为“AI 首评 → 玩家消息 → AI 回复”，避免重复呈现同一首评。

8010 的 `final` 事件会记录 `coCreationDurationSeconds`，定义为首次打开共创网页到确认最终 Stage 的耗时，按服务器十分钟期限计算并限制在 0–600 秒；设计意图填写不计入。对手游玩 Final Stage 时按 `R` 重开的次数通过 8000 结果字段 `restartCount` 记录，在 Challenge maps、Compare player challenges 和 Final map 的 `Opponent restarts` 中展示；网络重试、重新进入场景和问卷重填不计入，旧结果缺少字段时显示 `-`。对手游玩时长仍来自 8000 `result_submitted.durationSeconds`，只在 Result submitted 和挑战地图详情中展示。Dashboard 的 Match ID 和两位玩家 `studySessionId` 默认显示前 8 位，旁边的复制按钮复制完整值，搜索支持完整值与短值。

### 8010 跨 Stage DesignContext

地图版本通过 `parentVersionId` 和 `diff` 继承同一个关卡的结构演化；现在每个 `level_versions` 还保存可空的 `design_context_json` 快照。快照记录用户目标、设计约束、确认过的决策、拒绝过的方案、开放问题和 active disagreement，包含来源 Stage/turn，不新增数据库表。旧数据库启动时自动迁移并按 Stage 顺序保守回填，回填不调用模型，也不把 AI 普通正文当成用户意图。

DesignContext 的 authority 分为三层：`explicit` 是用户自己说出的目标或约束，`confirmed` 只能由用户确认/接受提案/接受折中/正式保留现状形成，`inferred` 只是 AI 可纠正的暂时理解。旧内容不会删除，用户修正时标记 `superseded` 或 `rejected` 并保留来源；inferred 不能覆盖更高权限内容，也不能单独驱动地图执行。最终 `designer_intentions` 仍是最终提交时的自报告，不与该后台记忆混用。

新 Stage（接受提案、手动保存、restore）复制父 Stage 的最新快照后再记录本次事件。接受提案追加 `confirmedDecision`，拒绝提案追加 `rejectedDecision`；手动编辑只继承语义记忆，地图 diff 本身不被自动解释为用户目标。Chat 读取完整语义快照，Revision 只接收 active explicit/confirmed 目标、约束、确认/拒绝决策、相关开放问题和执行 brief，Evaluator/手动编辑复核读取带来源的完整投影；三者不重新从跨 Stage 原始聊天猜测意图，且 8010 永不接收 8000 DG context、研究者目标或实验条件。

普通聊天可返回内部 `designContextPatch`，服务端负责校验和合并；非法 patch 只写失败审计，不阻塞可见对话。该字段被前端隐藏，不增加卡片或颜色。既有 REVISION、MANUAL EDIT、WARNING、LET'S DISCUSS、TENTATIVE INTENT 和最新紫卡唯一可操作限制保持不变。

挑战提交当前只包含地图 rows 和兼容用的初稿模式字段：

```json
{
  "rows": ["..."],
  "aiAssistantMode": "description_generation"
}
```

`aiAssistantMode` 仅为旧接口兼容字段，不在 dashboard 作为用户可见模式展示；当前在线路线固定为 `description_generation`。旧客户端多传的 `competitionMode` 或关系/体验字段被兼容忽略，不验证、不存储、不返回。历史 JSONL 原件保留，但新事件和派生 dashboard 不暴露这些字段。

## Build Settings

当前启用的共创与在线匹配场景：

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

`PC.unity`、`PC_Design.unity` 和 `PC_Level.unity` 仍作为禁用的历史实现资产保留，不属于当前 Build Settings 或在线导航。

## 验证

```powershell
python -m unittest discover -s Backend -p "test_*.py"
python -m unittest discover -s CoCreationPrototype/Backend/tests -p "test_*.py"
node --check CoCreationPrototype/Frontend/app.js
dotnet build Assembly-CSharp.csproj -v:minimal
```

## Tutorial PDF

Menu 的 `Tutorial` 按钮会打开 `http://111.231.136.4/frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf`。双语 PDF 由现有 8000 `/frontend/` 静态路由提供，并在浏览器的 PDF 查看器中打开，不会离开当前 Unity Menu 页面。

完整手动回归应使用 Unity `2022.3.62f2c1`，按当前 DG 在线路线执行：

1. 生成首版后进入 `CoCreation_Entry`，确认 8010 Stage 1 rows 完全一致。
2. 连续创建至少三个 Stage，检查聊天、差异、历史恢复及中英文切换。
3. 试玩最新与历史 Stage，确认进入 `DG_Level` 且没有重新调用生成接口。
4. 覆盖通关自动返回、`R` 重开、完成提交重试和浏览器异常中断，确认指标累计且 Stage 数量不变。
5. 最终确认后填写意图；确认完成卡仅在在线匹配会话显示“返回 Unity 继续”，点击后聚焦原 Unity 标签页并关闭 8010 标签页，Unity 只在意图提交之后获得最终 rows 并进入 `Challenge_Waiting`。
6. 双端继续完成挑战交换、`Online_Level`、`Match_Result` 和在线问卷。

如需单独回归保留的 PC 路径，必须先在 Unity Build Settings 中重新启用相应场景；这不属于当前在线研究路线。

部署 8010-only 变更时先备份 SQLite，再只上传变更的 `CoCreationPrototype` 文件并重启独立服务；不构建或上传 WebGL。只有用户提供或明确要求 WebGL 更新时，才按指定 Unity 版本构建、更新缓存键并上传。不要把 `.env`、API Key、SQLite、研究日志或 `WebGLBuild/` 提交到 Git。

## 8010 直访问单次测试模式

直接访问 `/cocreation/` 时，页面只显示“创建示例会话” landing，不读取旧的
`localStorage` 会话，也不会请求上一轮会话、对话、Stage 或地图记录。点击按钮后立即显示
“正在使用算法创建示例地图……”，再由 8010 后端参考 Unity `Algorithm_Level` 的结构模板、
墙体/水域布局和反向拉箱流程生成 10×12、两箱、两目标的可解地图，然后自动开始首轮 assessment。
刷新当前演示会话 URL 可以继续当前测试；演示完成后不显示“返回 Unity 继续”。
演示数据只写入 8010，不调用 8000 同步接口，不启动十分钟 deadline，也不记录正式匹配的
`coCreationDurationSeconds`。每次新演示会话在新地图验证和新会话创建成功后，清理上一轮
演示会话及其关联的对话、版本、试玩、提案和审计记录；正式 Unity 会话不会被清理。若地图
生成或会话创建失败，上一轮演示记录保持不变。前端静态资源缓存键为
`cocreation-continuous-progress-20260901-5`。
