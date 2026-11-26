# SURF 现行研究流程与数据边界

本文件维护跨 Unity/WebGL 客户端与统一 `:8000` FastAPI 平台（`server/` + `web/`）的现行研究协议。修改正式路线、问卷、DG、WebGL 桥接、Stage 生命周期、定稿或研究数据前应阅读本文件。系统分层架构见[系统架构](ARCHITECTURE.md)；涉及共创对话、设计记忆、Policy 2 方案发现和失败恢复时，还应查阅[共创回复可靠性](REPLY_RELIABILITY.md)。

研究流程或条件变更需要用户明确确认，并同步更新受影响的现行规范和规划资料。`Assets/EssayBase/8-3/SURF_Feedback.pdf`、`Assets/EssayBase/8-3/Feedback_Action_Plan.md` 是不随 Git 分发的本地研究资料；缺失时保留原有已确认设计，按需核实来源。

## 正式在线路线

```text
Menu → Questionnaire(Online1) → Online_Lobby → Match_Briefing
  → DG → DG_Level → CoCreation_Entry → /cocreation/ (iframe)
  → Challenge_Waiting → Online_Level → Match_Result
  → Questionnaire(Online2) → Menu
```

- `Questionnaire(Online1)` 在大厅之前打开赛前问卷，`Questionnaire(Online2)` 在结果之后打开赛后问卷。两者均为每轮流程的必需环节。保持原有问卷导航和提交行为，禁止加入持久跳过标志。
- `Draft` 场景已退役，在线房间的原始 `draft` 事件仍是独立且不可改写的研究记录。旧版 `Train` 研究页面与后端路线（及 `PC`、`PC_Design`、`PC_Level`）不在现行在线导航和统一后端接口范围内。`Algorithm_Level` 保留为独立启用的算法体验。启用场景以 [EditorBuildSettings.asset](../ProjectSettings/EditorBuildSettings.asset) 为准。
- 菜单的 Tutorial 按钮通过浏览器打开 `/frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf`（源文件位于 [Sokoban_Tutorial_Bilingual.pdf](../web/apps/dashboard/public/tutorial/Sokoban_Tutorial_Bilingual.pdf)），保留 Unity 菜单流程。正式挑战只有在玩家获得控制后才运行 30 秒计时。超时锁定输入、提交 `timed_out`、显示静态失败面板。两栏结果页独立显示双方的完成或超时、记录移动数，以及原关卡设计者提交的设计意图。双方已提交结果的计数包含超时，旧记录缺少 outcome 时按 `completed` 解释。继续按钮进入本轮赛后问卷。

路由和时间证据来自[菜单](../Assets/Scripts/Menu/MenuController.cs)、[问卷控制器](../Assets/Scripts/Study/QuestionnaireController.cs)、[挑战控制器](../Assets/Scripts/Online/OnlineLevelController.cs)和[结果页](../Assets/Scripts/Online/MatchResultController.cs)。

## DG 与模型隔离

即使所有业务运行在同一个 `:8000` FastAPI 进程内，**DG/蓝图模型与共创模型之间仍严格保持凭据、上下文与数据隔离**：

