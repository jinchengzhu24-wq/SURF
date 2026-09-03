# Multi-Agent

## 当前职责

8010 的流程只有两个 LLM 角色。意图理解、风险判断和协商由聊天助手完成；地图操作由关卡修改助手在获得明确授权后完成。确定性生成器、候选搜索器、结构校验器和 Sokoban 求解器不是 Agent。

| Agent | 服务 | 使用模型 | 当前职责 | 输入 → 输出 |
|---|---|---|---|---|
| Draft首版理解助手 | 8000 | DeepSeek v4 Flash (`deepseek-v4-flash`) | 阅读四道中立 DG 回答，形成首版难度与布局理解 | DG 回答 → `dgContext` |
| 关卡蓝图规划助手 | 8000 | DeepSeek v4 Flash (`deepseek-v4-flash`) | 将确认后的设计理解转换成首版生成蓝图 | `dgContext` → `LevelDesignPlan` |
| 共创聊天助手 | 8010 | Kimi K2.6 (`kimi-k2.6`) | 理解意图、分析方案、做证据约束的风险判断、维护分歧，并编译已授权的语义计划 | 当前 Stage、地图事实、求解/试玩证据、当前 Stage 对话 → 普通正文、五色引导、`RevisionPlan`、`executionContract` |
| 共创关卡修改助手 | 8010 | Kimi K2.6 (`kimi-k2.6`) | 只按已授权契约生成局部操作候选，不重新解释意图、不参与协商 | `RevisionPlan`、`executionContract`、当前 Stage、地图事实、求解指标 → 操作候选 |

8000 的 `dgContext`、研究者目标和实验条件不得进入 8010。8010 的直接示例会话也不经过 8000。父 Stage 传给手动编辑复核的上下文只包含用户明确说过的目标和理由，不包含 AI 未确认的意图假设。

## 模型隔离与 8010 输出边界

- 8000 的两个 Agent 继续使用 `deepseek-v4-flash`；8010 的两个 Agent 以及 Stage 开场、普通聊天、翻译和 Revision 相关 LLM 任务统一使用 Kimi `kimi-k2.6` 与 Moonshot 地址。
- 8010 使用 `thinking` disabled、`temperature=0.6` 和 `max_completion_tokens`。结构化请求优先使用严格 JSON schema；接口兼容性失败时只在同一 Kimi 请求内降级到 `json_object`，结构化校验失败后进行有限的同模型重试，不静默切换到 DeepSeek。
- 模型只负责生成候选内容，服务端负责最终文本清洗、地图事实、坐标、路线端点、进度权限、提案契约和确定性校验。`designContextPatch`、`coordinateLinks`、`gridDistance`、`tileAt`、`_solver`、`solutionSteps` 等内部字段不得进入用户可见正文。
- Stage 评价仍由 8010 后端生成和归档，但前端不再显示 Stage 评价卡。Stage 1 开场由后端幂等追加固定操作收尾；其他 Stage 和普通聊天不追加该收尾。旧 Stage 1 开场在读取展示时保守补齐，不重写历史数据库记录。

## 后续聊天、共创进度与路线

- 每个 Stage 的第一条 assistant opening 只承担开场观察、开场记录、分歧和意图假设处理，不更新“已确认决策”或“未解决问题”。从同一 Stage 的第二条及之后普通 assistant 回复开始，才允许消费合法的 `designContextPatch`，并累计、更新、去重未解决问题。
- `followUpQuestion`、活跃结构化分歧的 `nextQuestion` 以及回复中明确、具体的地图设计问题可以进入未解决问题；“你怎么看”“可以吗”“是否满意”等泛化问题不进入。模型 patch 中的 `decisions/rejections` 不能直接创建已确认决策；已确认决策只能由用户正式接受/拒绝提案或解决分歧产生。
- 地图相关回复强烈偏好一条简短、真实的移动路线，但只有出现“玩家/箱子到目标”“坐标到坐标”或“经过明确通道/区域”等明确移动关系时才生成。仅描述位置、靠近、相邻、方向或并列实体不标路线。
- 路线可见文字必须是正文连续子串，`coordinateLinks.text` 必须与其完全一致，`from/to` 必须来自生成时的当前 Stage 地图事实并通过可达性校验。端点错误、不可达、旧 Stage 或无法确认的关系不生成箭头；中英文切换只翻译路线文字，端点不变。

