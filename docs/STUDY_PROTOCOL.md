# SURF 现行研究流程与数据边界

本文件维护跨 Unity、8000 和 8010 的现行研究协议。修改正式路线、问卷、DG、桥接、Stage 生命周期、定稿或研究数据前应阅读本文件。涉及 8010 对话、设计记忆、方案和失败恢复时，还应查阅 [8010 回复可靠性](../CoCreationPrototype/REPLY_RELIABILITY.md)。

研究流程或条件变更需要用户明确确认，并同步更新受影响的现行规范和规划资料。`Assets/EssayBase/8-3/SURF_Feedback.pdf`、`Assets/EssayBase/8-3/Feedback_Action_Plan.md` 是不随 Git 分发的本地研究资料。缺失时保留原有已确认设计，按需核实来源。

## 正式在线路线

```text
Menu → Questionnaire(Online1) → Online_Lobby → Match_Briefing
  → DG → DG_Level → CoCreation_Entry → 8010
  → Challenge_Waiting → Online_Level → Match_Result
  → Questionnaire(Online2) → Menu
```

- `Questionnaire(Online1)` 在大厅之前打开赛前问卷，`Questionnaire(Online2)` 在结果之后打开赛后问卷。两者均为每轮流程的一部分。保持原有问卷导航和提交行为，禁止加入持久跳过标志。
- `Draft` 场景已退役。8000 的 `draft` 事件仍是独立且不可改写的研究记录。`PC`、`PC_Design`、`PC_Level` 和相关后端能力保留作研究资产。现行在线导航与启用场景不包含它们。`Algorithm_Level` 是独立启用的算法体验。场景配置以 [EditorBuildSettings.asset](../ProjectSettings/EditorBuildSettings.asset) 为准。
- 菜单的 Tutorial 按钮通过浏览器打开 [双语 PDF](../Frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf)，保留 Unity 菜单流程。正式挑战只有在玩家获得控制后才运行 30 秒计时。超时锁定输入、提交 `timed_out`、显示静态失败面板。两栏结果页独立显示双方的完成或超时、记录移动数，以及原关卡设计者提交的设计意图。双方已提交结果的计数包含超时，旧记录缺少 outcome 时按 `completed` 解释。继续按钮进入本轮赛后问卷。

路由和时间证据来自 [菜单](../Assets/Scripts/Menu/MenuController.cs)、[问卷控制器](../Assets/Scripts/Study/QuestionnaireController.cs)、[挑战控制器](../Assets/Scripts/Online/OnlineLevelController.cs)和[结果页](../Assets/Scripts/Online/MatchResultController.cs)。

## DG 与模型隔离

