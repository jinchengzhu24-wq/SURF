export const REPLY_PIPELINE_VERSION = "ordinary-harness-2026-10-13-v2";
export const REPLY_DIAGNOSTICS_KEY = "sokobanReplyDiagnosticsV2";

export function safeReplyDiagnostic(value) {
    const allowed = ["stage", "status", "code", "requestId", "messageKey", "versionId", "assistantTurnId",
        "pipelineVersion", "frontendVersion", "receivedCard", "displayCard", "renderedCard", "turnPresent", "httpStatus", "at"];
    const result = {};
    for (const key of allowed) {
        const item = value?.[key];
        if (typeof item === "boolean" || (typeof item === "number" && Number.isFinite(item))) result[key] = item;
        else if (typeof item === "string" && item.length <= 128 && /^[a-zA-Z0-9_:. /-]*$/.test(item)) result[key] = item;
    }
    return result;
}

export const MAX_MESSAGE_LENGTH = 2000;
export const LLM_REQUEST_TIMEOUT_MS = 120000;
export const MESSAGE_REQUEST_TIMEOUT_MS = 125000;
export const PROPOSAL_DISPLAY_LIMIT_SECONDS = 120;
export const LLM_PRIMARY_WAIT_SECONDS = 70;
export const SESSION_STORAGE_KEY = "sokobanCoCreationSession";
export const UNITY_PLAY_BRIDGE_TIMEOUT_MS = 1500;
export const DRAFT_REGENERATION_POLL_MS = 1000;
export const DRAFT_REGENERATION_API_TIMEOUT_MS = 8000;
export const DRAFT_REGENERATION_POLL_TIMEOUT_MS = 5000;
export const DRAFT_REGENERATION_PROTOCOL_VERSION = 5;
export const COMPONENT_RECOVERY_TIMEOUT_MS = 12000;
export const TILE_ORDER = [".", "#", "@", "p", "s", "t", " "];
export const DISCUSSION_FOCUS_LABEL = "LET'S DISCUSS / 一起聊聊";
export const GUIDANCE_CUE_LABELS = {
    en: {
        manual_edit: "MANUAL EDIT / 手动编辑",
        question: "LET'S DISCUSS / 一起聊聊",
        revision: "REVISION / 修改建议",
        intent: "TENTATIVE INTENT / 暂定意图",
        warning: "WARNING / 风险提示",
        tradeoff: "WARNING / 风险提示"
    },
    "zh-CN": {
        manual_edit: "MANUAL EDIT / 手动编辑",
        question: "LET'S DISCUSS / 一起聊聊",
        revision: "REVISION / 修改建议",
        intent: "TENTATIVE INTENT / 暂定意图",
        warning: "WARNING / 风险提示",
        tradeoff: "WARNING / 风险提示"
    }
};

GUIDANCE_CUE_LABELS.en.clarification = "NEEDS CLARIFICATION / \u9700\u8981\u6f84\u6e05";
GUIDANCE_CUE_LABELS["zh-CN"].clarification = "\u9700\u8981\u6f84\u6e05";