- DG 使用四道中立问题，分别关注首步检查、推箱依赖、空间分布和路线节奏。Q1–Q2 提供难度建议，Q3–Q4 提供布局建议。Unity 显示对四题的整体伙伴式短英文反馈和两项可编辑建议，保留后端字段及研究记录，不另展示逐参数理由。反馈提示以两至三句、约 25–40 个英文词为目标，备用文案保持中立并保留玩家选择。
- **DG 首版理解与关卡蓝图规划管线**（`POST /dg/guide/summary`、`POST /generate-level-plan`，实现于 `server/llm/pipelines/dg_guide.py` 与 `level_plan.py`）独占 DeepSeek 配置（`DEEPSEEK_*`）。确认后的 `dgContext` 和 `LevelDesignPlan` 只服务于首版规划与 Unity 地图生成，**禁止**传输、持久化或注入到共创模型上下文中。
- **共创对话、Policy 2 提案发现、关卡修改与翻译管线**（实现于 `server/llm/pipelines/{chat,understanding,binary,proposal,translation}.py`）独占 Moonshot/Kimi 配置（`COCREATION_LLM_*`），**禁止**读取或回退到 `DEEPSEEK_*`。共创会话只接收首版地图及正式匹配所需的有限元数据。变更正式研究模型需要明确确认。默认值与本地配置见 [README](../README.md#本地开发)。
- 研究者目标和实验条件不进入任何模型上下文。共创侧的设计推测和 `DesignContext` 语义记忆与用户最终自报告的 `designer_intentions` 独立记录；后者在确认最终 Stage 之后、对手挑战之前提交。

## 正式入场与草稿重生成

1. Unity 生成并验证首版地图，后端通过 `POST /online/rooms/{match_id}/draft` 保留原始 `draft` 研究事件。正式共创会话（`POST /api/sessions`）的入场页只读展示最新候选 rows，预览不写 Stage 研究投影。
2. 正式重生成复用当前 Unity 会话深拷贝保存的最后一份有效蓝图，换随机种子后分帧生成并校验。禁止重新调用 DG/蓝图 LLM 或悄悄改走无蓝图算法路径。服务端只替换最新通过验证的候选 rows。
3. 正式工作台在原 `/game/` 页面内通过同源 iframe 加载 `/cocreation/`。桥接协议统一定义为 `PROTOCOL_VERSION = 5`（契约位于 [protocol.ts](../web/packages/ui-core/src/bridge/protocol.ts)，构建产物为 [sokoban-bridge.js](../Assets/WebGLTemplates/SokobanPixel/TemplateData/sokoban-bridge.js)）。单一消息路由器严格校验同源白名单（显式拒绝 `"null"` origin）及 iframe 来源，发起试玩或重生成前核对宿主、会话及接收器；顶层打开正式工作台应引导返回原游戏页。交付 ACK 只代表投递，持久化的 `draft_regenerations` 终态决定重生成结果。等待领取和已领取租约默认 60/120 秒，失败、取消或超时保留旧候选。正常工作台不提供刷新操作，握手故障可重建 iframe，同 Stage 未保存编辑从本地快照恢复。
4. 用户点击进入共创后，最新候选在事务内固化为 Stage 1，并在同一步启动正式服务端 20 分钟 deadline。预览、刷新和重生成均不启动计时。到期后聊天、语言、编辑、保存、恢复、试玩及方案操作锁定，只开放最终提交；按最终提交规则，可把当前合法且可解的未保存草稿原子保存为最终 `human_edit` Stage。

Unity 侧入口见[共创控制器](../Assets/Scripts/Online/CoCreationEntryController.cs)与[共创草稿上下文](../Assets/Scripts/Online/CoCreationDraftContext.cs)。

## Stage、试玩与最终交接

- 一个共创会话只有一张关卡。Stage 地图是该关卡的不可变快照（`level_versions`），子版本保留 `parent_version_id` 链。人工编辑草稿在通过 `server/core/` 的 12×10 尺寸、符号、实体数量、外框外壳（`outer_shell`）和求解检查并成功保存后，才创建 `human_edit`。AI 候选仅允许 `add_wall`、`remove_wall`、`add_water`、`remove_water` 操作，严禁移动玩家/箱子/目标，且须经授权、真实差异复验及明确采用，细则见[提案履约与紫卡](REPLY_RELIABILITY.md#提案履约与紫卡)。
- 试玩只针对已保存 Stage，使用该版本的票据并分别记录加载、开始、移动、推动、重开、完成或中断等指标（`play_attempts`）。试玩不编辑地图、不创建版本，也不提交对手关卡。历史 Stage 的聊天和编辑只读，按权限可试玩、恢复或直接作为定稿。恢复创建一个新子版本并保留恢复来源和被替代版本；直接定稿历史 Stage 则引用原版本并归档受影响的待审候选。
- 保存、恢复、试玩创建、定稿与同步通过数据库原子表 `idempotency_keys` 保证幂等语义，短写事务（`BEGIN IMMEDIATE`）绝不跨越求解器或外部 LLM 调用。
- 最终 Stage 需由设计者显式确认。填写自报告意图（`POST /api/sessions/{session_id}/intention`）后，正式会话才完成并在**进程内 ACID 事务**（`server/application/rooms/` 与 `server/application/sessions/{lifecycle,stage,read_model}/`）中同步至在线房间事件；Unity 从授权的 `GET /api/integrations/sessions/{session_id}` 取得最终 rows 和意图，再提交匹配挑战。浏览器使用 `HttpOnly` 会话 cookie，Unity 的 integration token 用于限定范围内的状态读取及草稿重生成回报；各会话密钥均随机生成并以哈希持久化（`secret_hash` / `access_token_hash` / `integration_token_hash` / `bootstrap_token_hash`），保留过期和不匹配请求的拦截。

## 统一数据存储与研究记录

所有在线匹配、研究事件与共创数据统一存储在单库 SQLite WAL（默认 `server/data/sokoban.sqlite3`，DDL 见 [schema.py](../server/persistence/schema.py)，`PRAGMA user_version = 1`）：

| 数据域 | 持久化表与交接规则 |
| --- | --- |
| 在线房间与挑战 | `online_rooms`、`online_room_events`：保留玩家、房间码、状态、双方挑战地图与结果、原始 `draft` 事件及进程内同步的共创节点。`coCreationDurationSeconds` 按进入共创至定稿的服务端计时（默认 0–1200 秒）；`result_submitted.durationSeconds` 为对手挑战用时。 |
| 通用研究记录 | `study_records`：持久化 `POST /record-{survey-response,level-start,level-end,journey-event}` 的四类研究记录；通过 `GET /export/study-logs.jsonl` 兼容导出标准 JSONL 供离线研究分析。 |
| 共创会话与关卡 | `design_sessions`、`level_versions`、`play_attempts`、`draft_regenerations`：保留会话身份哈希、匹配关联、期限、当前/定稿版本、不可变地图快照、求解指标、实体绑定、父链与各自的 `design_context_json`。 |
| 对话、提案与运维状态 | `conversation_turns`、`change_proposals`、`revision_challenges`；独立索引运维表 `idempotency_keys`、`binary_discovery_topics`、`component_recovery_tasks`；以及严格只追加的 `audit_events` 审计表。公开 Session 投影经权限与字段白名单过滤，内部上下文、凭据和原始模型字段留在边界内。 |
| 历史数据迁移 | 通过独立 CLI [migrations.py](../server/persistence/migrations.py)（`python -m server.persistence.migrations`）导入历史 JSONL 日志或旧版 8010 SQLite 库；服务启动时不做冷启动全表扫描或隐式回填。 |

Matchmaking Dashboard（`/frontend/`，数据源 `GET /matchmaking-records-data`）保持对 `Manual Edit Review`、`LLM Challenge`、`Player Challenge`、意图修订及双方完整/短 Study Session ID 关联与搜索的支持。按 **Decision 5B** 安全策略：本地与研究便利读取（`/frontend/`、`/matchmaking-records-data`、`/export/study-logs.jsonl`）保持开放；破坏性删除（`POST /delete-online-match`、`POST /clear-matchmaking-records`）强制校验 `X-Delete-Password`（常量时间比对）。

研究记录改动默认面向新记录与正常幂等重试。保留已有兼容和原记录，历史批量修正或回填按 [AGENTS.md](../AGENTS.md#authorization-and-data-protection) 的授权约束处理。

## 演示与公网入口

- 直接进入 `/cocreation/` 创建独立 `algorithm_demo` 会话。`server/core/generator.py` 沿用两箱、两目标、水域、结构模板和反向拉箱思路，生成可解的 10×12 Draft。重生成只替换候选，显式进入才固化 Stage 1。Demo 无正式期限、无在线房间同步或匹配记录，采用 **TTL 过期清理**（`delete_expired_demo_sessions`），不误删其他并发用户的活跃 Demo，也绝不触碰正式研究数据。
- [deploy/nginx-sokoban.conf](../deploy/nginx-sokoban.conf) 以 `https://sokobanaidemo.top` 的 `/game/`、`/frontend/`、`/cocreation/` 为公开路径，统一反向代理至单个上游 `127.0.0.1:8000`（`/cocreation/` 与 `/api/` 保留 135 秒 LLM 超时），并对 `X-Forwarded-Proto` 按可信代理白名单加固。
- 游戏页 `DATA DASHBOARD` 按钮通过原生 `<dialog>` 验证密码（`POST /verify-dashboard-password`）后新标签页打开 `/frontend/`。大厅静态房间码输入由 `web/apps/webgl-bridge`（`ResizeObserver`）与 `BrowserNavigation.jslib` 同步，生成码只读可选中，加入码可粘贴并归一化为六位字母数字。共创网页保留中英文两套界面。

## 研究链路验收

按变更范围验证两次问卷、匹配房间、DG 与 Stage 1 rows 一致性、蓝图重生成及协议 v5 握手恢复、连续保存和恢复、Policy 2 A/B 方案发现与采纳、当前及历史 Stage 试玩、完成/中断指标、定稿及进程内 ACID 意图交接、双方挑战和超时结果。使用 [README 的本地验证命令](../README.md#本地验证)与 [AGENTS 的 Game view 要求](../AGENTS.md#unity-authoring)记录证据。正式 WebGL 联调使用授权的匹配环境和实际兼容构建，明确区分源码检查、离线自动化测试与真实运行结果。
