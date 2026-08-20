# Sokoban 人机共创与在线挑战

本仓库包含 Unity 2D Sokoban 客户端、8000 端口的匹配服务与研究 dashboard，以及 8010 端口的独立 LLM 共创工作台。当前原型把“与 LLM 共创地图”和“在 Unity 中游玩地图”分开：Unity 先生成并验证第一版地图，网页负责持续聊天、版本管理和可选试玩。

## 当前目标路由

```text
Menu
  → Online_Lobby → Match_Briefing → DG → DG_Level（生成并验证首版）
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

旧 `Competition_Mode`、`AI_Asistant_Mode`、`Draft` 以及 Competitive / Supportive 生成语义已经移除。匹配双方 Ready 后直接进入 DG 首版流程；PC 保留为暂未接入的实现资产。DG 中确认的关系、体验与最终难度只指导 8000 的首版生成，不会传入 8010 共创服务或其 LLM 上下文。

8010 共创服务、8000 中立匹配后端和包含 Stage Play 的 WebGL 部署通过 Nginx 暴露为 `http://111.231.136.4/cocreation/` 与 `http://111.231.136.4/game/`。8010 继续使用三栏 Pixel-adventure 工作台、五色引导卡、Stage 版本历史、试玩同步和最终意图流程；在线匹配会话提交最终意图后显示“返回 Unity 继续”按钮，由保留房间身份的原 Unity 标签页进入 `Challenge_Waiting`。前端脚本缓存键为 `cocreation-nginx-prefix-20260818-2`，样式缓存键为 `cocreation-return-unity-20260814-19`。

8010 工作台在浏览器首次访问授权后开始 10 分钟倒计时。截止后服务端锁定聊天、编辑、保存、恢复、试玩、提案与语言切换；网页只保留最终 Stage 提交。该提交可携带当前可解的本地草稿，并原子保存为最终人工 Stage 后进入意图填写。

2026-08-14 的地图改善提示词先更新为 `cocreation-v29-intent-search`：明确授权后，Pro 只把玩家方向编译成一至三个结构化 `RevisionStrategy`，不再输出地图 rows 或原子格子操作。后端使用语义算子、宽度 16/深度 3 的确定性局部搜索、最多 64 个内部候选、现有结构校验与 300,000 状态 Sokoban 求解器选择最多八个可解候选中的最优方案。模型阶段最多两次且总计不超过 26 秒，搜索在业务请求第 55 秒停止；完整请求仍受 60 秒墙钟限制。随后部署的 `cocreation-v30-disagreement-intent` 规定：玩家明确重新界定或反驳助手对难度、优先级或游玩效果的判断时，必须出现一张可纠正的暂定意图卡。`v31-zero-candidate-correction` 还会在首份合法计划因操作/焦点没有可编辑格子而构造零候选时，用现有的第二次 Pro 调用附带安全原因纠正计划。只有已有合法计划但确定性搜索找不到满足全部条件的可解修改时，才进入现有的局部效果放宽商量流程。Quality-Diversity / MAP-Elites 保留为后续候选多样性升级，不属于当前版本。本地和服务器 151 项测试、Python 编译及本地 JavaScript 语法检查通过；部署未改动数据库内容或 8000 服务。

## 共创规则

- `Stage 1` 必须与 PC/DG 在 Unity 中通过格式检查及 `LevelSolver` 验证后的 rows 完全一致。
- 会话围绕同一个持续演化的关卡进行。聊天历史、版本、差异、评价、提案决定、试玩证据、最终版本和设计意图均持久化。
- 手工修改只存在于浏览器草稿中，点击“保存为新 Stage”后才成为版本；历史 Stage 永不覆盖。
- LLM 修改先以提案和差异预览呈现。提案必须相对基础 Stage 至少真实改变一个瓦片，且只有设计者接受并再次通过服务器求解与非空差异校验后才创建 Stage；零改动提案不能保存或接受。
- LLM 的难度和体验判断是主观意见；服务器与 Unity 求解器结果才是确定性证据。
- Play 只允许当前选中的已保存 Stage。未保存草稿、请求处理中或存在待决定提案时禁用。
- Play 不会修改地图、创建 Stage、确认最终版本或提交在线挑战。
- 最终确认后先收集设计者自然形成的意图；在此之前，集成接口不会向 Unity暴露最终 rows。
- 英文/中文切换影响新 UI、错误信息和后续 LLM 回复；历史消息保留原始语言。