## 与《共创修改流程》图的对应关系

```text
用户表达修改需求
  → 共创聊天助手理解意图
  → 意图清楚？
       ├─ 否：普通正文 + TENTATIVE INTENT → 用户澄清 → 重新理解
       └─ 是：分析当前地图、目标、路径和游玩影响
              ├─ AI REVISION
              │    → 风险检查（WARNING 仅在有具体证据时出现）
              │    → 生成 / 质疑 / 换一个方案
              └─ MANUAL EDIT
                   → 用户编辑并保存
                   → 确定性校验与求解
                   → human_edit 风险复核
                        ├─ 无具体担忧：保留用户修改
                        └─ 有具体担忧：WARNING + LET'S DISCUSS
```

图中的 `WARNING` 是风险检查的条件性结果，不代表每次检查都必须显示红卡。图中的 `AI REVISION` 对应紫色概念方案卡；紫卡不是执行授权，也不是已保存地图。图中的 `LET'S DISCUSS` 对应结构化的 active `disagreement`，只表示双方仍在某个具体设计决策上有分歧。

## AI REVISION 分支

聊天助手先分析箱子、目标、墙体、水域、玩家位置、路径、推箱顺序、可达区域、求解指标、试玩证据以及用户明确目标。仅仅因为用户的方向不同于 AI 原方案，不得触发风险质疑。

紫卡始终表示已经形成、可以继续审查的方案，并提供三个动作：

1. `execute_revision`：用户点击“请助手生成这个方案”后，才把已引用的 `proposalOffer` 转换为 `RevisionPlan` 和 `executionContract`，交给关卡修改助手。
2. `challenge_revision`：用户点击“质疑这个方案”后，首轮只返回普通正文，重述方案、解释提出原因和目标游玩时刻，并邀请用户给出具体理由；首轮不生成新卡或地图。
3. `alternative_revision`：用户点击“换一个方案”后，不执行原方案、不生成地图行，要求提出语义上可识别不同的概念紫卡；有限重试仍无法区分时只说明失败。

用户说明质疑理由后，聊天助手根据地图证据判断：接受用户方向、由用户接受 AI 方向、形成折中时，先用正文说明共识，再生成新的紫卡；决定保留当前地图时结束分歧且不创建 Stage；仍有分歧时保持蓝卡，不生成紫卡。

## MANUAL EDIT 分支

用户可以不采用 AI 方案，直接在编辑器修改。保存前由后端确定性检查尺寸、符号、实体数量、外壳和可解性；失败只返回验证错误并保留草稿，不调用质疑流程。成功后创建 `human_edit` Stage，用户的修改始终保留。

首次复核接收修改前后 rows、完整 verified diff、changed components、前后地图事实、求解指标、试玩证据和祖先 Stage 上用户明确表达的设计目标。安全修改只在普通正文中确认和分析，不强行制造互动。只有能指出具体路径、元素交互、死锁/不可解、可读性、公平性或目标冲突时，才先用正文表达分歧，再显示 `WARNING` 和 active `LET'S DISCUSS`；AI 不撤销、覆盖或替换用户地图。

手动编辑的分歧继续通过普通聊天协商：接受用户理由则保留当前 `human_edit`；用户接受 AI 理由或双方形成需要再改图的折中时生成紫卡；部分共识但决定保留当前编辑时结束分歧；未达成一致则更新蓝卡并保持当前编辑。

同一个当前 Stage 可以保留多个概念性 `REVISION`，但 `sourceTurnId` 只能引用最近一条有效 `proposalOffer`。前端保留旧紫卡但禁用其三个动作，后端必须再次拒绝过期来源；普通正文不改变最新紫卡，历史 Stage 仍不可操作。

## 卡片边界

系统仍只有五类卡片：

