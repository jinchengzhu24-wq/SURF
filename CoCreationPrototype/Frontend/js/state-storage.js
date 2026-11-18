"use strict";

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
    chatBusyVersionId: "",
    chatStatus: "idle",
    chatError: null,
    replyFeedback: null,
    chatStartedAt: 0,
    chatTimerId: null,
    deadlineTimerId: null,
    deadlineAnchorSeconds: null,
    deadlineAnchorStartedAt: 0,
    deadlineIdentity: "",
    deadlinePresentationState: "",
    pendingMessage: null,
    pendingBinary: null,
    binaryRefreshTimer: null,
    proposalMode: false,
    questionFeedbackBusy: new Set(),
    assessing: new Set(),
    assessmentIdempotencyKeys: new Map(),
    assessmentRetryCounts: new Map(),
    assessmentRetryTimers: new Map(),
    translating: new Set(),
    translationFailures: new Set(),
    translationRetryAttempts: new Map(),
    translationInProgress: false,
    translationPendingRerun: false,
    translationRemainingCount: 0,
    translationRetryTimerId: null,
    retryAction: null,
    unityPlayActive: false,
    activeCoordinateLink: null,
    renderedMessageStageId: null,
    renderedMessageCount: 0,
    language: "zh-CN",
    landingMode: "demo",
    draftPollTimerId: null,
    draftPollRequestId: "",
    draftPollPromise: null,
    draftRegenerationKey: "",
    demoCreationPending: false,
    demoCreationFailed: false,
    draftRegenerationPending: false
};

const elements = Object.fromEntries([
    "playControlHint", "proposalModeCaption", "noticeTechnicalDetails", "chatTechnicalDetails", "mapControlHint", "chatControlHint", "finalizeControlHint", "sessionCompleteMessage",
    "workspace", "landing", "formalStandaloneBlock", "notice", "noticeMessage", "retryButton", "prototypeStatus", "deadlineStatus", "deadlineLabel", "deadlineValue", "deadlineAnnouncement",
    "landingEyebrow", "landingTitle", "landingBody", "setupChineseButton", "setupEnglishButton", "languageLockNotice", "draftActions", "regenerateDraftButton", "enterSessionButton", "draftPreview", "draftPreviewTitle", "draftGenerationLabel", "draftPreviewGrid", "draftPreviewLoading", "draftPreviewLoadingMessage", "draftPreviewStatus", "stageList", "stageCount", "methodPill", "historyBanner",
    "returnCurrentButton", "progressPanel", "progressSummary", "unresolvedQuestionsList", "answeredQuestionsPanel", "answeredQuestionsSummary", "answeredQuestionsList", "designInclinationsList", "chatScroll", "emptyChat", "messageList", "proposalArea",
    "chatRequestStatus", "replyFeedbackIcon", "chatRetryButton", "chatForm", "messageInput",
    "proposalRequestButton", "sendButton", "characterCount", "selectedStageEyebrow", "mapFrame", "mapBoard", "mapGrid", "mapOverlay",
    "mapToolbar", "mapMode", "validationCard", "saveStageButton", "discardDraftButton",
    "restoreStageButton", "playButton", "playAttemptCount", "playAttemptList", "finalActions",
    "finalizeButton", "intentionForm", "intentionInput", "completeCard", "returnUnityButton", "finalizeModal",
    "cancelFinalizeButton", "confirmFinalizeButton"
].map(id => [id, document.getElementById(id)]));

function unsavedMapDraftKey() {
    return `cocreationUnsavedMap:${state.sessionId}`;
}

function persistUnsavedMapDraft() {
    if (!state.sessionId || !state.session?.currentVersionId || !state.dirty) {
        clearUnsavedMapDraft();
        return;
    }
    localStorage.setItem(unsavedMapDraftKey(), JSON.stringify({
        sessionId: state.sessionId,
        baseVersionId: state.session.currentVersionId,
        rows: state.draftRows.slice(),
        dirty: true,
        savedAt: new Date().toISOString()
    }));
}

function clearUnsavedMapDraft() {
    if (state.sessionId) localStorage.removeItem(unsavedMapDraftKey());
}

