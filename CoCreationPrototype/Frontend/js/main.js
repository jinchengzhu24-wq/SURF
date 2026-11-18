"use strict";

async function initialize() {
    const hash = readHash();
    state.sessionId = hash.session || "";

    if (!state.sessionId) {
        state.session = null;
        state.language = "zh-CN";
        void createDemoSession();
        return;
    }

    try {
        if (hash.bootstrap) {
            try {
                await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/browser-access`, {
                    method: "POST",
                    body: { bootstrapToken: hash.bootstrap }
                });
            } catch (error) {
                // A same-browser iframe or named-window recovery may reuse the
                // one-time URL after the HttpOnly access cookie was issued.
                // The following session read still fails closed without it.
                if (error?.code !== "BOOTSTRAP_TOKEN_USED") throw error;
            }
            const cleanHash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
            cleanHash.delete("bootstrap");
            history.replaceState(
                null,
                "",
                `${window.location.pathname}${window.location.search}#${cleanHash}`
            );
        }

        localStorage.setItem(SESSION_STORAGE_KEY, state.sessionId);
        state.selectedVersionId = hash.stage || localStorage.getItem(selectedStageKey()) || "";
        const enteredWorkspace = await refreshSession();
        if (!enteredWorkspace) return;
        restoreComposerDraft();
        recoverPendingMessage();
        showPlayReturnNotice(hash.playReturn);
    } catch (error) {
        showLanding();
        showError(error, initialize);
    }
}

async function createDemoSession() {
    if (state.busy || state.demoCreationPending) return;
    state.demoCreationPending = true;
    state.demoCreationFailed = false;
    state.busy = true;
    hideNotice();
    showLanding("demo-loading");
    try {
        const created = await api("/api/demo-sessions", {
            method: "POST",
            body: {
                language: state.language,
                idempotencyKey: uniqueId("demo")
            }
        });
        await openCreatedSession(created.launchUrl);
    } catch (error) {
        state.demoCreationFailed = true;
        showError(error, () => void createDemoSession());
    } finally {
        state.busy = false;
        state.demoCreationPending = false;
        if (state.session?.demoMode && !state.session.languageLocked) {
            showLanding("demo-preview");
        } else if (!state.session) {
            showLanding("demo-error");
        }
    }
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
    announceEmbeddedLabReady();

    if (!state.session.demoMode && !hasEmbeddedFormalHost()) {
        showFormalStandaloneBlock();
        return false;
    }
    elements.formalStandaloneBlock.hidden = true;

    if (!state.session.languageLocked) {
        state.language = state.session.language || state.language;
        showLanding(state.session.demoMode ? "demo-preview" : "formal");
        resumeDraftRegenerationPolling();
        return false;
    }

    state.language = state.session.language;

    if (!findVersion(state.selectedVersionId)) {
        state.selectedVersionId = state.session.currentVersionId;
    }

    localStorage.setItem(selectedStageKey(), state.selectedVersionId);
    syncHash();
    state.activeCoordinateLink = null;
    resetDraftFromSelection();
    restoreUnsavedMapDraft();
    render();
    void ensureVisibleTranslations();
    await ensureAssessment(state.session.currentVersionId);
    return true;
}

function render() {
    elements.formalStandaloneBlock.hidden = true;
    elements.landing.hidden = true;
    elements.workspace.hidden = false;
    applyTranslations();
    setStatus(t("ready"), "ready");
    elements.methodPill.textContent = t(state.session.initialDraftMethod);
    renderStages();
    renderDeadline();
    renderProgressContext();
    renderMessages();
    renderProposal();
    renderMap();
    renderSessionState();
    renderChatRequestStatus();
    renderTranslationStatus();
    updateControls();
    scheduleComponentRecovery();
}