- DG 使用四道中立问题，分别关注首步检查、推箱依赖、空间分布和路线节奏。Q1–Q2 提供难度建议，Q3–Q4 提供布局建议。Unity 显示对四题的整体伙伴式短英文反馈和两项可编辑建议，保留后端字段及研究记录，不另展示逐参数理由。反馈提示以两至三句、约 25–40 个英文词为目标，备用文案也保持中立并保留玩家选择。
- 8000 的 Draft 首版理解助手与关卡蓝图规划助手使用该服务的 DeepSeek 配置。确认后的 `dgContext` 和 `LevelDesignPlan` 只服务于 8000 首版规划与 Unity 生成。8010 只接收首版地图及正式匹配需要的有限元数据，禁止传输、持久化或向模型提供 DG 回答、参数、理解及蓝图。
- 8010 的共创聊天助手、共创关卡修改助手及开场、翻译、理解、需求和意图复核等辅助任务使用本服务的 Kimi/Moonshot 配置。禁止读取或回退到 8000 的 `DEEPSEEK_*`。变更正式研究模型需要确认。默认值和本地配置参阅 [README](../README.md#本地开发)。
- 研究者目标和实验条件不进入模型上下文。8010 的设计推测和语义记忆与用户最终自报告意图独立记录。后者在确认最终 Stage 之后、对手挑战之前提交。

代码入口为 [8000 DG 指导](../Backend/sokoban_backend/services/dg_guide.py)、[8000 生成接口](../Backend/sokoban_backend/routes/generation.py)和 [8010 模型配置](../CoCreationPrototype/Backend/cocreation/llm/config_and_runtime.py)。

## 正式入场与草稿重生成

1. Unity 生成并验证首版地图，8000 保留原始 `draft` 研究事件。正式 8010 会话的入场页只读展示最新候选 rows。预览不写 Stage 研究投影。
2. 正式重生成复用当前 Unity 会话深拷贝保存的最后一份有效蓝图，换随机种子后分帧生成并校验。禁止重新调用 8000 LLM 或悄悄改走无蓝图算法路径。8010 只替换最新通过验证的候选 rows。
3. 正式工作台在原 `/game/` 页面内同源 iframe 使用。发起试玩或重生成前核对宿主、会话及接收器。重生成预检要求协议版本 5，顶层打开正式工作台应引导返回原游戏页。交付 ACK 只代表投递，持久任务的终态决定重生成结果。等待领取和已领取租约默认 60/120 秒。失败、取消或超时保留旧候选。正常工作台不提供刷新操作，握手故障可重建 iframe，同 Stage 未保存编辑从本地快照恢复。
4. 用户点击进入共创后，最新候选在事务内固化为 Stage 1，并在同一步启动正式服务端 20 分钟 deadline。预览、刷新和重生成均不启动计时。到期后聊天、语言、编辑、保存、恢复、试玩及方案操作锁定，只开放最终提交。按最终提交规则，可把当前合法且可解的未保存草稿原子保存为最终 `human_edit` Stage。

入口见 [Unity 共创控制器](../Assets/Scripts/Online/CoCreationEntryController.cs)、[共创草稿上下文](../Assets/Scripts/Online/CoCreationDraftContext.cs)、[WebGL 桥接](../Assets/WebGLTemplates/SokobanPixel/co-creation-bridge-contract.js)与 [8010 会话路由](../CoCreationPrototype/Backend/cocreation/api/routes_sessions.py)。

## Stage、试玩与最终交接

- 一个共创会话只有一张关卡。Stage 地图是该关卡的不可变快照，子版本保留父链。编辑草稿在通过后端尺寸、符号、实体数量、地图外壳和求解检查并成功保存后，才创建 `human_edit`。AI 候选还要经过授权、真实差异复验及明确采用，细则见 [8010 方案履约](../CoCreationPrototype/REPLY_RELIABILITY.md#提案履约与紫卡)。
- 试玩只针对已保存 Stage，使用该版本的票据并分别记录加载、开始、移动、推动、重开、完成或中断等指标。试玩不编辑地图、不创建版本，也不提交对手关卡。历史 Stage 的聊天和编辑只读，按权限可试玩、恢复或直接作为定稿。恢复创建一个新子版本并保留恢复来源和被替代版本，直接定稿历史 Stage 则引用原版本，按规则归档受影响的待审候选。
- 保存、恢复、试玩创建、定稿和研究同步按现有幂等与事务语义处理。保留先落实本地提交再进行外部同步的顺序。同步故障不重新创建已保存的 Stage，原键重试按最新持久化状态恢复投影。
- 最终 Stage 需由设计者显式确认。填写自报告意图后，正式会话才完成，Unity 从授权的集成接口取得最终 rows 和意图，再提交匹配挑战。浏览器使用 HttpOnly 会话 cookie，Unity 的 integration token 用于限定范围内的状态读取及草稿重生成回报。保留过期和不匹配请求的拦截。

## 服务数据与研究记录

| 数据归属 | 当前记录和交接 |
| --- | --- |
| 8010 会话与关卡 | 独立 SQLite/WAL，默认 `CoCreationPrototype/Backend/data/cocreation.sqlite3`。会话保留身份哈希、匹配引用、期限及当前/最终版本。Stage 保存地图、求解、实体绑定、父链和各自设计上下文。 |
| 8010 过程与授权 | 保留对话、翻译、Stage 开场归档、提案和决定、质疑和理由复核、试玩、最终 rows、设计意图、幂等键、审计与耗时。公开 Session 投影经权限和字段白名单过滤。内部上下文、认证凭据与模型专用字段留在对应边界。 |
| 8010 → 8000 | 正式进入共创后才发布 `first_stage`，之后同步必要的 Stage、开场、交互节点和 `final` 研究投影。保持 8010 为完整共创记录的权威源，完整聊天、DesignContext 与意图推测留在 8010，8000 的模型不接收这些投影作为输入。 |
| 8000 匹配与 Dashboard | 保留玩家、房间与双方结果、原 `draft` 和必要的共创投影。`coCreationDurationSeconds` 由 8010 从进入至定稿按服务端计时，默认范围 0–1200 秒。`result_submitted.durationSeconds` 是 8000 的对手挑战用时。 |

Dashboard 中 `Manual Edit Review` 关联观察与比较，`LLM Challenge` 保留有证据的人工编辑讨论，`Player Challenge` 保留原提案、理由、一次选择及方向决定，后续候选与选择各自可追溯。意图修订保留原卡、原文和审核后的表述。展示可缩写 Match ID 和 Study Session ID，保留完整值、复制能力及按双方的完整或短 Study Session ID 搜索。每个流程节点维持双方 Study Session ID 关联。

研究记录改动默认面向新记录与正常幂等重试。保留已有兼容和原记录，历史批量修正或回填按 [AGENTS.md](../AGENTS.md#authorization-and-data-protection) 的授权约束处理。

## 演示与公网入口

- 直接进入共创入口创建独立 `algorithm_demo` 会话。算法沿用两箱、两目标、水域、结构模板和反向拉箱思路，生成可解的 10×12 Draft。重生成只替换候选，显式进入才固化 Stage 1。Demo 无正式期限、8000 同步或匹配记录。只有成功创建新 Demo 才清理上一轮 Demo，正式数据与创建失败前的旧 Demo 均保留。
- [Nginx 配置](../CoCreationPrototype/Deployment/nginx-sokoban.conf)以 `https://sokobanaidemo.top` 的 `/game/`、`/frontend/`、`/cocreation/` 为公开路径，服务端口用于内部转发。HTTP、www 和旧源站地址按配置重定向。
- 游戏页 `DATA DASHBOARD` 在游戏页验证密码后打开 Dashboard。静态房间码输入由 WebGL 模板通过 `BrowserNavigation.jslib` 与 Unity 同步，生成码只读可选中，加入码可粘贴并归一化为六位字母数字。共创网页保留中英文两套界面。

## 研究链路验收

按变更范围验证两次问卷、匹配房间、DG 与 Stage 1 rows 一致性、蓝图重生成及握手失效、连续保存和恢复、当前及历史 Stage 试玩、完成/中断指标、定稿及设计意图交接、双方挑战和超时结果。使用 [README 的离线命令](../README.md#本地验证)和 [AGENTS 的 Game view 要求](../AGENTS.md#unity-authoring)记录证据。正式 WebGL 联调使用授权的匹配环境和实际兼容构建，区分源码、离线回归与真实运行结果。
