# 共创回复可靠性、流程恢复与发布契约

本文件维护统一 `:8000` FastAPI 平台中共创工作台模块（`server/application/{sessions,dialogue}/`、`server/api/routers/{sessions,stage,dialogue,proposals,integrations,play_attempts}.py`、`server/llm/`、`server/domain/`、`server/persistence/`、`server/core/`、`web/apps/cocreation/`）的对话、设计记忆、Policy 2 提案、执行、恢复与发布契约。修改这些路径、前端卡片或模型协议前应阅读相关章节。整体拓扑见[系统架构](ARCHITECTURE.md)，正式双人路线、入场计时及研究数据边界见[现行研究流程](STUDY_PROTOCOL.md)。

## 回复与地图事实

- 当前地图的唯一权威是 `StageSnapshot`，由当前不可变 `level_versions` 的 12×10 `Grid`（[grid.py](../server/core/grid.py)）、版本 ID 和实体绑定构成。对话、开场、提案、路线恢复和执行验证均使用这份快照。旧 Stage rows、助手旧坐标和用户说错的坐标仅作待核实讨论信息。
- 每条请求的 `dialogueEvidence` 保留真实 Turn ID、Stage 和用户原话（当前 Stage 最近 24 条 Turn 加活动主题已固定用户来源）。授权新动作或变更状态须绑定当前用户原话或正式操作，助手建议不产生用户约束。
- **原生结构化 JSON 输出**：所有活跃 LLM 任务均通过 `server.core.llm_client.call_structured`（[llm_client.py](../server/core/llm_client.py)）与 Pydantic v2 `StrictSchema` 模型（`server/llm/schemas.py`、`server/llm/_schemas_tasks.py`、`server/llm/_schemas_proposals.py`：`StageOpeningPayload`、`OrdinaryChatReplyPayload` / `OrdinaryChatWithDisagreementPayload`、`TurnRoutingPayload` / `TurnRoutingWithHumanEditPayload`、`HumanEditEngagementRecoveryPayload`、`BinaryDiscovery*Payload`（含 `BinaryStrategyPayload` 与 `BinaryPromisePayload`）、`RequirementRecordPayload`、`RevisionPlanPayload`、`MapOperationsPayload`、`ManualEditReviewNoConflictPayload` / `ManualEditReviewWithConflictPayload`、`ManualEditConflictClassificationPayload`、`ChallengeReasonReviewPayload`、`PlainTranslationPayload` / `TranslationPayload`、`TranslationVerificationPayload`、`DGGuideSummaryPayload`、`LevelPlanPayload`）进行权威结构化校验。生产环境 Moonshot/Kimi 传输层在启动时显式声明 `response_format_mode="json_object"`，DeepSeek 传输层显式声明 `response_format_mode="prompt_only"`（请求体不传 `response_format` 字段，搭配 `extra_body={"thinking": {"type": "disabled"}}`），两者均由 `call_structured` 将精简后的 Pydantic JSON Schema 注入系统提示词（并在校验失败的修复重试中持续保留）及做单层代码围栏剥离，以规避上游专有 JSON Schema 方言或模型 `response_format` 限制造成的不可重试 HTTP 400 拒收；`call_structured` 仍保留对兼容传输层的 `json_schema` 支持，并彻底废除旧版 `<GUIDANCE>||` 文本拼接解析及运行时遇到 400/422 静默降级的做法。
- `server/llm/grounding.py` 使用模块级预编译正则对照 `Grid` 快照核验实体、坐标、可达性及 `coordinateLinks`，不破坏正常正文，不把错误地图句或未验证结论写入回复、记忆、卡片或提案。
- 正常正文、可选卡片、链接、进度和记忆分别验收。可选组件无效时保留已验证正文；普通聊天和路线讨论耗尽机会后返回可重试错误且不写助手回合；仅已定义的 Stage 开场允许使用快照兜底。Stage assessment 仅归档，前端不展示独立评价卡。

## 请求预算、幂等与提交