function renderStages() {
    elements.stageList.textContent = "";
    elements.stageCount.textContent = String(state.session.versions.length);

    state.session.versions.slice().reverse().forEach(version => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "stage-card";
        button.dataset.uiHelpKind = "stage";
        button.dataset.uiHelpStage = String(version.stageNumber);
        button.disabled = deadlineExpired()
            || state.translationInProgress
            || state.unityPlayActive;
        if (version.versionId === state.selectedVersionId) button.classList.add("selected");
        if (version.versionId === state.session.currentVersionId) button.classList.add("current");
        button.addEventListener("click", () => selectVersion(version.versionId));

        const top = document.createElement("div");
        top.className = "stage-card-top";
        top.innerHTML = `<strong>${escapeHtml(t("stage"))} ${escapeHtml(String(version.stageNumber))}</strong><span>${version.versionId === state.session.currentVersionId ? escapeHtml(t("current")) : ""}</span>`;
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

function renderSessionState() {
    const status = state.session.status;
    elements.finalActions.hidden = status !== "active";
    elements.intentionForm.hidden = status !== "awaiting_intention";
    elements.completeCard.hidden = status !== "completed";
    // A completed session always offers the return action.  Some local or
    // legacy sessions do not carry online-room metadata, but they may still
    // have been opened by Unity and therefore retain a valid opener tab.
    elements.returnUnityButton.hidden = status !== "completed" || state.session.demoMode;
    elements.sessionCompleteMessage.textContent = t(state.session.demoMode ? "demoCompleteBody" : "sessionCompleteBody");
    elements.historyBanner.hidden = state.selectedVersionId === state.session.currentVersionId;
}

function deadlineExpired() { return Boolean(state.session?.deadlineExpired); }

function deadlinePresentationState(seconds) {
    if (seconds <= 0 || deadlineExpired()) return "expired";
    if (seconds <= 60) return "critical";
    if (seconds <= 300) return "warning";
    return "normal";
}

function deadlineAnnouncement(stateName) {
    if (stateName === "warning") return t("deadlineWarningAnnouncement");
    if (stateName === "critical") return t("deadlineCriticalAnnouncement");
    if (stateName === "expired") return t("deadlineExpiredAnnouncement");
    return "";
}

function renderDeadline() {
    window.clearInterval(state.deadlineTimerId);
    state.deadlineTimerId = null;
    if (!state.session?.deadlineAt || state.session.status !== "active") {
        elements.deadlineStatus.hidden = true;
        state.deadlineAnchorSeconds = null;
        state.deadlineAnchorStartedAt = 0;
        state.deadlineIdentity = "";
        state.deadlinePresentationState = "";
        return;
    }

    const deadlineIdentity = `${state.sessionId}:${state.session.deadlineAt}`;
    if (deadlineIdentity !== state.deadlineIdentity) {
        state.deadlineIdentity = deadlineIdentity;
        state.deadlinePresentationState = "";
    }
    const serverRemaining = Number(state.session.remainingSeconds);
    state.deadlineAnchorSeconds = Number.isFinite(serverRemaining)
        ? Math.max(0, Math.ceil(serverRemaining))
        : Math.max(0, Math.ceil((Date.parse(state.session.deadlineAt) - Date.now()) / 1000));
    state.deadlineAnchorStartedAt = performance.now();

    const update = () => {
        const elapsedSeconds = Math.floor((performance.now() - state.deadlineAnchorStartedAt) / 1000);
        const seconds = Math.max(0, state.deadlineAnchorSeconds - elapsedSeconds);
        if (seconds <= 0) state.session.deadlineExpired = true;
        state.session.remainingSeconds = seconds;
        const minutes = String(Math.floor(seconds / 60)).padStart(2, "0");
        const remainder = String(seconds % 60).padStart(2, "0");
        const presentationState = deadlinePresentationState(seconds);
        const stateChanged = presentationState !== state.deadlinePresentationState;

        elements.deadlineStatus.hidden = false;
        elements.deadlineStatus.className = `deadline-status ${presentationState}`;
        elements.deadlineLabel.textContent = deadlineExpired()
            ? t("deadlineExpired")
            : t("deadlineRemaining");
        elements.deadlineValue.textContent = deadlineExpired()
            ? `\u00b7 ${t("deadlineExpiredAction")}`
            : `${minutes}:${remainder}`;
        elements.deadlineStatus.setAttribute(
            "aria-label",
            deadlineExpired()
                ? `${t("deadlineExpired")}. ${t("deadlineExpiredAction")}`
                : `${t("deadlineRemaining")} ${minutes}:${remainder}`
        );

        if (stateChanged) {
            state.deadlinePresentationState = presentationState;
            elements.deadlineAnnouncement.textContent = deadlineAnnouncement(presentationState);
            if (deadlineExpired()) {
                state.assessmentRetryTimers.forEach(timerId => window.clearTimeout(timerId));
                state.assessmentRetryTimers.clear();
                if (state.unityPlayActive) {
                    state.unityPlayActive = false;
                    requestEmbeddedHostView("sokoban:cocreation-show-lab");
                }
            }
            updateControls();
        }
    };
    update();
    if (!deadlineExpired()) state.deadlineTimerId = window.setInterval(update, 1000);
}

function updateControls() {
    if (!state.session) {
        elements.regenerateDraftButton.disabled = state.busy || state.demoCreationPending;
        elements.enterSessionButton.disabled = true;
        elements.setupChineseButton.disabled = elements.setupEnglishButton.disabled = state.busy || state.demoCreationPending;
        return;
    }

    if (!state.session.languageLocked) {
        const regenerating = ["pending", "claimed"].includes(
            state.session.draftPreview?.regenerationStatus
        ) || state.draftRegenerationPending;
        elements.enterSessionButton.disabled = state.busy || regenerating;
        elements.regenerateDraftButton.disabled = state.busy || regenerating
            || !state.session.draftPreview?.mutable;
        elements.setupChineseButton.disabled = elements.setupEnglishButton.disabled = state.busy;
        return;
    }

    const expired = deadlineExpired();
    const editable = canEditSelected();
    const pending = Boolean(currentPendingProposal());
    const selectedHistoricalStage = state.selectedVersionId !== state.session.currentVersionId;
    const intentConflictPending = selectedStageHasPendingIntentConflict();
    const proposalFlowStatus = state.session?.proposalFlowState?.status || "inactive";
    const binaryActive = binaryFlowActive();
    elements.chatForm.classList.toggle("is-binary", binaryActive);
    scheduleBinaryRefresh();
    const proposalPlanning = proposalFlowStatus === "planning";
    const proposalRetryPending = proposalFlowStatus === "retry_pending" && Boolean(state.pendingMessage);
    const interactionBusy = state.busy || state.unityPlayActive;
    const generationPending = state.chatStatus === "waiting"
        || state.assessmentRetryTimers.size > 0
        || state.assessing.size > 0;
    elements.saveStageButton.disabled = !editable || !state.dirty || interactionBusy;
    elements.discardDraftButton.disabled = !editable || !state.dirty || interactionBusy;
    elements.restoreStageButton.hidden = state.selectedVersionId === state.session.currentVersionId || state.session.status !== "active";
    elements.restoreStageButton.disabled = interactionBusy || expired;
    elements.playButton.disabled = interactionBusy || expired || state.dirty || pending || !selectedVersion();
    elements.finalizeButton.disabled = interactionBusy || (!expired && generationPending)
        || !selectedVersion()
        || (!selectedHistoricalStage && !expired && (state.dirty || pending));
    elements.messageInput.disabled = interactionBusy || !editable || intentConflictPending
        || proposalPlanning || proposalRetryPending || binaryActive;
    elements.messageInput.placeholder = binaryActive
        ? (state.language === "zh-CN" ? "请点选 A/B；想重新描述时，先取消本次方案。" : "Choose A or B. Cancel this proposal to describe a different request.")
        : intentConflictPending
        ? t("intentConflictPlaceholder")
        : proposalPlanning
            ? t("proposalPlanningPlaceholder")
            : proposalRetryPending
                ? t("proposalRetryPlaceholder")
                : proposalFlowStatus === "revision_needed"
                    ? t("proposalSupplementPlaceholder")
                    : t("messagePlaceholder");
    const disagreementActive = selectedStageHasActiveDisagreement();
    const proposalLocked = proposalFlowActive();
    if ((!editable || disagreementActive || intentConflictPending) && state.proposalMode) {
        setProposalMode(false);
    }
    const proposalActive = state.proposalMode || proposalLocked;
    elements.proposalRequestButton.disabled = (
        interactionBusy || !editable || disagreementActive || proposalLocked
        || intentConflictPending
    );
    elements.proposalRequestButton.classList.toggle("is-active", proposalActive);
    elements.proposalRequestButton.setAttribute("aria-pressed", proposalActive ? "true" : "false");
    elements.sendButton.disabled = interactionBusy || !editable || intentConflictPending
        || proposalPlanning || proposalRetryPending || binaryActive
        || !elements.messageInput.value.trim();
    const cancel = document.getElementById("cancelProposalButton");
    if (cancel) {
        cancel.hidden = !binaryActive;
        cancel.textContent = state.language === "zh-CN" ? "取消本次方案" : "Cancel proposal";
        cancel.disabled = interactionBusy || expired || proposalPlanning || selectedHistoricalStage;
        cancel.onclick = cancelBinaryProposal;
    }
    document.querySelectorAll(".binary-choice-button").forEach(button => {
        button.disabled = !canChooseBinary(button.dataset.questionId)
            || button.dataset.questionStatus !== "pending";
    });
    document.querySelectorAll(".question-feedback-button").forEach(button => {
        button.disabled = interactionBusy || !editable
            || state.questionFeedbackBusy.has(button.dataset.feedbackKey || "");
    });
    document.querySelectorAll(".challenge-choice-button").forEach(button => {
        setButtonDisabled(button, !canUseChallengeChoice(button.dataset.challengeId));
    });
    document.querySelectorAll(".revision-action-button").forEach(button => {
        setButtonDisabled(button, interactionBusy || !editable || disagreementActive
            || button.dataset.actionable !== "true");
    });
    document.querySelectorAll(".intent-feedback-card button, .proposal-card button").forEach(button => {
        setButtonDisabled(button, interactionBusy || !editable);
    });
    document.querySelectorAll(".tile, .palette-button").forEach(button => {
        setButtonDisabled(button, interactionBusy || !editable);
    });
    elements.sendButton.textContent = t("send");
    renderControlHints();
}

function renderChatRequestStatus() {
    renderControlHints();
}

function renderTranslationStatus() {
    renderControlHints();
}

function selectedReplyFailure() {
    if (binaryFlowActive() && (state.session.proposalFlowState.retryable || state.pendingBinary && state.chatStatus === "error")) {
        return { error: state.chatError, message: state.language === "zh-CN"
            ? "本次方案尚未完成，已保存你的选择。可以重试或取消。"
            : "Your choices are saved. Retry or cancel this proposal.", retryAction: retryBinaryProposal };
    }
    if (canEditSelected() && state.chatStatus === "error"
        && (!state.pendingMessage || state.pendingMessage.baseVersionId === state.selectedVersionId)) {
        return { error: state.chatError, retryAction: state.chatError?.retryable && state.pendingMessage
            ? retryPendingMessage : null };
    }
    return state.replyFeedback?.versionId === state.selectedVersionId ? state.replyFeedback : null;
}

function renderReplyFeedback(blockReason = "") {
    const failure = selectedReplyFailure();
    const waiting = state.chatStatus === "waiting" && state.chatBusy
        && state.chatBusyVersionId === state.selectedVersionId && canEditSelected();
    const opening = state.assessing.has(state.selectedVersionId)
        || state.assessmentRetryTimers.has(state.selectedVersionId);
    const turns = selectedStageTurns();
    const missingTranslation = turns.some(turn => turn.role === "assistant" && turn.language !== state.language
        && !turn.translations?.[state.language]);
    const translationFailed = turns.some(turn => state.translationFailures.has(`${turn.turnId}:${state.language}`)
        && turn.language !== state.language && !turn.translations?.[state.language]);
    const message = failure ? (failure.message || localizedErrorMessage(failure.error))
        : waiting ? chatWaitingMessage()
        : opening ? t("assessmentPending")
        : state.busy && !state.chatBusy ? t("busyHint")
        : state.translationInProgress && missingTranslation ? t("translationInProgress")
            .replace("{count}", String(state.translationRemainingCount))
        : translationFailed ? t("translationUnavailable")
        : blockReason;
    if (elements.chatControlHint.textContent !== message) elements.chatControlHint.textContent = message;
    elements.replyFeedbackIcon.dataset.state = failure ? (failure.error ? "error" : "info")
        : waiting || opening || state.busy && !state.chatBusy ? "thinking"
        : state.translationInProgress && missingTranslation ? "translation"
        : translationFailed ? "error" : "info";
    elements.chatControlHint.hidden = !message;
    elements.chatRequestStatus.hidden = !message;
    elements.chatRequestStatus.className = `chat-request-status composer-feedback${failure?.error ? " error" : ""}`;
    elements.chatRetryButton.hidden = !failure?.retryAction;
    elements.chatRetryButton.textContent = t("retry");
    renderTechnicalDetails(elements.chatTechnicalDetails, failure?.error || null);
}

function showReplyNotice(message, error = null, retryAction = null, versionId = state.selectedVersionId) {
    state.replyFeedback = { message, error, retryAction, versionId };
    renderControlHints();
}

function chatWaitingMessage() {
    const elapsedSeconds = Math.max(0, Math.floor((Date.now() - state.chatStartedAt) / 1000));
    const pending = state.pendingMessage;
    const proposal = pending?.requestProposal === true
        || ["execute_revision", "alternative_revision"].includes(pending?.action)
        || proposalFlowActive();
    return `${t(proposal ? "chatWaitingProposal" : "chatWaitingPrimary")} · ${t("chatElapsed").replace("{seconds}", String(elapsedSeconds))}`;
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

elements.setupChineseButton.addEventListener("click", () => selectSetupLanguage("zh-CN"));
elements.setupEnglishButton.addEventListener("click", () => selectSetupLanguage("en"));
elements.enterSessionButton.addEventListener("click", confirmSessionLanguage);
elements.regenerateDraftButton.addEventListener("click", () => void regenerateDraft());
elements.retryButton.addEventListener("click", () => state.retryAction && state.retryAction());
elements.chatRetryButton.addEventListener("click", () => {
    const failure = selectedReplyFailure();
    if (failure?.retryAction) {
        state.replyFeedback = null;
        void failure.retryAction();
    }
});
elements.returnCurrentButton.addEventListener("click", selectCurrentVersion);
elements.progressPanel.addEventListener("toggle", () => {
    if (!state.session || !elements.progressPanel.dataset.progressKey) return;
    elements.progressSummary.setAttribute(
        "aria-expanded",
        elements.progressPanel.open ? "true" : "false",
    );
    localStorage.setItem(
        progressPanelStorageKey(),
        elements.progressPanel.open ? "open" : "closed",
    );
    requestAnimationFrame(updateProgressPanelMaxHeight);
});
elements.answeredQuestionsPanel.addEventListener("toggle", () => {
    elements.answeredQuestionsSummary.setAttribute(
        "aria-expanded",
        elements.answeredQuestionsPanel.open ? "true" : "false",
    );
    requestAnimationFrame(updateProgressPanelMaxHeight);
});
elements.chatForm.addEventListener("submit", sendMessage);
elements.proposalRequestButton.addEventListener("click", toggleProposalMode);
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
window.addEventListener("message", event => {
    void handleUnityBridgeMessage(event);
});
window.addEventListener("resize", () => {
    if (state.activeCoordinateLink) requestAnimationFrame(drawActiveCoordinateRoute);
    requestAnimationFrame(updateProgressPanelMaxHeight);
});
window.addEventListener("online", resumeDraftRegenerationPolling);
document.addEventListener("visibilitychange", () => {
    if (!document.hidden) resumeDraftRegenerationPolling();
    if (!document.hidden) componentRecovery.paused.clear();
    scheduleComponentRecovery();
});
window.addEventListener("pagehide", cancelComponentRecoveryTimer);

initializeUiHelp();
initializeWorkspaceHeaderAlignment();
applyTranslations();
initialize();