- 橙色 `TENTATIVE INTENT`：AI 对用户设计目的的可纠正理解。
- 紫色 `REVISION`：已经形成、可执行但仍须用户决定的 AI/协商方案。
- 绿色 `MANUAL EDIT`：用户可自行在右侧编辑器实践的路径。
- 红色 `WARNING`：有具体地图或试玩证据支持的机械风险。
- 蓝色 `LET'S DISCUSS`：结构化的尚未解决分歧，包含用户立场、AI 立场、核心分歧和下一问题。

普通解释、普通提问、普通建议留在正文；它们不自动生成新蓝卡。`guidance.disagreement.status == "active"` 时不得带 `proposalOffer`。只有 resolved 的 `user`、`ai` 或 `compromise` 才能重新形成紫卡；`retain_current` 只结束分歧，不创建地图版本。旧 turn 缺少 `disagreement` 时，前端继续按旧 `followUpQuestion` 兼容显示历史蓝卡。

## 8010 交接与保护

```text
co_creation_chat
  → RevisionPlan + executionContract
  → co_creation_revision
  → operation candidates
  → atomic deterministic execution
  → structure checks + validate_and_solve()
  → reviewable proposal
  → 用户接受
  → 新 Stage
```

普通聊天、质疑和换方案都不得生成 `proposedRows`。只有 `execute_revision` 且来源 turn 属于当前 Stage、Stage 未过期、契约有效并通过确定性验证时，才会创建待审查 proposal；只有用户接受 proposal 后才创建 `llm_accepted` Stage。失败、超时、空候选或非法模型输出不得放宽要求、覆盖原 Stage 或伪造分歧。

## 跨 Stage DesignContext

每个 `level_versions` 通过可空的 `design_context_json` 保存一个服务端拥有的 DesignContext 快照。创建子 Stage 时先复制父快照，再记录本次事件；父快照不可变。快照包括 `userGoals`、`designConstraints`、`confirmedDecisions`、`rejectedDecisions`、`openQuestions` 和 `activeDisagreement`，每项保留来源 Stage/turn。

authority 必须严格区分：`explicit` 是用户原文，`confirmed` 只能来自用户接受/确认/折中/保留现状等正式动作，`inferred` 只是可纠正的 AI 假设。模型不能自行把 inferred 写成 confirmed；用户修正时保留旧条目并标记 `superseded` 或 `rejected`。普通聊天可以返回内部 `designContextPatch`，但服务端重新校验并以用户原文和正式动作决定权限；非法 patch 只审计，不阻塞对话。

Chat 读取完整带来源快照；Revision 只读取 active explicit/confirmed 目标和约束、确认/拒绝决策、相关开放问题及执行 brief，不读取完整历史聊天、inferred 硬约束、8000 DG context、研究者目标或实验条件；Evaluator/手动编辑复核读取完整评估投影。DesignContext 只用于后台上下文，不增加卡片，不替代最终 `designer_intentions`，也不改变最新紫卡唯一可操作规则。

`proposalOffer` 可以带隐藏的 `executionBrief`，用于把可见的 summary/rationale 连接到可执行契约。它保存 `effect`、`anchors`、`focus`、`requiredTransitions`、`allowedOperators`、`preserve` 和 `playObjective`；明确的坐标及 `from → to` 必须原样进入 `RevisionPlan`，并由服务端对照当前 `tileAt` 再校验。精确的一格结构调整允许一个变更格，实体移动必须成对变更。修改助手候选全部非法时，服务端调用生产确定性 `search_revision_plan()` 兜底；旧紫卡缺少 execution brief 时保持兼容，但不得猜测邻近格。

动作、风险复核和分歧沿用 `audit_events`，包括 `card_action_requested`、`proposal_challenge_started`、`alternative_revision_requested`、`disagreement_started`、`disagreement_updated`、`disagreement_resolved` 和 `human_edit_reviewed`。

## Prompt 文件

- [Draft首版理解助手（8000）.md](Draft首版理解助手（8000）.md)
- [关卡蓝图规划助手（8000）.md](关卡蓝图规划助手（8000）.md)
- [共创聊天助手（8010）.md](共创聊天助手（8010）.md)
- [共创关卡修改助手（8010）.md](共创关卡修改助手（8010）.md)
