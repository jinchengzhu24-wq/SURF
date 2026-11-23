import {
    COMPONENT_RECOVERY_TIMEOUT_MS,
    DISCUSSION_FOCUS_LABEL,
    DRAFT_REGENERATION_API_TIMEOUT_MS,
    DRAFT_REGENERATION_POLL_MS,
    DRAFT_REGENERATION_POLL_TIMEOUT_MS,
    DRAFT_REGENERATION_PROTOCOL_VERSION,
    GUIDANCE_CUE_LABELS,
    LEVEL_VALIDATION_ERROR_CODES,
    LLM_PRIMARY_WAIT_SECONDS,
    LLM_REQUEST_TIMEOUT_MS,
    MAX_MESSAGE_LENGTH,
    MESSAGE_REQUEST_TIMEOUT_MS,
    ORIGINAL_ERROR_COPY,
    PROPOSAL_DISPLAY_LIMIT_SECONDS,
    REPLY_DIAGNOSTICS_KEY,
    REPLY_PIPELINE_VERSION,
    REQUEST_COPY,
    SESSION_STORAGE_KEY,
    TILE_ORDER,
    UI_ACTION_HELP,
    UI_TILE_HELP,
    UNITY_PLAY_BRIDGE_TIMEOUT_MS,
    chineseApiErrors,
    createI18n,
    safeReplyDiagnostic,
    translations,
    validationTileNames,
} from "./i18n.js?v=binary-discovery-20261123-v3-esm";
import {
    ELEMENT_IDS,
    createElements,
    createInitialState,
    createStateStorage,
    isCardActionPending,
    isLevelValidationError,
    recoverMessageFailure,
    setButtonDisabled,
    validApiPayload,
} from "./state-storage.js?v=binary-discovery-20261123-v3-esm";
import { createUnityBridge } from "./unity-bridge.js?v=binary-discovery-20261123-v3-esm";
import {
    buildEntityLabels,
    createMapEditor,
    formatEntityCoordinate,
    formatTileCoordinate,
    tileClass,
    tileLabel,
    tileName,
} from "./map-editor.js?v=binary-discovery-20261123-v3-esm";
import {
    assistantBodyWithoutCues,
    createChatCards,
    findCoordinateRoute,
    formatCoordinatePoint,
    isInternalProposalFailureCue,
    normalizedCoordinatePoint,
} from "./chat-cards.js?v=binary-discovery-20261123-v3-esm";
import { createSessionActions, retryDelay } from "./session-actions.js?v=binary-discovery-20261123-v3-esm";

export {
    COMPONENT_RECOVERY_TIMEOUT_MS,
    DISCUSSION_FOCUS_LABEL,
    DRAFT_REGENERATION_API_TIMEOUT_MS,
    DRAFT_REGENERATION_POLL_MS,
    DRAFT_REGENERATION_POLL_TIMEOUT_MS,
    DRAFT_REGENERATION_PROTOCOL_VERSION,
    ELEMENT_IDS,
    GUIDANCE_CUE_LABELS,
    LEVEL_VALIDATION_ERROR_CODES,
    LLM_PRIMARY_WAIT_SECONDS,
    LLM_REQUEST_TIMEOUT_MS,
    MAX_MESSAGE_LENGTH,
    MESSAGE_REQUEST_TIMEOUT_MS,
    ORIGINAL_ERROR_COPY,
    PROPOSAL_DISPLAY_LIMIT_SECONDS,
    REPLY_DIAGNOSTICS_KEY,
    REPLY_PIPELINE_VERSION,
    REQUEST_COPY,
    SESSION_STORAGE_KEY,
    TILE_ORDER,
    UI_ACTION_HELP,
    UI_TILE_HELP,
    UNITY_PLAY_BRIDGE_TIMEOUT_MS,
    assistantBodyWithoutCues,
    buildEntityLabels,
    chineseApiErrors,
    createChatCards,
    createElements,
    createI18n,
    createInitialState,
    createMapEditor,
    createSessionActions,
    createStateStorage,
    createUnityBridge,
    findCoordinateRoute,
    formatCoordinatePoint,
    formatEntityCoordinate,
    formatTileCoordinate,
    isCardActionPending,
    isInternalProposalFailureCue,
    isLevelValidationError,
    normalizedCoordinatePoint,
    recoverMessageFailure,
    retryDelay,
    safeReplyDiagnostic,
    setButtonDisabled,
    tileClass,
    tileLabel,
    tileName,
    translations,
    validApiPayload,
    validationTileNames,
};