| 边界 | 当前契约 |
| --- | --- |
| 模型传输与隔离 | 共创管线使用原生异步 `AsyncOpenAITransport`（由 FastAPI lifespan 统一管理 `httpx.AsyncClient` 连接池，零 `asyncio.run()`、零后台守护线程、零全局 ContextVar），独占 Moonshot/Kimi 配置且绝不回退至 `DEEPSEEK_*`；对 `kimi-k2.*` / Moonshot 显式配置 `extra_body={"thinking": {"type": "disabled"}}`、固定 `temperature=0.6`、`max_completion_tokens` 与 `response_format_mode="json_object"`；DeepSeek 传输层显式声明 `response_format_mode="prompt_only"`（请求体不传 `response_format`）与 `extra_body={"thinking": {"type": "disabled"}}` 并保留各管线自定义温度。错误分类严格区分 `LLMValidationError`、`UpstreamLLMError`、`DeadlineExceededError`，由 [main.py](../server/main.py) 统一映射为结构化 `422`/`503`/`504` 响应（`LLMValidationError` → `422`、`UpstreamLLMError` → `503`、`DeadlineExceededError` → `504`，避免源站 `502` JSON 被 Cloudflare 拦截替换为 HTML Bad Gateway 页面），绝不将内部 Python 异常（`TypeError`/`KeyError` 等）误判为可重试模型故障。所有日志与错误输出经 [settings.py](../server/core/settings.py) 的 `redact_secrets` 脱敏。 |
| 主请求预算 | 服务端总预算 120 秒（HTTP 入口建立 116 秒内部绝对截止时间）。Policy 2 轻量路由 `TurnRoutingPayload` 单次上限 15 秒；点击 `Proposal` 直通 `BinaryDiscoveryPipeline` 单槽上限 70 秒并预留 20 秒恢复储备；确定性预检、修复重验与求解共用最多 20 秒并服从截止时间。 |
| 浏览器超时与重试 | 前端（`web/apps/cocreation`，工作流位于 `src/store/workflows/`）消息请求 125 秒 `AbortController` 超时，非法 2xx 视为失败。写失败先只读刷新确认当前 Stage 与原幂等键后再恢复；重试带回原文本、动作、来源、Stage、草稿、方案模式和幂等键。 |
| 数据库原子幂等 | 幂等状态持久化在独立索引表 `idempotency_keys`（`PRIMARY KEY (session_id, scope, idempotency_key)`，见 [schema.py](../server/persistence/schema.py) 与 [operational_repo.py](../server/persistence/repositories/operational_repo.py)），取代进程内内存锁。同会话键绑定原正文、基础 Stage、动作及方案模式。 |
| 事务与并发提交 | `SQLiteDatabase.write_tx()` 仅在短提交阶段持有 `BEGIN IMMEDIATE`（<10ms），**绝不**跨越求解器或 LLM 调用。末次写事务以 CAS 复核活跃会话、当前 Stage、地图与实体、设计上下文及最新用户回合；过期方案或卡片动作返回 `PROPOSAL_STALE`。共创事件与定稿意图在同一 SQLite 事务内完成对 `online_rooms` / `online_room_events` 的进程内 ACID 同步。 |

`lastMessageFailure` 仅投影当前 Stage 可匹配的最近未交付消息故障，成功回复清除旧失败；原始模型输出、提示词与凭据不进入公开失败详情。

## 回合理解与 Policy 2 方案发现

共创工作台现行仅运行 **Policy 2 二选一方案发现**；旧版 Policy 1 自然语言澄清分支与旧选项校验逻辑已从统一后端中移除。