export const translations = {
    en: {
        title: "Sokoban Co-Creation Lab",
        subtitle: "Persistent level design workspace",
        loading: "Loading session...",
        ready: "Session synchronized",
        retry: "Retry",
        neutralBrief: "Neutral design brief",
        openFromUnity: "Create a first level in Unity, then continue here.",
        landingBody: "The lab keeps every accepted Stage, conversation and play attempt together. No predefined design goal is assigned.",
        startDemo: "Start a sample session",
        sessionSetup: "Session setup",
        formalLandingTitle: "Stage 1 is synced. Choose a language to begin co-creation.",
        formalLandingBody: "The lab keeps every accepted Stage, complete conversation, and play attempt together. No predefined design goal is assigned.",
        sessionLanguage: "Session language",
        formalLanguageLock: "This session will use the selected language and cannot be changed after entry. The 20-minute co-creation timer starts when you enter.",
        demoLanguageLock: "Your selection cannot be changed after the session is created.",
        enterSession: "Enter co-creation session",
        demoGenerationStatus: "Creating a sample map with the algorithm…",
        versionHistory: "Version history",
        stages: "Stages",
        conversation: "Co-creation conversation",
        discuss: "Discuss and refine this Stage",
        viewingHistory: "You are viewing this historical Stage and its own conversation. Return to the current Stage to continue chatting.",
        returnCurrent: "Return to current",
        assessmentPending: "Preparing the first Stage opening",
        assessmentPendingBody: "The assistant will discuss what stands out in the verified current map.",
        noStageConversation: "No conversation has been recorded for this Stage.",
        noStageConversationBody: "Each Stage keeps only the discussion attached to that saved version.",
        thinking: "Assistant is considering the current Stage...",
        chatWaitingPrimary: "Waiting for the primary assistant",
        chatWaitingFallback: "The assistant is taking longer than usual; retrying once",
        chatWaitingProposal: "A proposal may still be compiling and validating",
        chatWaitingServerResult: "Waiting for the server's final result",
        chatRetryPending: "The previous message did not finish. Retry it without creating a duplicate.",
        messageLabel: "Message the level design assistant",
        messagePlaceholder: "Explain what you want to change or ask about the level...",
        requestProposal: "Proposal",
        requestProposalHint: "Toggle proposal mode for the next message",
        send: "Send",
        sending: "Sending...",
        sendHint: "Enter to send · Shift+Enter for a new line",
        currentLevel: "Current level",
        playStage: "Play this Stage",
        playOpenedInUnity: "The selected Stage is opening in the Unity view.",
        playUnityUnavailable: "The embedded Unity view is unavailable. The co-creation workspace remains open; retry from the original game page.",
        saveStage: "Save as new Stage",
        discard: "Discard draft",
        continueFromStage: "Continue from this Stage",
        playEvidence: "Play evidence",
        noPlayEvidence: "No optional play attempt has been recorded for this Stage.",
        finalizeStage: "Confirm final Stage",
        finalizeHint: "Confirmation freezes editing. Your design intention is collected next.",
        intentionTitle: "Designer intention",
        intentionQuestion: "What would you like to say to the other player?",
        completeSession: "Complete session",
        sessionComplete: "Session complete",
        sessionCompleteBody: "The final Stage, intention, conversation and play evidence are safely recorded. Continue in the original Unity game to enter Challenge Waiting.",
        returnUnity: "Continue in Unity",
        returnUnityUnavailable: "The original Unity tab is no longer available. Return to that tab manually; do not open a new game page, because the match is stored in the original game instance.",
        returnUnityCloseBlocked: "Unity is ready in the original tab. If this lab tab did not close automatically, switch to the Unity tab to continue.",
        finalConfirmation: "Final confirmation",
        confirmQuestion: "Confirm this as your final Stage?",
        confirmBody: "Editing and chat will be locked after confirmation. You will then report your design intention.",
        cancel: "Cancel",
        confirmFinal: "Confirm final Stage",
        stage: "Stage",
        current: "Current",
        initial: "Initial",
        human_edit: "Designer edit",
        llm_accepted: "Accepted AI proposal",
        restored: "Restored version",
        verified: "Verified solvable",
        validationFailed: "Validation failed",
        steps: "steps",
        pushes: "pushes",
        editMode: "Editing the current Stage. Save explicitly to create a version.",
        readOnlyMode: "Historical Stage: read-only until restored as a new Stage.",
        lockedMode: "The final Stage is locked.",
        unsaved: "Unsaved draft",
        proposal: "Map proposal",
        suggestedDirection: "Assistant design direction",
        draftSuggestedRevision: "Ask the assistant to draft this",
        challengeRevision: "Challenge this plan",
        alternativeRevision: "Regenerate proposal",
        staleRevisionCard: "Only the latest revision card can be acted on.",
        proposalStale: "This proposal is based on an older Stage and is no longer active.",
        proposalAlreadySatisfied: "The current map already contains this change.",
        proposalPreconditionFailed: "The current map no longer matches this proposal's prerequisite.",
        proposalUnbound: "This historical proposal is readable but has no reliable execution binding.",
        proposalDisagreementActive: "Resolve the active disagreement before using this proposal.",
        proposalAlreadyExecuted: "This proposal has already been used once and is no longer active.",
        proposalSearchExhausted: "No verified map proposal could be produced for this direction. The current map was not changed.",
        discussionUser: "User direction",
        discussionAi: "AI view",
        discussionCore: "Core disagreement",
        discussionNext: "Next question",
        proposalConsent: "Please create a reviewable map proposal for this direction: {summary}",
        proposalValid: "Deterministic validation passed. Review the changed tiles before deciding.",
        accept: "Accept as new Stage",
        reject: "Reject",
        changedTiles: "changed tiles",
        objectiveEvidence: "Deterministic evidence",
        aiOpinion: "AI assessment",
        attemptCompleted: "Completed",
        attemptAbandoned: "Returned early",
        attemptInterrupted: "Interrupted",
        attemptStarted: "In progress",
        moves: "moves",
        restarts: "restarts",
        seconds: "seconds",
        partial_completion: "Partial-level completion",
        description_generation: "Description generation",
        algorithm_demo: "Algorithm demo",
        floor: "Floor",
        wall: "Wall",
        water: "Water",
        player: "Player",
        box: "Box",
        target: "Target",
        erase: "Empty",
        errorGeneric: "The request could not be completed.",
        errorNoSession: "Open this lab from Unity or start a sample session.",
        errorDirtyPlay: "Save or discard the map draft before playing.",
        errorPendingPlay: "Accept or reject the pending map proposal before playing.",
        errorIntentRequired: "Please describe your design intention before completing the session.",
        error_INVALID_LEVEL: "The map format is invalid.",
        error_INVALID_HEIGHT: "The map must contain exactly 10 rows.",
        error_INVALID_WIDTH: "Every map row must contain exactly 12 tiles.",
        error_UNKNOWN_TILE: "The map contains an unsupported tile symbol.",
        error_INVALID_PLAYER_COUNT: "The map must contain exactly one player.",
        error_INVALID_BOX_COUNT: "The map must contain one or two boxes.",
        error_MISMATCHED_TARGET_COUNT: "The number of targets must match the number of boxes.",
        error_UNSOLVABLE_LEVEL: "The deterministic solver could not find a solution for this map.",
        error_SEARCH_BUDGET_EXCEEDED: "Map validation reached its search budget before it could finish.",
        error_OPEN_OUTER_WALL: "The outer boundary must be closed with wall (#) tiles; water cannot replace a wall.",
        error_CLIENT_TIMEOUT: "The assistant did not finish within the browser safety limit. You can retry without creating a duplicate message.",
        error_UPSTREAM_TIMEOUT: "Kimi did not finish within the response limit. Retry without creating a duplicate message.",
        error_UPSTREAM_CONNECTION_ERROR: "Kimi is temporarily unreachable. Retry without creating a duplicate message.",
        error_UPSTREAM_SERVER_ERROR: "Kimi temporarily could not complete the response. Retry without creating a duplicate message.",
        error_MODEL_EMPTY_RESPONSE: "The latest model attempt returned blank content, and no earlier attempt produced a valid result. Retry without creating a duplicate.",
        error_MODEL_RESPONSE_INVALID: "Kimi could not reliably verify this request or reply. Retry without creating a duplicate message.",
        error_RETRYABLE_REQUEST_FAILED: "The request was interrupted or the server returned an unreadable error. Retry the same message without creating a duplicate.",
        error_MODEL_LOW_QUALITY_RESPONSE: "Kimi did not produce a reliable reply within this request's budget. Retry without creating a duplicate message.",
        error_INVALID_MESSAGE_ACTION: "That card action is invalid. Refresh and try again.",
        error_INVALID_CARD_SOURCE: "That revision card no longer belongs to the current Stage. Refresh and choose the current card.",
        error_PROPOSAL_STALE: "This proposal is based on an older Stage or map snapshot and is no longer active.",
        error_PROPOSAL_PRECONDITION_FAILED: "The current map no longer matches this proposal's prerequisite.",
        error_PROPOSAL_SEARCH_EXHAUSTED: "No verified map proposal could be produced for this direction. The current map was not changed.",
        error_REVISION_EXECUTION_INVALID: "The frozen revision did not pass the internal execution-contract check. The current map was not changed.",
        error_REVISION_EXECUTION_MISMATCH: "The replayed revision did not match its frozen tile changes. The current map was not changed.",
        error_DISAGREEMENT_ACTIVE: "Resolve the current disagreement before choosing another revision card.",
        playSyncFailed: "The Stage was completed, but the play result could not be synchronized. This attempt will be recorded as interrupted.",
        playLoadFailed: "The selected Stage could not be loaded in Unity. Please review the Stage and try again.",
        translatedDisplay: "Translated display",
        translationInProgress: "Translating AI messages ({count} remaining)...",
        translationUnavailable: "Translation temporarily unavailable · showing original"
    },
    "zh-CN": {
        title: "Sokoban 共创实验室",
        subtitle: "可持续追踪的关卡设计工作台",
        loading: "正在载入会话……",
        ready: "会话已同步",
        retry: "重试",
        neutralBrief: "中性设计说明",
        openFromUnity: "请先在 Unity 创建第一版关卡，再进入这里继续共创。",
        landingBody: "实验室会把每个已接受 Stage、完整对话和试玩记录关联保存，不会为你分配预设设计目标。",
        startDemo: "创建示例会话",
        sessionSetup: "会话设置",
        formalLandingTitle: "Stage 1 已同步，选择语言后开始共创。",
        formalLandingBody: "实验室会把每个已接受的 Stage、完整对话和试玩记录关联保存，不会为你分配预设设计目标。",
        sessionLanguage: "会话语言",
        formalLanguageLock: "本次会话将使用所选语言；进入共创后无法再次切换。点击进入后将开始 20 分钟共创计时。",
        demoLanguageLock: "创建会话后无法再次切换语言。",
        enterSession: "进入共创会话",
        demoGenerationStatus: "正在使用算法创建示例地图……",
        versionHistory: "版本历史",
        stages: "Stages",
        conversation: "共创对话",
        discuss: "讨论并继续完善当前 Stage",
        viewingHistory: "你正在查看这个历史 Stage 及其相关对话；返回当前 Stage 后才能继续聊天。",
        returnCurrent: "返回当前版本",
        assessmentPending: "正在准备首段回复",
        assessmentPendingBody: "助手会围绕已验证的当前地图中值得关注的部分展开讨论。",
        noStageConversation: "这个 Stage 暂时没有相关对话。",
        noStageConversationBody: "每个 Stage 只显示与该已保存版本关联的讨论。",
        thinking: "助手正在分析当前 Stage……",
        chatWaitingPrimary: "正在等待首选模型生成回复",
        chatWaitingFallback: "助手响应较慢，正在重试一次",
        chatWaitingProposal: "方案可能仍在生成并进行确定性验证",
        chatWaitingServerResult: "正在等待服务器返回最终结果",
        chatRetryPending: "上一条消息尚未完成，可安全重试且不会产生重复记录。",
        messageLabel: "给关卡设计助手发送消息",
        messagePlaceholder: "说明你想修改什么，或询问这个关卡的设计……",
        requestProposal: "方案",
        requestProposalHint: "切换下一条消息是否作为方案申请",
        send: "发送",
        sending: "发送中……",
        sendHint: "Enter 发送 · Shift+Enter 换行",
        currentLevel: "当前关卡",
        playStage: "试玩这个 Stage",
        saveStage: "保存为新 Stage",
        discard: "放弃草稿",
        continueFromStage: "从这个 Stage 继续",
        playEvidence: "试玩证据",
        noPlayEvidence: "这个 Stage 暂时没有可选试玩记录。",
        finalizeStage: "确认最终 Stage",
        finalizeHint: "确认后将冻结编辑，并在下一步记录你的设计意图。",
        intentionTitle: "设计者意图",
        intentionQuestion: "你想对另一位玩家说些什么？",
        completeSession: "完成会话",
        sessionComplete: "会话已完成",
        sessionCompleteBody: "最终 Stage、设计意图、对话和试玩证据均已安全记录。请返回原 Unity 游戏并进入 Challenge Waiting。",
        returnUnity: "返回 Unity 继续",
        returnUnityUnavailable: "无法访问原 Unity 标签页。请手动返回该标签页；不要重新打开游戏页面，因为匹配状态保存在原来的游戏实例中。",
        returnUnityCloseBlocked: "Unity 已在原标签页中准备继续。如果共创页面没有自动关闭，请切换到 Unity 标签页。",
        finalConfirmation: "最终确认",
        confirmQuestion: "确认将这个版本作为最终 Stage 吗？",
        confirmBody: "确认后将锁定编辑和聊天，随后需要填写你的设计意图。",
        cancel: "取消",
        confirmFinal: "确认最终 Stage",
        stage: "Stage",
        current: "当前",
        initial: "初始版本",
        human_edit: "设计者编辑",
        llm_accepted: "已接受 AI 提案",
        restored: "恢复的版本",
        verified: "已验证可解",
        validationFailed: "验证未通过",
        steps: "步",
        pushes: "次推动",
        editMode: "正在编辑当前 Stage；只有明确保存后才会产生新版本。",
        readOnlyMode: "历史 Stage 只读；选择“从这个 Stage 继续”才会生成新版本。",
        lockedMode: "最终 Stage 已锁定。",
        unsaved: "未保存草稿",
        proposal: "地图修改提案",
        suggestedDirection: "助手提出的设计方向",
        draftSuggestedRevision: "请助手生成这个方案",
        challengeRevision: "质疑这个方案",
        alternativeRevision: "换一个方案",
        staleRevisionCard: "仅最新方案可操作",
        proposalStale: "该方案基于旧 Stage，已失效。",
        proposalAlreadySatisfied: "当前地图已经包含这项修改。",
        proposalPreconditionFailed: "当前地图状态已不再符合这项方案的执行前提。",
        proposalUnbound: "这是历史方案，只能阅读，缺少可靠的执行绑定。",
        proposalAlreadyExecuted: "这项方案已经执行过一次，当前不再可用。",
        proposalSearchExhausted: "没有找到通过验证的地图方案，当前地图未改变。",
        discussionUser: "用户目前的方向",
        discussionAi: "AI 目前的观点",
        discussionCore: "分歧核心",
        discussionNext: "接下来要讨论",
        proposalConsent: "请根据这个方向生成一份可供审查的地图提案：{summary}",
        proposalValid: "确定性验证已通过，请检查改动格子后再决定。",
        accept: "接受为新 Stage",
        reject: "拒绝",
        changedTiles: "个格子发生变化",
        objectiveEvidence: "确定性证据",
        aiOpinion: "AI 评价",
        attemptCompleted: "已通关",
        attemptAbandoned: "提前返回",
        attemptInterrupted: "异常中断",
        attemptStarted: "进行中",
        moves: "次移动",
        restarts: "次重开",
        seconds: "秒",
        partial_completion: "部分关卡补全",
        description_generation: "描述生成",
        algorithm_demo: "算法示例",
        floor: "地面",
        wall: "墙",
        water: "水",
        player: "玩家",
        box: "箱子",
        target: "目标",
        erase: "空白",
        errorGeneric: "请求未能完成。",
        errorNoSession: "请从 Unity 打开共创实验室，或创建示例会话。",
        errorDirtyPlay: "请先保存或放弃地图草稿，再开始试玩。",
        errorPendingPlay: "请先接受或拒绝待处理的地图提案，再开始试玩。",
        errorIntentRequired: "请先填写设计意图，再完成会话。",
        error_INVALID_LEVEL: "地图格式无效，请检查编辑内容。",
        error_INVALID_HEIGHT: "地图必须正好包含 10 行。",
        error_INVALID_WIDTH: "地图每一行必须正好包含 12 个格子。",
        error_UNKNOWN_TILE: "地图包含不支持的格子符号。",
        error_INVALID_PLAYER_COUNT: "地图必须正好包含 1 个玩家。",
        error_INVALID_BOX_COUNT: "地图必须包含 1 或 2 个箱子。",
        error_MISMATCHED_TARGET_COUNT: "目标数量必须与箱子数量一致。",
        error_UNSOLVABLE_LEVEL: "确定性求解器未能找到这张地图的可行解。",
        error_SEARCH_BUDGET_EXCEEDED: "地图验证搜索已达到预算，暂时无法完成验证。",
        error_OPEN_OUTER_WALL: "外部边界必须由墙（#）封闭，水域不能替代外墙。",
        playSyncFailed: "关卡已经通关，但本次试玩结果未能同步；该记录之后会标记为异常中断。",
        playLoadFailed: "所选 Stage 未能在 Unity 中加载，请检查该版本后重试。",
        translatedDisplay: "翻译内容",
        translationInProgress: "正在翻译 AI 消息（剩余 {count} 条）……",
        translationUnavailable: "译文暂不可用 · 当前显示原文"
    }
};

translations.en.entityLegend = "P: Player · B1/B2: Boxes · T1/T2: Targets · ~: Water";
translations["zh-CN"].entityLegend = "P\uFF1A\u73A9\u5BB6 \u00B7 B1/B2\uFF1A\u7BB1\u5B50 \u00B7 T1/T2\uFF1A\u76EE\u6807 \u00B7 ~\uFF1A\u6C34\u57DF";