export function createWorkbenchApp(deps = {}) {
    const window = deps.window || globalThis.window;
    const document = deps.document || globalThis.document;
    const performance = {
        now: () => (deps.performance || window?.performance || globalThis.performance).now(),
    };
    const localStorage = deps.localStorage || window?.localStorage || {
        getItem() { return null; },
        setItem() {},
        removeItem() {},
    };
    const sessionStorage = deps.sessionStorage || window?.sessionStorage || {
        getItem() { return null; },
        setItem() {},
        removeItem() {},
    };
    const history = deps.history || window?.history || { replaceState() {} };
    const crypto = deps.crypto || window?.crypto || globalThis.crypto;
    const setTimeout = (...args) => (deps.setTimeout || window?.setTimeout || globalThis.setTimeout)(...args);
    const clearTimeout = (...args) => (deps.clearTimeout || window?.clearTimeout || globalThis.clearTimeout)(...args);
    const setInterval = (...args) => (deps.setInterval || window?.setInterval || globalThis.setInterval)(...args);
    const clearInterval = (...args) => (deps.clearInterval || window?.clearInterval || globalThis.clearInterval)(...args);
    const requestAnimationFrame = (...args) => {
        const fn = deps.requestAnimationFrame || window?.requestAnimationFrame;
        return typeof fn === "function" ? fn(...args) : 0;
    };
    const ResizeObserver = deps.ResizeObserver || window?.ResizeObserver;
    const getComputedStyle = (...args) => (deps.getComputedStyle || window?.getComputedStyle || globalThis.getComputedStyle)(...args);

    const state = deps.state || createInitialState();
    const elements = deps.elements || createElements(document);
    const ports = {};
    const fetch = (...args) => (ports.fetch ? ports.fetch(...args) : (deps.fetch || window?.fetch || globalThis.fetch)(...args));

    const ctx = {
        state,
        elements,
        window,
        document,
        performance,
        localStorage,
        sessionStorage,
        history,
        crypto,
        fetch,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        requestAnimationFrame,
        ResizeObserver,
        getComputedStyle,
        ports,
    };

    const i18n = createI18n(ctx);
    const stateStorage = createStateStorage(ctx);
    const unityBridge = createUnityBridge(ctx);
    ctx.unityHost = unityBridge.unityHost;
    const mapEditor = createMapEditor(ctx);
    const chatCards = createChatCards(ctx);
    const sessionActions = createSessionActions(ctx);

    const t = (...args) => ports.t(...args);
    const applyTranslations = (...args) => ports.applyTranslations(...args);
    const initializeUiHelp = (...args) => ports.initializeUiHelp(...args);
    const initializeWorkspaceHeaderAlignment = (...args) => ports.initializeWorkspaceHeaderAlignment(...args);
    const readHash = (...args) => ports.readHash(...args);
    const syncHash = (...args) => ports.syncHash(...args);
    const api = (...args) => ports.api(...args);
    const uniqueId = (...args) => ports.uniqueId(...args);
    const hideNotice = (...args) => ports.hideNotice(...args);
    const showError = (...args) => ports.showError(...args);
    const showPlayReturnNotice = (...args) => ports.showPlayReturnNotice(...args);
    const setStatus = (...args) => ports.setStatus(...args);
    const restoreComposerDraft = (...args) => ports.restoreComposerDraft(...args);
    const recoverPendingMessage = (...args) => ports.recoverPendingMessage(...args);
    const restoreUnsavedMapDraft = (...args) => ports.restoreUnsavedMapDraft(...args);
    const setProposalMode = (...args) => ports.setProposalMode(...args);
    const selectedStageKey = (...args) => ports.selectedStageKey(...args);
    const progressPanelStorageKey = (...args) => ports.progressPanelStorageKey(...args);
    const escapeHtml = (...args) => ports.escapeHtml(...args);
    const localizedErrorMessage = (...args) => ports.localizedErrorMessage(...args);
    const renderTechnicalDetails = (...args) => ports.renderTechnicalDetails(...args);
    const renderControlHints = (...args) => ports.renderControlHints(...args);
    const scheduleComponentRecovery = (...args) => ports.scheduleComponentRecovery(...args);
    const cancelComponentRecoveryTimer = (...args) => ports.cancelComponentRecoveryTimer(...args);
    const handleComposerInput = (...args) => ports.handleComposerInput(...args);
    const handleComposerKeydown = (...args) => ports.handleComposerKeydown(...args);
    const discardDraft = (...args) => ports.discardDraft(...args);
    const announceEmbeddedLabReady = (...args) => ports.announceEmbeddedLabReady(...args);
    const hasEmbeddedFormalHost = (...args) => ports.hasEmbeddedFormalHost(...args);
    const showFormalStandaloneBlock = (...args) => ports.showFormalStandaloneBlock(...args);
    const requestEmbeddedHostView = (...args) => ports.requestEmbeddedHostView(...args);
    const selectSetupLanguage = (...args) => ports.selectSetupLanguage(...args);
    const confirmSessionLanguage = (...args) => ports.confirmSessionLanguage(...args);
    const regenerateDraft = (...args) => ports.regenerateDraft(...args);
    const handleUnityBridgeMessage = (...args) => ports.handleUnityBridgeMessage(...args);
    const returnToUnity = (...args) => ports.returnToUnity(...args);
    const showLanding = (...args) => ports.showLanding(...args);
    const resumeDraftRegenerationPolling = (...args) => ports.resumeDraftRegenerationPolling(...args);
    const resetDraftFromSelection = (...args) => ports.resetDraftFromSelection(...args);
    const renderMap = (...args) => ports.renderMap(...args);
    const drawActiveCoordinateRoute = (...args) => ports.drawActiveCoordinateRoute(...args);
    const canEditSelected = (...args) => ports.canEditSelected(...args);
    const currentPendingProposal = (...args) => ports.currentPendingProposal(...args);
    const createMiniMap = (...args) => ports.createMiniMap(...args);
    const formatAttempt = (...args) => ports.formatAttempt(...args);
    const renderProgressContext = (...args) => ports.renderProgressContext(...args);
    const updateProgressPanelMaxHeight = (...args) => ports.updateProgressPanelMaxHeight(...args);
    const renderMessages = (...args) => ports.renderMessages(...args);
    const renderProposal = (...args) => ports.renderProposal(...args);
    const selectedStageHasPendingIntentConflict = (...args) => ports.selectedStageHasPendingIntentConflict(...args);
    const selectedStageHasActiveDisagreement = (...args) => ports.selectedStageHasActiveDisagreement(...args);
    const proposalFlowActive = (...args) => ports.proposalFlowActive(...args);
    const binaryFlowActive = (...args) => ports.binaryFlowActive(...args);
    const canChooseBinary = (...args) => ports.canChooseBinary(...args);
    const canUseChallengeChoice = (...args) => ports.canUseChallengeChoice(...args);
    const selectedStageTurns = (...args) => ports.selectedStageTurns(...args);
    const ensureVisibleTranslations = (...args) => ports.ensureVisibleTranslations(...args);
    const ensureAssessment = (...args) => ports.ensureAssessment(...args);
    const sendMessage = (...args) => ports.sendMessage(...args);
    const toggleProposalMode = (...args) => ports.toggleProposalMode(...args);
    const cancelBinaryProposal = (...args) => ports.cancelBinaryProposal(...args);
    const retryBinaryProposal = (...args) => ports.retryBinaryProposal(...args);
    const cancelBinaryRefreshTimer = (...args) => ports.cancelBinaryRefreshTimer(...args);
    const scheduleBinaryRefresh = (...args) => ports.scheduleBinaryRefresh(...args);
    const retryPendingMessage = (...args) => ports.retryPendingMessage(...args);
    const saveManualStage = (...args) => ports.saveManualStage(...args);
    const restoreSelectedStage = (...args) => ports.restoreSelectedStage(...args);
    const playSelectedStage = (...args) => ports.playSelectedStage(...args);
    const finalizeSession = (...args) => ports.finalizeSession(...args);
    const submitIntention = (...args) => ports.submitIntention(...args);
    const selectVersion = (...args) => ports.selectVersion(...args);
    const selectCurrentVersion = (...args) => ports.selectCurrentVersion(...args);
    const selectedVersion = (...args) => ports.selectedVersion(...args);
    const findVersion = (...args) => ports.findVersion(...args);

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
                const cleanHash = new URLSearchParams((window.location?.hash || "").replace(/^#/, ""));
                cleanHash.delete("bootstrap");
                history.replaceState(
                    null,
                    "",
                    `${window.location?.pathname || ""}${window.location?.search || ""}#${cleanHash}`
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
        const destination = new URL(launchUrl, window.location?.href || "http://localhost/");
        if (destination.origin !== window.location?.origin) {
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

    async function enterWorkspaceFromSession() {
        state.language = state.session.language || state.language;

        if (!findVersion(state.selectedVersionId)) {
            state.selectedVersionId = state.session.currentVersionId;
        }

        localStorage.setItem(selectedStageKey(), state.selectedVersionId);
        syncHash();
        state.activeCoordinateLink = null;
        resetDraftFromSelection();
        restoreUnsavedMapDraft();
        ports.render();
        void ensureVisibleTranslations();
        await ensureAssessment(state.session.currentVersionId);
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

        await ports.enterWorkspaceFromSession();
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
        ports.updateControls();
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
            setButtonDisabled(button, deadlineExpired()
                || state.translationInProgress
                || state.unityPlayActive);
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
        clearInterval(state.deadlineTimerId);
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
            if (deadlineExpired() && state.deadlineTimerId !== null) {
                clearInterval(state.deadlineTimerId);
                state.deadlineTimerId = null;
            }
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
                    state.assessmentRetryTimers.forEach(timerId => clearTimeout(timerId));
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
        if (!deadlineExpired()) state.deadlineTimerId = setInterval(update, 1000);
    }

    function updateControls() {
        if (!state.session) {
            elements.regenerateDraftButton.disabled = state.busy || state.demoCreationPending || Boolean(state.sessionId);
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
        const binaryRetryPending = binaryActive && proposalFlowStatus === "retry_pending";
        const interactionBusy = state.busy || state.unityPlayActive;
        const generationPending = state.chatStatus === "waiting"
            || state.assessmentRetryTimers.size > 0
            || state.assessing.size > 0;
        const canDiscardDraft = state.session.status === "active"
            && !state.unityPlayActive
            && state.selectedVersionId === state.session.currentVersionId;
        elements.saveStageButton.disabled = !editable || !state.dirty || interactionBusy;
        elements.discardDraftButton.disabled = !canDiscardDraft || !state.dirty || interactionBusy;
        elements.restoreStageButton.hidden = state.selectedVersionId === state.session.currentVersionId || state.session.status !== "active";
        elements.restoreStageButton.disabled = interactionBusy || expired;
        elements.playButton.disabled = interactionBusy || expired || state.dirty || pending || !selectedVersion();
        elements.finalizeButton.disabled = interactionBusy || (!expired && generationPending)
            || !selectedVersion()
            || (!selectedHistoricalStage && !expired && (state.dirty || pending));
        elements.messageInput.disabled = interactionBusy || !editable || intentConflictPending
            || proposalPlanning || proposalRetryPending || binaryActive;
        elements.messageInput.placeholder = intentConflictPending
            ? t("intentConflictPlaceholder")
            : proposalPlanning
                ? t("proposalPlanningPlaceholder")
                : (proposalRetryPending || binaryRetryPending)
                    ? t("proposalRetryPlaceholder")
                    : binaryActive && proposalFlowStatus === "revision_needed"
                        ? t("binaryRevisionNeededPlaceholder")
                        : binaryActive
                            ? (state.language === "zh-CN" ? "请点选 A/B；想重新描述时，先取消本次方案。" : "Choose A or B. Cancel this proposal to describe a different request.")
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
            cancel.disabled = interactionBusy || !editable || proposalPlanning;
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
                || binaryActive || button.dataset.actionable !== "true");
        });
        document.querySelectorAll(".intent-feedback-card button, .proposal-card button").forEach(button => {
            setButtonDisabled(button, interactionBusy || !editable);
        });
        document.querySelectorAll(".tile, .palette-button").forEach(button => {
            setButtonDisabled(button, interactionBusy || !editable);
        });
        const stageCardDisabled = expired
            || state.translationInProgress
            || state.unityPlayActive;
        document.querySelectorAll(".stage-card").forEach(button => {
            setButtonDisabled(button, stageCardDisabled);
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
        if (state.chatStatus === "waiting" && state.chatBusy && state.chatBusyVersionId === state.selectedVersionId) {
            return null;
        }
        if (!canEditSelected()) {
            return null;
        }
        if (binaryFlowActive() && (state.session.proposalFlowState.retryable || state.pendingBinary && state.chatStatus === "error")) {
            const hasChoices = (state.session?.proposalFlowState?.clarificationQuestionCount || 0) > 0
                || Boolean(state.session?.proposalFlowState?.pendingQuestion?.selectedChoiceId)
                || state.pendingBinary?.kind === "choice";
            return {
                error: state.chatError,
                message: state.language === "zh-CN"
                    ? (hasChoices
                        ? "本次方案尚未完成，已保存你的选择。可以重试或取消。"
                        : "本次方案尚未完成，已保存你的请求。可以重试或取消。")
                    : (hasChoices
                        ? "Your choices are saved. Retry or cancel this proposal."
                        : "Your proposal request is saved. Retry or cancel this proposal."),
                retryAction: retryBinaryProposal,
            };
        }
        if (state.chatStatus === "error"
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
        const binaryRetryAction = state.session?.proposalFlowState?.retryAction || state.pendingBinary;
        let storedBinaryAction = null;
        if (!binaryRetryAction && failure?.retryAction === retryBinaryProposal && state.sessionId) {
            try {
                storedBinaryAction = JSON.parse(localStorage.getItem(ports.binaryStorageKey()) || "null");
            } catch (_) {
                storedBinaryAction = null;
            }
        }
        const effectiveBinaryAction = binaryRetryAction || storedBinaryAction;
        const binaryChoiceDirtyBlocked = Boolean(
            failure?.retryAction === retryBinaryProposal
            && state.dirty
            && (effectiveBinaryAction?.kind === "choice" || Boolean(effectiveBinaryAction?.choiceId))
        );
        elements.chatRetryButton.hidden = !failure?.retryAction;
        elements.chatRetryButton.disabled = state.busy || state.unityPlayActive
            || !failure?.retryAction || binaryChoiceDirtyBlocked;
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
        ports.stopChatTimer();
        state.chatStartedAt = Date.now();
        state.chatTimerId = setInterval(ports.renderChatRequestStatus, 250);
    }

    function stopChatTimer() {
        if (state.chatTimerId !== null) {
            clearInterval(state.chatTimerId);
            state.chatTimerId = null;
        }
    }

    let domInitialized = false;
    function initDom() {
        if (domInitialized) return;
        domInitialized = true;
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
            if (!document.hidden) stateStorage.componentRecovery.paused.clear();
            scheduleComponentRecovery();
            scheduleBinaryRefresh();
        });
        window.addEventListener("pagehide", () => {
            cancelComponentRecoveryTimer();
            cancelBinaryRefreshTimer();
        });

        initializeUiHelp();
        initializeWorkspaceHeaderAlignment();
    }

    async function boot() {
        initDom();
        applyTranslations();
        return ports.initialize();
    }

    Object.assign(
        ports,
        i18n,
        stateStorage,
        unityBridge,
        mapEditor,
        chatCards,
        sessionActions,
        {
            initialize,
            createDemoSession,
            openCreatedSession,
            enterWorkspaceFromSession,
            refreshSession,
            render,
            renderStages,
            renderSessionState,
            deadlineExpired,
            deadlinePresentationState,
            deadlineAnnouncement,
            renderDeadline,
            updateControls,
            renderChatRequestStatus,
            renderTranslationStatus,
            selectedReplyFailure,
            renderReplyFeedback,
            showReplyNotice,
            chatWaitingMessage,
            startChatTimer,
            stopChatTimer,
        },
    );

    if (deps.overrides) {
        Object.assign(ports, deps.overrides);
    }

    if (deps.autoBindDom !== false) {
        initDom();
    }

    return {
        state,
        elements,
        ports,
        initDom,
        boot,
        ...ports,
    };
}

if (typeof window !== "undefined" && typeof document !== "undefined" && !window.__SOKOBAN_DISABLE_AUTO_BOOT__) {
    const workbench = createWorkbenchApp();
    void workbench.boot();
}
