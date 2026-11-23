import { LLM_REQUEST_TIMEOUT_MS, MESSAGE_REQUEST_TIMEOUT_MS } from "./i18n.js?v=binary-discovery-20261123-v3-esm";

export function retryDelay(attempt) {
    return Math.min(60000, 2000 * (2 ** Math.min(Math.max(0, attempt), 5)));
}

export function createSessionActions(ctx) {
    const { state, elements, window, document, localStorage, setTimeout, clearTimeout, unityHost, ports } = ctx;

    const t = (...args) => ports.t(...args);
    const uniqueId = (...args) => ports.uniqueId(...args);
    const api = (...args) => ports.api(...args);
    const retainNewerInteractionState = (...args) => ports.retainNewerInteractionState(...args);
    const hideNotice = (...args) => ports.hideNotice(...args);
    const showNotice = (...args) => ports.showNotice(...args);
    const showError = (...args) => ports.showError(...args);
    const renderControlHints = (...args) => ports.renderControlHints(...args);
    const persistPendingMessage = (...args) => ports.persistPendingMessage(...args);
    const clearPendingMessage = (...args) => ports.clearPendingMessage(...args);
    const setProposalMode = (...args) => ports.setProposalMode(...args);
    const composerKey = (...args) => ports.composerKey(...args);
    const proposalModeKey = (...args) => ports.proposalModeKey(...args);
    const selectedStageKey = (...args) => ports.selectedStageKey(...args);
    const updateCharacterCount = (...args) => ports.updateCharacterCount(...args);
    const recoverMessageFailure = (...args) => ports.recoverMessageFailure(...args);
    const scheduleComponentRecovery = (...args) => ports.scheduleComponentRecovery(...args);
    const withBusy = (...args) => ports.withBusy(...args);
    const clearUnsavedMapDraft = (...args) => ports.clearUnsavedMapDraft(...args);
    const isLevelValidationError = (...args) => ports.isLevelValidationError(...args);
    const isCardActionPending = (...args) => ports.isCardActionPending(...args);
    const syncHash = (...args) => ports.syncHash(...args);
    const restoreComposerDraft = (...args) => ports.restoreComposerDraft(...args);
    const canEditSelected = (...args) => ports.canEditSelected(...args);
    const renderMap = (...args) => ports.renderMap(...args);
    const currentPendingProposal = (...args) => ports.currentPendingProposal(...args);
    const resetDraftFromSelection = (...args) => ports.resetDraftFromSelection(...args);
    const selectedStageTurns = (...args) => ports.selectedStageTurns(...args);
    const selectedStageHasActiveDisagreement = (...args) => ports.selectedStageHasActiveDisagreement(...args);
    const proposalFlowActive = (...args) => ports.proposalFlowActive(...args);
    const binaryFlowActive = (...args) => ports.binaryFlowActive(...args);
    const ensureVisibleTranslations = (...args) => ports.ensureVisibleTranslations(...args);
    const hasEmbeddedFormalHost = (...args) => ports.hasEmbeddedFormalHost(...args);
    const showFormalStandaloneBlock = (...args) => ports.showFormalStandaloneBlock(...args);
    const tryOpenPlayInExistingUnity = (...args) => ports.tryOpenPlayInExistingUnity(...args);
    const deadlineExpired = (...args) => ports.deadlineExpired(...args);
    const render = (...args) => ports.render(...args);
    const updateControls = (...args) => ports.updateControls(...args);
    const startChatTimer = (...args) => ports.startChatTimer(...args);
    const stopChatTimer = (...args) => ports.stopChatTimer(...args);
    const renderChatRequestStatus = (...args) => ports.renderChatRequestStatus(...args);
    const refreshSession = (...args) => ports.refreshSession(...args);
    const ensureAssessment = (...args) => ports.ensureAssessment(...args);
    const submitPendingMessage = (...args) => ports.submitPendingMessage(...args);
    const submitBinaryAction = (...args) => ports.submitBinaryAction(...args);

    function scheduleAssessmentRetry(versionId) {
        if (deadlineExpired() || state.assessmentRetryTimers.has(versionId)) return;
        const attempt = state.assessmentRetryCounts.get(versionId) || 0;
        state.assessmentRetryCounts.set(versionId, attempt + 1);
        const timerId = setTimeout(() => {
            state.assessmentRetryTimers.delete(versionId);
            ensureAssessment(versionId);
        }, retryDelay(attempt));
        state.assessmentRetryTimers.set(versionId, timerId);
    }

    async function ensureAssessmentImpl(versionId) {
        if (deadlineExpired()) return;
        const version = findVersion(versionId);
        if (!state.session || version?.openingTurnId || state.session.assessments.some(item => item.versionId === versionId) || state.assessing.has(versionId)) return;
        state.assessing.add(versionId);
        renderControlHints();
        let idempotencyKey = state.assessmentIdempotencyKeys.get(versionId);
        if (!idempotencyKey) {
            idempotencyKey = uniqueId("assessment");
            state.assessmentIdempotencyKeys.set(versionId, idempotencyKey);
        }
        try {
            const updatedSession = await api(`/api/sessions/${state.sessionId}/versions/${versionId}/assessments`, {
                method: "POST",
                body: { idempotencyKey },
                timeoutMs: LLM_REQUEST_TIMEOUT_MS
            });
            const currentMaxSeq = Math.max(0, ...(state.session?.turns || []).map(turn => turn.sequence || 0));
            const updatedMaxSeq = Math.max(0, ...(updatedSession?.turns || []).map(turn => turn.sequence || 0));
            const versionsBehind = (updatedSession?.versions?.length || 0) < (state.session?.versions?.length || 0);
            if (versionsBehind || updatedMaxSeq < currentMaxSeq) {
                const mergedAssessments = (updatedSession.assessments || []).filter(
                    item => !state.session.assessments.some(existing => existing.versionId === item.versionId)
                );
                if (mergedAssessments.length) {
                    state.session = {
                        ...state.session,
                        assessments: [...state.session.assessments, ...mergedAssessments],
                    };
                }
            } else {
                state.session = retainNewerInteractionState(updatedSession);
            }
            state.assessmentRetryCounts.delete(versionId);
            state.assessmentIdempotencyKeys.delete(versionId);
            hideNotice();
            render();
        } catch (error) {
            if (error?.retryable && !deadlineExpired()) {
                scheduleAssessmentRetry(versionId);
                render();
                return;
            }
            if (!deadlineExpired()) {
                const retryAction = () => ensureAssessment(versionId);
                retryAction.assessmentVersionId = versionId;
                showError(error, retryAction);
            } else {
                render();
            }
        } finally {
            state.assessing.delete(versionId);
            renderControlHints();
        }
    }

    async function sendMessage(event) {
        event.preventDefault();
        if (binaryFlowActive()) return;
        const content = elements.messageInput.value.trim();
        if (!content || state.busy) return;

        if (
            !state.pendingMessage
            || isCardActionPending(state.pendingMessage)
            || state.pendingMessage.content !== content
            || state.pendingMessage.baseVersionId !== state.session.currentVersionId
            || Boolean(state.pendingMessage.requestProposal) !== state.proposalMode
        ) {
            const challenge = activeChallengeComposerState();
            state.pendingMessage = {
                content,
                baseVersionId: state.session.currentVersionId,
                idempotencyKey: uniqueId(state.proposalMode ? "proposal-request" : "message"),
                requestProposal: state.proposalMode,
                ...(challenge ? {
                    action: "continue_challenge",
                    challengeId: challenge.challengeId
                } : {})
            };
        }

        persistPendingMessage();
        await submitPendingMessage();
    }

    function activeChallengeComposerState() {
        const turns = selectedStageTurns();
        for (let index = turns.length - 1; index >= 0; index -= 1) {
            const challenge = turns[index]?.guidance?.challengeState;
            if (!challenge?.challengeId) continue;
            if (challenge.status === "resolved") return null;
            return challenge;
        }
        return null;
    }

    function toggleProposalMode() {
        if (
            state.busy
            || !canEditSelected()
            || selectedStageHasActiveDisagreement()
            || proposalFlowActive()
        ) return;
        setProposalMode(!state.proposalMode);
        updateControls();
    }

    function binaryStorageKey() { return `cocreationBinaryAction:${state.sessionId}`; }

    async function cancelBinaryProposal() {
        if (!binaryFlowActive() || !canEditSelected() || state.busy || state.unityPlayActive || state.session.proposalFlowState.status === "planning") return;
        return submitBinaryAction({ kind: "cancel", baseVersionId: state.session.currentVersionId,
            topicId: state.session.proposalFlowState.topicId, idempotencyKey: uniqueId("binary-cancel") });
    }

    async function retryBinaryProposal() {
        const interactionBusy = state.busy || state.unityPlayActive;
        if (interactionBusy || !canEditSelected()) return;
        let stored;
        try { stored = JSON.parse(localStorage.getItem(binaryStorageKey()) || "null"); } catch (_) { stored = null; }
        const action = state.session?.proposalFlowState?.retryAction || state.pendingBinary || stored;
        if (!action || action.baseVersionId !== state.session.currentVersionId) return;
        if (state.dirty && (action.kind === "choice" || Boolean(action.choiceId))) return;
        if (action.kind === "start") {
            const { kind, ...pending } = action;
            state.pendingMessage = pending;
            return submitPendingMessage();
        }
        return submitBinaryAction(action);
    }

    function isBinaryActionCommitted(session, action) {
        if (!session) return false;
        const flow = session.proposalFlowState;
        const status = flow?.status;
        if (status === "cancelled" || status === "proposal_ready") return true;
        if (!(flow?.interactionMode === "binary_choice" && flow?.active)) return true;
        if (action?.idempotencyKey && (session.turns || []).some(turn =>
            turn.role === "assistant"
            && (!action.baseVersionId || turn.versionId === action.baseVersionId)
            && turn.requestId === action.idempotencyKey
        )) {
            return true;
        }
        return false;
    }

    function clearBinaryPendingState() {
        state.pendingBinary = null;
        if (state.sessionId) localStorage.removeItem(binaryStorageKey());
        clearPendingMessage();
        setProposalMode(false);
        state.chatStatus = "idle";
        state.chatError = null;
    }

    function cancelBinaryRefreshTimer() {
        if (state.binaryRefreshTimer !== null && state.binaryRefreshTimer !== undefined) {
            clearTimeout(state.binaryRefreshTimer);
            state.binaryRefreshTimer = null;
        }
    }

    async function submitBinaryActionImpl(action) {
        if (state.busy || state.unityPlayActive || !canEditSelected()) return;
        if (state.dirty && (action.kind === "choice" || Boolean(action.choiceId))) return;
        cancelBinaryRefreshTimer();
        state.pendingBinary = action;
        localStorage.setItem(binaryStorageKey(), JSON.stringify(action));
        state.busy = state.chatBusy = true;
        state.chatBusyVersionId = action.baseVersionId;
        state.chatStatus = "waiting";
        state.chatError = null;
        startChatTimer();
        updateControls();
        try {
            const { kind, ...body } = action;
            state.session = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/proposal-discovery/${kind === "cancel" ? "cancel" : "choices"}`,
                { method: "POST", body, timeoutMs: MESSAGE_REQUEST_TIMEOUT_MS });
            clearBinaryPendingState();
        } catch (error) {
            state.chatError = error;
            state.chatStatus = "error";
            try {
                const latest = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}`);
                state.session = latest;
                error = recoverMessageFailure(error, latest, action);
                state.chatError = error;
                if (isBinaryActionCommitted(latest, action)) {
                    clearBinaryPendingState();
                } else if (!error?.retryable && error?.code !== "BINARY_BUSY") {
                    state.pendingBinary = null;
                    localStorage.removeItem(binaryStorageKey());
                }
            } catch (_) {
                if (!error?.retryable && error?.code !== "BINARY_BUSY") {
                    state.pendingBinary = null;
                    localStorage.removeItem(binaryStorageKey());
                }
            }
        } finally {
            stopChatTimer();
            state.busy = state.chatBusy = false;
            state.chatBusyVersionId = "";
            render();
        }
    }

    async function refreshBinarySessionLightweight() {
        const capturedSessionId = state.sessionId;
        const latest = await api(`/api/sessions/${encodeURIComponent(capturedSessionId)}`);
        if (state.sessionId !== capturedSessionId) return;
        if (state.busy || state.chatBusy || state.questionFeedbackBusy.size > 0) {
            scheduleBinaryRefresh();
            return;
        }
        const previousVersionId = state.session?.currentVersionId;
        state.session = latest;
        if (state.pendingBinary && isBinaryActionCommitted(latest, state.pendingBinary)) {
            clearBinaryPendingState();
        } else if (!binaryFlowActive() || ["cancelled", "proposal_ready"].includes(latest.proposalFlowState?.status)) {
            state.pendingBinary = null;
            if (state.sessionId) localStorage.removeItem(binaryStorageKey());
            if (state.chatStatus === "error") {
                state.chatStatus = "idle";
                state.chatError = null;
            }
        } else if (state.pendingBinary && state.chatError) {
            state.chatError = recoverMessageFailure(state.chatError, latest, state.pendingBinary);
        }
        if (state.pendingMessage && (
            ["cancelled", "proposal_ready"].includes(latest.proposalFlowState?.status)
            || (latest.turns || []).some(turn => turn.role === "assistant"
                && turn.versionId === state.pendingMessage.baseVersionId
                && turn.requestId === state.pendingMessage.idempotencyKey)
        )) {
            if (!isCardActionPending(state.pendingMessage)) {
                setProposalMode(false);
                elements.messageInput.value = "";
                localStorage.removeItem(composerKey());
                updateCharacterCount();
            }
            clearPendingMessage();
            state.chatStatus = "idle";
            state.chatError = null;
        }
        if (previousVersionId && latest.currentVersionId && previousVersionId !== latest.currentVersionId && !findVersion(state.selectedVersionId)) {
            state.selectedVersionId = latest.currentVersionId;
        }
        if (!state.dirty && state.selectedVersionId === latest.currentVersionId && previousVersionId !== latest.currentVersionId) {
            resetDraftFromSelection();
        }
        render();
        void ensureVisibleTranslations();
    }

    function scheduleBinaryRefresh() {
        const eligible = Boolean(
            binaryFlowActive()
            && state.session?.proposalFlowState?.status === "planning"
            && state.session?.status === "active"
            && !deadlineExpired()
            && !document.hidden
            && !state.busy
        );
        if (!eligible) {
            cancelBinaryRefreshTimer();
            return;
        }
        if (state.binaryRefreshTimer) return;
        state.binaryRefreshTimer = setTimeout(async () => {
            state.binaryRefreshTimer = null;
            if (
                !binaryFlowActive()
                || state.session?.proposalFlowState?.status !== "planning"
                || state.session?.status !== "active"
                || deadlineExpired()
                || document.hidden
                || state.busy
            ) return;
            try { await refreshBinarySessionLightweight(); } catch (_) { scheduleBinaryRefresh(); }
        }, 4000);
    }

    async function retryPendingMessageImpl() {
        if (!state.pendingMessage || state.busy) return;
        if (!isCardActionPending(state.pendingMessage)) {
            elements.messageInput.value = state.pendingMessage.content;
            localStorage.setItem(composerKey(), state.pendingMessage.content);
            updateCharacterCount();
        }
        await submitPendingMessage();
    }

    async function submitPendingMessageImpl() {
        const pending = state.pendingMessage;
        if (!pending || state.busy) return;
        const cardAction = isCardActionPending(pending);

        state.busy = true;
        state.chatBusy = true;
        state.chatBusyVersionId = pending.baseVersionId;
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
                timeoutMs: MESSAGE_REQUEST_TIMEOUT_MS
            });
            if (!cardAction) {
                setProposalMode(false);
                elements.messageInput.value = "";
                localStorage.removeItem(composerKey());
                updateCharacterCount();
            }
            clearPendingMessage();
            state.chatStatus = "idle";
            state.chatError = null;
            render();
        } catch (error) {
            if (error?.code === "PROPOSAL_STALE" && (!pending.action || pending.action === "none")) {
                // Keep the wording for a deliberate new answer, but never retry
                // an old question binding with the old key after dialogue changed.
                clearPendingMessage();
                elements.messageInput.value = pending.content;
                localStorage.setItem(composerKey(), pending.content);
                try {
                    await refreshSession();
                } catch (_refreshError) {
                    // Preserve the original stale-context error and draft.
                }
            } else if (
                [
                    "INVALID_CARD_SOURCE",
                    "PROPOSAL_STALE",
                    "PROPOSAL_PRECONDITION_FAILED",
                    "CHALLENGE_STALE"
                ].includes(error?.code)
                && pending.action
                && pending.action !== "none"
            ) {
                clearPendingMessage();
                try {
                    await refreshSession();
                } catch (_refreshError) {
                    // Keep the original proposal-state error visible if the refresh
                    // itself cannot complete.
                }
            } else if (error?.retryable) {
                // A direct-language modification can start a server-owned proposal
                // flow before Kimi returns. Refresh only that authority state so a
                // retryable failure still shows the Proposal toggle as locked,
                // while preserving the composer text and idempotency key.
                try {
                    const latest = await api(
                        `/api/sessions/${encodeURIComponent(state.sessionId)}`
                    );
                    state.session = latest;
                    error = recoverMessageFailure(error, latest, pending);
                    if (latest.turns?.some(turn => turn.role === "assistant" && turn.versionId === pending.baseVersionId
                        && turn.requestId === pending.idempotencyKey)) {
                        clearPendingMessage();
                        if (!cardAction) {
                            setProposalMode(false);
                            elements.messageInput.value = "";
                            localStorage.removeItem(composerKey());
                            updateCharacterCount();
                        }
                        state.chatStatus = "idle";
                        state.chatError = null;
                        render();
                        return;
                    }
                    render();
                } catch (_proposalStateRefreshError) {
                    // The original retryable error remains the useful action.
                }
            }
            if (error?.retryable && state.pendingMessage) {
                state.chatStatus = "error";
                state.chatError = error;
            } else {
                clearPendingMessage();
                state.chatStatus = "error";
                state.chatError = error;
            }
        } finally {
            stopChatTimer();
            state.busy = false;
            state.chatBusy = false;
            state.chatBusyVersionId = "";
            renderChatRequestStatus();
            updateControls();
            scheduleComponentRecovery();
        }
    }

    async function saveManualStage() {
        if (!state.dirty) return;
        const saveBody = { rows: state.draftRows.map(row => row),
            baseVersionId: state.session.currentVersionId, idempotencyKey: uniqueId("manual"),
            summary: state.language === "zh-CN" ? "设计者保存的地图修改" : "Designer-saved map edit" };
        await withBusy(async () => {
            state.validationError = null;
            renderMap();
            try {
                state.session = await api(`/api/sessions/${state.sessionId}/versions`, {
                    method: "POST",
                    body: saveBody
                });
                clearUnsavedMapDraft();
                selectVersion(state.session.currentVersionId, false, { forceReset: true });
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
        const restoreBody = { baseVersionId: state.session.currentVersionId, idempotencyKey: uniqueId("restore") };
        await withBusy(async () => {
            state.session = await api(`/api/sessions/${state.sessionId}/versions/${version.versionId}/restore`, {
                method: "POST",
                body: restoreBody
            });
            clearUnsavedMapDraft();
            selectVersion(state.session.currentVersionId, false, { forceReset: true });
            render();
            await ensureAssessment(state.session.currentVersionId);
        });
    }

    async function decideProposal(proposal, decision) {
        const decisionBody = { decision, baseVersionId: state.session.currentVersionId,
            idempotencyKey: uniqueId(decision), reason: "" };
        await withBusy(async () => {
            state.session = await api(`/api/sessions/${state.sessionId}/proposals/${proposal.proposalId}/decision`, {
                method: "POST",
                body: decisionBody
            });
            if (decision === "accept") {
                clearUnsavedMapDraft();
                selectVersion(state.session.currentVersionId, false, { forceReset: true });
                render();
                await ensureAssessment(state.session.currentVersionId);
            } else {
                selectVersion(state.session.currentVersionId, false, { preserveSameStageDraft: true });
                render();
            }
        });
    }

    async function playSelectedStage() {
        if (!state.session?.demoMode && !hasEmbeddedFormalHost()) {
            showFormalStandaloneBlock();
            return;
        }
        if (state.dirty) return showNotice(t("errorDirtyPlay"));
        if (currentPendingProposal()) return showNotice(t("errorPendingPlay"));
        const version = selectedVersion();
        const playKey = uniqueId("play");
        await withBusy(async () => {
            localStorage.setItem(selectedStageKey(), version.versionId);
            if (canEditSelected()) {
                localStorage.setItem(composerKey(), elements.messageInput.value);
            }
            const payload = await api(`/api/sessions/${state.sessionId}/versions/${version.versionId}/play-attempts`, {
                method: "POST",
                body: { idempotencyKey: playKey }
            });

            if (!state.session.demoMode) {
                if (await tryOpenPlayInExistingUnity(payload.playUrl)) {
                    state.unityPlayActive = true;
                    renderMap();
                    updateControls();
                    showNotice(t("playOpenedInUnity"));
                    return;
                }
                if (unityHost.embedded) {
                    showNotice(t("playUnityUnavailable"));
                    return;
                }
            }

            window.location.assign(payload.playUrl);
        });
    }

    async function finalizeSession() {
        elements.finalizeModal.hidden = true;
        const targetVersionId = state.selectedVersionId;
        const targetIsCurrent = targetVersionId === state.session.currentVersionId;
        const rows = deadlineExpired() && targetIsCurrent && state.dirty ? state.draftRows.map(row => row) : null;
        const rowsSignature = rows ? rows.join("\n") : null;
        if (
            !state.pendingFinalize
            || state.pendingFinalize.sessionId !== state.sessionId
            || state.pendingFinalize.targetVersionId !== targetVersionId
            || state.pendingFinalize.rowsSignature !== rowsSignature
        ) {
            state.pendingFinalize = {
                sessionId: state.sessionId,
                targetVersionId,
                rowsSignature,
                idempotencyKey: uniqueId("finalize")
            };
        }
        const finalizeBody = {
            baseVersionId: targetVersionId,
            idempotencyKey: state.pendingFinalize.idempotencyKey,
            rows
        };
        await withBusy(async () => {
            state.validationError = null;
            try {
                state.session = await api(`/api/sessions/${state.sessionId}/finalize`, {
                    method: "POST",
                    body: finalizeBody
                });
                state.pendingFinalize = null;
                clearUnsavedMapDraft();
                render();
                elements.intentionInput.focus();
            } catch (error) {
                if (isLevelValidationError(error)) {
                    state.validationError = error;
                    renderMap();
                }
                throw error;
            }
        });
    }

    async function submitIntention(event) {
        event?.preventDefault?.();
        const content = elements.intentionInput.value.trim();
        if (!content) return showNotice(t("errorIntentRequired"));
        if (
            !state.pendingIntention
            || state.pendingIntention.sessionId !== state.sessionId
            || state.pendingIntention.content !== content
        ) {
            state.pendingIntention = {
                sessionId: state.sessionId,
                content,
                idempotencyKey: uniqueId("intention")
            };
        }
        const intentionBody = {
            content,
            idempotencyKey: state.pendingIntention.idempotencyKey
        };
        await withBusy(async () => {
            state.session = await api(`/api/sessions/${state.sessionId}/intention`, {
                method: "POST",
                body: intentionBody
            });
            state.pendingIntention = null;
            clearUnsavedMapDraft();
            render();
        });
    }

    function selectVersion(versionId, shouldRender = true, options = {}) {
        if (!findVersion(versionId)) return;
        if (state.selectedVersionId === versionId && !options.forceReset) return;
        const preserveSameStageDraft = Boolean(
            options.preserveSameStageDraft
            && state.selectedVersionId === versionId
            && state.dirty
        );
        if (state.selectedVersionId && (state.selectedVersionId !== versionId || options.forceReset)) {
            setProposalMode(false);
            clearUnsavedMapDraft();
        }
        state.selectedVersionId = versionId;
        localStorage.removeItem(proposalModeKey());
        state.proposalMode = false;
        if (!preserveSameStageDraft) {
            state.activeCoordinateLink = null;
        }
        localStorage.setItem(selectedStageKey(), versionId);
        syncHash();
        if (!preserveSameStageDraft) {
            resetDraftFromSelection();
        }
        restoreComposerDraft();
        if (shouldRender) {
            render();
            void ensureVisibleTranslations();
        }
    }

    function selectCurrentVersion() { selectVersion(state.session.currentVersionId); }
    function selectedVersion() { return findVersion(state.selectedVersionId); }
    function findVersion(versionId) { return state.session && state.session.versions.find(item => item.versionId === versionId); }

    return {
        retryDelay,
        scheduleAssessmentRetry,
        ensureAssessment: ensureAssessmentImpl,
        sendMessage,
        activeChallengeComposerState,
        toggleProposalMode,
        binaryStorageKey,
        cancelBinaryProposal,
        retryBinaryProposal,
        submitBinaryAction: submitBinaryActionImpl,
        cancelBinaryRefreshTimer,
        scheduleBinaryRefresh,
        retryPendingMessage: retryPendingMessageImpl,
        submitPendingMessage: submitPendingMessageImpl,
        saveManualStage,
        restoreSelectedStage,
        decideProposal,
        playSelectedStage,
        finalizeSession,
        submitIntention,
        selectVersion,
        selectCurrentVersion,
        selectedVersion,
        findVersion,
    };
}
