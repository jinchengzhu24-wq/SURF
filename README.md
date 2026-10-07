# Sokoban 人机共创与在线挑战

本仓库包含 Unity 2D Sokoban 客户端、8000 端口的在线匹配与研究 Dashboard，以及独立的 8010 LLM 共创工作台。

当前产品把“地图共创”和“Unity 游玩”分开：Unity 负责生成并验证首版地图，8010 网页负责聊天、版本管理、手工编辑、AI 提案和 Stage 试玩；最终确认后，Unity 再取得最终地图进入在线挑战。

本文于 2026-10-07 按当前代码与已确认修订核对。产品流程以本文为入口，开发约束见 [AGENTS.md](AGENTS.md)，回复验收与恢复见 [可靠性说明](CoCreationPrototype/REPLY_RELIABILITY.md)，离线评测见 [评测说明](CoCreationPrototype/Backend/evaluations/README.md)。历史角色说明与示例保存在 `Multi-Agent/`；其中旧流程不能覆盖当前规则。

## 代码与职责入口

| 位置 | 职责 |
| --- | --- |
| `Assets/Scripts/`、`Assets/Scenes/` | Unity 导航、首版生成、iframe 协议、Stage 试玩与在线挑战；保留资产 `.meta` |
| `Assets/WebGLTemplates/SokobanPixel/`、`Assets/Plugins/WebGL/BrowserNavigation.jslib` | 游戏网页模板、Dashboard 密码入口、浏览器房间码和 Unity 通信 |
| `Backend/`、`Frontend/` | 8000 匹配、研究记录、两个首版 Agent 与 Dashboard |
| `CoCreationPrototype/Backend/app.py`、`repository.py` | 8010 API、流程状态、持久化、幂等、deadline 和研究投影 |
| `CoCreationPrototype/Backend/llm_client.py`、`design_context.py` | Kimi 任务、当前快照与分角色语义上下文、意图及证据 |
| `CoCreationPrototype/Backend/design_requirements.py`、`revision_workflow.py`、`proposal_search.py`、`level_validation.py` | 要求策略、执行约束、候选搜索、地图事实及求解；这些是确定性程序，不是 Agent |
| `CoCreationPrototype/Frontend/` | 共创网页、编辑器、卡片、进度、错误与重试恢复 |
| `CoCreationPrototype/Backend/reliability_report.py`、`semantic_eval.py`、`evaluations/` | 独立只读统计与语义评测，不接入用户流程 |

`Library/`、`Temp/`、`Logs/`、Unity 工程文件和 `WebGLBuild/` 是生成结果；凭据、数据库、研究日志和评测运行结果不提交到 Git。

## Agent 分工与运行保障

| Agent | 服务 / 模型 | 输入与输出边界 |
| --- | --- | --- |
| Draft首版理解助手 | 8000 / `deepseek-v4-flash` | 四道中立 DG 回答 → 可确认、可纠正的难度与布局理解 |
| 关卡蓝图规划助手 | 8000 / `deepseek-v4-flash` | 已确认 DG 理解 → `LevelDesignPlan`，由 Unity 确定性生成并验证地图 |
| 共创聊天助手 | 8010 / `kimi-k2.6` | 当前 StageSnapshot、用户表达、分角色 DesignContext 与必要证据 → 经过校验的正文、指导及授权语义计划 |
| 共创关卡修改助手 | 8010 / `kimi-k2.6` | 执行合同、当前 StageSnapshot、确定性事实与求解指标 → 局部操作候选；不接收完整聊天历史 |

Stage 开场、翻译、入口理解、要求复核和意图审查是辅助 LLM 任务，不增加独立产品 Agent。8010 统一使用 `thinking.disabled`、`temperature=0.6`；支持的结构化任务优先严格 JSON schema，接口兼容时可退到 `json_object`，仍需服务端验证。