translations["zh-CN"].playOpenedInUnity = "\u6240\u9009 Stage \u6b63\u5728 Unity \u89c6\u56fe\u4e2d\u6253\u5f00\u3002";
translations["zh-CN"].playUnityUnavailable = "\u5d4c\u5165\u7684 Unity \u89c6\u56fe\u4e0d\u53ef\u7528\u3002\u5171\u521b\u5de5\u4f5c\u53f0\u4ecd\u4fdd\u6301\u6253\u5f00\uff0c\u8bf7\u4ece\u539f\u6e38\u620f\u9875\u91cd\u8bd5\u3002";
translations.en.progressTitle = "Co-creation progress";
translations.en.expressedDirections = "Expressed directions";
translations.en.noExpressedDirections = "No explicit directions yet.";
translations.en.designInclinations = "Design inclinations";
translations.en.noDesignInclinations = "No confirmed design inclinations yet.";
translations.en.whyUnderstand = "Why I understand it this way";
translations.en.confirmedAtStage = "Confirmed at Stage {stage}";
translations.en.intentMatches = "Matches my idea";
translations.en.intentReject = "Not what I mean";
translations.en.intentAdjust = "I want to adjust it";
translations.en.intentModify = "Modify inclination";
translations.en.intentSave = "Save inclination";
translations.en.intentCancel = "Cancel";
translations.en.intentCharacters = "{count} / 1200";
translations.en.intentConfirmed = "Confirmed";
translations.en.intentRejected = "Rejected";
translations.en.intentRevised = "Revised and confirmed";
translations.en.intentSuperseded = "Replaced by a later inclination";
translations.en.intentStale = "This intent card is read-only.";
translations.en.intentKeepThis = "Keep this";
translations.en.intentKept = "Kept";
translations.en.intentNewOption = "Current new inclination";
translations.en.intentExistingOption = "Previously confirmed inclination";
translations.en.intentEvidence = "Your original wording";
translations.en.intentConflictPlaceholder = "Choose which inclination to keep above before continuing the conversation.";
translations.en.proposalPlanningPlaceholder = "Planning, searching, and validating a proposal for this topic…";
translations.en.proposalRetryPlaceholder = "Retry the unfinished proposal request to continue this topic.";
translations.en.proposalSupplementPlaceholder = "Add a direction for this topic or reply with a suggested number…";
translations.en.error_STALE_INTENT_CARD = "This intent card was already handled or is no longer current.";
translations.en.error_INVALID_INTENT_FEEDBACK = "Enter a clear design inclination between 4 and 1200 characters.";
translations.en.error_INTENT_CONFLICT_PENDING = "Choose which conflicting inclination to keep before sending another message.";
translations.en.error_INTENT_CONTEXT_CHANGED = "Confirmed inclination memory changed during review. Retry this message.";
translations.en.confirmedDecisions = "Confirmed decisions";
translations.en.unresolvedQuestions = "Unresolved questions";
translations.en.noConfirmedDecisions = "No confirmed decisions yet.";
translations.en.noUnresolvedQuestions = "No unresolved questions yet.";
translations.en.answeredQuestions = "Processed";
translations.en.noAnsweredQuestions = "No processed questions yet.";
translations.en.askedAtStage = "Asked at Stage {stage}";
translations.en.answeredAtStage = "Answered at Stage {stage}";
translations.en.ignoredAtStage = "Ignored at Stage {stage}";
translations.en.answeredLabel = "Answered";
translations.en.ignoredLabel = "Ignored";
translations.en.ignoreQuestion = "Ignore";
translations.en.restoreQuestion = "Restore";
translations.en.error_STALE_QUESTION = "This question is no longer available for that action.";
translations.en.viewDetailedObservation = "View detailed observation";
translations.en.fromStage = "From Stage {stage}";
translations.en.explicitLabel = "Explicit";
translations.en.confirmedLabel = "Confirmed";
translations.en.unresolvedLabel = "Unresolved";
translations.en.updatedAt = "Updated {time}";
translations.en.proposalConcreteChanges = "Concrete tile changes";
translations.en.proposalPreserved = "Preserved";
translations.en.proposalBefore = "Before";
translations.en.proposalAfter = "After";
translations.en.proposalObjective = "What this should achieve";
translations.en.proposalMustSatisfy = "Must satisfy";
translations.en.proposalTryToAchieve = "Try to achieve";
translations.en.proposalActualChanges = "Actual changes prepared";
translations.en.proposalRequestText = "Your request";
translations.en.proposalAnswerTexts = "Your description";
translations.en.proposalChangeList = "How I will change it";
translations.en.proposalReason = "Why I will change it this way";
translations.en.proposalSolution = "Solution verification";
translations.en.executeBoundProposal = "Execute the bound proposal.";
translations.en.error_SEMANTIC_CONSTRAINT_NOT_MET = "No candidate satisfied every explicit requirement; the current map was not changed.";
translations.en.error_SEMANTIC_POSTCONDITION_FAILED = "The reviewed proposal failed its semantic postcondition and was not applied.";
translations.en.error_EXECUTION_REPLAY_MISMATCH = "The actual diff no longer matches the reviewed purple card and was not applied.";
translations.en.error_CHALLENGE_REVIEW_FAILED = "Kimi could not judge this reason reliably. Retry the message.";
translations.en.error_CHALLENGE_REVIEW_RETRY_RETIRED = "Use the chat request retry for this judgment.";
translations.en.error_CHALLENGE_EXIT_UNSUPPORTED = "Continue the conversation to resolve this proposal challenge.";
translations.en.proposalChallenged = "This proposal is paused while its challenge is being resolved.";
translations["zh-CN"].progressTitle = "\u5171\u521b\u8fdb\u5ea6";
translations["zh-CN"].expressedDirections = "\u5df2\u8868\u8fbe\u65b9\u5411";
translations["zh-CN"].noExpressedDirections = "\u6682\u65e0\u660e\u786e\u8868\u8fbe\u7684\u65b9\u5411\u3002";
translations["zh-CN"].designInclinations = "\u8bbe\u8ba1\u503e\u5411";
translations["zh-CN"].noDesignInclinations = "\u6682\u65e0\u7ecf\u4f60\u786e\u8ba4\u6216\u4fee\u8ba2\u7684\u8bbe\u8ba1\u503e\u5411\u3002";
translations["zh-CN"].whyUnderstand = "\u4e3a\u4ec0\u4e48\u6211\u4f1a\u8fd9\u6837\u7406\u89e3";
translations["zh-CN"].confirmedAtStage = "\u5728 Stage {stage} \u786e\u8ba4";
translations["zh-CN"].intentMatches = "\u7b26\u5408\u6211\u7684\u60f3\u6cd5";
translations["zh-CN"].intentReject = "\u4e0d\u662f\u6211\u7684\u610f\u601d";
translations["zh-CN"].intentAdjust = "\u6211\u60f3\u8c03\u6574\u4e00\u4e0b";
translations["zh-CN"].intentModify = "\u4fee\u6539\u503e\u5411";
translations["zh-CN"].intentSave = "\u4fdd\u5b58\u503e\u5411";
translations["zh-CN"].intentCancel = "\u53d6\u6d88";
translations["zh-CN"].intentCharacters = "{count} / 1200";
translations["zh-CN"].intentConfirmed = "\u5df2\u786e\u8ba4";
translations["zh-CN"].intentRejected = "\u5df2\u5426\u5b9a";
translations["zh-CN"].intentRevised = "\u5df2\u4fee\u8ba2\u5e76\u786e\u8ba4";
translations["zh-CN"].intentSuperseded = "\u5df2\u88ab\u540e\u7eed\u503e\u5411\u66ff\u4ee3";
translations["zh-CN"].intentStale = "\u8fd9\u5f20\u503e\u5411\u5361\u73b0\u5728\u53ea\u8bfb\u3002";
translations["zh-CN"].intentKeepThis = "\u4fdd\u7559\u8fd9\u4e2a";
translations["zh-CN"].intentKept = "\u5df2\u4fdd\u7559";
translations["zh-CN"].intentNewOption = "\u5f53\u524d\u65b0\u610f\u56fe";
translations["zh-CN"].intentExistingOption = "\u4e4b\u524d\u5df2\u786e\u8ba4\u7684\u610f\u56fe";
translations["zh-CN"].intentEvidence = "\u4f60\u5f53\u65f6\u7684\u539f\u8bdd";
translations["zh-CN"].intentConflictPlaceholder = "\u8bf7\u5148\u5728\u4e0a\u65b9\u9009\u62e9\u8981\u4fdd\u7559\u7684\u610f\u56fe\uff0c\u4e4b\u540e\u624d\u80fd\u7ee7\u7eed\u804a\u5929\u3002";
translations["zh-CN"].proposalPlanningPlaceholder = "\u6b63\u5728\u4e3a\u5f53\u524d\u4e3b\u9898\u89c4\u5212\u3001\u641c\u7d22\u5e76\u9a8c\u8bc1\u65b9\u6848\u2026\u2026";
translations["zh-CN"].proposalRetryPlaceholder = "\u8bf7\u91cd\u8bd5\u672a\u5b8c\u6210\u7684\u65b9\u6848\u8bf7\u6c42\uff0c\u4ee5\u7ee7\u7eed\u5f53\u524d\u4e3b\u9898\u3002";
translations["zh-CN"].proposalSupplementPlaceholder = "\u8bf7\u8865\u5145\u5f53\u524d\u4e3b\u9898\u7684\u65b9\u5411\uff0c\u6216\u56de\u590d\u5efa\u8bae\u7f16\u53f7\u2026\u2026";
translations["zh-CN"].error_INTENT_CONFLICT_PENDING = "\u8bf7\u5148\u9009\u62e9\u8981\u4fdd\u7559\u7684\u51b2\u7a81\u610f\u56fe\u3002";
translations["zh-CN"].error_INTENT_CONTEXT_CHANGED = "\u51b2\u7a81\u590d\u6838\u671f\u95f4\u5df2\u786e\u8ba4\u610f\u56fe\u53d1\u751f\u4e86\u53d8\u5316\uff0c\u8bf7\u91cd\u8bd5\u8fd9\u6761\u6d88\u606f\u3002";
translations["zh-CN"].confirmedDecisions = "\u5df2\u786e\u8ba4\u51b3\u7b56";
translations["zh-CN"].unresolvedQuestions = "\u672a\u89e3\u51b3\u95ee\u9898";
translations["zh-CN"].noConfirmedDecisions = "\u6682\u65e0\u5df2\u786e\u8ba4\u51b3\u7b56\u3002";
translations["zh-CN"].noUnresolvedQuestions = "\u6682\u65e0\u672a\u89e3\u51b3\u95ee\u9898\u3002";
translations["zh-CN"].answeredQuestions = "\u5df2\u5904\u7406";
translations["zh-CN"].noAnsweredQuestions = "\u6682\u65e0\u5df2\u5904\u7406\u95ee\u9898\u3002";
translations["zh-CN"].askedAtStage = "\u63d0\u95ee\u4e8e Stage {stage}";
translations["zh-CN"].answeredAtStage = "\u4f5c\u7b54\u4e8e Stage {stage}";
translations["zh-CN"].ignoredAtStage = "\u5ffd\u7565\u4e8e Stage {stage}";
translations["zh-CN"].answeredLabel = "\u5df2\u4f5c\u7b54";
translations["zh-CN"].ignoredLabel = "\u5df2\u5ffd\u7565";
translations["zh-CN"].ignoreQuestion = "\u5ffd\u7565";
translations["zh-CN"].restoreQuestion = "\u6062\u590d";
translations["zh-CN"].error_STALE_QUESTION = "\u8fd9\u4e2a\u95ee\u9898\u5df2\u65e0\u6cd5\u6267\u884c\u8be5\u64cd\u4f5c\u3002";
translations["zh-CN"].viewDetailedObservation = "\u67e5\u770b\u8be6\u7ec6\u89c2\u5bdf";
translations["zh-CN"].fromStage = "\u6765\u81ea Stage {stage}";
translations["zh-CN"].explicitLabel = "\u5df2\u660e\u786e\u8868\u8fbe";
translations["zh-CN"].confirmedLabel = "\u5df2\u786e\u8ba4";
translations["zh-CN"].unresolvedLabel = "\u5f85\u89e3\u51b3";
translations["zh-CN"].updatedAt = "\u6700\u8fd1\u66f4\u65b0 {time}";
translations["zh-CN"].proposalConcreteChanges = "\u5177\u4f53\u683c\u5b50\u53d8\u5316";
translations["zh-CN"].proposalPreserved = "\u4fdd\u6301\u4e0d\u53d8";
translations["zh-CN"].proposalBefore = "\u4fee\u6539\u524d";
translations["zh-CN"].proposalAfter = "\u4fee\u6539\u540e";
translations["zh-CN"].proposalObjective = "\u60f3\u5b9e\u73b0\u4ec0\u4e48";
translations["zh-CN"].proposalMustSatisfy = "\u5fc5\u987b\u6ee1\u8db3";
translations["zh-CN"].proposalTryToAchieve = "\u5c3d\u91cf\u5b9e\u73b0";
translations["zh-CN"].proposalActualChanges = "\u5b9e\u9645\u51c6\u5907\u4fee\u6539";
translations["zh-CN"].proposalRequestText = "\u4f60\u7684\u9700\u6c42";
translations["zh-CN"].proposalAnswerTexts = "\u4f60\u7684\u63cf\u8ff0";
translations["zh-CN"].proposalChangeList = "\u6211\u4f1a\u600e\u6837\u6539";
translations["zh-CN"].proposalReason = "\u6211\u4e3a\u4ec0\u4e48\u8fd9\u6837\u6539";
translations["zh-CN"].proposalSolution = "\u89e3\u6cd5\u9a8c\u8bc1";
translations["zh-CN"].executeBoundProposal = "\u6267\u884c\u5df2\u7ed1\u5b9a\u7684\u65b9\u6848\u3002";
translations["zh-CN"].error_CHALLENGE_REVIEW_FAILED = "Kimi \u672a\u80fd\u53ef\u9760\u5730\u5224\u65ad\u8fd9\u6761\u7406\u7531\uff0c\u8bf7\u91cd\u8bd5\u8be5\u6d88\u606f\u3002";
translations["zh-CN"].error_CHALLENGE_REVIEW_RETRY_RETIRED = "\u8bf7\u4f7f\u7528\u804a\u5929\u8bf7\u6c42\u7684\u901a\u7528\u91cd\u8bd5\u3002";
translations["zh-CN"].error_CHALLENGE_EXIT_UNSUPPORTED = "\u8bf7\u901a\u8fc7\u7ee7\u7eed\u5bf9\u8bdd\u89e3\u51b3\u5f53\u524d\u65b9\u6848\u8d28\u7591\u3002";
translations["zh-CN"].proposalChallenged = "\u8fd9\u4e2a\u65b9\u6848\u5df2\u6682\u505c\uff0c\u9700\u5148\u5904\u7406\u5bf9\u5b83\u7684\u8d28\u7591\u3002";
translations["zh-CN"].alternativeRevision = "\u91cd\u65b0\u751f\u6210\u65b9\u6848";
translations["zh-CN"].proposalDisagreementActive = "\u8bf7\u5148\u89e3\u51b3\u5f53\u524d\u5206\u6b67\uff0c\u518d\u4f7f\u7528\u8fd9\u4e2a\u65b9\u6848";
translations.en.regenerateDraft = "Regenerate";
translations.en.enterSession = "Enter co-creation flow";
translations.en.draftPreviewTitle = "First draft preview";
translations.en.draftGeneration = "Generation {generation}";
translations.en.draftReady = "Verified and ready to enter co-creation.";
translations.en.draftRegenerating = "Unity is regenerating and validating a new draft. The current preview is preserved.";
translations.en.draftRegenerationFailed = "Regeneration did not complete. The previous draft is still available.";
translations.en.draftUnityUnavailable = "Keep the original Unity game tab open, then try again.";
translations.en.draftProtocolUnavailable = "The original Unity tab is unavailable or outdated. Reopen or refresh the game page before regenerating.";
translations.en.draftProtocolMismatch = "Unity and the co-creation page use different bridge versions. Refresh the game page.";
translations.en.draftSessionMismatch = "Unity is connected to a different co-creation session. Return from the current game session.";
translations.en.draftUnityNotReady = "Unity is still loading. Wait for the game view to become ready, then try again.";
translations.en.draftReceiverNotReady = "The regeneration controller is not ready yet. Wait a moment, then try again.";
translations.en.draftPrepareExpired = "The regeneration handoff expired before delivery. Try again.";
translations.en.draftReconnecting = "The connection was interrupted. Reconnecting to the persistent regeneration task…";
translations.en.draftBlueprintUnavailable = "The saved AI blueprint is unavailable. Start a new formal draft from Unity.";
translations.en.draftGenerationTimedOut = "Unity could not finish generation within the safe time limit.";
translations.en.draftValidationFailed = "Unity generated a candidate, but validation did not accept it.";
translations.en.formalLandingTitle = "Review the first draft, then enter co-creation.";
translations.en.formalLandingBody = "You can regenerate from the parameters selected in Unity. Only the draft you enter with becomes Stage 1.";
translations.en.demoLanguageLock = "The selected language is locked when you enter co-creation.";
translations.en.demoLandingTitle = "Algorithm-generated first Draft";
translations.en.demoLandingBody = "The algorithm is preparing a verified sample map. You can regenerate it before entering co-creation.";
translations.en.formalLandingTitle = "AI-planned and generated first Draft";
translations.en.formalLandingBody = "Unity generated and verified this map from the AI planning result. Only the draft you enter with becomes Stage 1.";
translations.en.demoDraftPreviewTitle = "Algorithm-generated first Draft";
translations.en.formalDraftPreviewTitle = "AI-planned and generated first Draft";
translations.en.draftInitialGenerating = "Generating and verifying the algorithm Draft…";
translations.en.draftInitialFailed = "The Draft could not be generated. Select Regenerate to try again.";
translations["zh-CN"].regenerateDraft = "\u91cd\u65b0\u751f\u6210";
translations["zh-CN"].enterSession = "\u8fdb\u5165\u5171\u521b\u6d41\u7a0b";
translations["zh-CN"].draftPreviewTitle = "\u9996\u7248 Draft \u9884\u89c8";
translations["zh-CN"].draftGeneration = "\u7b2c {generation} \u7248";
translations["zh-CN"].draftReady = "\u5df2\u901a\u8fc7\u9a8c\u8bc1\uff0c\u53ef\u8fdb\u5165\u5171\u521b\u6d41\u7a0b\u3002";
translations["zh-CN"].draftRegenerating = "Unity \u6b63\u5728\u91cd\u65b0\u751f\u6210\u5e76\u9a8c\u8bc1 Draft\uff0c\u5f53\u524d\u9884\u89c8\u4f1a\u4fdd\u7559\u3002";
translations["zh-CN"].draftRegenerationFailed = "\u91cd\u65b0\u751f\u6210\u672a\u5b8c\u6210\uff0c\u5df2\u4fdd\u7559\u4e0a\u4e00\u7248 Draft\u3002";
translations["zh-CN"].draftUnityUnavailable = "\u8bf7\u4fdd\u6301\u539f Unity \u6e38\u620f\u6807\u7b7e\u9875\u6253\u5f00\u540e\u91cd\u8bd5\u3002";
translations["zh-CN"].draftProtocolUnavailable = "\u539f Unity \u6807\u7b7e\u9875\u4e0d\u53ef\u7528\u6216\u7248\u672c\u8fc7\u65e7\uff0c\u8bf7\u91cd\u65b0\u6253\u5f00\u6216\u5237\u65b0\u6e38\u620f\u9875\u540e\u518d\u751f\u6210\u3002";
translations["zh-CN"].draftProtocolMismatch = "Unity \u4e0e\u5171\u521b\u9875\u7684\u6865\u63a5\u7248\u672c\u4e0d\u4e00\u81f4\uff0c\u8bf7\u5237\u65b0\u6e38\u620f\u9875\u3002";
translations["zh-CN"].draftSessionMismatch = "Unity \u5f53\u524d\u8fde\u63a5\u7684\u662f\u53e6\u4e00\u4e2a\u5171\u521b\u4f1a\u8bdd\uff0c\u8bf7\u8fd4\u56de\u5f53\u524d\u6e38\u620f\u4f1a\u8bdd\u3002";
translations["zh-CN"].draftUnityNotReady = "Unity \u4ecd\u5728\u52a0\u8f7d\uff0c\u8bf7\u7b49\u6e38\u620f\u753b\u9762\u5c31\u7eea\u540e\u91cd\u8bd5\u3002";
translations["zh-CN"].draftReceiverNotReady = "\u91cd\u65b0\u751f\u6210\u63a7\u5236\u5668\u5c1a\u672a\u5c31\u7eea\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002";
translations["zh-CN"].draftPrepareExpired = "\u91cd\u65b0\u751f\u6210\u4ea4\u63a5\u5df2\u8fc7\u671f\uff0c\u8bf7\u91cd\u8bd5\u3002";
translations["zh-CN"].draftReconnecting = "\u7f51\u7edc\u8fde\u63a5\u4e2d\u65ad\uff0c\u6b63\u5728\u91cd\u65b0\u8fde\u63a5\u6301\u4e45\u5316\u7684\u91cd\u751f\u6210\u4efb\u52a1\u2026";
translations["zh-CN"].draftBlueprintUnavailable = "\u5df2\u4fdd\u5b58\u7684 AI \u84dd\u56fe\u4e0d\u53ef\u7528\uff0c\u8bf7\u4ece Unity \u91cd\u65b0\u521b\u5efa\u6b63\u5f0f Draft\u3002";
translations["zh-CN"].draftGenerationTimedOut = "Unity \u672a\u80fd\u5728\u5b89\u5168\u65f6\u9650\u5185\u5b8c\u6210\u751f\u6210\u3002";
translations["zh-CN"].draftValidationFailed = "Unity \u5df2\u751f\u6210\u5019\u9009\u5730\u56fe\uff0c\u4f46\u9a8c\u8bc1\u672a\u901a\u8fc7\u3002";
translations["zh-CN"].formalLandingTitle = "\u67e5\u770b\u9996\u7248 Draft\uff0c\u518d\u8fdb\u5165\u5171\u521b\u6d41\u7a0b\u3002";
translations["zh-CN"].formalLandingBody = "\u4f60\u53ef\u4ee5\u57fa\u4e8e Unity \u4e2d\u9009\u62e9\u7684\u53c2\u6570\u91cd\u65b0\u751f\u6210\u3002\u53ea\u6709\u8fdb\u5165\u65f6\u7684\u5f53\u524d Draft \u4f1a\u6210\u4e3a Stage 1\u3002";
translations["zh-CN"].demoLanguageLock = "\u8fdb\u5165\u5171\u521b\u6d41\u7a0b\u540e\uff0c\u5f53\u524d\u8bed\u8a00\u9009\u62e9\u5c06\u9501\u5b9a\u3002";
translations["zh-CN"].demoLandingTitle = "\u7b97\u6cd5\u751f\u6210\u7684\u9996\u7248 Draft";
translations["zh-CN"].demoLandingBody = "\u7b97\u6cd5\u6b63\u5728\u51c6\u5907\u5df2\u9a8c\u8bc1\u7684\u793a\u4f8b\u5730\u56fe\uff0c\u8fdb\u5165\u5171\u521b\u524d\u53ef\u4ee5\u91cd\u65b0\u751f\u6210\u3002";
translations["zh-CN"].formalLandingTitle = "AI \u89c4\u5212\u5e76\u751f\u6210\u7684\u9996\u7248 Draft";
translations["zh-CN"].formalLandingBody = "Unity \u5df2\u57fa\u4e8e AI \u89c4\u5212\u7ed3\u679c\u751f\u6210\u5e76\u9a8c\u8bc1\u6b64\u5730\u56fe\uff0c\u53ea\u6709\u8fdb\u5165\u65f6\u7684\u5f53\u524d Draft \u4f1a\u6210\u4e3a Stage 1\u3002";
translations["zh-CN"].demoDraftPreviewTitle = "\u7b97\u6cd5\u751f\u6210\u7684\u9996\u7248 Draft";
translations["zh-CN"].formalDraftPreviewTitle = "AI \u89c4\u5212\u5e76\u751f\u6210\u7684\u9996\u7248 Draft";
translations["zh-CN"].draftInitialGenerating = "\u6b63\u5728\u751f\u6210\u5e76\u9a8c\u8bc1\u7b97\u6cd5 Draft\u2026";
translations["zh-CN"].draftInitialFailed = "Draft \u751f\u6210\u5931\u8d25\uff0c\u8bf7\u70b9\u51fb\u201c\u91cd\u65b0\u751f\u6210\u201d\u518d\u8bd5\u3002";
translations.en.embeddedOnlyTitle = "Return to the original Unity game page";
translations.en.embeddedOnlyBody = "Formal co-creation is available only inside the embedded workspace. This standalone page cannot regenerate, play, or enter the formal flow.";
translations["zh-CN"].embeddedOnlyTitle = "\u8bf7\u8fd4\u56de\u539f Unity \u6e38\u620f\u9875\u9762";
translations["zh-CN"].embeddedOnlyBody = "\u6b63\u5f0f\u5171\u521b\u4ec5\u5728\u6e38\u620f\u9875\u9762\u5185\u5d4c\u7684\u5de5\u4f5c\u53f0\u4e2d\u8fdb\u884c\u3002\u6b64\u72ec\u7acb\u9875\u9762\u4e0d\u80fd\u91cd\u65b0\u751f\u6210\u3001\u8bd5\u73a9\u6216\u8fdb\u5165\u6b63\u5f0f\u6d41\u7a0b\u3002";
translations.en.unsavedDraftRestored = "Your unsaved map edits were restored after the workspace refresh.";
translations["zh-CN"].unsavedDraftRestored = "\u5df2\u6062\u590d\u5237\u65b0\u524d\u672a\u4fdd\u5b58\u7684\u5730\u56fe\u4fee\u6539\u3002";
translations.en.deadlineRemaining = "LEFT";
translations.en.deadlineExpired = "TIME UP";
translations.en.deadlineExpiredAction = "SUBMIT FINAL STAGE";
translations.en.deadlineWarningAnnouncement = "Five minutes remain in the co-creation session.";
translations.en.deadlineCriticalAnnouncement = "One minute remains in the co-creation session.";
translations.en.deadlineExpiredAnnouncement = "Time is up. Submit the current map as the final Stage.";
translations["zh-CN"].deadlineRemaining = "\u5269\u4f59";
translations["zh-CN"].deadlineExpired = "\u65f6\u95f4\u5230";
translations["zh-CN"].deadlineExpiredAction = "\u8bf7\u63d0\u4ea4\u6700\u7ec8\u5173\u5361";
translations["zh-CN"].deadlineWarningAnnouncement = "\u5171\u521b\u65f6\u95f4\u8fd8\u5269\u4e94\u5206\u949f\u3002";
translations["zh-CN"].deadlineCriticalAnnouncement = "\u5171\u521b\u65f6\u95f4\u8fd8\u5269\u4e00\u5206\u949f\u3002";
translations["zh-CN"].deadlineExpiredAnnouncement = "\u65f6\u95f4\u5df2\u5230，\u8bf7\u5c06\u5f53\u524d\u5730\u56fe\u63d0\u4ea4\u4e3a\u6700\u7ec8 Stage\u3002";