是否把 PC/DG 作为正式研究条件、是否设置最少共创轮数、问卷指标和最终研究问题仍未决定，不应提前固化。

## 当前 DG 与保留的 PC 资产

### 当前首版生成模式

- `DG_Level` 根据玩家描述获取 LLM 方案，通过模板生成和回退策略形成完整地图，并在 Unity 端验证。
- 当前在线流程只启用 DG；PC/PC_Design/PC_Level 作为保留资产，不在 Build Settings 或导航中开放。
- 首版验证成功后不要求玩家先通关；DG rows 与 `description_generation` 写入 `CoCreationDraftContext`，随后加载 `CoCreation_Entry`。

### Stage 试玩模式

- 网页创建五分钟、一次性 Play Ticket，URL 只携带 attempt ID 与票据，不携带 rows。
- 8000 WebGL 的 Menu 启动组件换取完整 rows、来源、语言、提交 token 和返回 URL，并立即从地址栏清除票据。
- 当前 `description_generation` 加载 `DG_Level`；保留的 `partial_completion` 不在当前构建流程中调用。
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

前端视觉复用 8000 MatchMaking/Train dashboard 当前生效的 Pixel-adventure 设计语言：草绿色网格背景、木质 sticky topbar、米白石质面板、蓝紫标题板、硬边像素阴影、Consolas 标题与像素地图框。8010 所有可见文字以 700 粗体为基础，按钮和状态标签使用 800，页面标题保留 900。桌面仍为 Stage / 聊天 / 地图编辑器三栏；低于 1200px 收为两栏，低于 900px 为单栏，680px 下进一步缩小阴影、间距与瓦片。8010 保留独立 CSS，不在运行时引用 8000 文件。

LLM 使用理性、亲切、以第一人称为主的朋友式共创策略，并明确避免机械复述、固定开头、客服话术及“回应—评价—提问”模板。普通设计回复通常保留两至四段紧凑交流，给具体观察、个人判断、原因和可想象的游玩瞬间更多展开空间，但不写成报告。Stage 1 的首条开场不提问，并会明确说明：LLM 只能协助小范围、可审查的改动、建议与思路梳理；大幅重做应先由设计者在右侧编辑器完成。之后只有在评价不清、出现真实取舍、方向开始可执行或获得新试玩证据时，助手才会使用具体追问或独立见解；蓝色 `LET'S DISCUSS / 一起聊聊` 卡按需出现，绝不为了凑卡片强行生成。当玩家以“我认为……”“我觉得……”“在我看来……”或 `I think...`、`I disagree...` 等方式表达地图相关立场时，界面必定出现一张可纠正的橙色暂定意图卡，而不只保留助手自己的分析或蓝色讨论卡。暂定意图必须提炼玩家操作背后的可游玩目的，不能只给原话加“我暂时理解为”；若模型照搬，后端会改写为可纠正的解释。玩家说“你帮我改”“你来改吧”“按这个思路改”等自然授权时会直接进入严格地图提案流程；“你觉得怎么改”仍只讨论思路。普通聊天不得声称“改好了”或邀请试玩尚未生成的地图，已验证提案也必须等玩家明确接受才会成为新 Stage。执行授权本身不再被误写进橙色意图卡。整个共创会话始终只有一关，所有 Stage 都只是这同一关的保存版本；提示词与保存前规范化会拦截错误进度表述。其余意图、五色卡片、强证据风险门槛与严格地图验证流程保持不变。