系统已经同时使用提示词、上下文管理和执行保障：提示词指导表达，StageSnapshot 与 DesignContext 控制可用事实和记忆，合同、权限、求解、原子提交、幂等与有限恢复限制输出和操作。2026-10-06 的增强增加了统计与评测能力，没有改变 Agent 提示词或用户操作流程，也不代表真实模型理解能力已提高。

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
      → 只读首版 Draft 预览（可重新生成）
      → 点击“进入共创流程”后 Stage 1 = 当时最新的 Unity 首版 rows
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
`https://sokobanaidemo.top/frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf`。

`Online_Level` 的 30 秒挑战计时从玩家获得控制后开始。超时锁定输入、提交 `timed_out` 并显示静态失败面板；结果页分别显示超时与完成及各自记录的移动数。旧结果缺失 outcome 时按 `completed` 兼容。

## 系统边界与当前实现

- 唯一正式公网入口为 `https://sokobanaidemo.top/game/`、`https://sokobanaidemo.top/frontend/` 和 `https://sokobanaidemo.top/cocreation/`；Cloudflare 对外提供 HTTPS，HTTP、`www` 和旧 IP 统一跳转到 HTTPS 根域名。WebGL 运行时从当前页面 origin 解析首方地址，公开链接不使用 `:8000` 或 `:8010`。
- DG 使用四道中立地图设计问题：首步检查、推箱依赖、空间分布和路线结构。Q1–Q2 只用于 8000 的难度建议，Q3–Q4 只用于 8000 的布局建议；DG context 不会传入 8010 或其 LLM 上下文。
- 8000 的两个 Agent 使用 `deepseek-v4-flash`；8010 的聊天助手、关卡修改助手、Stage 开场、翻译、Revision 和意图反馈审查使用 Kimi `kimi-k2.6`。8010 不读取或回退到 8000 的 DeepSeek 环境变量。
- `Draft` 场景已退役。`PC`、`PC_Design` 和 `PC_Level` 仅作为历史实现资产保留，不在当前 Build Settings 或在线导航中。
- 8010 正式 Unity 会话在用户点击“进入共创流程”、最新 Draft 被原子固化为 Stage 1 后才启动服务端 deadline，当前为 20 分钟。浏览首版 Draft、刷新页面和重新生成均不计时。到期后聊天、编辑、保存、恢复、试玩和提案锁定，只保留最终 Stage 提交；提交时可将当前可解的本地草稿原子保存为最终 `human_edit` Stage。
- 直接访问 `/cocreation/` 创建的是独立演示会话，不启动 deadline、不同步 8000，也不写入正式匹配记录。
- `/game/` 页脚 `DATA DASHBOARD` 在游戏页完成密码验证后才打开 `/frontend/`。WebGL 房间码使用模板中的静态浏览器输入：生成码只读可选中复制，加入码可粘贴并归一化为六位字母数字；没有 `COPY CODE` 按钮。Dashboard 保留完整 Match/Study Session ID，显示前八位并支持复制完整值、按两位玩家的完整或短 Study Session ID 搜索。

## 服务器运行与部署

线上 Python 服务由 systemd 托管，不需要在 SSH 会话中手动常驻运行 `python app.py` 或 `uvicorn`：

- `sokoban-backend`：8000 服务，工作目录 `/root/SURF/Backend`。
- `sokoban-cocreation`：8010 共创服务，工作目录 `/root/SURF/CoCreationPrototype/Backend`，读取该目录的生产 `.env`。

两个服务均已设置为开机启动，并在异常退出后自动重启。源站 Nginx 监听 80 和 16384，Cloudflare 对外提供 443 HTTPS；用户应使用上面的域名入口，不直接访问 8000、8010 或 16384。

常用运维命令：

```bash
systemctl status sokoban-backend sokoban-cocreation nginx
systemctl restart sokoban-backend
systemctl restart sokoban-cocreation
journalctl -u sokoban-backend -f
journalctl -u sokoban-cocreation -f
```

只改 8000 服务代码时重启 `sokoban-backend`，只改 8010 服务代码时重启 `sokoban-cocreation`；静态前端、已构建 WebGL、文档或独立离线工具的变更本身不要求重启 Python 服务。8010 发布前通过 SQLite backup API 取得一致备份，备份将替换的文件并记录哈希；不要直接复制正在使用的 WAL 数据库主文件作为唯一备份。改配置时另行备份生产 `.env`，不上传本地凭据。

