"use strict";

const MAX_MESSAGE_LENGTH = 2000;
const SESSION_STORAGE_KEY = "sokobanCoCreationSession";
const TILE_ORDER = [".", "#", "@", "p", "s", "t", " "];

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
        versionHistory: "Version history",
        stages: "Stages",
        conversation: "Co-creation conversation",
        discuss: "Discuss and refine this Stage",
        viewingHistory: "You are viewing a historical Stage. Chat remains grounded in the latest Stage.",
        returnCurrent: "Return to current",
        assessmentPending: "Preparing the first Stage assessment",
        assessmentPendingBody: "The assistant will discuss the verified current map and ask what you want to refine.",
        thinking: "Assistant is considering the current Stage...",
        chatWaiting: "The assistant is generating a response. This may take up to 60 seconds.",
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
        intentionQuestion: "What experience did you want this level to create for another player?",
        completeSession: "Complete session",
        sessionComplete: "Session complete",
        sessionCompleteBody: "The final Stage, intention, conversation and play evidence are safely recorded. Return to the original Unity tab to continue.",
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
        steps: "steps",
        pushes: "pushes",
        editMode: "Editing the current Stage. Save explicitly to create a version.",
        readOnlyMode: "Historical Stage: read-only until restored as a new Stage.",
        lockedMode: "The final Stage is locked.",
        unsaved: "Unsaved draft",
        proposal: "Map proposal",
        suggestedDirection: "Assistant design direction",
        draftSuggestedRevision: "Ask the assistant to draft this",
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
        playSyncFailed: "The Stage was completed, but the play result could not be synchronized. This attempt will be recorded as interrupted.",
        playLoadFailed: "The selected Stage could not be loaded in Unity. Please review the Stage and try again."
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
        versionHistory: "版本历史",
        stages: "Stages",
        conversation: "共创对话",
        discuss: "讨论并继续完善当前 Stage",
        viewingHistory: "你正在查看历史 Stage；聊天仍以最新 Stage 为准。",
        returnCurrent: "返回当前版本",
        assessmentPending: "正在准备首版评价",
        assessmentPendingBody: "助手会围绕已验证的当前地图展开讨论，并询问你希望如何继续完善。",
        thinking: "助手正在分析当前 Stage……",
        chatWaiting: "助手正在生成回复，最长可能需要 60 秒。",
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
        intentionQuestion: "你希望这个关卡为另一位玩家带来怎样的体验？",
        completeSession: "完成会话",
        sessionComplete: "会话已完成",
        sessionCompleteBody: "最终 Stage、设计意图、对话和试玩证据均已安全记录。请返回原来的 Unity 标签页继续。",
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
        steps: "步",
        pushes: "次推动",
        editMode: "正在编辑当前 Stage；只有明确保存后才会产生新版本。",
        readOnlyMode: "历史 Stage 只读；选择“从这个 Stage 继续”才会生成新版本。",
        lockedMode: "最终 Stage 已锁定。",
        unsaved: "未保存草稿",
        proposal: "地图修改提案",
        suggestedDirection: "助手提出的设计方向",
        draftSuggestedRevision: "请助手具体生成这个方案",
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
        playSyncFailed: "关卡已经通关，但本次试玩结果未能同步；该记录之后会标记为异常中断。",
        playLoadFailed: "所选 Stage 未能在 Unity 中加载，请检查该版本后重试。"
    }
};

const state = {
    session: null,
    sessionId: "",
    selectedVersionId: "",
    draftRows: [],
    dirty: false,
    selectedTile: ".",
    busy: false,
    chatBusy: false,
    chatStatus: "idle",
    chatError: null,
    pendingMessage: null,
    assessing: new Set(),
    retryAction: null,
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
    UNSOLVABLE_LEVEL: "确定性求解器未能验证这张地图可解。",
    UPSTREAM_TIMEOUT: "LLM 响应超时，请稍后重试。",
    UPSTREAM_CONNECTION_ERROR: "暂时无法连接 LLM 服务，请稍后重试。",
    CONFIGURATION_ERROR: "服务器尚未正确配置 LLM 服务。"
};