function restoreUnsavedMapDraft() {
    if (!state.sessionId
        || !state.session?.currentVersionId
        || state.selectedVersionId !== state.session.currentVersionId) return false;
    try {
        const snapshot = JSON.parse(localStorage.getItem(unsavedMapDraftKey()) || "null");
        const validRows = Array.isArray(snapshot?.rows)
            && snapshot.rows.length === 10
            && snapshot.rows.every(row => typeof row === "string"
                && row.length === 12
                && [...row].every(tile => TILE_ORDER.includes(tile)));
        if (!snapshot
            || snapshot.sessionId !== state.sessionId
            || snapshot.baseVersionId !== state.session.currentVersionId
            || snapshot.dirty !== true
            || !validRows) {
            clearUnsavedMapDraft();
            return false;
        }
        const version = selectedVersion();
        if (!version) {
            clearUnsavedMapDraft();
            return false;
        }
        state.draftRows = snapshot.rows.slice();
        state.dirty = state.draftRows.some(
            (row, index) => row !== version.rows[index]
        );
        if (!state.dirty) {
            clearUnsavedMapDraft();
            return false;
        }
        showNotice(t("unsavedDraftRestored"));
        return true;
    } catch (_error) {
        clearUnsavedMapDraft();
        return false;
    }
}

function discardDraft() {
    state.activeCoordinateLink = null;
    resetDraftFromSelection();
    clearUnsavedMapDraft();
    renderMap();
    updateControls();
}