蓝色与紫色卡片不会再把正文原句直接搬进去。紫卡使用短标题总结实际修改方向，并在下方独立展开预期的游玩影响和验证重点；蓝卡把问句或见解整理成可单独理解的讨论焦点，补充具体游玩瞬间以及它会影响的下一步设计判断。完整地图提案的模型阶段只编译 `RevisionPlan`：首次结构不合法，或合法计划因“操作类型与焦点区域没有可编辑格子”而构造零候选时，继续使用 Pro，并只附带安全的结构原因生成一次纠正计划；首次空白、超时或连接故障时才切到 Flash。地图构造、结构校验和求解全部由后端完成，不再把失败地图交给模型重画。

LLM 每轮还会接收初稿方法与当前 Stage 来源，避免把生成器产物误说成玩家亲手设计。DG 首版只把上游描述和参数选择归因给玩家，具体墙体、水域、箱子、目标和玩家位置均作为生成结果讨论；由于 8010 当前不保存具体参数值，LLM 不得猜测参数。PC 首版只把箱子起点、目标和整体草图约束视为玩家输入，不能询问玩家为何放置生成水域、某个内部墙或玩家出生点。玩家在工作台保存的手工修改可以依据确定性 diff 归因；接受 LLM 提案和恢复历史版本则分别表述为共同接受的方向和重新查看旧版本。这些规则同时作用于首轮和后续对话。完整地图提案还会逐格对照当前 Stage：后端拒绝空 diff，prompt 要求修改摘要只能陈述 rows 中实际发生的更改，保存提案和接受提案时各有一次独立校验。

当玩家适合直接实践自己的想法时，LLM 可以简短提示使用右侧地图编辑器并保存为新 Stage，但不把手工编辑说成必选步骤。手工 Stage 通过求解验证后，后端会相对父版本确定性识别外壳、水域、内部墙体、箱子位置、目标点、玩家位置及可用地面的变化；新 Stage 的回复先准确确认这些改动和可解状态，再由 LLM评价其设计影响并继续对话。

对话以保存的 Stage 为边界：每条 turn 仍完整持久化并带有 `versionId`，但页面选择某个 Stage 时只显示该版本相关的对话，LLM 也只接收当前 Stage 的最近对话。唯一的关系型承接是已接受提案的 assistant turn：它不复制、不改写原记录，而通过 proposal/decision/version 关联同时显示为新 Stage 的第一条上下文。历史 Stage 的对话只读；返回当前 Stage 后才可继续发送，并恢复当前 Stage 自己的草稿或未完成请求。

8010 的一次业务请求使用真正的 60 秒总时长硬上限。普通聊天和 Stage 开场优先使用 `deepseek-v4-flash`，并沿用最多 40 秒的首选模型窗口；明确请求完整地图提案时优先使用 `deepseek-v4-pro` 编译计划，首次最多 18 秒，结构纠正或 Flash 传输后备最多 8 秒，模型阶段合计不超过 26 秒。确定性候选搜索在请求第 55 秒停止，为持久化和响应预留五秒。上游显式关闭思考输出，避免推理 token 耗尽后只返回空 JSON。普通回复若把唯一问题写进正文，后端会保留陈述，并把问题提炼或展开成独立的 `followUpQuestion` 讨论焦点；纯问题或多个问题仍会被拒绝。服务日志记录安全结构原因和搜索摘要，两次计划尝试共用同一 requestId 和消息幂等键，只有最终成功结果会入库。聊天输入区显示实时秒数与主/备用阶段，并在 65 秒执行浏览器安全中止；失败、刷新或连接中断后使用原消息幂等键重试，确保同一请求最多保存一条 user turn 和一条 assistant turn。

