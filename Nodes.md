# Dashboard Nodes / Dashboard 节点

## Draft / DG Record（草稿 / DG 记录）

- **Created when / 创建条件：** The four DG answers and the final difficulty and layout choices are saved. / 保存 DG 四题答案以及最终难度、布局选择时。
- **Records / 记录：** Player answers and final choices; AI reflection, recommendations, and rationales. / 玩家答案与最终选择；AI 反思、建议及理由。
- **Status or result / 状态或结果：** Recorded. / 已记录。
- **Relations / 关联：** Precedes First Stage. / 位于 First Stage 之前。

## First Stage（首版 Stage）

- **Created when / 创建条件：** The initial co-creation map is created. / 创建共创首版地图时。
- **Records / 记录：** Generation method, map, server validation and solver result; the Stage opening assessment is attached to this Stage. / 生成方式、地图、服务端验证和求解结果；Stage 开场评估附在该 Stage 中。
- **Status or result / 状态或结果：** Validated. / 已验证。
- **Relations / 关联：** Parent of later Stages and their discussion nodes. / 是后续 Stage 及其讨论节点的父版本。

## General Discussion（普通交流）

- **Created when / 创建条件：** A visible ordinary conversation begins; it ends when an intent, proposal, challenge, saved Stage, or final submission flow begins. / 可见的普通对话开始时；进入意图、方案、质疑、保存 Stage 或最终提交流程时结束。
- **Records / 记录：** Player messages and LLM replies. / 玩家发言和 LLM 回复。
- **Status or result / 状态或结果：** In progress or completed. / 进行中或已完成。
- **Relations / 关联：** Belongs to its current Stage. / 归属于当前 Stage。

## Intent（暂定意图）

- **Created when / 创建条件：** An ordinary reply displays a tentative intent card. / 普通回复出现可见的暂定意图卡时。
- **Records / 记录：** Player wording, LLM reply, tentative intent card, and the player’s confirmation, revision, or rejection action. / 玩家原话、LLM 回复、暂定意图卡，以及玩家确认、修改或拒绝操作。
- **Status or result / 状态或结果：** In progress, awaiting player choice, confirmed, or rejected. / 进行中、等待玩家选择、已确认或已拒绝。
- **Relations / 关联：** Belongs to its Stage. / 归属于所在 Stage。

## Intent Conflict（意图矛盾）

- **Created when / 创建条件：** A new tentative intent conflicts with a confirmed intent and requires a visible choice. / 新暂定意图与已确认意图矛盾，并需要可见选择时。
- **Records / 记录：** New and existing intentions, LLM explanation, and the player action that retains, replaces, or rejects an intention. / 新旧意图、LLM 说明，以及玩家保留、替换或拒绝意图的操作。
- **Status or result / 状态或结果：** Awaiting player choice or resolved. / 等待玩家选择或已解决。
- **Relations / 关联：** Belongs to its Stage and the conflicting Intent records. / 归属于所在 Stage，并关联冲突的 Intent 记录。

## Proposal（方案）

- **Created when / 创建条件：** A proposal topic starts. / 一个方案主题开始时。
- **Records / 记录：** Proposal request, the actual one to three clarification questions and answers, final plan, verified candidate map result, and player actions. / 方案请求、实际发生的一至三个澄清问题与回答、最终方案、已验证候选地图结果及玩家操作。
- **Status or result / 状态或结果：** In progress, proposal clarification 1/3–3/3, proposal ready, accepted, rejected, cancelled, or replaced. / 进行中、方案澄清 1/3 至 3/3、方案就绪、已接受、已拒绝、已取消或已替代。
- **Relations / 关联：** Belongs to a Stage; an alternative Proposal links to the Proposal it replaces. / 归属于一个 Stage；替代方案关联其替代的 Proposal。

## Player Challenge（玩家质疑）