export const chineseApiErrors = {
    STALE_INTENT_CARD: "\u8fd9\u5f20\u503e\u5411\u5361\u5df2\u5904\u7406\u6216\u4e0d\u518d\u662f\u5f53\u524d\u53ef\u64cd\u4f5c\u5361\u7247\u3002",
    INVALID_INTENT_FEEDBACK: "\u8bf7\u8f93\u5165 4 \u5230 1200 \u4e2a\u5b57\u7b26\u7684\u6e05\u6670\u8bbe\u8ba1\u503e\u5411\u3002",
    VERSION_CONFLICT: "当前 Stage 已发生变化，请刷新后再继续。",
    IDEMPOTENCY_CONFLICT: "该操作标识已用于不同内容，请重新发起操作。",
    UNCHANGED_LEVEL: "地图没有变化，无法保存为新 Stage。",
    PENDING_PROPOSAL: "请先接受或拒绝当前待处理的地图提案。",
    SESSION_LOCKED: "该共创会话已锁定，不能继续编辑。",
    SESSION_ACCESS_DENIED: "当前浏览器没有访问这个共创会话的权限。",
    INVALID_BOOTSTRAP_TOKEN: "会话链接无效，请从 Unity 重新进入。",
    BOOTSTRAP_TOKEN_USED: "该会话链接已经使用过，请在原浏览器标签页继续。",
    INVALID_PLAY_TICKET: "试玩票据无效。",
    PLAY_TICKET_USED: "试玩票据已经使用过。",
    PLAY_TICKET_EXPIRED: "试玩票据已过期，请返回工作台重新点击 Play。",
    INVALID_LEVEL: "地图格式无效，请检查编辑内容。",
    INVALID_HEIGHT: "地图必须正好包含 10 行。",
    INVALID_WIDTH: "地图每一行必须正好包含 12 个格子。",
    UNKNOWN_TILE: "地图包含不支持的格子符号。",
    INVALID_PLAYER_COUNT: "地图必须正好包含 1 个玩家。",
    INVALID_BOX_COUNT: "地图必须包含 1 或 2 个箱子。",
    MISMATCHED_TARGET_COUNT: "目标数量必须与箱子数量一致。",
    UNSOLVABLE_LEVEL: "确定性求解器未能找到这张地图的可行解。",
    SEARCH_BUDGET_EXCEEDED: "地图验证搜索已达到预算，暂时无法完成验证。",
    OPEN_OUTER_WALL: "外部边界必须由墙（#）封闭，水域不能替代外墙。",
    UPSTREAM_TIMEOUT: "LLM 响应超时，请稍后重试。",
    UPSTREAM_CONNECTION_ERROR: "暂时无法连接 LLM 服务，请稍后重试。",
    UPSTREAM_SERVER_ERROR: "Kimi 服务暂时未能完成本次回复，可使用原消息安全重试，不会产生重复记录。",
    MODEL_EMPTY_RESPONSE: "最后一次模型尝试返回了空白内容，且此前尝试也未产生有效结果；可使用原消息安全重试，不会产生重复记录。",
    MODEL_RESPONSE_INVALID: "Kimi 尚未可靠核实这次请求或回复；可使用原消息安全重试，不会产生重复记录。",
    RETRYABLE_REQUEST_FAILED: "请求中断或服务器返回了无法读取的错误；可用原消息安全重试，不会产生重复记录。",
    MODEL_LOW_QUALITY_RESPONSE: "Kimi 未能在本次请求预算内生成可靠回复；可使用原消息安全重试，不会产生重复记录。",
    CLIENT_TIMEOUT: "助手未能在浏览器安全时限内完成。可直接重试，且不会产生重复消息。",
    CONFIGURATION_ERROR: "服务器尚未正确配置 LLM 服务。",
    INVALID_MESSAGE_ACTION: "卡片操作无效，请刷新后重试。",
    INVALID_CARD_SOURCE: "这张修改建议无效、已过期或不再对应当前 Stage，请重新查看最新方案。",
    PROPOSAL_STALE: "该方案基于旧 Stage 或旧地图快照，已失效。",
    PROPOSAL_PRECONDITION_FAILED: "当前地图状态已不再符合这项方案的执行前提。",
    PROPOSAL_SEARCH_EXHAUSTED: "没有找到通过验证的地图方案，当前地图未改变。",
    REVISION_EXECUTION_INVALID: "已冻结的修改没有通过内部执行合同检查，当前地图未改变。",
    REVISION_EXECUTION_MISMATCH: "重放结果与已冻结的格子变化不一致，当前地图未改变。",
    DISAGREEMENT_ACTIVE: "当前仍有未解决的分歧，请先继续协商后再选择修改方案。",
    SEMANTIC_CONSTRAINT_NOT_MET: "没有候选同时满足所有明确要求，当前地图未改变。",
    SEMANTIC_POSTCONDITION_FAILED: "已审查方案未通过执行后语义校验，因此未应用。",
    EXECUTION_REPLAY_MISMATCH: "实际差异与已审查紫卡不一致，因此未应用。"
};