const elements = Object.fromEntries([
    "workspace", "landing", "notice", "noticeMessage", "retryButton", "prototypeStatus",
    "languageButton", "demoButton", "stageList", "stageCount", "methodPill", "historyBanner",
    "returnCurrentButton", "chatScroll", "emptyChat", "messageList", "typingRow", "proposalArea",
    "chatRequestStatus", "chatRequestMessage", "chatRetryButton", "chatForm", "messageInput",
    "sendButton", "characterCount", "selectedStageEyebrow", "mapGrid",
    "mapToolbar", "mapMode", "validationCard", "saveStageButton", "discardDraftButton",
    "restoreStageButton", "playButton", "playAttemptCount", "playAttemptList", "finalActions",
    "finalizeButton", "intentionForm", "intentionInput", "completeCard", "finalizeModal",
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
elements.finalizeButton.addEventListener("click", () => elements.finalizeModal.hidden = false);
elements.cancelFinalizeButton.addEventListener("click", () => elements.finalizeModal.hidden = true);
elements.confirmFinalizeButton.addEventListener("click", finalizeSession);
elements.intentionForm.addEventListener("submit", submitIntention);

applyTranslations();
initialize();

async function initialize() {
    const hash = readHash();
    state.sessionId = hash.session || localStorage.getItem(SESSION_STORAGE_KEY) || "";

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
    await withBusy(async () => {
        const sample = await api("/api/sample");
        const created = await api("/api/sessions", {
            method: "POST",
            body: {
                rows: sample.rows,
                initialDraftMethod: "description_generation",
                language: state.language,
                idempotencyKey: uniqueId("demo")
            }
        });
        window.location.assign(created.launchUrl);
    });
}

async function refreshSession() {
    state.session = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}`);
    state.language = state.session.language;

    if (!findVersion(state.selectedVersionId)) {
        state.selectedVersionId = state.session.currentVersionId;
    }

    localStorage.setItem(selectedStageKey(), state.selectedVersionId);
    syncHash();
    resetDraftFromSelection();
    render();
    await ensureAssessment(state.session.currentVersionId);
}

function render() {
    elements.landing.hidden = true;
    elements.workspace.hidden = false;
    setStatus(t("ready"), "ready");
    applyTranslations();
    elements.methodPill.textContent = t(state.session.initialDraftMethod);
    renderStages();
    renderMessages();
    renderProposal();
    renderMap();
    renderSessionState();
    renderChatRequestStatus();
    updateControls();
}

function renderStages() {
    elements.stageList.textContent = "";
    elements.stageCount.textContent = String(state.session.versions.length);

    state.session.versions.slice().reverse().forEach(version => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "stage-card";
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
    elements.emptyChat.hidden = state.session.turns.length > 0;

    state.session.turns.forEach(turn => {
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
        bubble.textContent = turn.content;
        content.appendChild(bubble);
        if (turn.role === "assistant" && turn.guidance && turn.guidance.proposalOffer) {
            content.appendChild(createGuidanceOffer(turn.guidance.proposalOffer));
        }
        row.appendChild(content);
        elements.messageList.appendChild(row);
    });
    requestAnimationFrame(() => elements.chatScroll.scrollTop = elements.chatScroll.scrollHeight);
}

function createGuidanceOffer(offer) {
    const card = document.createElement("section");
    card.className = "guidance-offer";
    card.innerHTML = `
        <p class="eyebrow">${escapeHtml(t("suggestedDirection"))}</p>
        <strong>${escapeHtml(offer.summary || "")}</strong>
        <p>${escapeHtml(offer.rationale || "")}</p>`;
    card.appendChild(makeButton(
        t("draftSuggestedRevision"),
        "secondary-button guidance-offer-button",
        () => prefillProposalConsent(offer)
    ));
    return card;
}

function prefillProposalConsent(offer) {
    const summary = String(offer.summary || "").trim();
    elements.messageInput.value = t("proposalConsent").replace("{summary}", summary);
    handleComposerInput();
    elements.messageInput.focus();
    elements.messageInput.setSelectionRange(elements.messageInput.value.length, elements.messageInput.value.length);
}

function renderProposal() {
    elements.proposalArea.textContent = "";
    const proposal = currentPendingProposal();
    if (!proposal) return;
    const card = document.createElement("section");
    card.className = "proposal-card";
    card.innerHTML = `
        <div class="proposal-heading"><div><p class="eyebrow">${escapeHtml(t("proposal"))}</p><h3>${escapeHtml(proposal.summary || t("proposal"))}</h3></div><span>${proposal.diff.length} ${escapeHtml(t("changedTiles"))}</span></div>
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

function renderMap() {
    const version = selectedVersion();
    if (!version) return;
    elements.selectedStageEyebrow.textContent = `${t("stage").toUpperCase()} ${version.stageNumber}`;
    elements.mapGrid.textContent = "";
    elements.mapGrid.style.gridTemplateColumns = "repeat(12, var(--tile-size))";
    state.draftRows.forEach((row, y) => {
        [...row].forEach((tile, x) => {
            const cell = document.createElement("button");
            cell.type = "button";
            cell.className = `tile ${tileClass(tile)}`;
            cell.textContent = tileLabel(tile);
            cell.title = t(tileName(tile));
            cell.disabled = !canEditSelected();
            if (version.diff.some(change => change.x === x && change.y === y)) cell.classList.add("changed");
            cell.addEventListener("click", () => editTile(x, y));
            elements.mapGrid.appendChild(cell);
        });
    });
    renderToolbar();
    const validation = version.validation;
    elements.validationCard.innerHTML = `
        <strong>✓ ${escapeHtml(t("verified"))}</strong>
        <span>${validation.solutionSteps} ${escapeHtml(t("steps"))} · ${validation.solutionPushes} ${escapeHtml(t("pushes"))} · ${validation.searchedStates} states</span>`;
    elements.mapMode.textContent = state.session.status !== "active" ? t("lockedMode") : version.versionId === state.session.currentVersionId ? t("editMode") : t("readOnlyMode");
    if (state.dirty) elements.mapMode.textContent += ` · ${t("unsaved")}`;
    renderAttempts(version);
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
    elements.historyBanner.hidden = state.selectedVersionId === state.session.currentVersionId;
}

function updateControls() {
    if (!state.session) {
        elements.demoButton.disabled = state.busy;
        return;
    }

    const editable = canEditSelected();
    const pending = Boolean(currentPendingProposal());
    elements.saveStageButton.disabled = !editable || !state.dirty || state.busy;
    elements.discardDraftButton.disabled = !editable || !state.dirty || state.busy;
    elements.restoreStageButton.hidden = state.selectedVersionId === state.session.currentVersionId || state.session.status !== "active";
    elements.restoreStageButton.disabled = state.busy;
    elements.playButton.disabled = state.busy || state.dirty || pending || !selectedVersion();
    elements.finalizeButton.disabled = state.busy || state.dirty || pending || state.selectedVersionId !== state.session.currentVersionId;
    elements.messageInput.disabled = state.busy || state.session.status !== "active";
    elements.sendButton.disabled = state.busy || state.session.status !== "active" || !elements.messageInput.value.trim();
    elements.sendButton.textContent = state.chatBusy ? t("sending") : t("send");
    elements.typingRow.hidden = !state.chatBusy;
}

function renderChatRequestStatus() {
    const waiting = state.chatStatus === "waiting";
    const failed = state.chatStatus === "error";
    elements.chatRequestStatus.hidden = !waiting && !failed;
    elements.chatRequestStatus.className = `chat-request-status ${waiting ? "waiting" : "error"}`;
    elements.chatRequestMessage.textContent = waiting
        ? t("chatWaiting")
        : localizedErrorMessage(state.chatError);
    elements.chatRetryButton.hidden = !failed || !state.chatError?.retryable || !state.pendingMessage;
    elements.chatRetryButton.textContent = t("retry");
}

async function ensureAssessment(versionId) {
    if (!state.session || state.session.assessments.some(item => item.versionId === versionId) || state.assessing.has(versionId)) return;
    state.assessing.add(versionId);
    try {
        state.session = await api(`/api/sessions/${state.sessionId}/versions/${versionId}/assessments`, {
            method: "POST",
            body: { idempotencyKey: uniqueId("assessment") }
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
    hideNotice();
    renderChatRequestStatus();
    updateControls();

    try {
        state.session = await api(`/api/sessions/${state.sessionId}/messages`, {
            method: "POST",
            body: pending
        });
        elements.messageInput.value = "";
        localStorage.removeItem(composerKey());
        clearPendingMessage();
        state.chatStatus = "idle";
        state.chatError = null;
        updateCharacterCount();
        render();
    } catch (error) {
        state.chatStatus = "error";
        state.chatError = error;
    } finally {
        state.busy = false;
        state.chatBusy = false;
        renderChatRequestStatus();
        updateControls();
    }
}

async function saveManualStage() {
    if (!state.dirty) return;
    await withBusy(async () => {
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
        if (decision === "accept") await ensureAssessment(state.session.currentVersionId);
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
            body: { baseVersionId: state.session.currentVersionId, idempotencyKey: uniqueId("finalize") }
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
}

function selectVersion(versionId, shouldRender = true) {
    if (!findVersion(versionId)) return;
    state.selectedVersionId = versionId;
    localStorage.setItem(selectedStageKey(), versionId);
    syncHash();
    resetDraftFromSelection();
    if (shouldRender) render();
}

function selectCurrentVersion() { selectVersion(state.session.currentVersionId); }
function selectedVersion() { return findVersion(state.selectedVersionId); }
function findVersion(versionId) { return state.session && state.session.versions.find(item => item.versionId === versionId); }

function resetDraftFromSelection() {
    const version = selectedVersion();
    state.draftRows = version ? version.rows.slice() : [];
    state.dirty = false;
}

function discardDraft() { resetDraftFromSelection(); renderMap(); updateControls(); }

function editTile(x, y) {
    if (!canEditSelected()) return;
    const row = [...state.draftRows[y]];
    row[x] = state.selectedTile;
    state.draftRows[y] = row.join("");
    state.dirty = state.draftRows.some((value, index) => value !== selectedVersion().rows[index]);
    renderMap();
    updateControls();
}

function canEditSelected() {
    return state.session && state.session.status === "active" && state.selectedVersionId === state.session.currentVersionId;
}

function currentPendingProposal() {
    return state.session && state.session.proposals.slice().reverse().find(item => item.status === "pending" && item.baseVersionId === state.session.currentVersionId);
}

function createMiniMap(rows, diff, extraClass = "") {
    const map = document.createElement("div");
    map.className = `mini-map ${extraClass}`;
    map.style.gridTemplateColumns = "repeat(12, 1fr)";
    const changed = new Set((diff || []).map(item => `${item.x},${item.y}`));
    rows.forEach((row, y) => [...row].forEach((tile, x) => {
        const cell = document.createElement("i");
        cell.className = tileClass(tile);
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

async function withBusy(action) {
    if (state.busy) return;
    state.busy = true;
    hideNotice();
    updateControls();
    try { await action(); }
    catch (error) { showError(error, () => withBusy(action)); }
    finally { state.busy = false; updateControls(); }
}

async function api(path, options = {}) {
    const request = { method: options.method || "GET", credentials: "include", headers: {} };
    if (options.body !== undefined) {
        request.headers["Content-Type"] = "application/json";
        request.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, request);
    let payload = {};
    try { payload = await response.json(); } catch (error) { payload = {}; }
    if (!response.ok) {
        const apiError = new Error(payload.message || t("errorGeneric"));
        apiError.code = payload.code || "REQUEST_FAILED";
        apiError.retryable = Boolean(payload.retryable);
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
    return localized
        || (state.language === "zh-CN" ? t("errorGeneric") : error?.message)
        || t("errorGeneric");
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
    elements.messageInput.value = localStorage.getItem(composerKey()) || "";
    updateCharacterCount();
    updateControls();
}

function recoverPendingMessage() {
    const stored = readPendingMessage();
    const turns = state.session?.turns || [];
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
        const pending = JSON.parse(localStorage.getItem(pendingMessageKey()) || "null");
        if (
            !pending
            || typeof pending.content !== "string"
            || typeof pending.baseVersionId !== "string"
            || typeof pending.idempotencyKey !== "string"
        ) return null;
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
}

function updateCharacterCount() { elements.characterCount.textContent = `${elements.messageInput.value.length} / ${MAX_MESSAGE_LENGTH}`; }
function handleComposerKeydown(event) { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); elements.chatForm.requestSubmit(); } }
function composerKey() { return `cocreationComposer:${state.sessionId}`; }
function pendingMessageKey() { return `cocreationPendingMessage:${state.sessionId}`; }
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
function makeButton(label, className, handler) { const button = document.createElement("button"); button.type = "button"; button.className = className; button.textContent = label; button.addEventListener("click", handler); return button; }
function tileClass(tile) { return tile === "#" ? "tile-wall" : tile === "@" ? "tile-water" : tile === "p" ? "tile-player" : tile === "s" ? "tile-box" : tile === "t" ? "tile-target" : tile === "." ? "tile-floor" : "tile-empty"; }
function tileName(tile) { return tile === "#" ? "wall" : tile === "@" ? "water" : tile === "p" ? "player" : tile === "s" ? "box" : tile === "t" ? "target" : tile === "." ? "floor" : "erase"; }
function tileLabel(tile) { return ({ p: "P", s: "B", t: "T", "@": "~" })[tile] || ""; }
function escapeHtml(value) { const span = document.createElement("span"); span.textContent = String(value ?? ""); return span.innerHTML; }