后端使用独立 SQLite/WAL。默认数据库为 `CoCreationPrototype/Backend/data/cocreation.sqlite3`，`.env` 和数据库文件均被本目录 `.gitignore` 排除。
完整地图修改采用“LLM 意图编译器 → 语义修改算子 → 确定性局部搜索 → 结构校验与 Sokoban 求解 → 加权选择”。`RevisionStrategy` 只描述效果、焦点区域、允许算子、保护项、修改预算和客观指标方向；后端负责扩展 `add/remove wall`、`move player/box/target` 与 `add/remove water`。自动提案最多允许 8 个真实变更格，实体移动自动成对清除旧位置和写入新位置，外壳与 void 从候选空间排除。若助手或玩家已明确提出把某一实体向左/右/上/下或对角方向移动，且玩家授权生成，后端会把该方向转换为硬约束：候选必须让同一实体相对原格朝该方向移动，不能用反方向或别的算子替代。候选还必须真正使用能够实现其声明效果的算子、满足声明的指标变化，并遵守“不要改水/墙/玩家/箱子/目标”等明确保留项；否则会被拒绝而非作为低分方案选出。内部搜索最多构造 64 张地图、保存最多八张可解候选，并依次按明确要求、指标方向、区域/组件吻合度、最小改动和稳定签名选择一份。混在有效修改中的 no-op 与相同重复操作会安全删除，空 diff、冲突操作和不相关修改仍被拒绝。

降级修改是高门槛后备方案，不是普通重试策略。模型无法编译合法 `RevisionPlan`、空白响应、超时、限流、连接或服务器错误会作为模型/传输错误直接返回，不会伪装成地图搜索失败。只有合法计划已经产生、但确定性搜索在候选和时间预算内仍找不到满足完整要求的可解地图时，系统才保存红色风险警告卡，并商量是否只放宽“同时实现全部次要效果”。可解性、外壳、明确禁止事项、核心方向和无关区域始终不可放宽。玩家同意后仍先看到不含地图的紫色修改建议，只有点击“请助手具体生成这个方案”才提交独立的降级搜索请求。

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

浏览器会话使用 HttpOnly cookie；Unity 创建会话后持有只读集成 token。Play Ticket 只能换取一次地图，随后使用仅限该 attempt 的 token 提交指标。Stage 保存、恢复、消息、提案决定和 Play 创建均使用幂等键；同一消息键若改换内容或 Stage 返回 `409 IDEMPOTENCY_CONFLICT`，基于过期 Stage 的新写入返回 `409 VERSION_CONFLICT`。会话中的 assistant turn 可额外返回 `guidance`，记录本轮引导动作、暂定意图推测、核心问题、不含地图的修改方向，以及至多两个 `manual_edit` / `warning` UI cues；历史 `tradeoff` cue 继续兼容读取并按红色警告展示。

## 8000 在线匹配

8000 后端继续提供匿名两人房间、Ready、挑战交换、结果提交、问卷和 dashboard。房间位于单进程内存中，使用六位房间码、一秒轮询和 30 分钟惰性清理。

挑战请求只包含：

```json
{
  "rows": ["..."],
  "aiAssistantMode": "partial_completion",
  "opponentExperienceGoal": "I hope my opponent feels a careful route choice."
}
```

旧客户端多传的 `competitionMode` 被兼容忽略，不验证、不存储、不返回。历史 JSONL 原件保留，但新事件和派生 dashboard 不暴露该字段。

## Build Settings

共创与匹配所需场景：

```text
Assets/Scenes/Menu.unity
Assets/Scenes/Matchmaking/Online/Online_Lobby.unity
Assets/Scenes/Matchmaking/Online/Match_Briefing.unity
Assets/Scenes/Matchmaking/DG.unity
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
5. 最终确认后填写意图；确认完成卡仅在在线匹配会话显示“返回 Unity 继续”，点击后聚焦原 Unity 标签页并关闭 8010 标签页，Unity 只在意图提交之后获得最终 rows 并进入 `Challenge_Waiting`。
6. 双端继续完成挑战交换、`Online_Level`、`Match_Result` 和在线问卷。

部署时先备份 8010 SQLite，再更新并重启独立服务；随后用指定 Unity 版本重建 WebGL、更新缓存键并上传 8000 静态构建。不要把 `.env`、API Key、SQLite、研究日志或 `WebGLBuild/` 提交到 Git。