Object.assign(chineseApiErrors, {
    INVALID_RESPONSE: "服务器响应不完整或无法读取，可重试原操作。",
    REQUIREMENT_INTERPRETATION_INVALID: "尚未可靠理解你的设计要求，可使用原消息重试。",
    REQUIREMENT_EVIDENCE_MISSING: "这项方案的原始要求证据暂时无法恢复，可重试原消息。",
    REQUIREMENT_PERMISSION_CONFLICT: "明确要求与当前允许的修改范围冲突，需要调整方案方向。",
    CHALLENGE_REVIEW_FAILED: "尚未可靠完成质疑理由的评审，可使用原消息重试。",
    DETERMINISTIC_SEARCH_EXHAUSTED: "尚未找到通过验证的方案，当前地图未改变。",
    CANDIDATE_DUPLICATED: "候选仍重复原方案，尚未形成经过验证的替代方案。",
    CANDIDATE_UNSOLVABLE: "候选未通过可解性验证，当前地图未改变。"
});
Object.assign(translations.en, {
    error_INVALID_RESPONSE: "The server response was incomplete or unreadable. Retry the original operation.",
    error_REQUIREMENT_INTERPRETATION_INVALID: "The design requirements could not be understood reliably. Retry the original message.",
    error_REQUIREMENT_EVIDENCE_MISSING: "The original requirement evidence could not be restored. Retry the original message.",
    error_REQUIREMENT_PERMISSION_CONFLICT: "Explicit requirements conflict with the allowed editing scope. Adjust the direction.",
    error_DETERMINISTIC_SEARCH_EXHAUSTED: "No verified proposal was found. The map was not changed.",
    error_CANDIDATE_DUPLICATED: "The candidates repeated the original proposal. No verified alternative was produced.",
    error_CANDIDATE_UNSOLVABLE: "The candidates did not pass solvability validation. The map was not changed."
});

export const LEVEL_VALIDATION_ERROR_CODES = new Set([
    "INVALID_LEVEL",
    "INVALID_HEIGHT",
    "INVALID_WIDTH",
    "UNKNOWN_TILE",
    "INVALID_PLAYER_COUNT",
    "INVALID_BOX_COUNT",
    "MISMATCHED_TARGET_COUNT",
    "UNSOLVABLE_LEVEL",
    "SEARCH_BUDGET_EXCEEDED",
    "OPEN_OUTER_WALL"
]);

export const validationTileNames = {
    " ": { en: "outer void", zh: "外围空白" },
    "#": { en: "wall (#)", zh: "墙（#）" },
    ".": { en: "floor (.)", zh: "地面（.）" },
    "@": { en: "water (@)", zh: "水域（@）" },
    p: { en: "player (p)", zh: "玩家（p）" },
    s: { en: "box (s)", zh: "箱子（s）" },
    t: { en: "target (t)", zh: "目标（t）" }
};

// Button labels are presentation only. Preserve the existing request wording.
export const REQUEST_COPY = Object.fromEntries(["en", "zh-CN"].map(language => [language,
    Object.freeze(Object.fromEntries(["executeBoundProposal", "challengeRevision", "alternativeRevision", "proposalConsent"]
        .map(key => [key, translations[language][key]])))
]));
export const ORIGINAL_ERROR_COPY = { en: { ...translations.en }, "zh-CN": { ...chineseApiErrors } };

