# Multi-Agent

## 当前职责

8010 的流程只有两个 LLM 角色。意图理解、风险判断和协商由聊天助手完成；地图操作由关卡修改助手在获得明确授权后完成。确定性生成器、候选搜索器、结构校验器和 Sokoban 求解器不是 Agent。

| Agent | 服务 | 当前职责 | 输入 → 输出 |
|---|---|---|---|
| Draft首版理解助手 | 8000 | 阅读四道中立 DG 回答，形成首版难度与布局理解 | DG 回答 → `dgContext` |
| 关卡蓝图规划助手 | 8000 | 将确认后的设计理解转换成首版生成蓝图 | `dgContext` → `LevelDesignPlan` |
| 共创聊天助手 | 8010 | 理解意图、分析方案、做证据约束的风险判断、维护分歧，并编译已授权的语义计划 | 当前 Stage、地图事实、求解/试玩证据、当前 Stage 对话 → 普通正文、五色引导、`RevisionPlan`、`executionContract` |
| 共创关卡修改助手 | 8010 | 只按已授权契约生成局部操作候选，不重新解释意图、不参与协商 | `RevisionPlan`、`executionContract`、当前 Stage、地图事实、求解指标 → 操作候选 |

8000 的 `dgContext`、研究者目标和实验条件不得进入 8010。8010 的直接示例会话也不经过 8000。父 Stage 传给手动编辑复核的上下文只包含用户明确说过的目标和理由，不包含 AI 未确认的意图假设。

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

动作、风险复核和分歧沿用 `audit_events`，包括 `card_action_requested`、`proposal_challenge_started`、`alternative_revision_requested`、`disagreement_started`、`disagreement_updated`、`disagreement_resolved` 和 `human_edit_reviewed`。

## Prompt 文件

- [Draft首版理解助手（8000）.md](Draft首版理解助手（8000）.md)
- [关卡蓝图规划助手（8000）.md](关卡蓝图规划助手（8000）.md)
- [共创聊天助手（8010）.md](共创聊天助手（8010）.md)
- [共创关卡修改助手（8010）.md](共创关卡修改助手（8010）.md)
