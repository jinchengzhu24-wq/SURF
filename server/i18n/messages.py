"""Single bilingual (en, zh-CN) backend message catalog for deterministic user-facing copy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SUPPORTED_LANGUAGES: tuple[str, str] = ("en", "zh-CN")
DEFAULT_LANGUAGE: str = "en"


def normalize_language(language: str | None) -> str:
    """Return a supported backend locale code."""
    return "zh-CN" if str(language or "").strip() == "zh-CN" else DEFAULT_LANGUAGE


MESSAGES: dict[str, dict[str, str]] = {
    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------
    "error.internal": {
        "en": "An unexpected internal error occurred.",
        "zh-CN": "服务器内部发生意外错误。",
    },
    "error.configuration": {
        "en": "The required service configuration is incomplete.",
        "zh-CN": "所需服务配置不完整。",
    },
    "error.session_not_found": {
        "en": "The co-creation session was not found.",
        "zh-CN": "未找到该共创会话。",
    },
    "error.session_access_denied": {
        "en": "Session access was denied.",
        "zh-CN": "会话访问被拒绝。",
    },
    "error.session_locked": {
        "en": "This co-creation session is no longer editable.",
        "zh-CN": "该共创会话已结束或锁定，无法继续编辑。",
    },
    "error.language_not_selected": {
        "en": "Select the session language before entering the co-creation workspace.",
        "zh-CN": "进入共创工作区前请先选择会话语言。",
    },
    "error.language_locked": {
        "en": "The session language was already selected and cannot be changed.",
        "zh-CN": "会话语言已锁定，无法更改。",
    },
    "error.session_deadline_expired": {
        "en": "Time is up. Submit the current map as the final Stage now.",
        "zh-CN": "共创时间已到，请将当前地图作为最终 Stage 提交。",
    },
    "error.version_conflict": {
        "en": "The current Stage changed. Refresh before continuing.",
        "zh-CN": "当前 Stage 已发生变化，请刷新后再继续。",
    },
    "error.idempotency_conflict": {
        "en": "This request key was already used for different input.",
        "zh-CN": "该幂等键已用于不同的请求内容。",
    },
    "error.invalid_identifier": {
        "en": "The supplied identifier is invalid.",
        "zh-CN": "提供的标识符格式无效。",
    },
    "error.invalid_bootstrap_token": {
        "en": "The session link is invalid.",
        "zh-CN": "会话链接无效。",
    },
    "error.bootstrap_token_used": {
        "en": "The session link was already used.",
        "zh-CN": "该会话链接已被使用。",
    },
    "error.validation_failed": {
        "en": "The submitted input did not pass validation.",
        "zh-CN": "提交的内容未通过校验。",
    },
    "error.protected_edit": {
        "en": "AI revisions may edit water and internal walls only.",
        "zh-CN": "AI 修改方案只能调整水域与内部墙体。",
    },
    "error.unchanged_proposal": {
        "en": "The proposed map must change at least one tile from the current Stage.",
        "zh-CN": "提案地图必须相对当前 Stage 至少改动一个格子。",
    },
    "error.unchanged_level": {
        "en": "Save requires at least one tile change.",
        "zh-CN": "保存需要至少改动一个格子。",
    },
    "error.draft_regeneration_active": {
        "en": "Draft regeneration is already running.",
        "zh-CN": "初始草稿重新生成正在进行中。",
    },
    "error.stale_draft_regeneration": {
        "en": "The regeneration request is no longer current.",
        "zh-CN": "该草稿重新生成请求已过期。",
    },
    "error.room_not_found": {
        "en": "Room not found.",
        "zh-CN": "未找到房间。",
    },
    "error.room_cancelled": {
        "en": "Room has been cancelled.",
        "zh-CN": "房间已取消。",
    },
    "error.room_full": {
        "en": "Room is full.",
        "zh-CN": "房间已满。",
    },
    "error.room_not_ready": {
        "en": "Both players must be ready before continuing.",
        "zh-CN": "需要两位玩家都准备就绪后才能继续。",
    },
    "error.room_invalid_token": {
        "en": "Invalid player token.",
        "zh-CN": "玩家令牌无效。",
    },
    "error.player_not_found": {
        "en": "Player not found.",
        "zh-CN": "未找到该玩家。",
    },
    "error.online_sync_rejected": {
        "en": "The item was saved, but the online match rejected its synchronization data.",
        "zh-CN": "内容已保存，但联机对局拒绝了同步数据。",
    },
    "error.online_sync_unavailable": {
        "en": (
            "The item was saved but cannot yet be synchronized to the online match. "
            "Please retry."
        ),
        "zh-CN": "内容已保存，但暂时无法同步至联机对局，请稍后重试。",
    },
    "error.upstream_llm": {
        "en": "The language model service failed to produce a valid response.",
        "zh-CN": "模型服务未能返回有效结果。",
    },
    "error.deadline_exceeded": {
        "en": "The request exceeded its processing time budget.",
        "zh-CN": "请求超出了处理时间预算。",
    },
    "error.rate_limit": {
        "en": "The upstream service rate limit was reached. Please retry shortly.",
        "zh-CN": "上游服务触发限流，请稍后重试。",
    },
    "error.legacy_chat_disabled": {
        "en": "The legacy chat endpoint is disabled.",
        "zh-CN": "旧版聊天接口已停用。",
    },
    "error.invalid_message_action": {
        "en": "The message action is invalid.",
        "zh-CN": "消息操作类型无效。",
    },
    "error.challenge_exit_unsupported": {
        "en": "An active proposal challenge must be resolved through the conversation.",
        "zh-CN": "进行中的方案质疑必须在对话中完成。",
    },
    # ------------------------------------------------------------------
    # Tiles
    # ------------------------------------------------------------------
    "tile.void": {"en": "void", "zh-CN": "边界外区域"},
    "tile.wall": {"en": "wall", "zh-CN": "墙"},
    "tile.floor": {"en": "floor", "zh-CN": "地面"},
    "tile.water": {"en": "water", "zh-CN": "水面"},
    "tile.player": {"en": "player", "zh-CN": "玩家"},
    "tile.box": {"en": "box", "zh-CN": "箱子"},
    "tile.target": {"en": "target", "zh-CN": "目标点"},
    # ------------------------------------------------------------------
    # Binary Discovery & Protected Refusals
    # ------------------------------------------------------------------
    "binary.entity.targets": {"en": "targets", "zh-CN": "目标点"},
    "binary.entity.boxes": {"en": "boxes", "zh-CN": "箱子"},
    "binary.entity.player": {"en": "player start", "zh-CN": "玩家起点"},
    "binary.entity.outer_shell": {"en": "outer shell", "zh-CN": "外墙轮廓"},
    "binary.entity.default": {"en": "protected elements", "zh-CN": "受保护对象"},
    "binary.protected_refusal_with_request": {
        "en": (
            'Your request “{snippet}” asks to change {entities}, which is beyond the current '
            "editing permissions. Only water and internal walls may be edited; player, boxes, "
            "targets, and the connected outer shell remain fixed, and the saved Stage and prior "
            "discussion are preserved."
        ),
        "zh-CN": (
            "你提到的“{snippet}”涉及修改{entities}，超出了当前 AI 修改权限。"
            "目前只能调整水域和内部墙体；玩家、箱子、目标点和连接外壳都会保持不变，"
            "已保存的 Stage 与此前讨论也会保留。"
        ),
    },
    "binary.protected_refusal_generic": {
        "en": (
            "This request asks to change {entities}, which is beyond the current editing "
            "permissions. Only water and internal walls may be edited; player, boxes, targets, "
            "and the connected outer shell remain fixed, and the saved Stage and prior "
            "discussion are preserved."
        ),
        "zh-CN": (
            "这项请求涉及修改{entities}，超出了当前 AI 修改权限。"
            "目前只能调整水域和内部墙体；玩家、箱子、目标点和连接外壳都会保持不变，"
            "已保存的 Stage 与此前讨论也会保留。"
        ),
    },
    "binary.selected_prefix": {
        "en": "Selected {letter}: {label}",
        "zh-CN": "选择 {letter}: {label}",
    },
    "binary.verification_summary": {
        "en": (
            "Permissions, selected actions and solvability verified; "
            "experience still needs playtesting."
        ),
        "zh-CN": "通过修改权限、动作兑现及可解性检查；体验仍需试玩。",
    },
    "binary.proposal_cancelled": {
        "en": (
            "Understood. I ended the current proposal topic and unlocked proposal entry. "
            "The current Stage is unchanged."
        ),
        "zh-CN": "明白，我已结束当前方案主题并解除方案入口锁定。当前 Stage 没有发生变化。",
    },
    "binary.stale_topic": {
        "en": "This topic is no longer active.",
        "zh-CN": "当前方案主题已失效。",
    },
    "binary.already_answered": {
        "en": "This question was already answered.",
        "zh-CN": "该问题已经作答。",
    },
    "binary.invalid_option": {
        "en": "This option does not belong to the question.",
        "zh-CN": "该选项不属于当前问题。",
    },
    # ------------------------------------------------------------------
    # Requirement Tradeoffs & Evaluation Notices
    # ------------------------------------------------------------------
    "requirements.tradeoff.fulfilled": {
        "en": "Verified in this candidate: {items}.",
        "zh-CN": "这版已核验的结果是：{items}。",
    },
    "requirements.tradeoff.partial": {
        "en": "Partly achieved: {items}.",
        "zh-CN": "以下目标只实现了一部分：{items}。",
    },
    "requirements.tradeoff.deferred": {
        "en": (
            "Not verified as achieved yet: {items}. I retained this validated candidate; "
            "accepting it does not withdraw those goals."
        ),
        "zh-CN": (
            "以下目标暂未核实实现：{items}。我先保留这份通过验证的候选；"
            "这些目标仍保留，接受这版不表示放弃它们。"
        ),
    },
    "requirements.tradeoff.box_alternations_note": {
        "en": (
            "Alternation counts describe one verified solution, not a proof of mandatory "
            "switching in every solution."
        ),
        "zh-CN": "切换次数来自已核对的一条求解路线，不能据此断言所有解法都必须这样切换。",
    },
    "requirements.tradeoff.locked": {
        "en": "Rechecked fixed requirements: {items}.",
        "zh-CN": "以下固定要求已经复验：{items}。",
    },
    "requirements.tradeoff.automatic_binding": {
        "en": (
            "For the unspecified focus, this proposal uses {labels} as a correctable "
            "proposal assumption."
        ),
        "zh-CN": "你没有指定具体对象的部分，这版先围绕 {labels} 尝试；这是本方案的选择。",
    },
    "requirements.conflict.permission": {
        "en": "An explicit requirement conflicts with fixed entities or editing permissions.",
        "zh-CN": "这部分明确要求与固定实体或允许修改的范围冲突，我不能执行。",
    },
    "requirements.conflict.semantic": {
        "en": "The candidates failed fixed requirements or direction checks.",
        "zh-CN": "本次候选没有通过固定要求或目标方向检查。",
    },
    "requirements.conflict.unsolvable": {
        "en": "The candidates failed solvability validation.",
        "zh-CN": "本次候选没有通过可解性验证。",
    },
    "requirements.conflict.contract": {
        "en": "The generated operation plans conflict with the current map or execution rules.",
        "zh-CN": "生成的操作计划与当前地图或执行规则不一致。",
    },
    "requirements.conflict.exact_transition": {
        "en": "No valid candidate matched the specified tile changes.",
        "zh-CN": "本次没有找到满足指定格子变化的有效候选。",
    },
    "requirements.conflict.default": {
        "en": "The bounded search found no change passing every check.",
        "zh-CN": "在本次有限搜索中，没有找到通过全部检查的修改。",
    },
    "requirements.failure_wrapper": {
        "en": (
            "{reason} The current map is unchanged and no purple card was created. "
            "The goals and answers are retained; this attempt does not prove the goals impossible."
        ),
        "zh-CN": (
            "我尝试了这次修改，但{reason} 当前地图没有改变，也没有生成紫色方案卡。"
            "你的明确目标和本主题的回答仍保留；这次失败不能证明这些目标永远无法实现。"
        ),
    },
    # ------------------------------------------------------------------
    # DG Guide Fallbacks & Prefixes
    # ------------------------------------------------------------------
    "dg.reflection_prefix": {
        "en": "My read is that ",
        "zh-CN": "我的理解是：",
    },
    "dg.rationale_prefix": {
        "en": "I read this as: ",
        "zh-CN": "我这样理解：",
    },
    "dg.choice.quick_start": {"en": "a quick start", "zh-CN": "快速直接开局"},
    "dg.choice.observe_then_decide": {
        "en": "a look at boxes, goals, and paths",
        "zh-CN": "先观察箱子、目标和通道",
    },
    "dg.choice.plan_ahead": {
        "en": "time to look over the map and plan",
        "zh-CN": "先整体观察和规划",
    },
    "dg.choice.no_set_start": {"en": "no set start style", "zh-CN": "未指定开局风格"},
    "dg.choice.easy_to_adjust": {
        "en": "pushes that mostly work on their own",
        "zh-CN": "彼此较独立、容易调整的推动",
    },
    "dg.choice.consider_order": {
        "en": "some pushes linked by place or order",
        "zh-CN": "部分受位置或顺序影响的推动",
    },
    "dg.choice.connected_pushes": {
        "en": "several pushes that need earlier pushes",
        "zh-CN": "多步相互依赖的推动",
    },
    "dg.choice.no_set_push": {"en": "no set push links", "zh-CN": "未指定推动依赖"},
    "dg.choice.focused_area": {
        "en": "key spots together in one area",
        "zh-CN": "集中在同一主要区域的关键位置",
    },
    "dg.choice.connected_areas": {
        "en": "key spots in a few linked areas",
        "zh-CN": "分布在几个相连区域的关键位置",
    },
    "dg.choice.wide_area": {
        "en": "key spots spread across a wider area",
        "zh-CN": "分布在更开阔空间的关键位置",
    },
    "dg.choice.no_set_space": {"en": "no set spot layout", "zh-CN": "未指定空间分布"},
    "dg.choice.short_routes": {
        "en": "short paths between nearby spots",
        "zh-CN": "连接相邻决策的短路径",
    },
    "dg.choice.occasional_detours": {
        "en": "mostly direct paths with a few side trips",
        "zh-CN": "主线较直接、偶有绕行的路径",
    },
    "dg.choice.long_routes": {
        "en": "long paths to explore or come back",
        "zh-CN": "较长的探索或折返路径",
    },
    "dg.choice.no_set_route": {"en": "no set path length", "zh-CN": "未指定路径长度"},
    "dg.fallback.summary": {
        "en": (
            "Got it, I'd try {first_move} with {push_planning} for now. "
            "How about {space} and {route}?"
        ),
        "zh-CN": (
            "明白，我先试着把{first_move}和{push_planning}结合起来；"
            "空间上用{space}、配合{route}怎么样？"
        ),
    },
    "dg.fallback.difficulty_random": {
        "en": (
            "I do not see a directional planning preference in the two answers, "
            "so I would leave Difficulty Random for Confirm."
        ),
        "zh-CN": "这两个回答中没有明确的规划复杂度偏好，因此建议在确认时保留随机难度。",
    },
    "dg.fallback.difficulty_rationale": {
        "en": (
            "I connect the first-move choice to {first_move} and the push decisions to "
            "{push_planning}, which supports {difficulty} planning complexity."
        ),
        "zh-CN": (
            "我将首步选择（{first_move}）与推动安排（{push_planning}）对应起来，"
            "更适合 {difficulty} 规划复杂度。"
        ),
    },
    "dg.fallback.layout_random": {
        "en": (
            "I do not see a directional spatial preference in the two answers, "
            "so I would leave Layout Random for Confirm."
        ),
        "zh-CN": "这两个回答中没有明确的空间布局偏好，因此建议在确认时保留随机布局。",
    },
    "dg.fallback.layout_rationale": {
        "en": (
            "I connect the space choice to {space} and the route choice to {route}, "
            "which supports a {layout} layout."
        ),
        "zh-CN": "我将空间选择（{space}）与路径节奏（{route}）对应起来，更适合 {layout} 布局。",
    },
    # ------------------------------------------------------------------
    # Proposal / Intent / Challenge / Map Grounding Notices
    # ------------------------------------------------------------------
    "proposal.verified_ready": {
        "en": (
            "I turned the direction we confirmed into a reviewable candidate. "
            "The purple card is bound to verified tile changes; "
            "the current map has not changed yet."
        ),
        "zh-CN": (
            "我已经把刚才确认的方向落实成一份可审查候选。"
            "这张紫色方案卡绑定的是已经验证过的真实格子变化；地图目前还没有改变。"
        ),
    },
    "proposal.organized_review": {
        "en": (
            "I have organized this map proposal and checked its real before/after tile changes. "
            "It is still yours to review; I would first look at whether the highlighted cells "
            "really answer the direction we discussed before deciding whether to accept it."
        ),
        "zh-CN": (
            "我把这次地图提案整理好了，也逐格核对了前后的真实变化。它现在仍是一份等你审查的方案；"
            "我更想让你先看高亮位置是否真的回应了刚才的方向，再决定要不要接受。"
        ),
    },
    "proposal.retry_exhausted": {
        "en": (
            "I noticed that you tried generating the revision again. I am sorry, but this request "
            "appears to be beyond what I can reliably produce. Please make the change yourself "
            "using the direction we discussed."
        ),
        "zh-CN": (
            "我注意到你尝试了一次重新生成，非常抱歉，由于我的能力不足，"
            "我可能无法帮助你进行这个修改，请你根据我们商量好的方案进行自主修改。"
        ),
    },
    "proposal.upstream_timeout": {
        "en": (
            "I tried to generate a reviewable proposal, but the upstream model did not respond "
            "within the proposal time budget. This is not a map or contract-validation result; "
            "the map is unchanged and I will not present an incomplete generation as a proposal."
        ),
        "zh-CN": (
            "我已经按刚才确认的方向尝试生成待审查方案，但上游模型在本次方案生成的时间预算内"
            "未返回（请求超时）。这不是地图或合同验证失败；地图没有改变，"
            "我不会把不完整的生成结果当成方案。"
        ),
    },
    "proposal.upstream_truncated_timeout": {
        "en": (
            "I tried to generate a reviewable proposal. The first RevisionPlan response reached "
            "its output limit, and the corrective retry then timed out, so no complete verifiable "
            "proposal was produced. The map is unchanged and I will not present the truncated "
            "result as a proposal."
        ),
        "zh-CN": (
            "我已经按刚才确认的方向尝试生成待审查方案。首次 RevisionPlan 回复达到输出上限，"
            "随后修复重试超时，因此没有得到完整可验证的方案。地图没有改变，我不会把截断结果当成方案。"
        ),
    },
    "proposal.upstream_rejected": {
        "en": (
            "I tried to generate a reviewable proposal, but the upstream proposal endpoint "
            "rejected the request. The map is unchanged; I will not present an incomplete "
            "generation as a proposal."
        ),
        "zh-CN": (
            "我已经按刚才确认的方向尝试生成待审查方案，但上游方案接口拒绝了这次请求。"
            "地图没有改变；我不会把不完整的生成结果当成方案。"
        ),
    },
    "proposal.manual_edit_cue": {
        "en": (
            "This proposal stays pending while you review it. If you would rather tune it "
            "yourself, reject it first and continue around the highlighted area in the right-hand "
            "editor; I will use your actual edit as our next shared evidence."
        ),
        "zh-CN": (
            "这份提案会先保持待审查状态。如果你更想亲手微调，可以先拒绝它，再在右侧编辑器沿着"
            "高亮区域继续改；我会把你的实际修改当作下一轮共同判断的依据。"
        ),
    },
    "proposal.alternative_clarification": {
        "en": (
            "To produce a distinct verified alternative proposal, please clarify which local route "
            "relationship you want to change instead."
        ),
        "zh-CN": (
            "为了给出与原方案不同的可执行备选改法，"
            "请先说明你希望优先保留或调整哪一段路径关系。"
        ),
    },
    "proposal.execution_binding_clarification": {
        "en": (
            "Please identify the area or entity to adjust and state each cell's current and "
            "desired tile. I will check it against the current saved Stage before proposing "
            "a direction."
        ),
        "zh-CN": (
            "请明确指出要调整的区域或实体，以及每个格子修改前后的状态；"
            "我会先按当前保存的 Stage 核对，再继续给出一个方向。"
        ),
    },
    "proposal.no_changes_detected": {
        "en": "No tile changes detected.",
        "zh-CN": "未检测到实际格子改动。",
    },
    "proposal.verified_changes_short": {
        "en": "Verified tile changes ({count} total): {details}.",
        "zh-CN": "已核对实际改动（共{count}格）：{details}。",
    },
    "proposal.verified_changes_summary": {
        "en": (
            "Verified tile changes ({count} total): {details}. "
            "See the highlighted map cells for every location."
        ),
        "zh-CN": "已核对实际改动（共{count}格）：{details}。具体位置以地图高亮为准。",
    },
    "proposal.solver_delta_both": {
        "en": (
            "Verified solvable candidate: minimum moves {before_steps} -> {after_steps}, "
            "pushes {before_pushes} -> {after_pushes}."
        ),
        "zh-CN": (
            "已验证可解候选：最少步数 {before_steps} → {after_steps}，"
            "推动 {before_pushes} → {after_pushes}。"
        ),
    },
    "proposal.solver_delta_steps": {
        "en": "Verified solvable candidate: minimum moves {before_steps} -> {after_steps}.",
        "zh-CN": "已验证可解候选：最少步数 {before_steps} → {after_steps}。",
    },
    "proposal.solver_delta_after_both": {
        "en": (
            "Verified solvable candidate: {after_steps} minimum moves, "
            "{after_pushes} pushes."
        ),
        "zh-CN": "已验证可解候选：最少步数 {after_steps}，推动 {after_pushes}。",
    },
    "proposal.solver_delta_after_steps": {
        "en": "Verified solvable candidate: {after_steps} minimum moves.",
        "zh-CN": "已验证可解候选：最少步数 {after_steps}。",
    },
    "intent.conflict_explanation": {
        "en": (
            "I read the new core direction as “{new_label}”, while the most recently confirmed "
            "direct direction is “{old_label}”. Both govern the same {attribute} of {subject} "
            "but require opposite changes, so they cannot both guide this decision. "
            "I will not choose between them; please keep one of the two cards below."
        ),
        "zh-CN": (
            "我从你现在的表达中读到的核心方向是“{new_label}”，而最近确认意图对应的直接方向是“{old_label}”。"
            "两者都在决定{subject}的{attribute}，却要求向相反方向调整，因此不能同时指导这项设计决定。"
            "我不会替你选择，请在下面两张卡中决定保留哪一个。"
        ),
    },
    "intent.evidence.confirm": {
        "en": "You confirmed this inclination.",
        "zh-CN": "你确认了这条倾向。",
    },
    "intent.evidence.revise": {
        "en": "You revised and confirmed this inclination.",
        "zh-CN": "你修订并确认了这条倾向。",
    },
    "intent.evidence.reject": {
        "en": "You rejected this hypothesis.",
        "zh-CN": "你否定了这条推测。",
    },
    "challenge.ask_reason": {
        "en": "Please explain why you challenge this proposal.",
        "zh-CN": "请说明你质疑的理由。",
    },
    "challenge.primary_hypothesis": {
        "en": (
            "The challenge may be asking whether {change} is sufficient to {objective}, "
            "rather than merely changing appearance or walking distance."
        ),
        "zh-CN": (
            "这项质疑可能是在判断：{change}是否足以实现{objective}，"
            "而不只是改变外观或移动距离。"
        ),
    },
    "challenge.secondary_hypothesis": {
        "en": (
            "Another possibility is that the change weakens the promised preservation of "
            "{preserved}."
        ),
        "zh-CN": "另一种可能是，这项改动会削弱方案承诺保留的{preserved}。",
    },
    "challenge.scope_choice_legacy": {
        "en": (
            "I understand that you want a different scope of changes. Please choose the original "
            "proposal or a new plan; the map has not changed."
        ),
        "zh-CN": (
            "我理解你提出了不同的修改范围。现在先确认采用原方案，"
            "还是按你的新方向重新规划；地图还没有改变。"
        ),
    },
    "map.claim.entity_identity_unknown": {
        "en": (
            "The current Stage confirms an entity at the claimed cell, but cannot reliably map it "
            "to historical {entity} identity. Please describe the entity by its current row and "
            "column so I can continue neutrally."
        ),
        "zh-CN": (
            "当前 Stage 只能确认相关格子上的实体，暂时无法把它与历史 {entity} 身份可靠对应。"
            "请用当前地图的行列位置描述它，我会按这个中性位置继续分析。"
        ),
    },
    "map.claim.entity_not_bound": {
        "en": (
            "The current Stage has no verifiable binding for {entity}. "
            "Please describe the entity by its current row and column."
        ),
        "zh-CN": (
            "当前 Stage 中没有可验证的 {entity} 身份。"
            "请改用当前地图的行列位置描述要讨论的实体。"
        ),
    },
    "map.claim.entity_mismatch": {
        "en": (
            "The position you gave for {entity} does not match the current Stage. "
            "The saved map places it at row {row}, column {column}; I will use that position "
            "for the analysis."
        ),
        "zh-CN": (
            "你提到的 {entity} 位置与当前 Stage 不一致。"
            "当前保存地图中 {entity} 位于第 {row} 行第 {column} 列，我会以这个位置继续分析。"
        ),
    },
    "map.claim.tile_mismatch": {
        "en": (
            "The tile you gave at row {row}, column {column} does not match the current Stage; "
            "the saved map has {actual_tile} there. I will use the saved tile for the analysis."
        ),
        "zh-CN": (
            "你提到的第 {row} 行第 {column} 列与当前 Stage 不一致；"
            "当前保存地图中这里是{actual_tile}，我会以这个格子状态继续分析。"
        ),
    },
    "fallback.stage1_orientation": {
        "en": (
            "Share a reaction or try playing; you can also make local edits in the right panel. "
            "I help think through small, reviewable changes; you steer any broad rebuild, with me "
            "as a sounding board."
        ),
        "zh-CN": (
            "可以先聊聊或试玩，再一起商量；你也可以在右侧局部编辑。"
            "我能帮你梳理思路、提出可审查的小改动；大改由你掌舵，我当参谋。"
        ),
    },
}

_TILE_KEYS = {
    " ": "tile.void",
    "#": "tile.wall",
    ".": "tile.floor",
    "@": "tile.water",
    "p": "tile.player",
    "P": "tile.player",
    "s": "tile.box",
    "B": "tile.box",
    "t": "tile.target",
    "T": "tile.target",
}

_PROTECTED_ENTITY_KEYS = {
    "targets": "binary.entity.targets",
    "boxes": "binary.entity.boxes",
    "player": "binary.entity.player",
    "outer_shell": "binary.entity.outer_shell",
}


def get_message(key: str, language: str | None = None, **params: Any) -> str:
    """Resolve and format a localized message by key."""
    locale = normalize_language(language)
    entry = MESSAGES.get(key)
    if entry is None:
        return key
    template = entry.get(locale) or entry.get(DEFAULT_LANGUAGE) or key
    if not params:
        return template
    try:
        return template.format(**params)
    except (KeyError, ValueError):
        return template


def get_tile_label(tile: str, language: str | None = None, *, with_symbol: bool = False) -> str:
    """Return localized display label for a Sokoban tile character."""
    locale = normalize_language(language)
    key = _TILE_KEYS.get(tile)
    name = get_message(key, locale) if key else str(tile)
    if not with_symbol:
        return name
    return f"{name}（{tile}）" if locale == "zh-CN" else f"{name} ({tile})"


def format_protected_refusal(
    protected_entities: Sequence[str] | None,
    initial_request: str | None,
    language: str | None,
) -> str:
    """Render a participant-facing permission explanation for protected map entities."""
    locale = normalize_language(language)
    labels = list(
        dict.fromkeys(
            get_message(_PROTECTED_ENTITY_KEYS[item], locale)
            for item in (protected_entities or ())
            if item in _PROTECTED_ENTITY_KEYS
        )
    )
    sep = "、" if locale == "zh-CN" else ", "
    entity_text = sep.join(labels) or get_message("binary.entity.default", locale)
    snippet = " ".join(str(initial_request or "").split())[:120]
    if snippet:
        return get_message(
            "binary.protected_refusal_with_request",
            locale,
            snippet=snippet,
            entities=entity_text,
        )
    return get_message(
        "binary.protected_refusal_generic",
        locale,
        entities=entity_text,
    )


def format_tradeoff_notice(
    *,
    fulfilled: Sequence[str],
    partial: Sequence[str],
    deferred: Sequence[str],
    has_box_alternations: bool,
    locked: Sequence[str],
    binding_labels: Sequence[str],
    language: str | None,
) -> str:
    """Render the bilingual deterministic requirement tradeoff notice."""
    locale = normalize_language(language)
    sep = "；" if locale == "zh-CN" else "; "
    parts: list[str] = []
    if fulfilled:
        parts.append(
            get_message("requirements.tradeoff.fulfilled", locale, items=sep.join(fulfilled))
        )
    if partial:
        parts.append(
            get_message("requirements.tradeoff.partial", locale, items=sep.join(partial))
        )
    if deferred:
        parts.append(
            get_message("requirements.tradeoff.deferred", locale, items=sep.join(deferred))
        )
    if has_box_alternations:
        parts.append(get_message("requirements.tradeoff.box_alternations_note", locale))
    if locked:
        parts.append(get_message("requirements.tradeoff.locked", locale, items=sep.join(locked)))
    if binding_labels:
        parts.append(
            get_message(
                "requirements.tradeoff.automatic_binding",
                locale,
                labels=", ".join(binding_labels),
            )
        )
    return " ".join(part for part in parts if part)


def _extract_solver_metrics(solver: Any) -> tuple[int | None, int | None]:
    if solver is None:
        return None, None
    if isinstance(solver, Mapping):
        steps = solver.get("solutionSteps")
        pushes = solver.get("solutionPushes")
    else:
        steps = getattr(solver, "solution_steps", None)
        pushes = getattr(solver, "solution_pushes", None)
    valid_steps = steps if isinstance(steps, int) and not isinstance(steps, bool) else None
    valid_pushes = pushes if isinstance(pushes, int) and not isinstance(pushes, bool) else None
    return valid_steps, valid_pushes


def format_solver_delta_notice(
    before_solver: Mapping[str, Any] | None,
    after_solver: Any,
    language: str | None,
) -> str:
    """Format a deterministic bilingual solver delta notice from dict or SolveResult."""
    locale = normalize_language(language)
    before_steps, before_pushes = _extract_solver_metrics(before_solver)
    after_steps, after_pushes = _extract_solver_metrics(after_solver)
    if after_steps is None:
        return ""
    if before_steps is not None:
        if before_pushes is not None and after_pushes is not None:
            return get_message(
                "proposal.solver_delta_both",
                locale,
                before_steps=before_steps,
                after_steps=after_steps,
                before_pushes=before_pushes,
                after_pushes=after_pushes,
            )
        return get_message(
            "proposal.solver_delta_steps",
            locale,
            before_steps=before_steps,
            after_steps=after_steps,
        )
    if after_pushes is not None:
        return get_message(
            "proposal.solver_delta_after_both",
            locale,
            after_steps=after_steps,
            after_pushes=after_pushes,
        )
    return get_message("proposal.solver_delta_after_steps", locale, after_steps=after_steps)


def format_dg_fallback_summary(
    *,
    first_move_key: str,
    push_planning_key: str,
    space_key: str,
    route_key: str,
    difficulty: str,
    layout: str,
    reason: str = "",
    language: str | None = "en",
) -> dict[str, str]:
    """Build the deterministic DG fallback summary payload."""
    locale = normalize_language(language)
    first_move = get_message(f"dg.choice.{first_move_key}", locale)
    push_planning = get_message(f"dg.choice.{push_planning_key}", locale)
    space = get_message(f"dg.choice.{space_key}", locale)
    route = get_message(f"dg.choice.{route_key}", locale)

    if difficulty == "Random":
        difficulty_rationale = get_message("dg.fallback.difficulty_random", locale)
    else:
        difficulty_rationale = get_message(
            "dg.fallback.difficulty_rationale",
            locale,
            first_move=first_move,
            push_planning=push_planning,
            difficulty=difficulty,
        )

    if layout == "Random":
        layout_rationale = get_message("dg.fallback.layout_random", locale)
    else:
        layout_rationale = get_message(
            "dg.fallback.layout_rationale",
            locale,
            space=space,
            route=route,
            layout=layout,
        )

    summary = get_message(
        "dg.fallback.summary",
        locale,
        first_move=first_move,
        push_planning=push_planning,
        space=space,
        route=route,
    )[:480]

    return {
        "summary": summary,
        "recommendedDifficulty": difficulty,
        "difficultyRationale": difficulty_rationale,
        "recommendedLayout": layout,
        "layoutRationale": layout_rationale,
        "source": "deterministic_fallback",
        "fallbackReason": reason,
    }


def format_verified_diff_summary(
    changes: Sequence[Mapping[str, Any]],
    language: str | None = "en",
) -> str:
    """Format a deterministic human-readable diff summary for tile changes."""
    locale = normalize_language(language)
    if not changes:
        return get_message("proposal.no_changes_detected", locale)

    if len(changes) <= 8:
        if locale == "zh-CN":
            details = "；".join(
                f"第{int(c['y']) + 1}行第{int(c['x']) + 1}列："
                f"{get_tile_label(str(c['before']), locale)}→"
                f"{get_tile_label(str(c['after']), locale)}"
                for c in changes
            )
        else:
            details = "; ".join(
                f"row {int(c['y']) + 1}, column {int(c['x']) + 1}: "
                f"{get_tile_label(str(c['before']), locale)} → "
                f"{get_tile_label(str(c['after']), locale)}"
                for c in changes
            )
        return get_message(
            "proposal.verified_changes_short",
            locale,
            count=len(changes),
            details=details,
        )

    transition_counts: dict[tuple[str, str], int] = {}
    for change in changes:
        pair = (str(change["before"]), str(change["after"]))
        transition_counts[pair] = transition_counts.get(pair, 0) + 1

    if locale == "zh-CN":
        details = "；".join(
            f"{get_tile_label(before, locale)}→{get_tile_label(after, locale)} {count}格"
            for (before, after), count in transition_counts.items()
        )
    else:
        details = "; ".join(
            f"{get_tile_label(before, locale)} → {get_tile_label(after, locale)}: {count}"
            for (before, after), count in transition_counts.items()
        )
    return get_message(
        "proposal.verified_changes_summary",
        locale,
        count=len(changes),
        details=details,
    )


def format_map_claim_correction(
    conflicts: Sequence[Mapping[str, Any]] | None,
    language: str | None = "en",
) -> str:
    """Format a deterministic correction for the first contradicted map claim."""
    if not conflicts:
        return ""
    locale = normalize_language(language)
    first = conflicts[0]
    if first.get("kind") == "entity":
        entity = str(first.get("entity") or "")
        reason = first.get("reason")
        expected = first.get("expected") if isinstance(first.get("expected"), Mapping) else None
        if reason == "entity_identity_unknown":
            return get_message("map.claim.entity_identity_unknown", locale, entity=entity)
        if not expected:
            return get_message("map.claim.entity_not_bound", locale, entity=entity)
        return get_message(
            "map.claim.entity_mismatch",
            locale,
            entity=entity,
            row=expected.get("row"),
            column=expected.get("column"),
        )

    actual_tile = str(first.get("actualTile") or "")
    fallback_tile = "当前格子" if locale == "zh-CN" else "a different tile"
    tile_name = (
        get_tile_label(actual_tile, locale)
        if actual_tile in _TILE_KEYS
        else fallback_tile
    )
    return get_message(
        "map.claim.tile_mismatch",
        locale,
        row=first.get("row"),
        column=first.get("column"),
        actual_tile=tile_name,
    )