// Player-facing copy; never use these new labels as action request content.
Object.assign(translations["en"], {
    "subtitle": "Share ideas. Make changes. Give them a play.",
    "demoLandingTitle": "Start with this map?",
    "formalLandingTitle": "Start with this map?",
    "demoLandingBody": "Take a look. Want a different map? Generate another!",
    "formalLandingBody": "Take a look. Want a different map? Generate another!",
    "draftLoadingTitle": "Getting your map ready…",
    "draftLoadingBody": "Getting the map ready and checking for a solution…",
    "draftInitialGenerating": "Getting the map ready and checking for a solution…",
    "draftRegenerating": "Preparing another map. This one stays until the new map is ready.",
    "draftInitialFailed": "This map couldn’t be prepared. Select “Generate another” to try again.",
    "draftRegenerationFailed": "The new map couldn’t be prepared. Your previous map is still here. Try generating another.",
    "draftReady": "Your map is ready!",
    "demoDraftPreviewTitle": "Starting map",
    "formalDraftPreviewTitle": "Starting map",
    "draftGeneration": "Generation {generation}",
    "enterSession": "Start co-creating",
    "regenerateDraft": "Generate another",
    "sessionSetup": "Before we begin",
    "sessionLanguage": "Choose language",
    "formalLanguageLock": "Language is locked after starting. Selecting “Start co-creating” starts your 20 minutes.",
    "demoLanguageLock": "Language is locked after co-creation starts.",
    "draftLegend": "P Player · B Box · T Target · ~ Water",
    "ready": "Ready",
    "versionHistory": "Version history",
    "stages": "Saved versions",
    "versionHint": "Stage 1, Stage 2… are saved versions of the same map. You can revisit them here.",
    "discuss": "What do you think of this version?",
    "messagePlaceholder": "Got an idea? Tell us what you’d like to discuss or change.",
    "assessmentPending": "AI is taking a look at the map…",
    "assessmentPendingBody": "Take a look around the map while the opening reply is being prepared.",
    "requestProposal": "Ask AI for a plan",
    "requestProposalHint": "Want a concrete plan? Turn this on, then send your idea. You decide whether to use the proposal.",
    "playStage": "Play this version",
    "playEvidence": "Play history",
    "noPlayEvidence": "Haven’t tried this version yet? Give it a play.",
    "saveStage": "Save this version",
    "saveHint": "Saving creates a new Stage and keeps the earlier versions.",
    "discard": "Discard unsaved changes",
    "continueFromStage": "Continue from this version",
    "draftSuggestedRevision": "Preview these changes",
    "challengeRevision": "I see it differently",
    "alternativeRevision": "Try another plan",
    "proposalValid": "The proposed map has a solution. Take a look at the changed tiles before deciding.",
    "accept": "Use this and save a new version",
    "reject": "Don’t use this one",
    "designInclinations": "Your confirmed ideas",
    "unresolvedQuestions": "Questions still open",
    "finalizeStage": "Choose this version",
    "confirmFinal": "Choose this version",
    "confirmQuestion": "Is this your final pick?",
    "confirmBody": "After confirming, you cannot edit or continue chatting in this session. Next, leave a message for the other player.",
    "finalizeHint": "After confirming, you cannot edit or continue chatting in this session. Next, leave a message for the other player.",
    "intentionTitle": "Leave a message for the other player",
    "completeSession": "Save message and finish",
    "sessionComplete": "That’s a wrap!",
    "sessionCompleteBody": "Your map and message are saved. Return to the original game page to continue.",
    "demoCompleteBody": "Your map and message are saved. This co-creation session is complete.",
    "chatWaitingPrimary": "Preparing a reply. Please wait…",
    "chatWaitingProposal": "Preparing a proposal. Please wait…",
    "chatElapsed": "Waiting {seconds} seconds",
    "thinking": "Preparing a reply. Please wait…",
    "proposalPlanningPlaceholder": "Preparing a proposal. Please wait…",
    "draftReconnecting": "The connection was interrupted. Checking the result of your last action.",
    "deadlineExpiredAnnouncement": "Time is up. Please submit your final version.",
    "deadlineExpiredAction": "SUBMIT FINAL VERSION",
    "verified": "This version has a solution",
    "previousVerified": "The last saved version has a solution",
    "draftUnchecked": "Your unsaved changes have not been checked",
    "savedSolution": "Saved version: {steps} moves · {pushes} pushes",
    "solutionSummary": "{steps} moves · {pushes} pushes",
    "editMode": "Choose a tile, then click the map to edit. Save when you’re ready.",
    "readOnlyMode": "You’re viewing an earlier save. Select “Continue from this version” to edit from it.",
    "lockedMode": "This session has ended. The map can no longer be edited.",
    "proposalUnbound": "This old proposal is view-only. Please use the latest proposal.",
    "proposalPreconditionFailed": "The map has changed, so this proposal can no longer be used.",
    "errorIntentRequired": "Leave a message before finishing this session.",
    "contextChanged": "The conversation has changed. Send your request based on the current dialogue. This request did not change the map.",
    "replyFailed": "This reply couldn’t be completed. Please retry.",
    "connectionFailed": "The connection was interrupted. Checking the result of your last action.",
    "modificationFailed": "These changes did not pass the checks. The map was not changed.",
    "checkIncomplete": "The check could not finish, so we cannot yet confirm whether this map has a solution.",
    "oldProposal": "This proposal is no longer available. Please check the latest proposal.",
    "noProposal": "No suitable proposal was found. The map was not changed.",
    "requirementsUnclear": "The requested changes could not be understood reliably. Retry the original message.",
    "permissionConflict": "This direction goes beyond the allowed changes. Please adjust the idea.",
    "unsolved": "No solution was found for this map. Please check the map.",
    "needsDiscussion": "Discuss the current difference of views before choosing another proposal.",
    "noAlternative": "No different proposal was found. The current map was not changed.",
    "busyHint": "Your last action is still being processed. Please wait.",
    "playActiveHint": "Return here after your play attempt.",
    "closedHint": "This session has ended. Editing and chat are closed.",
    "historyHint": "Return to the current Stage to edit or chat.",
    "noChangesHint": "There are no new map changes to save.",
    "dirtyPlayHint": "Save or discard your changes before playing.",
    "pendingHint": "Accept or reject the pending proposal before continuing.",
    "finalDirtyHint": "Save or discard your changes before choosing the final version.",
    "conflictHint": "Choose which idea to keep before continuing the conversation.",
    "retryHint": "Retry the unfinished message first.",
    "emptyMessageHint": "Write your idea, then select Send.",
    "noVersionHint": "The map is not ready yet. Please wait.",
    "openingHint": "The AI reply is still being prepared. Wait before confirming.",
    "saveReason": "Save: {reason}",
    "playReason": "Play: {reason}",
    "chatReason": "Chat: {reason}",
    "finalReason": "Final version: {reason}",
    "chatAriaLabel": "Talk about the map with AI",
    "workspaceAriaLabel": "Map co-creation workspace",
    "routeAriaLabel": "Show route from row {fromRow}, column {fromColumn} to row {toRow}, column {toColumn}"
});
Object.assign(translations["zh-CN"], {
    "subtitle": "聊聊想法，动手改改，再下场试试",
    "demoLandingTitle": "从这张地图开始吗？",
    "formalLandingTitle": "从这张地图开始吗？",
    "demoLandingBody": "先看看地图。不喜欢？换一张！",
    "formalLandingBody": "先看看地图。不喜欢？换一张！",
    "draftLoadingTitle": "地图正在准备中……",
    "draftLoadingBody": "地图正在准备中，还要检查一下能不能通关……",
    "draftInitialGenerating": "地图正在准备中，还要检查一下能不能通关……",
    "draftRegenerating": "正在准备另一张地图。生成成功前，眼前这张会保留。",
    "draftInitialFailed": "这张地图没能准备好，点“换一张地图”再试一次。",
    "draftRegenerationFailed": "新地图没能准备好，之前那张还在。可以点击“换一张地图”再试一次。",
    "draftReady": "地图已准备好！",
    "demoDraftPreviewTitle": "初始地图",
    "formalDraftPreviewTitle": "初始地图",
    "draftGeneration": "第 {generation} 张",
    "enterSession": "开始共创",
    "regenerateDraft": "换一张地图",
    "sessionSetup": "开始之前",
    "sessionLanguage": "选择语言",
    "formalLanguageLock": "开始后，本轮语言不能更换。点击“开始共创”后，将开始 20 分钟计时。",
    "demoLanguageLock": "开始共创后，语言不能更改。",
    "draftLegend": "P 玩家 · B 箱子 · T 目标 · ~ 水",
    "ready": "准备好了",
    "versionHistory": "版本记录",
    "stages": "地图存档",
    "versionHint": "Stage 1、Stage 2……是同一张地图先后保存的版本。随时可以回来看看。",
    "discuss": "这版地图，你怎么看？",
    "messagePlaceholder": "有什么想法？想聊聊，或想改哪里，都可以从这里开始。",
    "assessmentPending": "AI 正在看看这张地图……",
    "assessmentPendingBody": "可以先看看地图，等回复准备好再一起聊聊。",
    "requestProposal": "请 AI 出个方案",
    "requestProposalHint": "想看看具体怎么改？打开这个开关，再发送你的想法。方案出来后，由你决定用不用。",
    "playStage": "下场试试这一版",
    "playEvidence": "试玩足迹",
    "noPlayEvidence": "还没下场试过？可以先走一趟。",
    "saveStage": "给这版存个档",
    "saveHint": "保存会创建一个新 Stage，之前的版本也会保留。",
    "discard": "撤销未保存的修改",
    "continueFromStage": "接着这一版改",
    "draftSuggestedRevision": "先看看改完的样子",
    "challengeRevision": "我有点不同想法",
    "alternativeRevision": "换个方案看看",
    "proposalValid": "改完的地图已经确认可解。看看哪些格子变了，喜欢再采用。",
    "accept": "就用这个，存成新版本",
    "reject": "这次先不用",
    "designInclinations": "你确认过的点子",
    "unresolvedQuestions": "还没聊完的话题",
    "finalizeStage": "就选这一版",
    "confirmFinal": "就选这一版",
    "confirmQuestion": "这一版，定下来了吗？",
    "confirmBody": "确认后，这轮不能再修改地图或继续聊天。接下来，给另一位玩家留句话。",
    "finalizeHint": "确认后，这轮不能再修改地图或继续聊天。接下来，给另一位玩家留句话。",
    "intentionTitle": "给另一位玩家留句话",
    "completeSession": "保存留言，完成共创",
    "sessionComplete": "这轮创作，收工啦！",
    "sessionCompleteBody": "关卡和留言都已保存。回到原游戏页面，继续接下来的挑战。",
    "demoCompleteBody": "关卡和留言都已保存，这次共创完成了。",
    "chatWaitingPrimary": "回复还在准备中，稍等一下……",
    "chatWaitingProposal": "正在准备修改方案，稍等一下……",
    "chatElapsed": "已等待 {seconds} 秒",
    "thinking": "回复还在准备中，稍等一下……",
    "proposalPlanningPlaceholder": "正在准备修改方案，稍等一下……",
    "draftReconnecting": "连接中断了，正在确认刚才的操作结果。",
    "deadlineExpiredAnnouncement": "时间到了，请提交最终版本。",
    "deadlineExpiredAction": "请提交最终版本",
    "verified": "这一版已确认可解",
    "previousVerified": "上次保存的版本已确认可解",
    "draftUnchecked": "当前修改尚未检查",
    "savedSolution": "已保存版本：{steps} 步 · {pushes} 次推动",
    "solutionSummary": "{steps} 步 · {pushes} 次推动",
    "editMode": "选一种格子，再点地图就能修改。改好后记得存档。",
    "readOnlyMode": "这是以前的存档。想接着改，可以点“接着这一版改”。",
    "lockedMode": "这一轮已结束，地图不能再修改。",
    "proposalUnbound": "这份旧方案现在只能查看，请使用最新方案。",
    "proposalPreconditionFailed": "地图已经变了，这份方案不能直接使用。",
    "errorIntentRequired": "先留句话，再完成这次共创。",
    "contextChanged": "对话已经更新，请根据当前内容重新发送。地图未因这次请求改变。",
    "replyFailed": "这次回复没能完成，请重试。",
    "connectionFailed": "连接中断了，正在确认刚才的操作结果。",
    "modificationFailed": "这次修改没能通过检查，地图未改变。",
    "checkIncomplete": "检查暂时没能完成，还不能确认这张地图是否可解。",
    "oldProposal": "这份方案现在不能使用，请查看最新方案。",
    "noProposal": "暂时没找到合适的修改方案，地图未改变。",
    "requirementsUnclear": "还没能准确理解这次修改要求，请重试原消息。",
    "permissionConflict": "这个方向超出了当前允许修改的范围，请调整一下想法。",
    "unsolved": "暂时没找到这张地图的通关走法，请检查一下地图。",
    "needsDiscussion": "先把当前的不同想法聊清楚，再继续选方案。",
    "noAlternative": "还没找到不同的新方案，当前地图未改变。",
    "busyHint": "正在处理刚才的操作，请稍等。",
    "playActiveHint": "试玩结束后，回到这里继续。",
    "closedHint": "这轮已结束，不能再修改或聊天。",
    "historyHint": "先回到当前 Stage，再继续修改或聊天。",
    "noChangesHint": "地图还没有新改动。",
    "dirtyPlayHint": "先保存或撤销修改，再来试玩。",
    "pendingHint": "先采用或拒绝待确认方案，再继续这项操作。",
    "finalDirtyHint": "先保存或撤销修改，再选定最终版本。",
    "conflictHint": "先选择要保留的想法，再继续聊天。",
    "retryHint": "先重试刚才未完成的消息。",
    "emptyMessageHint": "写下你的想法，再点发送。",
    "noVersionHint": "地图还没有准备好，请稍等。",
    "openingHint": "AI 的回复还在准备中，稍等一下再确认。",
    "saveReason": "存档：{reason}",
    "playReason": "试玩：{reason}",
    "chatReason": "聊天：{reason}",
    "finalReason": "最终版本：{reason}",
    "chatAriaLabel": "和 AI 聊聊地图",
    "workspaceAriaLabel": "地图共创工作台",
    "routeAriaLabel": "查看从第 {fromRow} 行第 {fromColumn} 列到第 {toRow} 行第 {toColumn} 列的路线"
});