async function withBusy(action, onError = null) {
    if (state.busy) return;
    state.busy = true;
    hideNotice();
    updateControls();
    try { await action(); }
    catch (error) {
        if (onError) onError();
        if (error?.retryable && state.sessionId) {
            try {
                state.session = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}`);
                render();
            } catch (_refreshError) { /* Preserve the original operation error. */ }
        }
        showError(error, () => withBusy(action, onError));
    }
    finally { state.busy = false; updateControls(); }
}

function validApiPayload(path, payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
    const sessionValid = session => Boolean(session && typeof session.sessionId === "string"
        && typeof session.status === "string" && Array.isArray(session.versions)
        && Array.isArray(session.turns) && Object.hasOwn(session, "currentVersionId"));
    if (/\/(?:questions|intent-hypotheses)\/[^/]+\/feedback$/.test(path)) {
        return typeof payload.outcome === "string" && sessionValid(payload.session);
    }
    if (/\/play-attempts$/.test(path)) {
        return typeof payload.attemptId === "string" && typeof payload.playUrl === "string";
    }
    if (path === "/api/demo-sessions" || /\/browser-access$/.test(path)) {
        return typeof payload.sessionId === "string" && Boolean(payload.sessionId);
    }
    if (/^\/api\/sessions\/[^/]+(?:\/|$)/.test(path)) return sessionValid(payload);
    return Object.keys(payload).length > 0;
}

function cancelComponentRecoveryTimer() {
    if (componentRecovery.timer !== null) window.clearTimeout(componentRecovery.timer);
    componentRecovery.timer = null;
    componentRecovery.scheduled = "";
}

function recoveryPageEligible() {
    return !document.hidden && state.session?.status === "active" && !deadlineExpired()
        && state.selectedVersionId === state.session?.currentVersionId;
}

function scheduleComponentRecovery() {
    if (!recoveryPageEligible()) {
        cancelComponentRecoveryTimer();
        return;
    }
    if (componentRecovery.busy) return;
    if (state.busy || state.chatBusy) return cancelComponentRecoveryTimer();
    const task = (state.session?.interactionState?.recoveries || []).find(item =>
        ["pending", "retry_pending", "claimed", "waiting_message"].includes(item.status)
        && !componentRecovery.paused.has(item.recoveryId)
        && item.baseVersionId === state.session.currentVersionId
        && (["claimed", "waiting_message"].includes(item.status) || !componentRecovery.dispatched.has(item.nextAttemptKey)));
    if (!task) return cancelComponentRecoveryTimer();
    const scheduled = `${state.sessionId}:${task.recoveryId}:${task.status}:${task.nextAttemptKey}`;
    if (componentRecovery.scheduled === scheduled) return;
    cancelComponentRecoveryTimer();
    componentRecovery.scheduled = scheduled;
    const delay = task.status === "claimed"
        ? Math.max(1000, Date.parse(task.leaseUntil || "") - Date.now() + 250)
        : task.status === "waiting_message" ? 1000
        : task.attemptsUsed > 0 ? 3000 : 1000;
    componentRecovery.timer = window.setTimeout(() => {
        componentRecovery.timer = null;
        componentRecovery.scheduled = "";
        void recoverComponent(task);
    }, Math.min(15500, Number.isFinite(delay) ? delay : 15000));
}

function mergeRecoverySession(session, captured) {
    if (!recoveryPageEligible() || state.sessionId !== captured.sessionId
        || state.selectedVersionId !== captured.selectedVersionId
        || state.session.currentVersionId !== captured.versionId
        || session.sessionId !== captured.sessionId || session.currentVersionId !== captured.versionId) return false;
    const lastSequence = value => Math.max(0, ...(value.turns || []).map(turn => turn.sequence || 0));
    if (lastSequence(session) < lastSequence(state.session)
        || (session.interactionState?.revision || 0) < (state.session.interactionState?.revision || 0)
        || (session.updatedAt && state.session.updatedAt && session.updatedAt < state.session.updatedAt)) return false;
    // Session data is separate from composer, captured retries, selection and local map draft.
    state.session = session;
    render();
    return true;
}

function retainNewerInteractionState(session) {
    const current = state.session;
    if (!session?.interactionState || session.sessionId !== current?.sessionId
        || session.currentVersionId !== current.currentVersionId
        || session.interactionState.revision >= (current.interactionState?.revision || 0)) return session;
    const liveTurns = new Map((current.turns || []).map(turn => [turn.turnId, turn]));
    return { ...session, interactionState: current.interactionState,
        turns: (session.turns || []).map(turn => {
            const live = liveTurns.get(turn.turnId);
            return live ? { ...turn, proposalState: live.proposalState } : turn;
        }) };
}

async function recoverComponent(task) {
    if (componentRecovery.busy || state.busy || state.chatBusy || !recoveryPageEligible()
        || task.baseVersionId !== state.session.currentVersionId) return;
    componentRecovery.busy = true;
    const captured = { sessionId: state.sessionId, selectedVersionId: state.selectedVersionId,
        versionId: state.session.currentVersionId };
    try {
        let session;
        if (["claimed", "waiting_message"].includes(task.status)) {
            // A different tab or interrupted request may own the lease. GET never runs a model.
            session = await api(`/api/sessions/${captured.sessionId}`, { timeoutMs: COMPONENT_RECOVERY_TIMEOUT_MS });
        } else {
            if (componentRecovery.dispatched.has(task.nextAttemptKey)) return;
            componentRecovery.dispatched.add(task.nextAttemptKey);
            session = await api(`/api/sessions/${captured.sessionId}/component-recoveries/${task.recoveryId}/retry`, {
                method: "POST", timeoutMs: COMPONENT_RECOVERY_TIMEOUT_MS,
                body: { baseVersionId: task.baseVersionId, idempotencyKey: task.nextAttemptKey }
            });
        }
        const merged = mergeRecoverySession(session, captured);
        if (!merged && state.sessionId === captured.sessionId
            && state.session?.currentVersionId === captured.versionId && !recoveryPageEligible()) {
            // Hidden/history views defer UI delivery. Replay the same server key
            // on return; it cannot spend a second model attempt.
            componentRecovery.dispatched.delete(task.nextAttemptKey);
        }
    } catch (_) {
        // Confirm a server commit without turning auxiliary failure into a chat error.
        try {
            const session = await api(`/api/sessions/${captured.sessionId}`, { timeoutMs: COMPONENT_RECOVERY_TIMEOUT_MS });
            mergeRecoverySession(session, captured);
        } catch (_) { componentRecovery.paused.add(task.recoveryId); }
    } finally {
        componentRecovery.busy = false;
        scheduleComponentRecovery();
    }
}

async function api(path, options = {}) {
    const request = { method: options.method || "GET", credentials: "include", headers: {} };
    const idempotentWrite = request.method !== "GET"
        && typeof options.body?.idempotencyKey === "string" && Boolean(options.body.idempotencyKey);
    const safeRetry = idempotentWrite || request.method === "GET"
        || /\/translations\/(?:en|zh-CN)$/.test(path) || /\/language$/.test(path);
    const controller = options.timeoutMs ? new AbortController() : null;
    const timeoutId = controller ? window.setTimeout(() => controller.abort(), options.timeoutMs) : null;
    if (controller) request.signal = controller.signal;
    if (options.body !== undefined) {
        request.headers["Content-Type"] = "application/json";
        request.body = JSON.stringify(options.body);
    }
    let response;
    let phase = "transport";
    const messageDiagnostic = /\/messages$/.test(path) ? {
        messageKey: options.body?.idempotencyKey, versionId: options.body?.baseVersionId } : null;
    if (messageDiagnostic) recordReplyDiagnostic("transport", "started", messageDiagnostic);
    try {
        response = await fetch(path.startsWith("/api/") ? API_PREFIX + path : path, request);
        phase = "response_body";
        if (messageDiagnostic) {
            Object.assign(messageDiagnostic, { requestId: response.headers?.get?.("X-Request-ID"),
                pipelineVersion: response.headers?.get?.("X-Reply-Pipeline-Version"), httpStatus: response.status });
            recordReplyDiagnostic("response_body", "started", messageDiagnostic);
        }
        let payload;
        try { payload = await response.json(); }
        catch (error) {
            if (error?.name === "AbortError" || controller?.signal.aborted) throw error;
            const unreadable = new Error(t("errorGeneric"));
            unreadable.code = response.ok ? "INVALID_RESPONSE" : "RETRYABLE_REQUEST_FAILED";
            unreadable.retryable = safeRetry && (response.ok || response.status >= 500);
            throw unreadable;
        }
        if (!response.ok) {
            const apiError = new Error(payload?.message || t("errorGeneric"));
            const retryableHttpError = safeRetry && response.status >= 500 && !payload?.code;
            apiError.code = payload?.code || (retryableHttpError ? "RETRYABLE_REQUEST_FAILED" : "REQUEST_FAILED");
            apiError.details = payload?.details || null;
            apiError.retryable = isLevelValidationError(apiError) ? false
                : retryableHttpError || Boolean(payload?.retryable);
            throw apiError;
        }
        if (!validApiPayload(path, payload)) {
            const invalid = new Error(t("errorGeneric"));
            invalid.code = "INVALID_RESPONSE";
            invalid.retryable = safeRetry;
            throw invalid;
        }
        if (messageDiagnostic) {
            replyHttpAttempts.set(messageDiagnostic.messageKey, { ...messageDiagnostic });
            if (replyHttpAttempts.size > 200) replyHttpAttempts.delete(replyHttpAttempts.keys().next().value);
            const turn = (payload.turns || []).find(item => item.role === "assistant" && item.requestId === messageDiagnostic.messageKey);
            recordReplyDiagnostic("response_body", "passed", { ...messageDiagnostic, assistantTurnId: turn?.turnId,
                turnPresent: Boolean(turn), receivedCard: Boolean(turn?.guidance?.intentHypothesis),
                code: turn ? "" : "ASSISTANT_TURN_MISSING" });
        }
        return typeof payload.sessionId === "string" && Array.isArray(payload.turns)
            ? retainNewerInteractionState(payload) : payload;
    } catch (error) {
        if (error?.name === "AbortError" || controller?.signal.aborted) {
            error = new Error(t("error_CLIENT_TIMEOUT"));
            error.code = "CLIENT_TIMEOUT";
            error.retryable = safeRetry;
        } else if (!error?.code && safeRetry) {
            const retryError = new Error(t("errorGeneric"));
            retryError.code = "RETRYABLE_REQUEST_FAILED";
            retryError.retryable = true;
            error = retryError;
        }
        if (messageDiagnostic) recordReplyDiagnostic(phase, "failed", { ...messageDiagnostic,
            code: ["CLIENT_TIMEOUT", "INVALID_RESPONSE", "RETRYABLE_REQUEST_FAILED"].includes(error.code) ? error.code : "REQUEST_FAILED" });
        error.details = { ...(error.details || {}), httpStatus: response?.status,
            requestId: response?.headers?.get?.("X-Request-ID") || error.details?.requestId,
            contentType: response?.headers?.get?.("Content-Type"), failureStage: error.details?.failureStage || phase };
        throw error;
    } finally {
        if (timeoutId !== null) window.clearTimeout(timeoutId);
    }
}

function recoverMessageFailure(error, session, pending) {
    if (!["RETRYABLE_REQUEST_FAILED", "INVALID_RESPONSE", "CLIENT_TIMEOUT"].includes(error?.code)) return error;
    const failure = session?.lastMessageFailure;
    if (!failure || failure.messageKey !== pending?.idempotencyKey
        || failure.baseVersionId !== pending?.baseVersionId || failure.committed !== false
        || typeof failure.code !== "string" || !failure.code) return error;
    const recovered = new Error(error.message);
    recovered.code = failure.code;
    recovered.retryable = Boolean(failure.retryable);
    recovered.details = { ...(error.details || {}), ...(failure.details || {}),
        requestId: failure.requestId || error.details?.requestId, committed: false };
    return recovered;
}

function showError(error, retryAction) {
    if (state.session && !elements.workspace.hidden) {
        showReplyNotice("", error, error.retryable ? retryAction : null,
            retryAction?.assessmentVersionId || state.selectedVersionId);
        return;
    }
    showNotice(localizedErrorMessage(error), error.retryable ? retryAction : null);
    renderTechnicalDetails(elements.noticeTechnicalDetails, error);
}

function localizedErrorMessage(error) {
    if (error?.details?.failureStage === "context_binding") return t("contextChanged");
    if (error?.code === "PENDING_MESSAGE") return t("chatRetryPending");
    if (error?.code === "PROPOSAL_PRECONDITION_FAILED") {
        const reason = error.details?.reason;
        if (reason === "already_satisfied") return t("proposalAlreadySatisfied");
        if (reason === "unbound") return t("proposalUnbound");
        if (reason === "already_executed") return t("proposalAlreadyExecuted");
        return t("proposalPreconditionFailed");
    }
    const groups = {
        replyFailed: ["UPSTREAM_TIMEOUT", "UPSTREAM_CONNECTION_ERROR", "UPSTREAM_SERVER_ERROR", "MODEL_EMPTY_RESPONSE", "MODEL_RESPONSE_INVALID", "MODEL_LOW_QUALITY_RESPONSE", "CLIENT_TIMEOUT", "RETRYABLE_REQUEST_FAILED", "INVALID_RESPONSE", "CHALLENGE_REVIEW_FAILED"],
        modificationFailed: ["REVISION_EXECUTION_INVALID", "REVISION_EXECUTION_MISMATCH", "SEMANTIC_POSTCONDITION_FAILED", "EXECUTION_REPLAY_MISMATCH", "CANDIDATE_UNSOLVABLE"],
        checkIncomplete: ["SEARCH_BUDGET_EXCEEDED"],
        oldProposal: ["INVALID_CARD_SOURCE", "PROPOSAL_STALE"],
        noProposal: ["PROPOSAL_SEARCH_EXHAUSTED", "DETERMINISTIC_SEARCH_EXHAUSTED", "SEMANTIC_CONSTRAINT_NOT_MET"],
        requirementsUnclear: ["REQUIREMENT_INTERPRETATION_INVALID", "REQUIREMENT_EVIDENCE_MISSING"],
        permissionConflict: ["REQUIREMENT_PERMISSION_CONFLICT"],
        unsolved: ["UNSOLVABLE_LEVEL"],
        needsDiscussion: ["DISAGREEMENT_ACTIVE"],
        noAlternative: ["CANDIDATE_DUPLICATED"],
    };
    for (const [key, codes] of Object.entries(groups)) {
        if (codes.includes(error?.code)) return t(key);
    }
    const special = {
        IDEMPOTENCY_CONFLICT: ["这次操作没有完成，请重新发起。", "This action could not be completed. Start it again."],
        INVALID_PLAY_TICKET: ["这次试玩链接不能使用，请回到工作台重新点试玩。", "This play link is unavailable. Return to the workspace and select Play again."],
        PLAY_TICKET_USED: ["这次试玩链接已用过，请回到工作台重新点试玩。", "This play link has been used. Return to the workspace and select Play again."],
        PLAY_TICKET_EXPIRED: ["这次试玩链接已过期，请回到工作台重新点试玩。", "This play link has expired. Return to the workspace and select Play again."],
        CONFIGURATION_ERROR: ["回复服务暂时不可用，请稍后再试。", "Replies are temporarily unavailable. Please try again later."],
    };
    if (special[error?.code]) return special[error.code][state.language === "zh-CN" ? 0 : 1];
    const message = state.language === "zh-CN"
        ? chineseApiErrors[error?.code] : translations.en[`error_${error?.code}`];
    const details = formatValidationDetails(error);
    return [message || t("errorGeneric"), details].filter(Boolean).join(" ");
}

function technicalDetailsLabel() { return state.language === "zh-CN" ? "技术详情" : "Technical details"; }

function technicalDetailsMarkup(error) {
    if (!error) return "";
    const code = /^[A-Z0-9_]{1,100}$/.test(error.code || "") ? error.code : "";
    return `<details class="technical-details"><summary>${escapeHtml(technicalDetailsLabel())}</summary><p>${escapeHtml(code)} ${escapeHtml(technicalErrorMessage(error))}</p></details>`;
}

function renderTechnicalDetails(target, error) {
    if (!target) return;
    target.hidden = !error;
    if (!error) { target.innerHTML = ""; target.open = false; return; }
    const code = /^[A-Z0-9_]{1,100}$/.test(error.code || "") ? error.code : "";
    target.innerHTML = `<summary>${escapeHtml(technicalDetailsLabel())}</summary><p>${escapeHtml(code)} ${escapeHtml(technicalErrorMessage(error))}</p>`;
}

function renderControlHints() {
    const session = state.session;
    if (!session) {
        elements.chatRequestStatus.hidden = true;
        elements.chatControlHint.hidden = true;
        return;
    }
    const common = deadlineExpired() ? "deadlineExpiredAnnouncement"
        : session.status !== "active" ? "closedHint"
        : state.unityPlayActive ? "playActiveHint"
        : "";
    const historical = state.selectedVersionId !== session.currentVersionId;
    const pending = Boolean(currentPendingProposal());
    const generationPending = state.chatStatus === "waiting"
        || state.assessmentRetryTimers.size > 0 || state.assessing.size > 0;
    const flow = session.proposalFlowState?.status;
    const save = elements.saveStageButton.disabled
        ? common || (historical ? "historyHint" : "") : "";
    const play = elements.playButton.disabled
        ? (state.busy || state.unityPlayActive || deadlineExpired() ? common : "")
            || (state.dirty ? "dirtyPlayHint" : pending ? "pendingHint" : !selectedVersion() ? "noVersionHint" : "") : "";
    const chat = elements.messageInput.disabled || elements.sendButton.disabled
        ? common || (historical ? "historyHint" : selectedStageHasPendingIntentConflict() ? "conflictHint"
            : flow === "planning" ? "proposalPlanningPlaceholder"
            : flow === "retry_pending" && state.pendingMessage ? "retryHint"
            : "") : "";
    const final = elements.finalizeButton.disabled
        ? (state.busy || state.unityPlayActive ? common : "")
            || (generationPending ? "" : !selectedVersion() ? "noVersionHint"
                : !historical && state.dirty ? "finalDirtyHint" : !historical && pending ? "pendingHint" : "") : "";
    const display = (node, value) => { if (node) { node.textContent = value; node.hidden = !value; } };
    display(elements.mapControlHint, save ? t(save) : "");
    display(elements.playControlHint, play ? t(play) : "");
    renderReplyFeedback(chat ? t(chat) : "");
    display(elements.finalizeControlHint, final ? t(final) : "");
    display(elements.proposalModeCaption, t(proposalFlowActive() ? "proposalTopicActive"
        : state.proposalMode ? "proposalModeOn" : "proposalModeOff"));
}

function technicalErrorMessage(error) {
    if (error?.details?.failureStage === "context_binding") {
        return state.language === "zh-CN"
            ? "对话或交互状态已经更新，请根据当前对话重新发送请求。地图未因这次请求改变。"
            : "The dialogue or interaction state changed. Send a request based on the current dialogue. This request did not change the map.";
    }
    if (error?.code === "PENDING_MESSAGE") return t("chatRetryPending");
    if (error?.code === "PROPOSAL_PRECONDITION_FAILED") {
        const reason = error.details?.reason;
        if (reason === "already_satisfied") return t("proposalAlreadySatisfied");
        if (reason === "unbound") return t("proposalUnbound");
        if (reason === "already_executed") return t("proposalAlreadyExecuted");
        return t("proposalPreconditionFailed");
    }
    if (error?.code === "PROPOSAL_STALE") return t("proposalStale");
    const localized = state.language === "zh-CN"
        ? ORIGINAL_ERROR_COPY["zh-CN"][error?.code]
        : ORIGINAL_ERROR_COPY.en[`error_${error?.code}`];
    const componentMessage = error?.details?.failureStage === "clarification_review"
        ? (state.language === "zh-CN"
            ? "方案澄清的复核未完成，可使用原消息安全重试。"
            : "The proposal clarification review could not be verified. Retry the original message safely.")
        : null;
    const message = componentMessage || localized || t("errorGeneric");
    const attemptsUsed = Number(error?.details?.attemptsUsed);
    const maximumAttempts = Number(error?.details?.maximumAttempts);
    const attemptSuffix = Number.isInteger(attemptsUsed) && Number.isInteger(maximumAttempts)
        && maximumAttempts > 0 && attemptsUsed >= 0 && attemptsUsed <= maximumAttempts
        && error?.details?.maximumAttempts != null && error?.details?.attemptsUsed != null
        ? ` (${attemptsUsed}/${maximumAttempts})`
        : "";
    const repairUsed = error?.details?.repairAttemptsUsed;
    const repairMaximum = error?.details?.maximumRepairAttempts;
    const repairSuffix = Number.isInteger(repairUsed) && Number.isInteger(repairMaximum)
        && repairMaximum > 0 && repairUsed >= 0 && repairUsed <= repairMaximum
        ? ` · ${state.language === "zh-CN" ? "局部修复" : "Component repair"} ${repairUsed}/${repairMaximum}`
        : "";
    const details = formatValidationDetails(error);
    return details ? `${message}${attemptSuffix}${repairSuffix} ${details}` : `${message}${attemptSuffix}${repairSuffix}`;
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
    renderTechnicalDetails(elements.noticeTechnicalDetails, null);
    elements.noticeMessage.textContent = message;
    elements.retryButton.hidden = !retryAction;
    elements.notice.classList.add("visible");
}

function showPlayReturnNotice(status) {
    if (status === "sync_failed") showNotice(t("playSyncFailed"));
    if (status === "load_failed") showNotice(t("playLoadFailed"));
}

function hideNotice() {
    state.replyFeedback = null;
    state.retryAction = null;
    renderTechnicalDetails(elements.noticeTechnicalDetails, null);
    elements.notice.classList.remove("visible");
    elements.retryButton.hidden = true;
}

function setStatus(label, status) {
    elements.prototypeStatus.className = `prototype-status ${status}`;
    elements.prototypeStatus.querySelector("span:last-child").textContent = label;
}

function handleComposerInput() {
    if (state.sessionId) localStorage.setItem(composerKey(), elements.messageInput.value);
    updateCharacterCount();
    updateControls();
}

function restoreComposerDraft() {
    elements.messageInput.value = canEditSelected()
        ? localStorage.getItem(composerKey()) || ""
        : "";
    const proposalModeAllowed = canEditSelected() && !selectedStageHasActiveDisagreement();
    state.proposalMode = proposalModeAllowed
        && localStorage.getItem(proposalModeKey()) === "on";
    if (!proposalModeAllowed) localStorage.removeItem(proposalModeKey());
    updateCharacterCount();
    updateControls();
}

function recoverPendingMessage() {
    if (binaryFlowActive()) {
        state.pendingMessage = null;
        return;
    }
    const stored = readPendingMessage();
    const turns = (state.session?.turns || []).filter(
        turn => turn.versionId === state.session.currentVersionId
    );
    const finalTurn = turns[turns.length - 1];
    if (state.session?.proposalFlowState?.interactionMode === "binary_choice"
        && state.session.proposalFlowState.recordedUserTurnIds?.includes(finalTurn?.turnId)) {
        clearPendingMessage();
        return;
    }
    const unmatched = finalTurn?.role === "user"
        && finalTurn.requestId
        && !turns.some(turn => turn.role === "assistant" && turn.requestId === finalTurn.requestId)
        ? {
            content: finalTurn.content,
            baseVersionId: finalTurn.versionId,
            idempotencyKey: finalTurn.requestId,
            requestProposal: finalTurn.requestProposal === true
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
    setProposalMode(pending.requestProposal === true);
    persistPendingMessage();
    elements.messageInput.value = pending.content;
    localStorage.setItem(composerKey(), pending.content);
    const pendingError = new Error(t("chatRetryPending"));
    pendingError.code = "PENDING_MESSAGE";
    pendingError.retryable = true;
    state.chatStatus = "error";
    state.chatError = pendingError;
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
        if (
            pending.requestProposal !== undefined
            && typeof pending.requestProposal !== "boolean"
        ) return null;
        if (pending.action !== undefined && ![
            "none",
            "execute_revision",
            "challenge_revision",
            "alternative_revision",
            "continue_challenge"
        ].includes(pending.action)) return null;
        if (
            ["execute_revision", "challenge_revision", "alternative_revision"].includes(pending.action)
            && typeof pending.sourceTurnId !== "string"
        ) return null;
        if (
            pending.action === "continue_challenge"
            && typeof pending.challengeId !== "string"
        ) return null;
        if (pending.challengeChoice !== undefined && !["ai", "user"].includes(pending.challengeChoice)) return null;
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

function setProposalMode(enabled) {
    state.proposalMode = Boolean(enabled);
    if (!state.sessionId || !state.session) return;
    if (state.proposalMode) localStorage.setItem(proposalModeKey(), "on");
    else localStorage.removeItem(proposalModeKey());
}

function updateCharacterCount() { elements.characterCount.textContent = `${elements.messageInput.value.length} / ${MAX_MESSAGE_LENGTH}`; }
function handleComposerKeydown(event) { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); elements.chatForm.requestSubmit(); } }
function composerKey() { return `cocreationComposer:${state.sessionId}:${state.session?.currentVersionId || "none"}`; }
function pendingMessageKey() { return `cocreationPendingMessage:${state.sessionId}:${state.session?.currentVersionId || "none"}`; }
function proposalModeKey() { return `cocreationProposalMode:${state.sessionId}:${state.selectedVersionId || "none"}`; }
function selectedStageKey() { return `cocreationStage:${state.sessionId}`; }
function progressPanelKey() { return `${state.sessionId}:${state.selectedVersionId || "none"}`; }
function progressPanelStorageKey() { return `cocreationProgress:${progressPanelKey()}`; }

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

function makeButton(label, className, handler, options = {}) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    if (options.disabled) {
        button.disabled = true;
        button.setAttribute("aria-disabled", "true");
    }
    const helpKey = Object.keys(UI_ACTION_HELP).find(key => translations[state.language]?.[key] === label);
    if (helpKey) button.dataset.uiHelpKey = helpKey;
    if (className.includes("challenge-choice-button")) button.dataset.uiHelpKey = "challengeChoice";
    if (options.title) button.dataset.uiHelpStatus = options.title;
    if (handler && !options.disabled) button.addEventListener("click", handler);
    return button;
}
function setButtonDisabled(button, disabled) {
    button.disabled = disabled;
    if (disabled) button.setAttribute("aria-disabled", "true");
    else button.removeAttribute("aria-disabled");
}
function escapeHtml(value) { const span = document.createElement("span"); span.textContent = String(value ?? ""); return span.innerHTML; }