- **普通自由对话（未点击 `Proposal`）**：走 `UnderstandingPipeline` 的轻量路由 `TurnRoutingPayload`（单次上限 15 秒），分类行为类型、提及对象、地图相关性与受保护对象变更请求，并在原文证据下拦截越权改图请求。
- **点击 `Proposal`（`requestProposal=True`）**：无活跃分歧时直通 `BinaryDiscoveryPipeline` 单槽处理；若首轮明确要求修改外壳、玩家、箱子或目标，直接返回 `action="protected_refusal"` 与既有权限说明并安全取消主题，不创建 Stage。
- **三问上限与具体可执行 A/B**：
  - 信息充分时直接进入规划；需澄清时每次发布一道问题与两个具备实质差异的 A/B 按钮，同一主题最多发布三道题（幂等重试、失败发布或翻译不消耗题数）。
  - 两个选项均须基于同一不可变 `StageSnapshot` 生成可执行、可解候选，满足权限、结构、既有固定要求与累计动作，全主题总改动预算不超过 **12 格**（`MAX_EDIT_BUDGET = 12`）。
  - 澄清中仅允许点击当前 A/B 按钮或专用取消（取消不调用模型、不创建 Stage），禁止自由文本绕过。
  - 选择接口 `POST /api/sessions/{session_id}/proposal-discovery/choices` 与取消接口 `POST /api/sessions/{session_id}/proposal-discovery/cancel`（及 `/cocreation/api/...` 别名）绑定 Stage、主题、问题、选项、展示语言与幂等键。
  - 主题私有状态（候选、证明、实施格子）存储在独立索引表 `binary_discovery_topics`（而非从 `audit_events` 扫描），公开 `proposalFlowState` 仅投影问题、选项标签、状态与可重试动作。
  - 进入最终规划后，服务端确定性合并已验初始要求与已选 `choice.promise`，衔接到 `ProposalPipeline` 择优与有界束搜索回落（`server/domain/proposals/`）。

## 提案履约与紫卡

1. **要求核验**：`RequirementRecordPayload` 核验属性、实体、单位、范围、值与显式转换。确定性归一化清理平台固定保护项的冗余条目，对数值与精确坐标转换保持严格原文证据校验。`invariant` 不可放宽，`goal` 可部分满足但不得反向，`preference` 保留不确定性。
2. **确定性地图权限**：AI 编辑严格限定于 [grid.py](../server/core/grid.py) 与 [server/domain/proposals/](../server/domain/proposals/) 定义的四种操作——`add_wall`、`remove_wall`、`add_water`、`remove_water`；严禁移动或增删外壳（`outer_shell`）、玩家（`p`）、箱子（`s`）和目标点（`t`）。墙体授权不延伸到水域，反之亦然。
3. **修改与求解验证**：`ProposalPipeline` 输出局部操作候选并结合有界束搜索回落（`BEAM_WIDTH=24`、`BEAM_DEPTH=3`），通过 [solver.py](../server/core/solver.py) 执行确定性验证：`solve_min_steps`（带静态死锁裁剪）验证可解性与最少步数，**仅当要求涉及推箱数时才惰性调用 `solve_min_pushes`**。任何分支均不得交付未经验证或无实际有效改动的地图。
4. **紫卡动作与采用**：候选通过复验后冻结逐格 `executionBrief`。紫卡仅对当前 Stage 最新有效 `proposalOffer` 开放 `availableActions`，旧卡保持只读；后端拒绝过期 `sourceTurnId`（区分 `INVALID_CARD_SOURCE` 与 `PROPOSAL_STALE`）。`execute_revision` 仅重放原冻结差异并复验生成待审 proposal，用户明确采用时再次确定性校验和求解后才创建新 Stage。拒绝、失效和失败均保留原图。

## 质疑、人工编辑与分歧

- 仅使用具体地图、求解、试玩或明确目标证据陈述风险。`WARNING` 须有具体条件证据；不同意 AI、搜索失败或使用人工编辑器本身不构成警告。
- 标准 `REVISION` 卡允许执行、质疑或替代。首次质疑邀请说明理由；清晰理由复核后生成 `ai_revision_challenge` 选择卡（`challengeCardSchemaVersion=2`）。`choice_pending` 选择“是”重验原冻结差异，“否”在排除旧候选的前提下生成仅允许 `execute_revision` 的新紫卡；`retain_current` 结束分歧且不创建 Stage。
- 手工编辑通过保存校验即创建新 `human_edit` Stage。`UnderstandingPipeline` 产出 `ManualEditReviewPayload`（观察与基于差异/求解/证据的比较），仅在存在具体冲突时增加一次 `LET'S DISCUSS`，不附带 `WARNING`，也不自动撤回编辑。`humanEditEngagement` 在绑定最新用户原话后记为 `acknowledged` 并解除提案阻塞。