Object.assign(GUIDANCE_CUE_LABELS.en, {
    revision: "REVISION · How about these changes?", intent: "TENTATIVE INTENT · Am I getting your idea?",
    warning: "WARNING · Something to watch for", tradeoff: "WARNING · Something to watch for",
    question: "LET’S DISCUSS · Let’s talk this through",
    manual_edit: "MANUAL EDIT · Try tweaking it yourself",
    clarification: "NEEDS CLARIFICATION · Tell me a bit more"
});
Object.assign(GUIDANCE_CUE_LABELS["zh-CN"], {
    revision: "REVISION · 要不要试试这样改？", intent: "TENTATIVE INTENT · 我这样理解，对吗？",
    warning: "WARNING · 这里留意一下", tradeoff: "WARNING · 这里留意一下",
    question: "LET’S DISCUSS · 这里，咱们聊聊",
    manual_edit: "MANUAL EDIT · 也可以自己动手改改",
    clarification: "NEEDS CLARIFICATION · 这里再明确一下"
});

// Help and status labels stay beside the controls they explain.
Object.assign(translations.en, {
    versionHelp: "About Stages", proposalHelp: "How it works", editorTitle: "Make it your own",
    finishTitle: "Ready to wrap up?", proposalModeOff: "For your next message",
    proposalModeOn: "Next: request a plan", proposalTopicActive: "Continuing this proposal"
});
Object.assign(translations["zh-CN"], {
    versionHelp: "Stage 是什么？", proposalHelp: "开关说明", editorTitle: "动手改改",
    finishTitle: "准备定稿？", proposalModeOff: "仅影响下一条消息",
    proposalModeOn: "下一条：请求方案", proposalTopicActive: "继续讨论当前方案"
});

