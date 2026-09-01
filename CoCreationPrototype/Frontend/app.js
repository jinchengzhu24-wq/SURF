"use strict";

const MAX_MESSAGE_LENGTH = 2000;
const SESSION_STORAGE_KEY = "sokobanCoCreationSession";
const API_PREFIX = window.location.pathname.startsWith("/cocreation")
    ? "/cocreation"
    : "";
const TILE_ORDER = [".", "#", "@", "p", "s", "t", " "];
const DISCUSSION_FOCUS_LABEL = "LET'S DISCUSS / 一起聊聊";
const GUIDANCE_CUE_LABELS = {
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

const translations = {
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
        demoGenerationStatus: "Creating a sample map with the algorithm…",
        versionHistory: "Version history",
        stages: "Stages",
        conversation: "Co-creation conversation",
        discuss: "Discuss and refine this Stage",
        viewingHistory: "You are viewing this historical Stage and its own conversation. Return to the current Stage to continue chatting.",
        returnCurrent: "Return to current",
        assessmentPending: "Preparing the first Stage assessment",
        assessmentPendingBody: "The assistant will discuss what stands out in the verified current map.",
        noStageConversation: "No conversation has been recorded for this Stage.",
        noStageConversationBody: "Each Stage keeps only the discussion attached to that saved version.",
        thinking: "Assistant is considering the current Stage...",
        chatWaitingPrimary: "Waiting for the primary assistant",
        chatWaitingFallback: "The primary assistant was slow; trying the fallback assistant",
        chatRetryPending: "The previous message did not finish. Retry it without creating a duplicate.",
        messageLabel: "Message the level design assistant",
        messagePlaceholder: "Explain what you want to change or ask about the level...",
        send: "Send",
        sending: "Sending...",
        sendHint: "Enter to send · Shift+Enter for a new line",
        currentLevel: "Current level",
        playStage: "Play this Stage",
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
        alternativeRevision: "Try another plan",
        staleRevisionCard: "Only the latest revision card can be acted on.",
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
        error_MODEL_EMPTY_RESPONSE: "The latest model attempt returned blank content, and no earlier attempt produced a valid result. Retry without creating a duplicate.",
        error_INVALID_MESSAGE_ACTION: "That card action is invalid. Refresh and try again.",
        error_INVALID_CARD_SOURCE: "That revision card no longer belongs to the current Stage. Refresh and choose the current card.",
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
        demoGenerationStatus: "正在使用算法创建示例地图……",
        versionHistory: "版本历史",
        stages: "Stages",
        conversation: "共创对话",
        discuss: "讨论并继续完善当前 Stage",
        viewingHistory: "你正在查看这个历史 Stage 及其相关对话；返回当前 Stage 后才能继续聊天。",
        returnCurrent: "返回当前版本",
        assessmentPending: "正在准备首版评价",
        assessmentPendingBody: "助手会围绕已验证的当前地图中值得关注的部分展开讨论。",
        noStageConversation: "这个 Stage 暂时没有相关对话。",
        noStageConversationBody: "每个 Stage 只显示与该已保存版本关联的讨论。",
        thinking: "助手正在分析当前 Stage……",
        chatWaitingPrimary: "正在等待首选模型生成回复",
        chatWaitingFallback: "首选模型响应较慢，正在尝试备用模型",
        chatRetryPending: "上一条消息尚未完成，可安全重试且不会产生重复记录。",
        messageLabel: "给关卡设计助手发送消息",
        messagePlaceholder: "说明你想修改什么，或询问这个关卡的设计……",
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

const state = {
    session: null,
    sessionId: "",
    selectedVersionId: "",
    draftRows: [],
    dirty: false,
    validationError: null,
    selectedTile: ".",
    busy: false,
    chatBusy: false,
    chatStatus: "idle",
    chatError: null,
    chatStartedAt: 0,
    chatTimerId: null,
    deadlineTimerId: null,
    pendingMessage: null,
    assessing: new Set(),
    translating: new Set(),
    translationFailures: new Set(),
    translationInProgress: false,
    translationRemainingCount: 0,
    retryAction: null,
    activeCoordinateLink: null,
    language: "zh-CN"
};

const chineseApiErrors = {
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
    MODEL_EMPTY_RESPONSE: "最后一次模型尝试返回了空白内容，且此前尝试也未产生有效结果；可使用原消息安全重试，不会产生重复记录。",
    CLIENT_TIMEOUT: "助手未能在浏览器安全时限内完成。可直接重试，且不会产生重复消息。",
    CONFIGURATION_ERROR: "服务器尚未正确配置 LLM 服务。",
    INVALID_MESSAGE_ACTION: "卡片操作无效，请刷新后重试。",
    INVALID_CARD_SOURCE: "这张修改建议无效、已过期或不再对应当前 Stage，请重新查看最新方案。",
    DISAGREEMENT_ACTIVE: "当前仍有未解决的分歧，请先继续协商后再选择修改方案。"
};

const LEVEL_VALIDATION_ERROR_CODES = new Set([
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

const validationTileNames = {
    " ": { en: "outer void", zh: "外围空白" },
    "#": { en: "wall (#)", zh: "墙（#）" },
    ".": { en: "floor (.)", zh: "地面（.）" },
    "@": { en: "water (@)", zh: "水域（@）" },
    p: { en: "player (p)", zh: "玩家（p）" },
    s: { en: "box (s)", zh: "箱子（s）" },
    t: { en: "target (t)", zh: "目标（t）" }
};

const elements = Object.fromEntries([
    "workspace", "landing", "notice", "noticeMessage", "retryButton", "prototypeStatus", "deadlineStatus",
    "languageButton", "demoButton", "demoGenerationStatus", "stageList", "stageCount", "methodPill", "historyBanner",
    "returnCurrentButton", "chatScroll", "emptyChat", "messageList", "translationStatus", "typingRow", "proposalArea",
    "chatRequestStatus", "chatRequestMessage", "chatRetryButton", "chatForm", "messageInput",
    "sendButton", "characterCount", "selectedStageEyebrow", "mapFrame", "mapBoard", "mapGrid", "mapOverlay",
    "mapToolbar", "mapMode", "validationCard", "saveStageButton", "discardDraftButton",
    "restoreStageButton", "playButton", "playAttemptCount", "playAttemptList", "finalActions",
    "finalizeButton", "intentionForm", "intentionInput", "completeCard", "returnUnityButton", "finalizeModal",
    "cancelFinalizeButton", "confirmFinalizeButton"
].map(id => [id, document.getElementById(id)]));

elements.languageButton.addEventListener("click", toggleLanguage);
elements.demoButton.addEventListener("click", createDemoSession);
elements.retryButton.addEventListener("click", () => state.retryAction && state.retryAction());
elements.chatRetryButton.addEventListener("click", retryPendingMessage);
elements.returnCurrentButton.addEventListener("click", selectCurrentVersion);
elements.chatForm.addEventListener("submit", sendMessage);
elements.messageInput.addEventListener("input", handleComposerInput);
elements.messageInput.addEventListener("keydown", handleComposerKeydown);
elements.saveStageButton.addEventListener("click", saveManualStage);
elements.discardDraftButton.addEventListener("click", discardDraft);
elements.restoreStageButton.addEventListener("click", restoreSelectedStage);
elements.playButton.addEventListener("click", playSelectedStage);
elements.finalizeButton.addEventListener("click", () => {
    if (deadlineExpired()) void finalizeSession();
    else elements.finalizeModal.hidden = false;
});
elements.cancelFinalizeButton.addEventListener("click", () => elements.finalizeModal.hidden = true);
elements.confirmFinalizeButton.addEventListener("click", finalizeSession);
elements.intentionForm.addEventListener("submit", submitIntention);
elements.returnUnityButton.addEventListener("click", returnToUnity);
window.addEventListener("resize", () => {
    if (state.activeCoordinateLink) requestAnimationFrame(drawActiveCoordinateRoute);
});

applyTranslations();
initialize();

async function initialize() {
    const hash = readHash();
    state.sessionId = hash.session || "";

    if (!state.sessionId) {
        showLanding();
        return;
    }

    try {
        if (hash.bootstrap) {
            await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/browser-access`, {
                method: "POST",
                body: { bootstrapToken: hash.bootstrap }
            });
            await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/language`, {
                method: "PATCH",
                body: { language: "zh-CN" }
            });
        }

        localStorage.setItem(SESSION_STORAGE_KEY, state.sessionId);
        state.selectedVersionId = hash.stage || localStorage.getItem(selectedStageKey()) || "";
        await refreshSession();
        restoreComposerDraft();
        recoverPendingMessage();
        showPlayReturnNotice(hash.playReturn);
    } catch (error) {
        showLanding();
        showError(error, initialize);
    }
}

async function createDemoSession() {
    const showGenerationStatus = () => {
        elements.demoGenerationStatus.hidden = false;
        elements.demoGenerationStatus.textContent = t("demoGenerationStatus");
    };
    const hideGenerationStatus = () => {
        elements.demoGenerationStatus.hidden = true;
        elements.demoGenerationStatus.textContent = "";
    };
    showGenerationStatus();
    await withBusy(async () => {
        showGenerationStatus();
        const created = await api("/api/demo-sessions", {
            method: "POST",
            body: {
                language: state.language,
                idempotencyKey: uniqueId("demo")
            }
        });
        await openCreatedSession(created.launchUrl);
    }, hideGenerationStatus);
}

async function openCreatedSession(launchUrl) {
    const destination = new URL(launchUrl, window.location.href);
    if (destination.origin !== window.location.origin) {
        window.location.assign(destination.href);
        return;
    }

    // A hash-only navigation does not reload this SPA. Update the address and
    // initialize the new session explicitly so the map appears immediately.
    window.history.replaceState(
        null,
        "",
        `${destination.pathname}${destination.search}${destination.hash}`
    );
    await initialize();
}

async function refreshSession() {
    state.session = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}`);
    state.language = state.session.language;

    if (!findVersion(state.selectedVersionId)) {
        state.selectedVersionId = state.session.currentVersionId;
    }

    localStorage.setItem(selectedStageKey(), state.selectedVersionId);
    syncHash();
    state.activeCoordinateLink = null;
    resetDraftFromSelection();
    render();
    void ensureVisibleTranslations();
    await ensureAssessment(state.session.currentVersionId);
}

function render() {
    elements.landing.hidden = true;
    elements.workspace.hidden = false;
    setStatus(t("ready"), "ready");
    applyTranslations();
    elements.methodPill.textContent = t(state.session.initialDraftMethod);
    renderStages();
    renderDeadline();
    renderMessages();
    renderProposal();
    renderMap();
    renderSessionState();
    renderChatRequestStatus();
    renderTranslationStatus();
    updateControls();
}

function renderStages() {
    elements.stageList.textContent = "";
    elements.stageCount.textContent = String(state.session.versions.length);

    state.session.versions.slice().reverse().forEach(version => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "stage-card";
        button.disabled = deadlineExpired() || state.translationInProgress;
        if (version.versionId === state.selectedVersionId) button.classList.add("selected");
        if (version.versionId === state.session.currentVersionId) button.classList.add("current");
        button.addEventListener("click", () => selectVersion(version.versionId));

        const top = document.createElement("div");
        top.className = "stage-card-top";
        top.innerHTML = `<strong>${escapeHtml(t("stage"))} ${version.stageNumber}</strong><span>${version.versionId === state.session.currentVersionId ? escapeHtml(t("current")) : ""}</span>`;
        const mini = createMiniMap(version.rows, version.diff);
        const meta = document.createElement("div");
        meta.className = "stage-meta";
        meta.innerHTML = `<span>${escapeHtml(t(version.source))}</span><span>✓ ${escapeHtml(t("verified"))}</span>`;
        const evidence = document.createElement("small");
        const latest = version.playAttempts[0];
        evidence.textContent = latest ? formatAttempt(latest) : t("noPlayEvidence");
        button.append(top, mini, meta, evidence);
        elements.stageList.appendChild(button);
    });
}

function renderMessages() {
    elements.messageList.textContent = "";
    const turns = selectedStageTurns();
    elements.emptyChat.hidden = turns.length > 0;

    if (turns.length === 0) {
        const historical = state.selectedVersionId !== state.session.currentVersionId;
        elements.emptyChat.querySelector("h3").textContent = t(
            historical ? "noStageConversation" : "assessmentPending"
        );
        elements.emptyChat.querySelector("p").textContent = t(
            historical ? "noStageConversationBody" : "assessmentPendingBody"
        );
    }

    turns.forEach(turn => {
        const row = document.createElement("div");
        row.className = `message-row ${turn.role}`;
        if (turn.role === "assistant") {
            const avatar = document.createElement("div");
            avatar.className = "message-avatar";
            avatar.textContent = "AI";
            row.appendChild(avatar);
        }
        const content = document.createElement("div");
        content.className = "message-content";
        const bubble = document.createElement("div");
        bubble.className = "message-bubble";
        if (turn.role === "assistant") {
            renderAssistantBubble(turn, bubble);
        } else {
            bubble.textContent = turn.content;
        }
        content.appendChild(bubble);
        row.appendChild(content);
        elements.messageList.appendChild(row);
    });
    requestAnimationFrame(() => elements.chatScroll.scrollTop = elements.chatScroll.scrollHeight);
}

function renderAssistantBubble(turn, bubble) {
    const localized = localizedAssistantTurn(turn);
    const proposal = proposalForTurn(turn.turnId);
    const guidance = guidanceForDisplay(localized.guidance || {}, proposal);
    const question = String(guidance.followUpQuestion || "").trim();
    const uiCues = Array.isArray(guidance.uiCues) ? guidance.uiCues : [];
    const activeDisagreement = guidance.disagreement?.status === "active"
        ? guidance.disagreement
        : null;
    const bodyNode = document.createElement("div");
    bodyNode.className = "message-body";
    const body = assistantBodyWithoutCues(localized.content, uiCues, question);
    renderAssistantBody(bodyNode, body, guidance.coordinateLinks, turn);
    bubble.appendChild(bodyNode);

    if (localized !== turn) {
        const translatedLabel = document.createElement("small");
        translatedLabel.className = "translation-label";
        translatedLabel.textContent = t("translatedDisplay");
        bubble.appendChild(translatedLabel);
    } else if (
        turn.language !== state.language
        && state.translationFailures.has(`${turn.turnId}:${state.language}`)
    ) {
        const unavailableLabel = document.createElement("small");
        unavailableLabel.className = "translation-label translation-unavailable";
        unavailableLabel.textContent = t("translationUnavailable");
        bubble.appendChild(unavailableLabel);
    }

    const cueList = document.createElement("div");
    cueList.className = "guidance-cues";

    const offer = guidance.proposalOffer;

    if (offer && offer.summary) {
        const revisionCue = createGuidanceCue(
            "revision",
            offer.summary,
            offer.rationale || ""
        );
        if (canEditSelected() && !selectedStageHasActiveDisagreement()) {
            const actionable = isLatestRevisionOfferTurn(turn);
            [
                ["execute_revision", "draftSuggestedRevision"],
                ["challenge_revision", "challengeRevision"],
                ["alternative_revision", "alternativeRevision"]
            ].forEach(([action, labelKey]) => {
                revisionCue.appendChild(makeButton(
                    t(labelKey),
                    `secondary-button guidance-cue-button${actionable ? "" : " guidance-cue-button-stale"}`,
                    actionable
                        ? () => sendRevisionCardAction(action, turn, offer)
                        : null,
                    {
                        disabled: !actionable,
                        title: actionable ? "" : t("staleRevisionCard")
                    }
                ));
            });
            if (!actionable) {
                const staleNote = document.createElement("small");
                staleNote.className = "guidance-cue-stale-note";
                staleNote.textContent = t("staleRevisionCard");
                revisionCue.appendChild(staleNote);
            }
        }
        cueList.appendChild(revisionCue);
    } else if (proposal && proposal.summary) {
        cueList.appendChild(createGuidanceCue(
            "revision",
            localizedProposalSummary(proposal)
        ));
    } else if (guidance.intentHypothesis) {
        cueList.appendChild(createGuidanceCue("intent", guidance.intentHypothesis));
    }

    uiCues.forEach(cue => {
        if (cue && ["manual_edit", "warning", "tradeoff"].includes(cue.type) && cue.text) {
            const displayType = cue.type === "tradeoff" ? "warning" : cue.type;
            cueList.appendChild(createGuidanceCue(displayType, cue.text));
        }
    });

    if (cueList.childElementCount) bubble.appendChild(cueList);
    if (activeDisagreement) {
        bubble.appendChild(createDisagreementCard(activeDisagreement));
    } else if (question && !guidance.discussionCardMode) {
        // Turns written before the structured disagreement field retain their
        // historical blue card. New ordinary questions stay in the body.
        // Legacy render contract: if (question) bubble.appendChild(createDiscussionFocus(question));
        bubble.appendChild(createDiscussionFocus(question));
    }
}

function selectedStageHasActiveDisagreement() {
    return selectedStageTurns().some(turn =>
        turn.role === "assistant" && turn.guidance?.disagreement?.status === "active"
    );
}

function isRevisionOfferTurn(turn) {
    const offer = turn?.guidance?.proposalOffer;
    return Boolean(turn?.role === "assistant"
        && offer
        && typeof offer === "object"
        && String(offer.summary || "").trim());
}

function latestRevisionOfferTurn() {
    return selectedStageTurns().slice().reverse().find(isRevisionOfferTurn) || null;
}

function isLatestRevisionOfferTurn(turn) {
    return latestRevisionOfferTurn()?.turnId === turn?.turnId;
}

function guidanceForDisplay(source, proposal = null) {
    const guidance = { ...source };
    const cues = Array.isArray(source.uiCues) ? source.uiCues.filter(Boolean) : [];
    const warning = cues.find(cue => ["warning", "tradeoff"].includes(cue.type) && cue.text);
    let manual = cues.find(cue => cue.type === "manual_edit" && cue.text);

    if (guidance.proposalOffer || proposal) {
        manual ||= {
            type: "manual_edit",
            text: proposalCompanionManualText(Boolean(proposal))
        };
        guidance.intentHypothesis = null;
        guidance.followUpQuestion = null;
        guidance.uiCues = [manual, warning].filter(Boolean);
        return guidance;
    }
    if (manual) {
        guidance.intentHypothesis = null;
        guidance.followUpQuestion = null;
        guidance.uiCues = [manual];
        return guidance;
    }
    guidance.uiCues = warning ? [warning] : [];
    return guidance;
}

function proposalCompanionManualText(pendingProposal) {
    if (state.language === "zh-CN") {
        return pendingProposal
            ? "这份地图还只是待确认的方案。如果你想自己调整，请先拒绝它，再从右侧编辑器接着改；这样不会把两套改动混在一起。"
            : "如果这个方向接近你的想法，也可以先在右侧编辑器围绕同一区域做个小范围尝试，再比较哪种处理更像你想要的体验。";
    }
    return pendingProposal
        ? "This map is still a pending proposal. If you would rather adjust it yourself, reject it first and continue in the editor so the two sets of changes do not get mixed together."
        : "If this direction feels close to what you mean, you can also try a small edit in the same area and compare which version better matches the experience you want.";
}

function createDiscussionFocus(text) {
    const cue = document.createElement("section");
    cue.className = "discussion-focus";
    const label = document.createElement("span");
    label.className = "discussion-focus-label";
    label.textContent = DISCUSSION_FOCUS_LABEL;
    const message = document.createElement("strong");
    message.textContent = text;
    cue.append(label, message);
    return cue;
}

function createDisagreementCard(disagreement) {
    const cue = document.createElement("section");
    cue.className = "discussion-focus disagreement-card";
    const label = document.createElement("span");
    label.className = "discussion-focus-label";
    label.textContent = DISCUSSION_FOCUS_LABEL;
    cue.appendChild(label);

    const fields = [
        ["discussionUser", disagreement.userPosition],
        ["discussionAi", disagreement.aiPosition],
        ["discussionCore", disagreement.coreDisagreement],
        ["discussionNext", disagreement.nextQuestion]
    ];
    fields.forEach(([title, value]) => {
        if (!String(value || "").trim()) return;
        const item = document.createElement("div");
        item.className = "disagreement-field";
        const heading = document.createElement("small");
        heading.textContent = t(title);
        const message = document.createElement("strong");
        message.textContent = value;
        item.append(heading, message);
        cue.appendChild(item);
    });
    return cue;
}

function assistantBodyWithoutCues(content, uiCues, question) {
    let body = String(content || "").trimEnd();
    const suffixes = [
        ...uiCues.map(cue => String(cue?.text || "").trim()).filter(Boolean),
        question
    ].filter(Boolean);

    suffixes.reverse().forEach(suffix => {
        if (body.endsWith(suffix)) {
            body = body.slice(0, -suffix.length).trimEnd();
        }
    });

    return body;
}

const coordinateRouteDirections = [
    [-1, 0],
    [1, 0],
    [0, -1],
    [0, 1]
];

function renderAssistantBody(node, body, links, turn) {
    const text = String(body || "");
    const candidates = [];
    const occupiedRanges = [];

    (Array.isArray(links) ? links : []).forEach((link, linkIndex) => {
        const linkText = String(link?.text || "").trim();
        if (!linkText) return;

        const route = findCoordinateRoute(state.draftRows, link.from, link.to);
        if (!route) return;

        let start = text.indexOf(linkText);
        while (start >= 0 && occupiedRanges.some(range => (
            start < range.end && start + linkText.length > range.start
        ))) {
            start = text.indexOf(linkText, start + 1);
        }
        if (start < 0) return;

        const candidate = {
            link,
            linkIndex,
            route,
            start,
            end: start + linkText.length,
            text: linkText
        };
        occupiedRanges.push({ start: candidate.start, end: candidate.end });
        candidates.push(candidate);
    });

    candidates.sort((left, right) => left.start - right.start);
    if (!candidates.length) {
        node.textContent = text;
        return;
    }

    let cursor = 0;
    candidates.forEach(candidate => {
        if (candidate.start > cursor) {
            node.appendChild(document.createTextNode(text.slice(cursor, candidate.start)));
        }
        const button = document.createElement("button");
        button.type = "button";
        button.className = "coordinate-link";
        button.textContent = candidate.text;
        button.dataset.coordinateLinkIndex = String(candidate.linkIndex);
        button.setAttribute(
            "aria-label",
            `Show route from ${formatCoordinatePoint(candidate.link.from)} to ${formatCoordinatePoint(candidate.link.to)}`
        );
        button.addEventListener("click", () => toggleCoordinateLink(turn, candidate.linkIndex, candidate.link));
        node.appendChild(button);
        cursor = candidate.end;
    });
    if (cursor < text.length) node.appendChild(document.createTextNode(text.slice(cursor)));
}

function formatCoordinatePoint(point) {
    return `row ${point?.row}, column ${point?.column}`;
}

function normalizedCoordinatePoint(point) {
    if (!point || !Number.isInteger(point.row) || !Number.isInteger(point.column)) return null;
    if (point.row < 1 || point.row > 10 || point.column < 1 || point.column > 12) return null;
    return { row: point.row, column: point.column };
}

function findCoordinateRoute(rows, from, to) {
    if (!Array.isArray(rows) || rows.length !== 10 || rows.some(row => typeof row !== "string" || row.length !== 12)) {
        return null;
    }
    const source = normalizedCoordinatePoint(from);
    const destination = normalizedCoordinatePoint(to);
    if (!source || !destination || (source.row === destination.row && source.column === destination.column)) {
        return null;
    }

    const start = [source.row - 1, source.column - 1];
    const end = [destination.row - 1, destination.column - 1];
    const startKey = `${start[0]},${start[1]}`;
    const endKey = `${end[0]},${end[1]}`;
    const isOpen = (row, column) => {
        if (row < 0 || row >= rows.length || column < 0 || column >= rows[row].length) return false;
        const tile = rows[row][column];
        if (tile === " " || tile === "#" || tile === "@") return false;
        const key = `${row},${column}`;
        return tile !== "s" || key === startKey || key === endKey;
    };

    if (!isOpen(start[0], start[1]) || !isOpen(end[0], end[1])) return null;

    const queue = [start];
    const previous = new Map([[startKey, null]]);
    let queueIndex = 0;
    while (queueIndex < queue.length) {
        const [row, column] = queue[queueIndex++];
        const currentKey = `${row},${column}`;
        if (currentKey === endKey) break;

        coordinateRouteDirections.forEach(([rowDelta, columnDelta]) => {
            const nextRow = row + rowDelta;
            const nextColumn = column + columnDelta;
            const nextKey = `${nextRow},${nextColumn}`;
            if (!isOpen(nextRow, nextColumn) || previous.has(nextKey)) return;
            previous.set(nextKey, currentKey);
            queue.push([nextRow, nextColumn]);
        });
    }

    if (!previous.has(endKey)) return null;
    const path = [];
    let key = endKey;
    while (key !== null) {
        const [row, column] = key.split(",").map(Number);
        path.push({ row: row + 1, column: column + 1 });
        key = previous.get(key);
    }
    return path.reverse();
}

function toggleCoordinateLink(turn, linkIndex, link) {
    const active = state.activeCoordinateLink;
    const sameLink = active
        && active.versionId === state.selectedVersionId
        && active.turnId === turn.turnId
        && active.linkIndex === linkIndex;
    if (sameLink) {
        state.activeCoordinateLink = null;
    } else if (findCoordinateRoute(state.draftRows, link.from, link.to)) {
        state.activeCoordinateLink = {
            versionId: state.selectedVersionId,
            turnId: turn.turnId,
            linkIndex,
            from: { ...link.from },
            to: { ...link.to }
        };
    } else {
        state.activeCoordinateLink = null;
    }
    drawActiveCoordinateRoute();
}

function proposalForTurn(turnId) {
    return state.session.proposals.find(proposal => proposal.assistantTurnId === turnId);
}

function createGuidanceCue(type, text, detail = "") {
    const cue = document.createElement("section");
    cue.className = `guidance-cue guidance-cue-${type.replace("_", "-")}`;
    const label = document.createElement("span");
    label.className = "guidance-cue-label";
    label.textContent = GUIDANCE_CUE_LABELS[state.language]?.[type]
        || GUIDANCE_CUE_LABELS.en[type];
    const message = document.createElement("strong");
    message.textContent = text;
    cue.append(label, message);

    if (detail) {
        const rationale = document.createElement("p");
        rationale.textContent = detail;
        cue.appendChild(rationale);
    }

    return cue;
}

function prefillProposalConsent(offer) {
    // Legacy helper kept for integrations that call it directly. Purple-card
    // Historical callback shape was: () => prefillProposalConsent(offer)
    // clicks now go through sendRevisionCardAction so the server receives the
    // explicit execute_revision action and source turn.
    const summary = String(offer.summary || "").trim();
    elements.messageInput.value = t("proposalConsent").replace("{summary}", summary);
    handleComposerInput();
    elements.chatForm.requestSubmit();
}

function sendRevisionCardAction(action, turn, offer) {
    if (!canEditSelected() || state.busy) return;
    if (!isLatestRevisionOfferTurn(turn)) {
        showNotice(t("staleRevisionCard"));
        render();
        return;
    }
    const summary = String(offer?.summary || "").trim();
    const content = action === "execute_revision"
        ? t("proposalConsent").replace("{summary}", summary)
        : action === "challenge_revision"
            ? `${t("challengeRevision")}: ${summary}`
            : `${t("alternativeRevision")}: ${summary}`;
    state.pendingMessage = {
        content,
        baseVersionId: state.session.currentVersionId,
        idempotencyKey: uniqueId(`card-${action}`),
        action,
        sourceTurnId: turn.turnId
    };
    persistPendingMessage();
    void submitPendingMessage();
}

function renderProposal() {
    elements.proposalArea.textContent = "";
    if (!canEditSelected()) return;
    const proposal = currentPendingProposal();
    if (!proposal) return;
    const card = document.createElement("section");
    card.className = "proposal-card";
    const summary = localizedProposalSummary(proposal);
    card.innerHTML = `
        <div class="proposal-heading"><div><p class="eyebrow">${escapeHtml(t("proposal"))}</p><h3>${escapeHtml(summary || t("proposal"))}</h3></div><span>${proposal.diff.length} ${escapeHtml(t("changedTiles"))}</span></div>
        <p>${escapeHtml(t("proposalValid"))}</p>`;
    card.appendChild(createMiniMap(proposal.proposedRows, proposal.diff, "proposal-map"));
    const actions = document.createElement("div");
    actions.className = "proposal-actions";
    const reject = makeButton(t("reject"), "secondary-button", () => decideProposal(proposal, "reject"));
    const accept = makeButton(t("accept"), "primary-button", () => decideProposal(proposal, "accept"));
    actions.append(reject, accept);
    card.appendChild(actions);
    elements.proposalArea.appendChild(card);
}

function clearMapOverlay() {
    elements.mapOverlay.textContent = "";
    elements.mapOverlay.removeAttribute("viewBox");
}

function drawActiveCoordinateRoute() {
    clearMapOverlay();
    const active = state.activeCoordinateLink;
    if (!active || active.versionId !== state.selectedVersionId) return;

    const route = findCoordinateRoute(state.draftRows, active.from, active.to);
    const board = elements.mapBoard;
    if (!route || !board) {
        state.activeCoordinateLink = null;
        return;
    }

    const boardRect = board.getBoundingClientRect();
    const points = route.map(point => {
        const cell = elements.mapGrid.querySelector(
            `[data-row="${point.row}"][data-column="${point.column}"]`
        );
        if (!cell) return null;
        const cellRect = cell.getBoundingClientRect();
        return `${cellRect.left + cellRect.width / 2 - boardRect.left},${cellRect.top + cellRect.height / 2 - boardRect.top}`;
    });
    if (points.some(point => !point) || !boardRect.width || !boardRect.height) {
        state.activeCoordinateLink = null;
        return;
    }

    elements.mapOverlay.setAttribute("viewBox", `0 0 ${boardRect.width} ${boardRect.height}`);
    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    const marker = document.createElementNS("http://www.w3.org/2000/svg", "marker");
    marker.setAttribute("id", "coordinate-route-arrowhead");
    marker.setAttribute("viewBox", "0 0 10 10");
    marker.setAttribute("refX", "8");
    marker.setAttribute("refY", "5");
    marker.setAttribute("markerWidth", "7");
    marker.setAttribute("markerHeight", "7");
    marker.setAttribute("orient", "auto-start-reverse");
    marker.setAttribute("markerUnits", "userSpaceOnUse");
    const arrowhead = document.createElementNS("http://www.w3.org/2000/svg", "path");
    arrowhead.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
    arrowhead.setAttribute("fill", "#d62828");
    marker.appendChild(arrowhead);
    defs.appendChild(marker);
    elements.mapOverlay.appendChild(defs);

    const polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    polyline.setAttribute("points", points.join(" "));
    polyline.setAttribute("fill", "none");
    polyline.setAttribute("stroke", "#d62828");
    polyline.setAttribute("stroke-width", "3");
    polyline.setAttribute("stroke-linecap", "round");
    polyline.setAttribute("stroke-linejoin", "round");
    polyline.setAttribute("stroke-dasharray", "7 5");
    polyline.setAttribute("marker-end", "url(#coordinate-route-arrowhead)");
    elements.mapOverlay.appendChild(polyline);
}

function renderMap() {
    state.activeCoordinateLink = null;
    const version = selectedVersion();
    clearMapOverlay();
    if (!version) return;
    elements.selectedStageEyebrow.textContent = `${t("stage").toUpperCase()} ${version.stageNumber}`;
    elements.mapGrid.textContent = "";
    elements.mapGrid.style.gridTemplateColumns = "repeat(12, var(--tile-size))";
    const entityLabels = buildEntityLabels(state.draftRows);
    state.draftRows.forEach((row, y) => {
        [...row].forEach((tile, x) => {
            const cell = document.createElement("button");
            cell.type = "button";
            cell.className = `tile ${tileClass(tile)}`;
            const entityLabel = entityLabels.get(`${x},${y}`);
            cell.textContent = entityLabel || tileLabel(tile);
            cell.title = formatEntityCoordinate(entityLabel, x, y);
            cell.dataset.row = String(y + 1);
            cell.dataset.column = String(x + 1);
            if (entityLabel) cell.dataset.entityId = entityLabel;
            cell.setAttribute(
                "aria-label",
                entityLabel
                    ? formatEntityCoordinate(entityLabel, x, y)
                    : `${t(tileName(tile))} ${formatTileCoordinate(x, y)}`,
            );
            cell.disabled = !canEditSelected();
            if (version.diff.some(change => change.x === x && change.y === y)) cell.classList.add("changed");
            cell.addEventListener("click", () => editTile(x, y));
            elements.mapGrid.appendChild(cell);
        });
    });
    renderToolbar();
    const validation = version.validation;
    const failedValidation = state.dirty && isLevelValidationError(state.validationError);
    elements.validationCard.className = `validation-card${failedValidation ? " invalid" : ""}`;
    elements.validationCard.setAttribute("role", failedValidation ? "alert" : "status");
    elements.validationCard.innerHTML = failedValidation
        ? `
        <strong>✗ ${escapeHtml(t("validationFailed"))}</strong>
        <span>${escapeHtml(localizedErrorMessage(state.validationError))}</span>`
        : `
        <strong>✓ ${escapeHtml(t("verified"))}</strong>
        <span>${validation.solutionSteps} ${escapeHtml(t("steps"))} · ${validation.solutionPushes} ${escapeHtml(t("pushes"))} · ${validation.searchedStates} states</span>`;
    elements.mapMode.textContent = state.session.status !== "active" ? t("lockedMode") : version.versionId === state.session.currentVersionId ? t("editMode") : t("readOnlyMode");
    if (state.dirty) elements.mapMode.textContent += ` · ${t("unsaved")}`;
    renderAttempts(version);
    drawActiveCoordinateRoute();
    requestAnimationFrame(drawActiveCoordinateRoute);
}

function renderToolbar() {
    elements.mapToolbar.textContent = "";
    TILE_ORDER.forEach(tile => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `palette-button ${tileClass(tile)}`;
        if (state.selectedTile === tile) button.classList.add("selected");
        button.disabled = !canEditSelected();
        button.innerHTML = `<span>${escapeHtml(tileLabel(tile) || "×")}</span><small>${escapeHtml(t(tileName(tile)))}</small>`;
        button.addEventListener("click", () => { state.selectedTile = tile; renderToolbar(); });
        elements.mapToolbar.appendChild(button);
    });
}

function renderAttempts(version) {
    elements.playAttemptList.textContent = "";
    elements.playAttemptCount.textContent = String(version.playAttempts.length);
    if (!version.playAttempts.length) {
        elements.playAttemptList.textContent = t("noPlayEvidence");
        return;
    }
    version.playAttempts.forEach(attempt => {
        const item = document.createElement("div");
        item.className = `attempt-item ${attempt.status}`;
        item.textContent = formatAttempt(attempt);
        elements.playAttemptList.appendChild(item);
    });
}

function renderSessionState() {
    const status = state.session.status;
    elements.finalActions.hidden = status !== "active";
    elements.intentionForm.hidden = status !== "awaiting_intention";
    elements.completeCard.hidden = status !== "completed";
    // A completed session always offers the return action.  Some local or
    // legacy sessions do not carry online-room metadata, but they may still
    // have been opened by Unity and therefore retain a valid opener tab.
    elements.returnUnityButton.hidden = status !== "completed" || state.session.demoMode;
    elements.historyBanner.hidden = state.selectedVersionId === state.session.currentVersionId;
}

function deadlineExpired() { return Boolean(state.session?.deadlineExpired); }

function renderDeadline() {
    window.clearInterval(state.deadlineTimerId);
    if (!state.session?.deadlineAt || state.session.status !== "active") {
        elements.deadlineStatus.hidden = true;
        return;
    }
    const update = () => {
        const seconds = Math.max(0, Math.ceil((Date.parse(state.session.deadlineAt) - Date.now()) / 1000));
        if (seconds <= 0) state.session.deadlineExpired = true;
        const minutes = String(Math.floor(seconds / 60)).padStart(2, "0");
        const remainder = String(seconds % 60).padStart(2, "0");
        elements.deadlineStatus.hidden = false;
        elements.deadlineStatus.className = `deadline-status ${deadlineExpired() ? "expired" : ""}`;
        elements.deadlineStatus.textContent = deadlineExpired()
            ? "TIME IS UP — SUBMIT THE CURRENT MAP AS THE FINAL STAGE."
            : `TIME LEFT ${minutes}:${remainder}`;
        updateControls();
    };
    update();
    state.deadlineTimerId = window.setInterval(update, 1000);
}

function updateControls() {
    if (!state.session) {
        elements.demoButton.disabled = state.busy;
        return;
    }

    const expired = deadlineExpired();
    const editable = canEditSelected();
    const pending = Boolean(currentPendingProposal());
    elements.saveStageButton.disabled = !editable || !state.dirty || state.busy;
    elements.discardDraftButton.disabled = !editable || !state.dirty || state.busy;
    elements.restoreStageButton.hidden = state.selectedVersionId === state.session.currentVersionId || state.session.status !== "active";
    elements.restoreStageButton.disabled = state.busy || expired;
    elements.playButton.disabled = state.busy || expired || state.dirty || pending || !selectedVersion();
    elements.finalizeButton.disabled = state.busy || state.selectedVersionId !== state.session.currentVersionId || (!expired && (state.dirty || pending));
    elements.languageButton.disabled = state.busy || expired || state.translationInProgress;
    elements.messageInput.disabled = state.busy || !editable;
    elements.sendButton.disabled = state.busy || !editable || !elements.messageInput.value.trim();
    elements.sendButton.textContent = state.chatBusy ? t("sending") : t("send");
    elements.typingRow.hidden = !state.chatBusy;
}

function renderChatRequestStatus() {
    if (!canEditSelected()) {
        elements.chatRequestStatus.hidden = true;
        return;
    }
    const waiting = state.chatStatus === "waiting";
    const failed = state.chatStatus === "error";
    elements.chatRequestStatus.hidden = !waiting && !failed;
    elements.chatRequestStatus.className = `chat-request-status ${waiting ? "waiting" : "error"}`;
    elements.chatRequestMessage.textContent = waiting
        ? chatWaitingMessage()
        : localizedErrorMessage(state.chatError);
    elements.chatRetryButton.hidden = !failed || !state.chatError?.retryable || !state.pendingMessage;
    elements.chatRetryButton.textContent = t("retry");
}

function renderTranslationStatus() {
    if (!elements.translationStatus) return;
    const active = state.translationInProgress && state.translationRemainingCount > 0;
    elements.translationStatus.hidden = !active;
    if (active) {
        elements.translationStatus.textContent = t("translationInProgress")
            .replace("{count}", String(state.translationRemainingCount));
    }
}

function chatWaitingMessage() {
    const elapsedSeconds = Math.min(
        65,
        Math.max(0, Math.floor((Date.now() - state.chatStartedAt) / 1000))
    );
    const phase = elapsedSeconds < 40
        ? t("chatWaitingPrimary")
        : t("chatWaitingFallback");
    return `${phase} · ${elapsedSeconds} / 65 ${t("seconds")}`;
}

function startChatTimer() {
    stopChatTimer();
    state.chatStartedAt = Date.now();
    state.chatTimerId = window.setInterval(renderChatRequestStatus, 250);
}

function stopChatTimer() {
    if (state.chatTimerId !== null) {
        window.clearInterval(state.chatTimerId);
        state.chatTimerId = null;
    }
}

async function ensureAssessment(versionId) {
    const version = findVersion(versionId);
    if (!state.session || version?.openingTurnId || state.session.assessments.some(item => item.versionId === versionId) || state.assessing.has(versionId)) return;
    state.assessing.add(versionId);
    try {
        state.session = await api(`/api/sessions/${state.sessionId}/versions/${versionId}/assessments`, {
            method: "POST",
            body: { idempotencyKey: uniqueId("assessment") },
            timeoutMs: 65000
        });
        render();
    } catch (error) {
        showError(error, () => ensureAssessment(versionId));
    } finally {
        state.assessing.delete(versionId);
    }
}

async function sendMessage(event) {
    event.preventDefault();
    const content = elements.messageInput.value.trim();
    if (!content || state.busy) return;

    if (
        !state.pendingMessage
        || state.pendingMessage.content !== content
        || state.pendingMessage.baseVersionId !== state.session.currentVersionId
    ) {
        state.pendingMessage = {
            content,
            baseVersionId: state.session.currentVersionId,
            idempotencyKey: uniqueId("message")
        };
    }

    persistPendingMessage();
    await submitPendingMessage();
}

async function retryPendingMessage() {
    if (!state.pendingMessage || state.busy) return;
    elements.messageInput.value = state.pendingMessage.content;
    localStorage.setItem(composerKey(), state.pendingMessage.content);
    updateCharacterCount();
    await submitPendingMessage();
}

async function submitPendingMessage() {
    const pending = state.pendingMessage;
    if (!pending || state.busy) return;

    state.busy = true;
    state.chatBusy = true;
    state.chatStatus = "waiting";
    state.chatError = null;
    startChatTimer();
    hideNotice();
    renderChatRequestStatus();
    updateControls();

    try {
        state.session = await api(`/api/sessions/${state.sessionId}/messages`, {
            method: "POST",
            body: pending,
            timeoutMs: 65000
        });
        elements.messageInput.value = "";
        localStorage.removeItem(composerKey());
        clearPendingMessage();
        state.chatStatus = "idle";
        state.chatError = null;
        updateCharacterCount();
        render();
    } catch (error) {
        if (
            error?.code === "INVALID_CARD_SOURCE"
            && pending.action
            && pending.action !== "none"
        ) {
            clearPendingMessage();
            elements.messageInput.value = "";
            localStorage.removeItem(composerKey());
            updateCharacterCount();
            try {
                await refreshSession();
            } catch (_refreshError) {
                // Keep the original stale-card error visible if the refresh
                // itself cannot complete.
            }
        }
        state.chatStatus = "error";
        state.chatError = error;
    } finally {
        stopChatTimer();
        state.busy = false;
        state.chatBusy = false;
        renderChatRequestStatus();
        updateControls();
    }
}

async function saveManualStage() {
    if (!state.dirty) return;
    await withBusy(async () => {
        state.validationError = null;
        renderMap();
        try {
            state.session = await api(`/api/sessions/${state.sessionId}/versions`, {
                method: "POST",
                body: {
                    rows: state.draftRows,
                    baseVersionId: state.session.currentVersionId,
                    idempotencyKey: uniqueId("manual"),
                    summary: state.language === "zh-CN" ? "设计者保存的地图修改" : "Designer-saved map edit"
                }
            });
            selectVersion(state.session.currentVersionId, false);
            render();
            await ensureAssessment(state.session.currentVersionId);
        } catch (error) {
            if (isLevelValidationError(error)) {
                state.validationError = error;
                renderMap();
            }
            throw error;
        }
    });
}

async function restoreSelectedStage() {
    const version = selectedVersion();
    await withBusy(async () => {
        state.session = await api(`/api/sessions/${state.sessionId}/versions/${version.versionId}/restore`, {
            method: "POST",
            body: { baseVersionId: state.session.currentVersionId, idempotencyKey: uniqueId("restore") }
        });
        selectVersion(state.session.currentVersionId, false);
        render();
        await ensureAssessment(state.session.currentVersionId);
    });
}

async function decideProposal(proposal, decision) {
    await withBusy(async () => {
        state.session = await api(`/api/sessions/${state.sessionId}/proposals/${proposal.proposalId}/decision`, {
            method: "POST",
            body: { decision, baseVersionId: state.session.currentVersionId, idempotencyKey: uniqueId(decision), reason: "" }
        });
        selectVersion(state.session.currentVersionId, false);
        render();
    });
}

async function playSelectedStage() {
    if (state.dirty) return showNotice(t("errorDirtyPlay"));
    if (currentPendingProposal()) return showNotice(t("errorPendingPlay"));
    const version = selectedVersion();
    await withBusy(async () => {
        localStorage.setItem(selectedStageKey(), version.versionId);
        localStorage.setItem(composerKey(), elements.messageInput.value);
        const payload = await api(`/api/sessions/${state.sessionId}/versions/${version.versionId}/play-attempts`, {
            method: "POST",
            body: { idempotencyKey: uniqueId("play") }
        });
        window.location.assign(payload.playUrl);
    });
}

async function finalizeSession() {
    elements.finalizeModal.hidden = true;
    await withBusy(async () => {
        state.session = await api(`/api/sessions/${state.sessionId}/finalize`, {
            method: "POST",
            body: {
                baseVersionId: state.session.currentVersionId,
                idempotencyKey: uniqueId("finalize"),
                rows: deadlineExpired() ? state.draftRows : null
            }
        });
        render();
        elements.intentionInput.focus();
    });
}

async function submitIntention(event) {
    event.preventDefault();
    const content = elements.intentionInput.value.trim();
    if (!content) return showNotice(t("errorIntentRequired"));
    await withBusy(async () => {
        state.session = await api(`/api/sessions/${state.sessionId}/intention`, {
            method: "POST",
            body: { content, idempotencyKey: uniqueId("intention") }
        });
        render();
    });
}

function returnToUnity() {
    hideNotice();
    const unityWindow = window.opener;

    if (!unityWindow || unityWindow.closed) {
        showNotice(t("returnUnityUnavailable"));
        return;
    }

    try {
        unityWindow.focus();
        window.close();
        window.setTimeout(() => showNotice(t("returnUnityCloseBlocked")), 150);
    } catch (_error) {
        showNotice(t("returnUnityUnavailable"));
    }
}

async function toggleLanguage() {
    const next = state.language === "en" ? "zh-CN" : "en";
    if (!state.session) {
        state.language = next;
        applyTranslations();
        return;
    }
    await withBusy(async () => {
        state.session = await api(`/api/sessions/${state.sessionId}/language`, { method: "PATCH", body: { language: next } });
        state.language = next;
        render();
    });
    void ensureVisibleTranslations();
}

function selectVersion(versionId, shouldRender = true) {
    if (!findVersion(versionId)) return;
    state.selectedVersionId = versionId;
    state.activeCoordinateLink = null;
    localStorage.setItem(selectedStageKey(), versionId);
    syncHash();
    resetDraftFromSelection();
    restoreComposerDraft();
    if (shouldRender) {
        render();
        void ensureVisibleTranslations();
    }
}

function selectCurrentVersion() { selectVersion(state.session.currentVersionId); }
function selectedVersion() { return findVersion(state.selectedVersionId); }
function findVersion(versionId) { return state.session && state.session.versions.find(item => item.versionId === versionId); }
function selectedStageTurns() {
    if (!state.session) return [];

    const openingTurnId = selectedVersion()?.openingTurnId;
    const supersededAssessmentTurnIds = new Set(
        openingTurnId
            ? state.session.assessments
                .filter(item => item.versionId === state.selectedVersionId)
                .map(item => item.assistantTurnId)
            : []
    );
    const directTurns = state.session.turns.filter(
        turn => turn.versionId === state.selectedVersionId
            && !supersededAssessmentTurnIds.has(turn.turnId)
    );
    const openingTurn = openingTurnId
        ? state.session.turns.find(turn => turn.turnId === openingTurnId)
        : null;

    if (!openingTurn || directTurns.some(turn => turn.turnId === openingTurn.turnId)) {
        return directTurns;
    }

    return [openingTurn, ...directTurns];
}

function localizedAssistantTurn(turn) {
    if (turn.role === "assistant" && proposalForTurn(turn.turnId)) {
        return {
            ...turn,
            content: verifiedProposalMessage()
        };
    }
    if (turn.role !== "assistant" || turn.language === state.language) return turn;
    const translation = turn.translations?.[state.language];
    if (!translation) return turn;
    return {
        ...turn,
        content: translation.body,
        guidance: translation.guidance || {}
    };
}

function localizedProposalSummary(proposal) {
    return verifiedProposalSummary(proposal.diff);
}

function verifiedProposalMessage() {
    if (state.language === "zh-CN") {
        return "我把这次地图提案整理好了，也逐格核对了前后的真实变化。它现在仍是一份等你审查的方案；我更想让你先看高亮位置是否真的回应了刚才的方向，再决定要不要接受。";
    }
    return "I have organized this map proposal and checked its real before/after tile changes. It is still yours to review; I would first look at whether the highlighted cells really answer the direction we discussed before deciding whether to accept it.";
}

function verifiedProposalSummary(diff) {
    const changes = Array.isArray(diff) ? diff : [];
    if (!changes.length) {
        return state.language === "zh-CN" ? "未检测到实际格子改动。" : "No tile changes detected.";
    }

    const names = state.language === "zh-CN"
        ? { " ": "边界外区域", "#": "墙", ".": "地面", "@": "水面", p: "玩家", s: "箱子", t: "目标点" }
        : { " ": "void", "#": "wall", ".": "floor", "@": "water", p: "player", s: "box", t: "target" };

    if (changes.length <= 8) {
        const details = changes.map(change => state.language === "zh-CN"
            ? `第${change.y + 1}行第${change.x + 1}列：${names[change.before]}→${names[change.after]}`
            : `row ${change.y + 1}, column ${change.x + 1}: ${names[change.before]} → ${names[change.after]}`
        );
        return state.language === "zh-CN"
            ? `已核对实际改动（共${changes.length}格）：${details.join("；")}。`
            : `Verified tile changes (${changes.length} total): ${details.join("; ")}.`;
    }

    const transitionCounts = new Map();
    changes.forEach(change => {
        const key = `${change.before}\u0000${change.after}`;
        transitionCounts.set(key, (transitionCounts.get(key) || 0) + 1);
    });
    const details = Array.from(transitionCounts.entries()).map(([key, count]) => {
        const [before, after] = key.split("\u0000");
        return state.language === "zh-CN"
            ? `${names[before]}→${names[after]} ${count}格`
            : `${names[before]} → ${names[after]}: ${count}`;
    });
    return state.language === "zh-CN"
        ? `已核对实际改动（共${changes.length}格）：${details.join("；")}。具体位置以地图高亮为准。`
        : `Verified tile changes (${changes.length} total): ${details.join("; ")}. See the highlighted map cells for every location.`;
}

async function translateVisibleBatch(batch, targetLanguage) {
    const turnIds = batch.map(turn => turn.turnId);
    const requestKey = `${targetLanguage}:${turnIds.join(",")}`;
    if (state.translating.has(requestKey)) return;
    state.translating.add(requestKey);

    try {
        const translatedSession = await api(
            `/api/sessions/${state.sessionId}/translations/${encodeURIComponent(targetLanguage)}`,
            {
                method: "POST",
                body: { turnIds },
                timeoutMs: 65000
            }
        );
        const translatedById = new Map(
            translatedSession.turns.map(turn => [turn.turnId, turn])
        );
        state.session.turns = state.session.turns.map(turn => {
            const translated = translatedById.get(turn.turnId);
            return translated ? { ...turn, translations: translated.translations } : turn;
        });
        turnIds.forEach(turnId => {
            state.translationFailures.delete(`${turnId}:${targetLanguage}`);
        });
    } catch (error) {
        turnIds.forEach(turnId => {
            state.translationFailures.add(`${turnId}:${targetLanguage}`);
        });
    } finally {
        state.translating.delete(requestKey);
        state.translationRemainingCount = Math.max(0, state.translationRemainingCount - batch.length);
        render();
    }
}

async function ensureVisibleTranslations() {
    if (!state.session || state.translationInProgress) return;
    const targetLanguage = state.language;
    const missing = selectedStageTurns().filter(
        turn => turn.role === "assistant"
            && turn.language !== targetLanguage
            && !turn.translations?.[targetLanguage]
    );

    if (!missing.length) {
        state.translationRemainingCount = 0;
        renderTranslationStatus();
        updateControls();
        return;
    }

    const batches = [];
    for (let index = 0; index < missing.length; index += 8) {
        batches.push(missing.slice(index, index + 8));
    }
    state.translationInProgress = true;
    state.translationRemainingCount = missing.length;
    renderTranslationStatus();
    updateControls();

    let nextBatchIndex = 0;
    const worker = async () => {
        while (nextBatchIndex < batches.length) {
            const batch = batches[nextBatchIndex];
            nextBatchIndex += 1;
            await translateVisibleBatch(batch, targetLanguage);
        }
    };
    const workerCount = Math.min(2, batches.length);
    await Promise.all(Array.from({ length: workerCount }, worker));
    state.translationInProgress = false;
    state.translationRemainingCount = 0;
    render();
}

function resetDraftFromSelection() {
    const version = selectedVersion();
    state.draftRows = version ? version.rows.slice() : [];
    state.dirty = false;
    state.validationError = null;
}

function discardDraft() {
    state.activeCoordinateLink = null;
    resetDraftFromSelection();
    renderMap();
    updateControls();
}

function editTile(x, y) {
    if (!canEditSelected()) return;
    state.activeCoordinateLink = null;
    const row = [...state.draftRows[y]];
    row[x] = state.selectedTile;
    state.draftRows[y] = row.join("");
    state.dirty = state.draftRows.some((value, index) => value !== selectedVersion().rows[index]);
    state.validationError = null;
    renderMap();
    updateControls();
}

function canEditSelected() {
    return state.session && state.session.status === "active" && !deadlineExpired() && state.selectedVersionId === state.session.currentVersionId;
}

function currentPendingProposal() {
    return state.session && state.session.proposals.slice().reverse().find(item => item.status === "pending" && item.baseVersionId === state.session.currentVersionId);
}

function createMiniMap(rows, diff, extraClass = "") {
    const map = document.createElement("div");
    map.className = `mini-map ${extraClass}`;
    map.style.gridTemplateColumns = "repeat(12, 1fr)";
    const entityLabels = buildEntityLabels(rows);
    const changed = new Set((diff || []).map(item => `${item.x},${item.y}`));
    rows.forEach((row, y) => [...row].forEach((tile, x) => {
        const cell = document.createElement("i");
        cell.className = tileClass(tile);
        const entityLabel = entityLabels.get(`${x},${y}`);
        cell.title = formatEntityCoordinate(entityLabel, x, y);
        if (entityLabel) {
            cell.dataset.entityId = entityLabel;
            cell.setAttribute("role", "img");
            cell.setAttribute("aria-label", formatEntityCoordinate(entityLabel, x, y));
        }
        if (changed.has(`${x},${y}`)) cell.classList.add("changed");
        map.appendChild(cell);
    }));
    return map;
}

function formatAttempt(attempt) {
    const statusKey = attempt.status === "completed" ? "attemptCompleted" : attempt.status === "abandoned" ? "attemptAbandoned" : attempt.status === "interrupted" ? "attemptInterrupted" : "attemptStarted";
    return `${t(statusKey)} · ${attempt.moveCount} ${t("moves")} · ${attempt.pushCount} ${t("pushes")} · ${attempt.restartCount} ${t("restarts")} · ${Number(attempt.durationSeconds || 0).toFixed(1)} ${t("seconds")}`;
}

function showLanding() {
    elements.workspace.hidden = true;
    elements.landing.hidden = false;
    setStatus(t("errorNoSession"), "pending");
    applyTranslations();
}

async function withBusy(action, onError = null) {
    if (state.busy) return;
    state.busy = true;
    hideNotice();
    updateControls();
    try { await action(); }
    catch (error) {
        if (onError) onError();
        showError(error, () => withBusy(action, onError));
    }
    finally { state.busy = false; updateControls(); }
}

async function api(path, options = {}) {
    const request = { method: options.method || "GET", credentials: "include", headers: {} };
    const controller = options.timeoutMs ? new AbortController() : null;
    const timeoutId = controller
        ? window.setTimeout(() => controller.abort(), options.timeoutMs)
        : null;

    if (controller) request.signal = controller.signal;
    if (options.body !== undefined) {
        request.headers["Content-Type"] = "application/json";
        request.body = JSON.stringify(options.body);
    }
    let response;

    try {
        const requestPath = path.startsWith("/api/")
            ? API_PREFIX + path
            : path;
        response = await fetch(requestPath, request);
    } catch (error) {
        if (error?.name === "AbortError") {
            const timeoutError = new Error(t("error_CLIENT_TIMEOUT"));
            timeoutError.code = "CLIENT_TIMEOUT";
            timeoutError.retryable = true;
            throw timeoutError;
        }
        throw error;
    } finally {
        if (timeoutId !== null) window.clearTimeout(timeoutId);
    }
    let payload = {};
    try { payload = await response.json(); } catch (error) { payload = {}; }
    if (!response.ok) {
        const apiError = new Error(payload.message || t("errorGeneric"));
        apiError.code = payload.code || "REQUEST_FAILED";
        apiError.details = payload.details || null;
        apiError.retryable = isLevelValidationError(apiError) ? false : Boolean(payload.retryable);
        throw apiError;
    }
    return payload;
}

function showError(error, retryAction) {
    showNotice(localizedErrorMessage(error), error.retryable ? retryAction : null);
}

function localizedErrorMessage(error) {
    if (error?.code === "PENDING_MESSAGE") return t("chatRetryPending");
    const localized = state.language === "zh-CN"
        ? chineseApiErrors[error?.code]
        : translations.en[`error_${error?.code}`];
    const message = localized
        || (state.language === "zh-CN" ? t("errorGeneric") : error?.message)
        || t("errorGeneric");
    const details = formatValidationDetails(error);
    return details ? `${message} ${details}` : message;
}

function isLevelValidationError(error) {
    return LEVEL_VALIDATION_ERROR_CODES.has(error?.code);
}

function formatValidationDetails(error) {
    if (!isLevelValidationError(error) || !error?.details || typeof error.details !== "object") return "";
    const details = error.details;
    const isChinese = state.language === "zh-CN";
    const row = Number(details.row);
    const column = Number(details.column);
    const hasRow = Number.isInteger(row);
    const hasColumn = Number.isInteger(column);
    const rowLabel = hasRow ? (isChinese ? `第 ${row + (error.code === "OPEN_OUTER_WALL" ? 0 : 1)} 行` : `row ${row + (error.code === "OPEN_OUTER_WALL" ? 0 : 1)}`) : "";
    const columnLabel = hasColumn ? (isChinese ? `第 ${column + (error.code === "OPEN_OUTER_WALL" ? 0 : 1)} 列` : `column ${column + (error.code === "OPEN_OUTER_WALL" ? 0 : 1)}`) : "";

    if (error.code === "OPEN_OUTER_WALL" && hasRow && hasColumn) {
        const tile = validationTileNames[details.tile]?.[isChinese ? "zh" : "en"] || String(details.tile ?? "?");
        return isChinese
            ? `（首个破口：${rowLabel}${columnLabel}，格子为 ${tile}）`
            : `(first breach: ${rowLabel}, ${columnLabel}, tile ${tile})`;
    }

    if (error.code === "INVALID_WIDTH" && hasRow) {
        return isChinese ? `（出错位置：${rowLabel}）` : `(problem row: ${rowLabel})`;
    }

    if (error.code === "UNKNOWN_TILE") {
        const tiles = Array.isArray(details.tiles) ? details.tiles.join(", ") : String(details.tiles ?? "?");
        return isChinese
            ? `（${rowLabel || "地图中"}发现未知符号：${tiles}）`
            : `(${rowLabel || "unknown location"}; unsupported symbols: ${tiles})`;
    }

    if (error.code === "INVALID_PLAYER_COUNT" || error.code === "INVALID_BOX_COUNT") {
        const count = Number(details.count);
        if (Number.isFinite(count)) return isChinese ? `（当前数量：${count}）` : `(current count: ${count})`;
    }

    if (error.code === "MISMATCHED_TARGET_COUNT") {
        const boxes = Number(details.boxes);
        const targets = Number(details.targets);
        if (Number.isFinite(boxes) && Number.isFinite(targets)) {
            return isChinese
                ? `（箱子：${boxes}，目标：${targets}）`
                : `(boxes: ${boxes}, targets: ${targets})`;
        }
    }

    if (error.code === "UNSOLVABLE_LEVEL" || error.code === "SEARCH_BUDGET_EXCEEDED") {
        const searchedStates = Number(details.searchedStates);
        if (Number.isFinite(searchedStates)) {
            return isChinese
                ? `（已搜索状态：${searchedStates}）`
                : `(searched states: ${searchedStates})`;
        }
    }

    return "";
}

function showNotice(message, retryAction = null) {
    state.retryAction = retryAction;
    elements.noticeMessage.textContent = message;
    elements.retryButton.hidden = !retryAction;
    elements.notice.classList.add("visible");
}

function showPlayReturnNotice(status) {
    if (status === "sync_failed") showNotice(t("playSyncFailed"));
    if (status === "load_failed") showNotice(t("playLoadFailed"));
}

function hideNotice() {
    state.retryAction = null;
    elements.notice.classList.remove("visible");
    elements.retryButton.hidden = true;
}

function setStatus(label, status) {
    elements.prototypeStatus.className = `prototype-status ${status}`;
    elements.prototypeStatus.querySelector("span:last-child").textContent = label;
}

function applyTranslations() {
    document.documentElement.lang = state.language;
    document.querySelectorAll("[data-i18n]").forEach(element => element.textContent = t(element.dataset.i18n));
    document.querySelectorAll("[data-i18n-placeholder]").forEach(element => element.placeholder = t(element.dataset.i18nPlaceholder));
    elements.languageButton.textContent = state.language === "en" ? "中文" : "English";
    updateCharacterCount();
    renderChatRequestStatus();
    renderTranslationStatus();
}

function handleComposerInput() {
    if (state.sessionId) localStorage.setItem(composerKey(), elements.messageInput.value);
    if (state.pendingMessage && elements.messageInput.value.trim() !== state.pendingMessage.content) {
        clearPendingMessage();
        state.chatStatus = "idle";
        state.chatError = null;
        renderChatRequestStatus();
    }
    updateCharacterCount();
    updateControls();
}

function restoreComposerDraft() {
    elements.messageInput.value = canEditSelected()
        ? localStorage.getItem(composerKey()) || ""
        : "";
    updateCharacterCount();
    updateControls();
}

function recoverPendingMessage() {
    const stored = readPendingMessage();
    const turns = (state.session?.turns || []).filter(
        turn => turn.versionId === state.session.currentVersionId
    );
    const finalTurn = turns[turns.length - 1];
    const unmatched = finalTurn?.role === "user"
        && finalTurn.requestId
        && !turns.some(turn => turn.role === "assistant" && turn.requestId === finalTurn.requestId)
        ? {
            content: finalTurn.content,
            baseVersionId: finalTurn.versionId,
            idempotencyKey: finalTurn.requestId
        }
        : null;
    const pending = stored || unmatched;

    if (!pending) return;

    const completed = turns.some(
        turn => turn.role === "assistant" && turn.requestId === pending.idempotencyKey
    );

    if (completed) {
        if (elements.messageInput.value.trim() === pending.content) {
            elements.messageInput.value = "";
            localStorage.removeItem(composerKey());
        }
        clearPendingMessage();
        updateCharacterCount();
        updateControls();
        return;
    }

    const draft = elements.messageInput.value.trim();
    if (draft && draft !== pending.content) {
        clearPendingMessage();
        return;
    }

    state.pendingMessage = pending;
    persistPendingMessage();
    elements.messageInput.value = pending.content;
    localStorage.setItem(composerKey(), pending.content);
    state.chatStatus = "error";
    state.chatError = {
        code: "PENDING_MESSAGE",
        message: t("chatRetryPending"),
        retryable: true
    };
    updateCharacterCount();
    renderChatRequestStatus();
    updateControls();
}

function readPendingMessage() {
    try {
        const currentKey = pendingMessageKey();
        const legacyKey = `cocreationPendingMessage:${state.sessionId}`;
        const pending = JSON.parse(
            localStorage.getItem(currentKey)
            || localStorage.getItem(legacyKey)
            || "null"
        );
        if (
            !pending
            || typeof pending.content !== "string"
            || typeof pending.baseVersionId !== "string"
            || typeof pending.idempotencyKey !== "string"
        ) return null;
        if (pending.action !== undefined && ![
            "none",
            "execute_revision",
            "challenge_revision",
            "alternative_revision"
        ].includes(pending.action)) return null;
        if (
            pending.action
            && pending.action !== "none"
            && typeof pending.sourceTurnId !== "string"
        ) return null;
        if (pending.baseVersionId !== state.session.currentVersionId) return null;
        localStorage.setItem(currentKey, JSON.stringify(pending));
        localStorage.removeItem(legacyKey);
        return pending;
    } catch (error) {
        localStorage.removeItem(pendingMessageKey());
        return null;
    }
}

function persistPendingMessage() {
    if (state.pendingMessage) {
        localStorage.setItem(pendingMessageKey(), JSON.stringify(state.pendingMessage));
    }
}

function clearPendingMessage() {
    state.pendingMessage = null;
    localStorage.removeItem(pendingMessageKey());
    localStorage.removeItem(`cocreationPendingMessage:${state.sessionId}`);
}

function updateCharacterCount() { elements.characterCount.textContent = `${elements.messageInput.value.length} / ${MAX_MESSAGE_LENGTH}`; }
function handleComposerKeydown(event) { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); elements.chatForm.requestSubmit(); } }
function composerKey() { return `cocreationComposer:${state.sessionId}:${state.session?.currentVersionId || "none"}`; }
function pendingMessageKey() { return `cocreationPendingMessage:${state.sessionId}:${state.session?.currentVersionId || "none"}`; }
function selectedStageKey() { return `cocreationStage:${state.sessionId}`; }

function readHash() {
    return Object.fromEntries(new URLSearchParams(window.location.hash.replace(/^#/, "")));
}

function syncHash() {
    if (!state.sessionId) return;
    const hash = new URLSearchParams({ session: state.sessionId });
    if (state.selectedVersionId) hash.set("stage", state.selectedVersionId);
    history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${hash}`);
}

function uniqueId(prefix) {
    const value = crypto.randomUUID ? crypto.randomUUID().replaceAll("-", "") : `${Date.now()}${Math.random().toString(16).slice(2)}`;
    return `${prefix}_${value}`.slice(0, 64);
}

function t(key) { return translations[state.language][key] || translations.en[key] || key; }
function makeButton(label, className, handler, options = {}) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    if (options.disabled) {
        button.disabled = true;
        button.setAttribute("aria-disabled", "true");
    }
    if (options.title) button.title = options.title;
    if (handler && !options.disabled) button.addEventListener("click", handler);
    return button;
}
function buildEntityLabels(rows) {
    const labels = new Map();
    const counts = { s: 0, t: 0 };
    (rows || []).forEach((row, y) => [...String(row || "")].forEach((tile, x) => {
        if (tile === "p") labels.set(`${x},${y}`, "P");
        if (tile === "s" || tile === "t") {
            counts[tile] += 1;
            labels.set(`${x},${y}`, `${tile === "s" ? "B" : "T"}${counts[tile]}`);
        }
    }));
    return labels;
}
function formatEntityCoordinate(entityLabel, x, y) {
    return entityLabel
        ? `${entityLabel} (row ${y + 1}, column ${x + 1})`
        : formatTileCoordinate(x, y);
}
function tileClass(tile) { return tile === "#" ? "tile-wall" : tile === "@" ? "tile-water" : tile === "p" ? "tile-player" : tile === "s" ? "tile-box" : tile === "t" ? "tile-target" : tile === "." ? "tile-floor" : "tile-empty"; }
function tileName(tile) { return tile === "#" ? "wall" : tile === "@" ? "water" : tile === "p" ? "player" : tile === "s" ? "box" : tile === "t" ? "target" : tile === "." ? "floor" : "erase"; }
function formatTileCoordinate(x, y) { return `(${y + 1}, ${x + 1})`; }
function tileLabel(tile) { return ({ p: "P", s: "B", t: "T", "@": "~" })[tile] || ""; }
function escapeHtml(value) { const span = document.createElement("span"); span.textContent = String(value ?? ""); return span.innerHTML; }