## DesignContext、意图与问题进度

- `level_versions.design_context_json` 由服务端维护。子 Stage 克隆父 Stage 快照后再记录新证据或决定，历史快照不回溯吸收后代状态。`explicit`（用户明说）、`confirmed`（正式确认）与 `inferred`（可纠正推测）严格分层，推测不得覆盖明确记忆或变为执行硬约束。用户自报告的最终设计意图（持久化于 `design_sessions.final_intention_json` 并同步至 `online_rooms` / `online_room_events`）与这套记忆完全独立。
- 橙色 `TENTATIVE INTENT` 仅在普通轮中可选显示，需通过专用反馈接口确认、修订或拒绝（拒绝不调用模型）。
- 进度抽屉分为 `Unresolved questions`（来源为非橙卡助手内容，已回答或忽略进入 `Processed`）与确认/修订后的 `Design inclinations`（证据轨迹最多 12 条）。忽略与恢复均为确定性接口，不写额外聊天且不调用模型；Stage 开场不推进问题或已确认决定。

## 局部修复、翻译与必要状态恢复

- **结构化重试**：`call_structured` 仅针对 `LLMValidationError` / `UpstreamLLMError` 在剩余截止时间内重试；有效语义拒绝保持拒绝，内部编程错误直接暴露不吞没。上游理解、要求或操作变化后按依赖重新校验权限、快照、求解与冻结差异。
- **翻译独立预算**：`TranslationPipeline` 为翻译生成与翻译复核分别分配独立尝试预算（彻底消除旧版共用预算导致的尝试死锁），按 Turn 逐条保存成功项；复核失败保留原文，A/B 题组以整组为单位保持 ID/顺序一致或整组回退。
- **必要状态恢复表**：人工编辑分歧讨论的必要状态技术故障仍有可靠正文时，在同一事务内写入正文与恢复任务到独立索引表 `component_recovery_tasks`（取代旧版写入并扫描 `audit_events` 的方式）。界面在当前 Stage 可见且无并发写操作时可触发 `POST /api/sessions/{session_id}/component-recoveries/{recovery_id}/retry`：总预算 10 秒、模型上限 8 秒、浏览器 12 秒，两次终身尝试，短事务领取租约 15 秒，提交时执行 CAS 校验。`GET` 端点绝不触发模型。

## 审计与离线验证

- `audit_events` 为严格只追加、按 `(session_id, event_type, id)` 建索引的审计表，不承载实时运维状态，并严格遵守两类载荷边界：
  - **进度与分歧证据类审计事件**（如 `intent_evidence_recorded`、`intent_progress_rewrite`、`intent_hypothesis_recorded`、`revision_execution_outcome`、`human_edit_reviewed`、`disagreement_started` / `disagreement_acknowledged` / `disagreement_resolved`）：为进度抽屉与读时投影提供确定性证据轨迹，允许保留必要的短文本证据摘录（如 `<= 500` 字符的 `observation` / `summaryText` / `detailedText`）与结构化决策字段，严禁写入系统提示词、原始模型 JSON 输出、完整 12×10 地图或凭据。
  - **纯遥测元数据审计事件**（如 `reply_delivery_outcome`、`message_generation_failed`、`reply_generation_failed`、`challenge_reason_review_pending`、`component_recovery`、`room_sync_missing_room` 等）：严格仅记录状态码、计数、耗时、标识与结构化元数据，不写入对话正文、提示词、原始模型输出、地图或凭据。
- 只读可靠性统计与离线语义评测工具位于 [reliability_report.py](../server/tools/reliability_report.py) 和 [semantic_eval.py](../server/tools/semantic_eval.py)（评测用例集位于 [server/evaluations/](../server/evaluations/)），可直接从仓库根目录执行（零真实模型调用）：