Windows SSH/SCP 必须显式使用 `$env:USERPROFILE\.ssh\sokoban` 和 `-o IdentitiesOnly=yes`。8010-only 发布仅上传变更的 `CoCreationPrototype` 文件，不使用整仓 `deploy_scp`、不重启 8000、不构建 WebGL。验证源站与公网 `/cocreation/health`、`/cocreation/ready` 及受影响静态资源；恢复失败的代码时不要用旧数据库覆盖期间新增的研究记录。运维参考见本地 [SERVER.md](SERVER.md)，8010 服务代码发布与回滚步骤见 [可靠性说明](CoCreationPrototype/REPLY_RELIABILITY.md#8010-部署与回滚)；其中重启步骤适用于服务代码变更。`SERVER.md` 不随 Git 分发，旧示例若与上述规则冲突，以当前规则为准。

本地开发分别在两个终端运行 `python Backend/app.py` 和 `python CoCreationPrototype/Backend/app.py`，依赖清单分别为 `Backend/requirements.txt` 与 `CoCreationPrototype/Backend/requirements.txt`。两个入口读取各自目录的 `.env`；8010 配置示例为 `CoCreationPrototype/Backend/.env.example`，只使用 Kimi/`COCREATION_LLM_*` 配置，不回退到 8000 的 DeepSeek 配置。

## 8010 工作台规则

- 一个会话始终只包含同一个关卡；`Stage 1`、`Stage 2` 及后续 Stage 都是该关卡的不可变版本，不是不同关卡或关卡 progression。
- 入场页只读展示 Unity DG 验证后的当前首版 Draft；正式重新生成只复用当前 Unity 会话中深拷贝保存的最后一个有效 LLM 蓝图，并使用新随机种子在 Unity 内分帧生成和验证，绝不再次调用 8000 LLM，也不降级为无蓝图算法地图。8010 只保存最新候选 rows，不接收 DG 参数或蓝图。点击“进入共创流程”后，该候选才成为不可变 Stage 1。手工编辑和 AI 提案都必须通过尺寸、符号、实体数量、外墙和 Sokoban 可解性校验。
- 手工草稿只有保存为新 Stage 后才持久化。AI 提案先保存为待审查 proposal，只有用户明确接受并再次通过后端验证，才创建新 Stage。
- Play 只针对已保存 Stage，不会修改地图、创建 Stage、确认最终版本或提交在线挑战。试玩会保存到对应 Stage 的 `play_attempts`。
- 地图事实以当前 StageSnapshot 为唯一来源。服务器会重新校验当前坐标、实体、路线和可点击链接；历史 Stage、旧助手文本和用户错误坐标不能作为当前地图事实。
- 普通聊天只返回经过校验的分析文本；proposal、disagreement、intent hypothesis 等内部字段经过服务端投影后才可供前端显示。Stage assessment 只归档，不显示独立评价卡；Stage 1 固定操作指导由后端追加一次，历史兼容仅修展示，不改写记录。研究者目标、实验条件和 8000 DG context 不进入 8010。
- Proposal 按钮切换下一条消息的方案模式，不立即发送。Enter/Send 保留输入原文，并按当前模式携带 `requestProposal`；普通模式不强制申请方案，方案模式进入同一有界发现流程。发送成功后模式复位，失败重试恢复原模式、原文、Stage 与幂等键；有持久化方案主题时，短回答继续原主题。
- 标准紫卡可请求生成、质疑或替代方案；只有当前 Stage 最新有效 `proposalOffer` 可操作，旧卡只读，普通非方案消息不会使最新紫卡过期。候选通过校验后冻结逐格改动；请求生成得到待审查 proposal，用户接受并再次验证后才创建 Stage。卡片文字与模型分析本身不授权落图。
- 人工编辑保存后先保留可解 Stage，再原子提交两条助手消息：无卡片的观察及始终可见的比较。只有有证据的冲突增加入口 `LET'S DISCUSS`，不加 WARNING、不自动回滚。后续 Kimi 可用绑定最新原话证据的 `human_edit` 专属 `acknowledged` 解除阻塞，不确定胜方或确认决定；必需状态或证据不可靠时按预算重试，最终不写不完整助手轮次。
- 紫卡质疑首轮仅固定邀请说明理由，后台保存两个暂定假设；收到清楚理由后显示一次选择聊聊卡，后续含糊答复使用普通气泡，不重复出卡。`choice_pending` 的“是”重验原方案冻结改动并生成待审查提案；“否”保留原始要求及理由、排除旧候选，生成仅有 `execute_revision` 的新紫卡。选择按钮与明确文字回答走同一判定。两条路径都须接受待审查提案才能创建 Stage；活跃分歧阻止另起方案及紫卡操作。

## 8010 后端数据保留

8010 使用独立 SQLite/WAL 数据库，默认路径为 `CoCreationPrototype/Backend/data/cocreation.sqlite3`。数据库和 `.env` 不提交到 Git。正式 Unity 会话的历史记录不会因新会话创建而清理；独立演示会话只保留最新一轮。

| 表 | 当前保留的数据 |
| --- | --- |
| `design_sessions` | 会话身份、demo 标记、匹配 ID/玩家编号、初稿方法、进入前的当前 Draft 候选与重生成任务状态、语言与锁定时间、deadline、当前/最终版本、状态和时间戳。访问、集成和 bootstrap token 只保存哈希，不保存明文 token。 |
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

历史 Stage 保持聊天和编辑只读，但可以试玩、作为新 Stage 的恢复来源，或直接确认为最终 Stage。直接确认历史 Stage 不会创建副本；它会保留当前 Stage 的 lineage，将 `finalVersionId` 指向所选历史版本，并在填写设计意图后由现有 Unity 流程把该版本交给对手。若当前 Stage 仍有待处理的 AI 提案，选择历史 Stage 为最终版本会将这些提案归档为 `superseded`。

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
- 新消息先由 Kimi 结合原话、近期表达与当前 StageSnapshot 判断所指地图元素和表达行为；普通评价、设计意图与思路询问不自动启动方案。模糊称呼可保持不确定并给出可纠正的评论，不要求用户逐格报坐标。新 AI 方案只允许修改水域和内部墙；外壳、玩家、箱子和目标点固定，服务端在方案与执行阶段复验。历史方案卡沿用其冻结差异。
- 同一 Stage 的方案发现由持久化 `proposalDiscovery` 主题控制，短回答绑定已有问题；澄清上限按最终可见问题数计，不按回复轮数计。信息充分可提前生成，三问预算用尽后停止追问，在修改权限内保守选择未指定对象并进入 `RevisionPlan`，不要求用户逐格报坐标。最多三个策略逐一进行合同预检、候选、确定性验证/搜索及一次带真实拒绝摘要的语义重规划。上游故障保持 `retry_pending` 与原消息键；确定性失败保留 `revision_needed` 主题、原申请、回答、补充方向和失败包，仅有具体冲突证据时显示 WARNING。成功、明确取消或 Stage 切换才结束主题。
- “设计倾向”只投影 confirmed hypothesis，并显示最多 12 条逐 Stage 的证据轨迹；pending、rejected、tentative 和未经确认的 inferred 内容不会进入 Revision 硬约束。
- 进度面板按 Stage 展示 `Unresolved questions`、`Design inclinations` 和折叠的 `Processed` 历史。问题仅来自最终可见、非橙卡助手输出；回答由有界 Kimi 复核，忽略/恢复问题走确定性动作，不新增聊天轮次。忽略的问题离开 LLM 上下文，历史 Stage 不吸收后代的回答、恢复状态或证据。

## 8010 与 8000 的数据边界

8010 是完整共创记录的权威来源。预览候选不会同步到 8000；用户进入共创时才把最终候选作为 `first_stage` 同步一次。之后正式会话只向 8000 同步必要的研究投影：已保存的人工/AI Stage、Stage opening/turn 以及最终 `final` 事件。最终事件中的 `coCreationDurationSeconds` 由 8010 服务端计算，当前范围为 0–1200 秒；对手游玩时长仍使用 8000 的 `result_submitted.durationSeconds`。

Unity 的集成接口只有在会话完成后返回最终 rows 和用户最终自报告意图。8010 不把 DesignContext、intentHypotheses、完整聊天、研究者目标或实验条件发送给 8000 的 LLM 或 Unity 执行流程。

## 提案要求与三问上限（2026-10-02 确认修订）

- 本节替代此前把提案搜索失败一律展示为 WARNING、把明确方向全部视为硬约束的规则。新方案使用带版本的 `requirementRecord`：不可放宽的固定要求、可取舍但不能反向的明确目标、可忽略的含糊偏好分别记录。每项要求保留用户原话及来源 Turn；Kimi 编译后独立复核，服务端核对属性、单位、数值、实体和证据。执行不再从拼接聊天或关键词距离推断数量要求，B1 等实体编号不作为指标数值。
- 一个当前 Stage 的提案主题最多展示三个澄清问题，按最终可见正文中的实际问题数计数；同一回复的多个问题、受保护对象分支和修复分支共用预算。短回答绑定最近的问题，幂等重试不重复计数；主题从完整持久化记录重建，历史漏计只在读取时修复。信息充分可立即生成，用尽预算后允许在权限范围内选择未指定对象，不增加第四问。
- 提问使用直白的偏好表达，不要求用户补出坐标、固定推动次数或未经验证的必然通路效果。真实固定实体修改请求明确拒绝该部分，并保留地图和原讨论。提及箱子、切换频率或打断 B1 推进不等于改变初始箱子位置或数量。
- 用户明确优先级、当前明确聚焦目标、其他目标完成数依次用于选择候选；同等完成程度选择更少改动，再按稳定策略顺序排序。可验证目标允许保持或部分改善，反向改动在正式 `shadow` 和演示 `enforce` 下都被拒绝。固定要求、精确转换和权限不能被取舍；矛盾在生成前检查。
- 修改助手只收到执行合同、当前 StageSnapshot、确定性事实和指标，不收到聊天历史或 8000 DG。模型候选、确定性搜索、一次重规划及最终冻结差异重放共用要求与方向检查，新方案总请求预算仍为 120 秒。旧冻结卡沿用原执行规则。
- 紫色方案正文说明已核实、部分实现、暂未核实的目标，以及系统自行选择的对象。取舍不表示用户撤回目标，明确目标随子 Stage 的 DesignContext 继承；自动选择仅为方案假设。路线切换/推动数据只描述已核对的一条解法，不证明所有解法必须切换，也不保证实际解题时间增加。
- 全部候选失败时保留地图与主题，无新 Stage、无伪成功。理解不可靠返回可重试错误；权限冲突、无解候选和方向检查失败分别说明。没有具体风险证据不增加 WARNING，不新增确认、补充或退出按钮。工作区缺失的研究反馈 PDF 与行动计划文档不自行重建。

## 方案语义与视觉目标（2026-10-07 确认修订）

- 当前主题分别保存带用户原文与 Turn 的目标、编辑组件、空间焦点和明确保留条件。围绕目标、箱子、玩家入口或外壳附近调整水墙，不自动成为移动这些实体的请求；短回答必须绑定正在回答的问题和完整主题。
- 准备拒绝固定实体修改、改变既定范围、删除目标/保留条件或重建旧主题时，入口分类须独立语义复核，最多两轮并共享原 116 秒截止时间。最终需求编译仍单独审核原文覆盖；真实保护规则、合同冻结和求解检查不放宽。拒绝只说明核实到的对象/动作，理解不可靠则返回可重试错误并保留主题。
- 澄清维度由语义决定，信息充分即可规划。视觉目标讨论排布、疏密、重复和局部焦点，不强行转换为运输长度、玩法难度或固定解法。AI 用第一人称表达自己的判断/建议，不能替用户说意图或创建未经确认的限制；风险组件局部复核和修复，保留可靠内容。
- `visual_composition` 不是美观评分。结构/可解性只证明地图有效，视觉效果由用户预览和决定。解释与纠正信息保存在现有主题元数据和审计表，无新数据库表/按钮；历史对话不重写。新增中英文参照/编辑反例的合成评测与多轮回归不等于真实模型准确率。

## 回复可靠性与恢复（2026-10-03）

可靠正文与完整表达质量分别验收：可选意图卡、辅助记忆、进度复核和表达质量问题不应连带丢弃已经验证的正文。地图事实、用户原话证据、授权、必需分歧状态、执行合同和可解性继续阻断对应结论或操作。普通聊天最终没有可靠正文时返回可重试错误，不写助手轮次，也不使用固定开场替代回答。

要求编译和独立复核保留两轮上限；逐条定位失败、锁定合格要求并复核完整覆盖性。质疑理由的地图清理进入已有两次评审。分歧“否”和替代方案携带原始用户目标与正式理由，并从候选生成、修复和搜索开始排除旧方案；“是”仍重放冻结差异。已可靠理解但找不到合格候选时，可交付经校验的 Kimi 失败分析，保留讨论主题和原 Stage，不显示紫卡或伪造分歧已解决。

各子任务共享 HTTP 请求的内部 116 秒截止时间；可选增强累计最多 10 秒，并预留 20 秒。人工编辑保存与评审分开，完整观察/比较两条消息原子提交；翻译逐条保留成功结果，失败项保留原文。浏览器从发送请求到读取、解析响应全程计时，非法成功响应按失败处理，写操作重试复用输入、Stage 和幂等键。

流程覆盖、验收命令、指标口径及 8010 单独部署步骤见 [回复可靠性说明](CoCreationPrototype/REPLY_RELIABILITY.md)。生产成功率须根据部署后的真实记录衡量，不从固定模型测试推算。

## 只读报告与离线语义评测

`reliability_report.py` 从现有审计事件统计任务、问题类别、正文恢复、有效提案与已记录生成耗时；优先列出最终未交付正文较多的三类问题。报告不读聊天原文，仅提供匿名引用和观测时间。错误码不能区分模型误解与检查误伤，需人工复核；浏览器交付、审计前失败、人工语义质量和 Token 成本尚未覆盖。

```powershell
python CoCreationPrototype/Backend/reliability_report.py path/to/cocreation.sqlite3 --since 2026-10-03T09:00:00Z --format text
python CoCreationPrototype/Backend/semantic_eval.py validate
```

统计按会话、任务、消息键归并；多条结果记录不等于内部模型尝试或精确 HTTP 重试数。生成、正文、质量指标的缺失布尔值为未知，`rate` 使用 `observed` 分母；正文交付不等于有效方案或浏览器收到响应，少量样本与空问题列表不证明稳定成功率。时间允许明确时区，`--until` 为不包含的结束时间。

固定集目前为 24 个合成入口理解案例、两张可解地图。`validate`、`score`、`compare` 不调用模型；`run --live` 单独启用真实 Kimi 采样，复用生产入口理解函数，不创建线上会话或写 SQLite。预期值和人工说明不进入模型上下文。缺失样本保留在评测分母，比较使用相同套件和重复次数；结果保存在 Git 忽略的 `CoCreationPrototype/Backend/evaluation_runs/`。评测不覆盖完整正文、活跃质疑或地图执行，不能当作线上成功率；2026-10-06 本轮仅做离线验收，尚未进行真实模型采样。详细运行及比较格式见 [评测说明](CoCreationPrototype/Backend/evaluations/README.md)。

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
Assets/Scenes/Matchmaking/Online/Questionnaire(Online1).unity
Assets/Scenes/Matchmaking/Online/Online_Lobby.unity
Assets/Scenes/Matchmaking/Online/Match_Briefing.unity
Assets/Scenes/Matchmaking/DG.unity
Assets/Scenes/Matchmaking/DG_Level.unity
Assets/Scenes/Matchmaking/Online/CoCreation_Entry.unity
Assets/Scenes/Matchmaking/Online/Challenge_Waiting.unity
Assets/Scenes/Matchmaking/Online/Online_Level.unity
Assets/Scenes/Matchmaking/Online/Match_Result.unity
Assets/Scenes/Matchmaking/Online/Questionnaire(Online2).unity
```

`Assets/Scenes/Try/Algorithm_Level.unity` 也启用于 Build Settings，属于独立算法体验，不属于上述正式在线路线。`Draft`、PC 与旧训练/LLM 场景不作为当前正式导航入口；场景清单以 `ProjectSettings/EditorBuildSettings.asset` 为准，Unity 版本以 `ProjectSettings/ProjectVersion.txt` 为准。

## 演示模式

直接访问 `/cocreation/` 时，页面会自动创建 `algorithm_demo` 会话：先显示统一的 Draft 地图区域、旋转箭头和禁用的操作按钮，再原位显示 10×12、两箱、两目标的可解算法 Draft。演示页标题为“算法生成的首版 Draft”；正式 Unity 会话的同一位置显示“AI 规划并生成的首版 Draft”。正式会话只允许在 `/game/` 的同页 iframe 中操作，并在创建持久任务前完成版本 5 协议预检；顶层打开正式会话只显示返回原游戏页的拦截说明。iframe、试玩接收器和重生成接收器分别维护 ready 状态，消息 ACK 只代表已投递，最终结果以 8010 持久任务为准。待领取和已领取任务分别使用 60 秒和 120 秒租约，后台扫描保证即使浏览器停止轮询也会进入超时终态；网络中断、失败或超时均保留旧 Draft。正常工作台不显示刷新控件；只有握手故障遮罩中的“重新尝试”会重建 iframe，同一 Stage 的未保存地图编辑由浏览器本地快照恢复。两种入口都可在进入前不限次数重新生成且只保留最新候选，点击“进入共创流程”后才创建 Stage 1 并开始开场。演示数据只写入 8010，不创建正式 deadline、不同步 8000，也不记录正式匹配的 `coCreationDurationSeconds`。

创建新的演示会话成功后，只清理上一轮演示会话及其关联的聊天、版本、试玩、提案和审计记录；正式 Unity 会话不会被清理。如果新地图或新会话创建失败，上一轮演示记录保持不变。

## 验证

```powershell
python -m unittest discover -s Backend -p "test_*.py"
python -m unittest discover -s CoCreationPrototype/Backend/tests -p "test_*.py"
node --test CoCreationPrototype/Frontend/tests/*.cjs
node --check CoCreationPrototype/Frontend/app.js
python CoCreationPrototype/Backend/semantic_eval.py validate
```

测试使用固定模型输出、临时数据库及故障注入，不要求真实 API Key。若 Unity 已生成可用的工程文件，可用 `dotnet build Assembly-CSharp.csproj -v:minimal` 辅助检查 C# 编译；它不能替代 Unity Test Runner 或 WebGL 构建验证。

完整手动回归应使用 Unity `2022.3.62f2c1`，覆盖：Stage 1 rows 一致性、连续创建和恢复多个 Stage、最新/历史 Stage 试玩、完成/中断指标、意图确认、最终 rows 返回 Unity，以及在线挑战和两次问卷。

8010-only 部署先取得 SQLite 一致备份，只上传变更的 `CoCreationPrototype` 文件；服务代码变更重启 `sokoban-cocreation`，文档、静态资源和独立离线工具本身不要求服务重启。不要构建或上传 WebGL；只有用户提供或明确要求构建时才更新 WebGL。不要把 `.env`、API Key、SQLite、研究日志、Unity 缓存、评测结果或 `WebGLBuild/` 提交到 Git。部署规则见上文与 [可靠性说明](CoCreationPrototype/REPLY_RELIABILITY.md#8010-部署与回滚)。

研究规划基线为 `Assets/EssayBase/8-3/SURF_Feedback.pdf` 与 `Feedback_Action_Plan.md`；这些本地资料不随 Git 分发，当前工作区缺失时不自行重建。改动若涉及研究流程或条件，先确认修订；代码检查或历史文档差异本身不授权改变研究设计。