- **Created when / 创建条件：** The player chooses `Challenge this plan`. / 玩家选择 `Challenge this plan` 时。
- **Records / 记录：** Source Proposal, LLM initial hypotheses, player challenge reason, LLM replies, and final resolution. / 来源 Proposal、LLM 初始假设、玩家质疑理由、LLM 回复及最终解决结果。
- **Status or result / 状态或结果：** In progress, awaiting player choice, or resolved. / 进行中、等待玩家选择或已解决。
- **Relations / 关联：** Child of the challenged Proposal and belongs to its Stage. / 是被质疑 Proposal 的子节点，并归属于所在 Stage。

## Manual Edit Review（人工编辑复核）

- **Created when / 创建条件：** A manually edited Stage is saved and reviewed. / 人工编辑的 Stage 被保存并完成复核时。
- **Records / 记录：** Player save action, verified map difference, LLM card-free observation, and LLM comparison. / 玩家保存操作、已验证地图差异、LLM 无卡片观察及 LLM 比较。
- **Status or result / 状态或结果：** Reviewed. / 已复核。
- **Relations / 关联：** Belongs to the saved `Stage N · Manual`; may be the parent of LLM Challenge. / 归属于保存的 `Stage N · Manual`；可作为 LLM Challenge 的父节点。

## LLM Challenge（LLM 质疑）

- **Created when / 创建条件：** Manual Edit Review finds an evidence-backed disagreement based on map, solver, play, or confirmed design-inclination evidence. / Manual Edit Review 基于地图、求解、试玩或已确认设计倾向证据发现有效分歧时。
- **Records / 记录：** LLM challenge statement, evidence basis, and the requested discussion question. / LLM 质疑说明、证据依据及要求讨论的问题。
- **Status or result / 状态或结果：** In progress or resolved. / 进行中或已解决。
- **Relations / 关联：** Child of its Manual Edit Review. Later player and LLM conversation belongs to General Discussion. / 是对应 Manual Edit Review 的子节点；后续玩家与 LLM 对话归入 General Discussion。

## Stage N · AI / Manual（Stage N · AI / Manual）

- **Created when / 创建条件：** An AI Proposal is accepted or a player saves a manual map edit. / 接受 AI Proposal 或玩家保存人工地图编辑时。
- **Records / 记录：** Source, parent Stage, map, verified difference, and solver result. / 来源、父 Stage、地图、已验证差异和求解结果。
- **Status or result / 状态或结果：** Validated. / 已验证。
- **Relations / 关联：** Child of its parent Stage; AI Stages link to their Proposal and Manual Stages link to Manual Edit Review. / 是父 Stage 的子版本；AI Stage 关联其 Proposal，Manual Stage 关联其 Manual Edit Review。

## Final Stage（最终 Stage）

- **Created when / 创建条件：** The player explicitly submits the final Stage. / 玩家明确提交最终 Stage 时。
- **Records / 记录：** Final map, final version, and server-calculated co-creation duration. / 最终地图、最终版本及服务端计算的共创时长。
- **Status or result / 状态或结果：** Finalized. / 已最终确认。
- **Relations / 关联：** Leads to Message and Challenge Maps. / 后接 Message 与 Challenge Maps。

## Message（最终设计意图）

- **Created when / 创建条件：** The player submits final design intention after Final Stage. / 玩家在 Final Stage 后提交最终设计意图时。
- **Records / 记录：** Player’s final design-intention message. / 玩家填写的最终设计意图 Message。
- **Status or result / 状态或结果：** Confirmed. / 已确认。
- **Relations / 关联：** Belongs to Final Stage. / 归属于 Final Stage。

## Challenge Maps / Match Results（挑战地图 / 对局结果）

- **Created when / 创建条件：** A final map is delivered to the opponent, and when match results arrive. / 最终地图发给对手时，以及收到对局结果时。
- **Records / 记录：** Delivered map and opponent play outcome, time, moves, restarts, and minimum moves. / 下发地图及对手对局结果、用时、步数、重开次数和最少步数。
- **Status or result / 状态或结果：** Pending, completed, or timed out. / 等待中、已完成或已超时。
- **Relations / 关联：** Follows Final Stage. / 位于 Final Stage 之后。