```powershell
& .\.venv\Scripts\python.exe -m server.tools.reliability_report server/data/sokoban.sqlite3 --since 2026-01-01T00:00:00Z --format text
& .\.venv\Scripts\python.exe -m server.tools.semantic_eval validate
```

- 常规回归与契约测试使用 [README 的本地验证命令](../README.md#本地验证)（`ruff`、`pytest`、`npm --prefix web run typecheck/test/build`），全部采用注入的离线 `LLMTransport` 替身、`FakeClock` 与临时 SQLite 存储，零真实模型费用。任何真实模型采样评测均须获得本轮明确授权，并隔离本地数据与成本。

## 部署与回滚

此节适用于已按 [AGENTS.md](../AGENTS.md#authorization-and-data-protection) 获得本轮明确授权的发布。统一生产配置全部位于仓库 `deploy/` 目录（环境变量模板见 [.env.example](../.env.example)，本地凭据统一保存在根目录 `.env`）：

1. **发布前备份**：核对批准的变更范围与生产基线。通过 SQLite 在线 backup API（`sqlite3 .backup` 或 Python `Connection.backup`）取得 `server/data/sokoban.sqlite3` 的一致快照，**WAL 模式下严禁直接裸拷贝使用中的数据库主文件**，同时备份将被替换的代码与配置并保护 `.env` 凭据。
2. **统一构建与发布**：使用 `deploy/deploy.ps1`（或等效手工步骤），通过 SSH 别名 `sokoban` 与 `%USERPROFILE%\.ssh\sokoban`（`IdentitiesOnly=yes`，无硬编码 IP/凭据）发布；支持 `-WhatIf` 干跑。脚本在本地构建 `web/`（输出 `web/dist/dashboard`、`web/dist/cocreation`、`web/dist/survey` 及 WebGL 桥接脚本），打包 `server/`（含 `server/core/` 与 `server/evaluations/`）、`web/dist/`、`deploy/` 与 `pyproject.toml` 上传至目标主机。
3. **服务配置与重启**：
   - systemd 托管：更新并重启单一非 root 服务 `deploy/sokoban.service`（`User=sokoban`、`NoNewPrivileges=true`、`PrivateTmp=true`、`EnvironmentFile=-/opt/sokoban/.env`，通过 `uvicorn server.main:create_app --factory` 监听 `127.0.0.1:8000`）。
   - 容器托管：使用多阶段 `deploy/Dockerfile`（Node 22 + Python 3.12 非 root `appuser`，入口 `uvicorn server.main:create_app --factory`）与 `deploy/compose.yaml`（映射 `8000:8000` 并挂载持久化 SQLite 数据卷）。
   - Nginx 反向代理：`deploy/nginx-sokoban.conf` 将 `/game/`、`/frontend/`、`/cocreation/`、`/survey/` 与 `/` 统一转发至 `127.0.0.1:8000`（`/cocreation/` 与 `/api/` 保留 135 秒超时，`/survey` 入口保留查询参数并对 `/survey`、`/survey/`、`/survey/index.html` 与其他入口页面及 `sokoban-bridge.js` 配置 `no-cache`，并按可信代理加固 `X-Forwarded-Proto`）。仅修改文档或纯静态资源时按需处理，无需不必要的服务中断。
4. **发布后核验**：验证源站及公网 `GET /health`、`GET /ready`、`/frontend/`、`/cocreation/`、`/survey/` 静态资源及预期的未授权/密码拦截响应（注意只做只读静态核验，不要通过直接打开无会话 `/cocreation/` 或在 `/survey/` 提交未授权正式问卷污染研究记录，核验只读接口或已授权测试会话）。
5. **代码回滚**：恢复上一版本的代码与构建产物并重启 `sokoban.service`，执行同范围健康与只读检查。保留发布期间新采集的研究数据；数据库备份仅用于单独获准的数据恢复。

经授权的 WebGL 更新须额外核对本地兼容构建与 `/game/` 下的 loader、data、framework、wasm 及 `TemplateData/sokoban-bridge.js` 响应。环境状态与本地运维细节见 Git 忽略的 `SERVER.md`。