// Presentation-only help. These strings never enter action payloads or model context.
export const UI_ACTION_HELP = {
    setupChineseButton: ["选择本轮界面和对话语言；开始共创后不能更换。", "Choose the language for this session. It cannot be changed after co-creation starts."],
    setupEnglishButton: ["选择本轮界面和对话语言；开始共创后不能更换。", "Choose the language for this session. It cannot be changed after co-creation starts."],
    regenerateDraftButton: ["重新准备一张初始地图。新地图准备成功前，当前预览会保留。", "Prepare another starting map. The current preview stays until the new map is ready."],
    enterSessionButton: ["从眼前这张初始地图开始共创，它会成为 Stage 1。", "Start co-creating with this starting map. It becomes Stage 1."],
    playButton: ["试玩当前选中的已保存版本，结束后回到这里继续。", "Play the selected saved version, then return here to continue."],
    saveStageButton: ["把当前修改存成一个新 Stage，之前保存的版本会保留。", "Save your changes as a new Stage. Earlier saved versions are kept."],
    discardDraftButton: ["撤销尚未保存的地图修改，回到当前选中版本的地图。", "Discard unsaved map changes and return to the selected saved map."],
    restoreStageButton: ["以这张历史地图创建新 Stage，接着修改；历史版本本身不会被覆盖。", "Create a new Stage from this earlier map and continue editing. The earlier version is kept."],
    finalizeButton: ["选定当前查看的版本作为最终版本。确认后，本轮编辑和聊天结束，接着填写留言。", "Choose the version you are viewing as the final version. Confirmation ends editing and chat; your message comes next."],
    confirmFinalizeButton: ["确认最终版本。本轮将不能继续改图或聊天，下一步填写给对方的留言。", "Confirm the final version. Editing and chat will close; next, write your message to the other player."],
    cancelFinalizeButton: ["先不定稿，回到地图继续看看。", "Leave the final choice open and return to the map."],
    sendButton: ["发送输入框里的消息。若方案开关已打开，这条消息会申请修改方案。", "Send your message. If proposal mode is on, this message requests a modification plan."],
    proposalRequestButton: ["切换下一条消息是否申请修改方案。打开开关不会立即发送，也不会直接改图。", "Choose whether your next message requests a plan. Switching this on does not send anything or change the map."],
    returnCurrentButton: ["回到最新的 Stage，继续当前对话和编辑。", "Return to the latest Stage to continue the current conversation and editing."],
    returnUnityButton: ["回到原游戏页面，继续接下来的匹配流程。", "Return to the original game page to continue the match."],
    retryButton: ["重试提示中尚未完成的操作，沿用该操作已保存的内容。", "Retry the unfinished action using its captured content."],
    chatRetryButton: ["再试一次刚才未完成的消息，保留原来的文字和方案开关设置。", "Retry the unfinished message with the same text and proposal setting."],
    completeSession: ["保存你写下的留言，完成这一轮共创。", "Save your message and finish this co-creation session."],
    draftSuggestedRevision: ["生成这份修改建议的地图预览。预览出来后，还需要你明确采用才会创建新 Stage。", "Prepare the proposed map preview. A new Stage is created only after you explicitly accept the pending proposal."],
    challengeRevision: ["告诉 AI 你对这个方案有不同想法，接下来通过对话说明原因。", "Tell AI you see this plan differently, then explain your concerns in the conversation."],
    alternativeRevision: ["请 AI 另提一个不同方案。不会直接替换当前地图。", "Ask AI for a different proposal. This does not directly replace the current map."],
    accept: ["采用待确认的地图方案，并保存为新的 Stage。", "Accept the pending map proposal and save it as a new Stage."],
    reject: ["不采用这份待确认方案，保留当前地图。", "Reject the pending proposal and keep the current map."],
    intentMatches: ["反馈这个理解符合你的想法。这是对理解的反馈，不是改图授权。", "Say this interpretation matches your idea. This feedback does not authorize a map edit."],
    intentReject: ["反馈这个理解不是你的意思，不会因此修改地图。", "Say this interpretation is not what you meant. This does not change the map."],
    intentAdjust: ["打开文字框，把你的想法说明得更准确。", "Open the text field to explain your idea more precisely."],
    intentModify: ["修改这条已记录的设计想法，再提交确认。", "Edit this recorded design idea and submit the adjustment."],
    intentSave: ["提交你调整后的想法。不会因此自动修改地图。", "Submit your adjusted idea. This does not automatically edit the map."],
    intentCancel: ["关闭这次文字调整，不提交本次修改。", "Close this text adjustment without submitting it."],
    intentKeepThis: ["选择保留这张卡里的设计想法。不会因此改图。", "Choose to keep the idea on this card. This does not edit the map."],
    ignoreQuestion: ["把这个问题移到“已处理”，之后不再带入对话；需要时可以手动恢复。", "Move this question to Processed and leave it out of later conversation. You can restore it manually."],
    restoreQuestion: ["把这个问题恢复到还没聊完的话题中。", "Restore this question to the open questions."],
    challengeChoice: ["回答上方讨论中的问题。这个选择本身不会创建新 Stage。", "Answer the question in the discussion above. This choice does not itself create a new Stage."],
};
export const UI_TILE_HELP = {
    ".": ["可以走动的地面。", "Walkable floor."],
    "#": ["挡住通路的墙。", "A wall that blocks passage."],
    "@": ["不能直接走过的水域。", "Water that the player cannot walk through."],
    p: ["试玩时玩家的起点。", "The player’s starting position during play."],
    s: ["需要推动到目标上的箱子。B1、B2 用于区分箱子。", "A box to push onto a target. B1 and B2 distinguish the boxes."],
    t: ["箱子需要到达的目标位置。T1、T2 用于区分目标。", "A target position for a box. T1 and T2 distinguish the targets."],
    " ": ["地图外围的空白，不是可行走的地面。", "Empty space outside the map, not walkable floor."],
};
Object.assign(translations.en, {
    editorHelpTitle: "Editing help", editorHelpBody: "Hover over a tool or tile to see what it means. Select a tool, then click a map tile to replace it. Save changes to create a new Stage.",
    progressHelpBody: "Revisit open questions and your confirmed ideas here. Open Processed to revisit earlier questions.",
});
Object.assign(translations["zh-CN"], {
    editorHelpTitle: "编辑说明", editorHelpBody: "悬停在工具或格子上查看说明。先选工具，再点击地图格子进行替换；改好后存档，才会成为新 Stage。",
    progressHelpBody: "这里收着还没聊完的问题，以及你确认过的点子。展开“已处理”可回看之前的问题。",
});
export function createI18n(ctx) {
    const {
        state,
        elements,
        window,
        document,
        sessionStorage,
        setTimeout,
        clearTimeout,
        ResizeObserver,
        getComputedStyle,
        ports,
    } = ctx;

    const replyHttpAttempts = new Map();
    let replyDiagnostics = [];
    try {
        const saved = JSON.parse(sessionStorage?.getItem(REPLY_DIAGNOSTICS_KEY) || "[]");
        if (Array.isArray(saved)) replyDiagnostics = saved.slice(-200).map(safeReplyDiagnostic);
    } catch (_) { /* Diagnostics never block the workbench. */ }

    function recordReplyDiagnostic(stage, status, fields = {}) {
        try {
            const item = safeReplyDiagnostic({ stage, status, frontendVersion: REPLY_PIPELINE_VERSION, at: Date.now(), ...fields });
            replyDiagnostics.push(item);
            replyDiagnostics = replyDiagnostics.slice(-200);
            sessionStorage?.setItem(REPLY_DIAGNOSTICS_KEY, JSON.stringify(replyDiagnostics));
        } catch (_) { /* Storage and diagnostics have no effect on delivery. */ }
    }

    function turnReplyIdentity(turn) {
        const known = replyHttpAttempts.get(turn.requestId)
            || [...replyDiagnostics].reverse().find(item => item.messageKey === turn.requestId && item.requestId) || {};
        return { requestId: known.requestId, pipelineVersion: known.pipelineVersion,
            messageKey: turn.requestId, versionId: turn.versionId, assistantTurnId: turn.turnId };
    }

    const uiHelpView = { target: null, pendingTarget: null, timer: null, previousDescription: null };
    const tileName = (...args) => ports.tileName(...args);
    const deadlineExpired = (...args) => ports.deadlineExpired(...args);
    const selectedStageHasActiveDisagreement = (...args) => ports.selectedStageHasActiveDisagreement(...args);
    const proposalFlowActive = (...args) => ports.proposalFlowActive(...args);
    const updateCharacterCount = (...args) => ports.updateCharacterCount(...args);
    const renderChatRequestStatus = (...args) => ports.renderChatRequestStatus(...args);
    const renderTranslationStatus = (...args) => ports.renderTranslationStatus(...args);
    const renderDraftPreview = (...args) => ports.renderDraftPreview(...args);

    function t(key) { return translations[state.language][key] || translations.en[key] || key; }
    function helpLocale(pair) { return pair?.[state.language === "zh-CN" ? 0 : 1] || ""; }

    function markTileHelp(node, tile, x, y, entity, kind) {
        node.dataset.uiHelpKind = kind;
        node.dataset.uiHelpTile = tile;
        node.dataset.uiHelpRow = String(y + 1);
        node.dataset.uiHelpColumn = String(x + 1);
        if (entity) node.dataset.uiHelpEntity = entity;
    }

    function uiHelpFor(target) {
        const zh = state.language === "zh-CN";
        const kind = target.dataset.uiHelpKind;
        if (kind === "palette" || kind === "map" || kind === "preview") {
            const tile = target.dataset.uiHelpTile;
            const title = [target.dataset.uiHelpEntity, t(tileName(tile))].filter(Boolean).join(" · ");
            const coordinates = kind === "palette" ? "" : zh
                ? `第 ${target.dataset.uiHelpRow} 行，第 ${target.dataset.uiHelpColumn} 列。`
                : `Row ${target.dataset.uiHelpRow}, column ${target.dataset.uiHelpColumn}. `;
            const stagePreview = kind === "preview" && target.closest?.(".stage-card");
            const instruction = kind === "palette" ? helpLocale(["选中这个工具后，再点击地图放置。改动需要另行保存。", "Select this tool, then click the map to place it. Save your changes separately."])
                : stagePreview ? helpLocale(["点击缩略图可查看这一版的地图、对话和试玩记录。", "Click the thumbnail to view this version’s map, conversation, and play history."])
                    + (state.dirty ? helpLocale([" 切换会放弃未保存的修改，请先存档。", " Switching discards unsaved changes, so save them first."]) : "")
                : kind === "preview" ? helpLocale(["这是预览中的格子，只供查看。", "This is a preview tile, for viewing only."])
                : target.disabled ? helpLocale(["这一格当前不能编辑。", "This tile cannot be edited right now."])
                : helpLocale([`点击会用当前选中的“${t(tileName(state.selectedTile))}”工具替换这一格。`, `Click to replace this tile with the selected ${t(tileName(state.selectedTile))} tool.`]);
            return { title, body: coordinates + helpLocale(UI_TILE_HELP[tile]) + " " + instruction,
                status: target.disabled ? uiHelpBlockedStatus(target, kind) : "" };
        }
        if (kind === "stage") return { title: `Stage ${target.dataset.uiHelpStage}`,
            body: helpLocale(["查看这一版的地图、对话和试玩记录。", "View this version’s map, conversation, and play history."])
                + (state.dirty ? helpLocale([" 切换会放弃未保存的修改，请先存档。", " Switching discards unsaved changes, so save them first."]) : ""),
            status: target.disabled ? uiHelpBlockedStatus(target, kind) : "" };
        if (target.dataset.coordinateLinkIndex != null) return { title: helpLocale(["查看路线", "View route"]),
            body: helpLocale(["在地图上显示这段文字对应的路线，再点一次收起。", "Show the route described by this text on the map. Click again to hide it."]), status: "" };
        const key = target.dataset.uiHelpKey || target.id || (target.dataset.i18n === "completeSession" ? "completeSession" : "");
        const pair = UI_ACTION_HELP[key];
        if (!pair) return null;
        const labelKey = ({ saveStageButton: "saveStage", discardDraftButton: "discard", playButton: "playStage", restoreStageButton: "continueFromStage", finalizeButton: "finalizeStage", confirmFinalizeButton: "confirmFinal", cancelFinalizeButton: "cancel", sendButton: "send", proposalRequestButton: "requestProposal", enterSessionButton: "enterSession", regenerateDraftButton: "regenerateDraft", returnUnityButton: "returnUnity", returnCurrentButton: "returnCurrent", retryButton: "retry", chatRetryButton: "retry", setupChineseButton: "sessionLanguage", setupEnglishButton: "sessionLanguage" })[key] || key;
        return { title: translations[state.language]?.[labelKey] || target.textContent.trim(), body: helpLocale(pair),
            status: target.disabled || target.getAttribute("aria-disabled") === "true" ? uiHelpBlockedStatus(target, key) : "" };
    }

    function uiHelpBlockedStatus(target, key) {
        if (target.dataset.uiHelpStatus) return target.dataset.uiHelpStatus;
        if (deadlineExpired()) return t("deadlineExpiredAnnouncement");
        if (state.unityPlayActive) return t("playActiveHint");
        if (state.busy) return t("busyHint");
        const hint = ({ playButton: elements.playControlHint, saveStageButton: elements.mapControlHint,
            sendButton: elements.chatControlHint, finalizeButton: elements.finalizeControlHint })[key];
        if (hint?.textContent) return hint.textContent;
        if (key === "saveStageButton" && !state.dirty) return t("noChangesHint");
        if (key === "discardDraftButton" && !state.dirty) return t("noChangesHint");
        if (key === "sendButton" && !elements.messageInput.value.trim()) return t("emptyMessageHint");
        if (key === "proposalRequestButton" && (selectedStageHasActiveDisagreement() || proposalFlowActive())) return t("needsDiscussion");
        if (state.session?.status && state.session.status !== "active") return t("closedHint");
        if (["map", "palette"].includes(key) && state.selectedVersionId !== state.session?.currentVersionId) return t("historyHint");
        return helpLocale(["当前暂不可用。", "Currently unavailable."]);
    }

    function uiHelpTarget(node) {
        if (!node?.closest) return null;
        return node.closest("[data-ui-help-kind], [data-ui-help-key], button, [data-coordinate-link-index]");
    }

    function hideUiHelp() {
        if (uiHelpView.timer != null) clearTimeout(uiHelpView.timer);
        uiHelpView.timer = null;
        uiHelpView.pendingTarget = null;
        const node = uiHelpView.target;
        if (node) {
            if (uiHelpView.previousDescription == null) node.removeAttribute("aria-describedby");
            else node.setAttribute("aria-describedby", uiHelpView.previousDescription);
        }
        uiHelpView.target = null;
        uiHelpView.previousDescription = null;
        const tooltip = document.getElementById("uiTooltip");
        if (tooltip) tooltip.hidden = true;
    }

    function showUiHelp(target) {
        if (!target?.isConnected) return;
        const help = uiHelpFor(target);
        if (!help) return;
        hideUiHelp();
        const tooltip = document.getElementById("uiTooltip");
        const title = document.getElementById("uiTooltipTitle");
        const body = document.getElementById("uiTooltipBody");
        const status = document.getElementById("uiTooltipStatus");
        title.textContent = help.title;
        body.textContent = help.body;
        status.textContent = help.status;
        status.hidden = !help.status;
        tooltip.hidden = false;
        uiHelpView.target = target;
        uiHelpView.previousDescription = target.getAttribute("aria-describedby");
        target.setAttribute("aria-describedby", [uiHelpView.previousDescription, "uiTooltip"].filter(Boolean).join(" "));
        const rect = target.getBoundingClientRect();
        const box = tooltip.getBoundingClientRect();
        const left = Math.max(8, Math.min(rect.left + rect.width / 2 - box.width / 2, window.innerWidth - box.width - 8));
        const above = rect.top >= box.height + 16;
        const top = above ? rect.top - box.height - 10 : Math.min(rect.bottom + 10, window.innerHeight - box.height - 8);
        tooltip.style.left = `${left}px`;
        tooltip.style.top = `${Math.max(8, top)}px`;
        tooltip.dataset.placement = above ? "above" : "below";
        tooltip.style.setProperty("--help-anchor-x", `${Math.max(14, Math.min(rect.left + rect.width / 2 - left, box.width - 14))}px`);
    }

    function queueUiHelp(target, immediate = false) {
        if (target === uiHelpView.target || target === uiHelpView.pendingTarget) return;
        hideUiHelp();
        if (!target || !uiHelpFor(target)) return;
        uiHelpView.pendingTarget = target;
        uiHelpView.timer = setTimeout(() => showUiHelp(target), immediate ? 0 : 360);
    }

    function initializeUiHelp() {
        document.addEventListener("pointerover", event => {
            if (event.pointerType !== "touch") queueUiHelp(uiHelpTarget(event.target));
        });
        document.addEventListener("pointerout", event => {
            const target = uiHelpTarget(event.target);
            if (target && !target.contains(event.relatedTarget)) hideUiHelp();
        });
        document.addEventListener("focusin", event => queueUiHelp(uiHelpTarget(event.target), true));
        document.addEventListener("focusout", hideUiHelp);
        document.addEventListener("pointerdown", event => {
            hideUiHelp();
            document.querySelectorAll(".contextual-help[open]").forEach(details => {
                if (!details.contains(event.target)) details.open = false;
            });
        });
        document.addEventListener("keydown", event => {
            if (event.key === "Escape") {
                hideUiHelp();
                document.querySelectorAll(".contextual-help[open]").forEach(details => details.open = false);
            }
        });
        window.addEventListener("scroll", hideUiHelp, true);
        window.addEventListener("resize", hideUiHelp);
    }

    // Keep peer workspace headers aligned, including translated titles and blocked-play hints.
    function initializeWorkspaceHeaderAlignment() {
        if (!ResizeObserver) return;
        const headers = [document.querySelector?.(".chat-panel > .panel-header"), document.querySelector?.(".map-panel > .panel-header")];
        if (headers.some(header => !header)) return;
        const synchronize = () => {
            if (headers.some(header => !header.getClientRects().length)) return;
            const height = Math.max(72, ...headers.map(header => {
                const style = getComputedStyle(header);
                const edges = [style.paddingTop, style.paddingBottom, style.borderTopWidth, style.borderBottomWidth]
                    .reduce((sum, value) => sum + (parseFloat(value) || 0), 0);
                return Math.max(0, ...Array.from(header.children, child => child.getBoundingClientRect().height)) + edges;
            }));
            headers.forEach(header => header.style.minHeight = `${height}px`);
        };
        const observer = new ResizeObserver(synchronize);
        headers.forEach(header => Array.from(header.children).forEach(child => observer.observe(child)));
        synchronize();
    }

    function applyTranslations() {
        hideUiHelp();
        if (document.documentElement) document.documentElement.lang = state.language;
        document.querySelectorAll("[data-i18n]").forEach(element => element.textContent = t(element.dataset.i18n));
        document.querySelectorAll("[data-i18n-placeholder]").forEach(element => element.placeholder = t(element.dataset.i18nPlaceholder));
        document.querySelectorAll("[data-i18n-title]").forEach(element => element.title = t(element.dataset.i18nTitle));
        document.querySelectorAll("[data-i18n-aria-label]").forEach(element => element.setAttribute("aria-label", t(element.dataset.i18nAriaLabel)));
        elements.setupChineseButton.setAttribute("aria-pressed", String(state.language === "zh-CN"));
        elements.setupEnglishButton.setAttribute("aria-pressed", String(state.language === "en"));
        updateCharacterCount();
        renderChatRequestStatus();
        renderTranslationStatus();
        if (!elements.landing.hidden && state.session && !state.session.languageLocked) {
            renderDraftPreview();
        }
    }

    return {
        replyHttpAttempts,
        getReplyDiagnostics: () => replyDiagnostics,
        recordReplyDiagnostic,
        turnReplyIdentity,
        helpLocale,
        markTileHelp,
        uiHelpFor,
        uiHelpBlockedStatus,
        uiHelpTarget,
        hideUiHelp,
        showUiHelp,
        queueUiHelp,
        initializeUiHelp,
        initializeWorkspaceHeaderAlignment,
        applyTranslations,
        t,
    };
}
