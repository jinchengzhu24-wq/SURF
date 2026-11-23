import {
    DRAFT_REGENERATION_API_TIMEOUT_MS,
    DRAFT_REGENERATION_POLL_MS,
    DRAFT_REGENERATION_POLL_TIMEOUT_MS,
    DRAFT_REGENERATION_PROTOCOL_VERSION,
    UNITY_PLAY_BRIDGE_TIMEOUT_MS,
} from "./i18n.js?v=binary-discovery-20261123-v3-esm";
import { buildEntityLabels, tileClass, tileLabel } from "./map-editor.js?v=binary-discovery-20261123-v3-esm";

export function createUnityBridge(ctx) {
    const { state, elements, window, document, setTimeout, clearTimeout, clearInterval, ports } = ctx;

    const CURRENT_ORIGIN_HOST = window.location?.hostname
        || (window.location?.origin || "").replace(/^https?:\/\//, "").split(":")[0];
    const UNITY_ORIGINS = Array.from(new Set([
        window.location?.origin || "",
        "https://sokobanaidemo.top",
        "https://www.sokobanaidemo.top",
        ...(["localhost", "127.0.0.1"].includes(CURRENT_ORIGIN_HOST)
            ? ["http://127.0.0.1:8000", "http://localhost:8000"]
            : [])
    ].filter(Boolean)));
    const unityHost = {
        targetWindow: null,
        origin: "",
        sessionId: "",
        protocolVersion: 0,
        embedded: false,
        ready: false
    };

    const t = (...args) => ports.t(...args);
    const applyTranslations = (...args) => ports.applyTranslations(...args);
    const markTileHelp = (...args) => ports.markTileHelp(...args);
    const api = (...args) => ports.api(...args);
    const withBusy = (...args) => ports.withBusy(...args);
    const uniqueId = (...args) => ports.uniqueId(...args);
    const hideNotice = (...args) => ports.hideNotice(...args);
    const showNotice = (...args) => ports.showNotice(...args);
    const showError = (...args) => ports.showError(...args);
    const showPlayReturnNotice = (...args) => ports.showPlayReturnNotice(...args);
    const setStatus = (...args) => ports.setStatus(...args);
    const restoreComposerDraft = (...args) => ports.restoreComposerDraft(...args);
    const recoverPendingMessage = (...args) => ports.recoverPendingMessage(...args);
    const refreshSession = (...args) => ports.refreshSession(...args);
    const initialize = (...args) => ports.initialize(...args);
    const createDemoSession = (...args) => ports.createDemoSession(...args);
    const updateControls = (...args) => ports.updateControls(...args);
    const selectedVersion = (...args) => ports.selectedVersion(...args);
    const renderMap = (...args) => ports.renderMap(...args);
    const requestEmbeddedHostView = (...args) => ports.requestEmbeddedHostView(...args);
    const ensureDraftRegenerationPolling = (...args) => ports.ensureDraftRegenerationPolling(...args);

    function handleUnityHostInit(event, message) {
        if (window.parent === window
            || event.source !== window.parent
            || !UNITY_ORIGINS.includes(event.origin)
            || message.protocolVersion !== DRAFT_REGENERATION_PROTOCOL_VERSION
            || !message.sessionId
            || message.sessionId !== state.sessionId) {
            return false;
        }
        unityHost.targetWindow = event.source;
        unityHost.origin = event.origin;
        unityHost.sessionId = message.sessionId;
        unityHost.protocolVersion = message.protocolVersion;
        unityHost.embedded = true;
        unityHost.ready = true;
        announceEmbeddedLabReady();
        if (state.session && !state.session.demoMode && !elements.formalStandaloneBlock.hidden) {
            void refreshSession().then(enteredWorkspace => {
                if (enteredWorkspace) {
                    restoreComposerDraft();
                    recoverPendingMessage();
                }
            }).catch(error => showError(error, initialize));
        }
        return true;
    }

    function hasEmbeddedFormalHost() {
        return window.parent !== window
            && unityHost.embedded
            && unityHost.ready
            && unityHost.targetWindow === window.parent
            && unityHost.sessionId === state.sessionId
            && unityHost.protocolVersion === DRAFT_REGENERATION_PROTOCOL_VERSION;
    }

    function showFormalStandaloneBlock() {
        clearInterval(state.deadlineTimerId);
        elements.deadlineStatus.hidden = true;
        elements.landing.hidden = true;
        elements.workspace.hidden = true;
        elements.formalStandaloneBlock.hidden = false;
        applyTranslations();
        setStatus(t("ready"), "ready");
    }

    function announceEmbeddedLabReady() {
        if (!unityHost.embedded
            || !unityHost.ready
            || !state.session
            || unityHost.sessionId !== state.sessionId) {
            return;
        }
        unityHost.targetWindow.postMessage({
            type: "sokoban:cocreation-lab-ready",
            protocolVersion: DRAFT_REGENERATION_PROTOCOL_VERSION,
            sessionId: state.sessionId
        }, unityHost.origin);
    }

    function activeUnityBridge() {
        if (unityHost.embedded
            && unityHost.ready
            && unityHost.targetWindow
            && unityHost.sessionId === state.sessionId) {
            return {
                unityWindow: unityHost.targetWindow,
                origins: [unityHost.origin],
                protocolVersion: DRAFT_REGENERATION_PROTOCOL_VERSION,
                embedded: true
            };
        }
        return null;
    }

    function requestEmbeddedHostViewImpl(type, requestId = "") {
        const bridge = activeUnityBridge();
        if (!bridge?.embedded) return false;
        bridge.unityWindow.postMessage({
            type,
            protocolVersion: DRAFT_REGENERATION_PROTOCOL_VERSION,
            sessionId: state.sessionId,
            requestId
        }, bridge.origins[0]);
        return true;
    }

    function tryOpenPlayInExistingUnity(playUrl) {
        const bridge = activeUnityBridge();
        if (!bridge) return Promise.resolve(false);
        const targetWin = bridge.unityWindow;

        const requestId = uniqueId("unity_play");

        return new Promise(resolve => {
            let settled = false;
            const finish = value => {
                if (settled) return;
                settled = true;
                clearTimeout(timeoutId);
                window.removeEventListener("message", receiveAcknowledgement);
                if (!value && bridge.embedded) {
                    requestEmbeddedHostView("sokoban:cocreation-show-lab");
                }
                resolve(value);
            };
            const receiveAcknowledgement = event => {
                const message = event.data;
                if (event.source !== targetWin
                    || !bridge.origins.includes(event.origin)
                    || !message
                    || message.type !== "sokoban:cocreation-play-ack"
                    || message.requestId !== requestId
                    || (bridge.embedded && message.protocolVersion !== bridge.protocolVersion)) {
                    return;
                }
                finish(message.accepted === true);
            };
            const timeoutId = setTimeout(
                () => finish(false),
                UNITY_PLAY_BRIDGE_TIMEOUT_MS
            );

            window.addEventListener("message", receiveAcknowledgement);
            const message = {
                type: "sokoban:cocreation-play-request",
                requestId,
                sessionId: state.sessionId,
                playUrl,
                protocolVersion: bridge.protocolVersion
            };

            bridge.origins.forEach(origin => {
                try {
                    targetWin.postMessage(message, origin);
                } catch (_error) {
                    // Try the remaining known Unity origins before falling back.
                }
            });
        });
    }

    async function handleUnityBridgeMessage(event) {
        const message = event.data;
        if (message?.type === "sokoban:cocreation-host-init") {
            handleUnityHostInit(event, message);
            return;
        }

        const bridge = activeUnityBridge();
        if (!bridge
            || event.source !== bridge.unityWindow
            || !bridge.origins.includes(event.origin)
            || !message
            || message.sessionId !== state.sessionId) {
            return;
        }

        if (message.type === "sokoban:cocreation-draft-regenerate-return") {
            const currentRequestId = state.session?.draftPreview?.regenerationRequestId
                || state.draftPollRequestId;
            if (message.protocolVersion !== bridge.protocolVersion
                || !currentRequestId
                || message.requestId !== currentRequestId
                || !["completed", "failed"].includes(message.status)) {
                return;
            }
            try {
                await ensureDraftRegenerationPolling(currentRequestId);
            } catch (error) {
                showError(error, () => handleUnityBridgeMessage(event));
            }
            return;
        }

        if (message.type !== "sokoban:cocreation-play-return") return;
        if (message.protocolVersion !== bridge.protocolVersion) return;

        try {
            state.unityPlayActive = false;
            const enteredWorkspace = await refreshSession();
            if (enteredWorkspace) {
                restoreComposerDraft();
                recoverPendingMessage();
                showPlayReturnNotice(message.status);
            }
        } catch (error) {
            if (state.session && !elements.workspace.hidden && selectedVersion()) {
                renderMap();
            }
            updateControls();
            showError(error, () => handleUnityBridgeMessage(event));
        }
    }

    function returnToUnity() {
        hideNotice();
        if (requestEmbeddedHostView("sokoban:cocreation-show-unity")) {
            return;
        }
        showNotice(t("returnUnityUnavailable"));
    }

    function selectSetupLanguage(language) {
        if (state.busy || state.demoCreationPending || state.session?.languageLocked) return;
        state.language = language;
        applyTranslations();
        if (!elements.landing.hidden) {
            renderDraftPreview();
            renderLandingStatus();
        }
    }

    async function confirmSessionLanguage() {
        if (!state.session || state.session.languageLocked) return;
        if (!state.session.demoMode && !hasEmbeddedFormalHost()) {
            showFormalStandaloneBlock();
            return;
        }
        await withBusy(async () => {
            await api(`/api/sessions/${state.sessionId}/language`, {
                method: "PATCH",
                body: { language: state.language }
            });
            const enteredWorkspace = await refreshSession();
            if (enteredWorkspace) {
                restoreComposerDraft();
                recoverPendingMessage();
            }
        });
    }

    async function regenerateDraft() {
        if (state.busy || state.draftRegenerationPending) return;
        if (!state.session) {
            if (!state.sessionId) void createDemoSession();
            return;
        }
        if (!state.session.draftPreview?.mutable || state.session.languageLocked) return;
        if (!state.session.demoMode && !hasEmbeddedFormalHost()) {
            showFormalStandaloneBlock();
            return;
        }
        const regenerationKey = state.draftRegenerationKey || uniqueId("draft_regeneration");
        state.draftRegenerationKey = regenerationKey;
        let preparedBridge = null;
        if (!state.session.demoMode) {
            preparedBridge = prepareUnityForDraftRegeneration();
        }
        state.draftRegenerationPending = true;
        state.busy = true;
        hideNotice();
        renderDraftPreview();
        updateControls();
        try {
            if (!state.session.demoMode) {
                preparedBridge = await preparedBridge;
                if (!preparedBridge?.accepted) {
                    showNotice(draftBridgeFailureMessage(preparedBridge?.reason));
                    return;
                }
            }
            state.session = await createOrRecoverDraftRegeneration(regenerationKey);
            renderDraftPreview();
            updateControls();

            const requestId = state.session.draftPreview?.regenerationRequestId;
            if (!state.session.demoMode && requestId) {
                const delivery = await sendUnityDraftRegenerationRequest(preparedBridge, requestId);
                if (delivery.received && !delivery.accepted) {
                    const latest = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}`, {
                        timeoutMs: DRAFT_REGENERATION_POLL_TIMEOUT_MS
                    });
                    state.session = latest;
                    if (latest.draftPreview?.regenerationRequestId === requestId
                        && latest.draftPreview?.regenerationStatus === "pending") {
                        state.session = await api(
                            `/api/sessions/${encodeURIComponent(state.sessionId)}/draft-regenerations/${encodeURIComponent(requestId)}/cancel`,
                            { method: "POST", timeoutMs: DRAFT_REGENERATION_API_TIMEOUT_MS }
                        );
                        state.draftRegenerationKey = "";
                        showNotice(draftBridgeFailureMessage(delivery.reason));
                        return;
                    }
                }
                await ensureDraftRegenerationPolling(requestId);
            }
            state.draftRegenerationKey = "";
        } catch (error) {
            if (preparedBridge) abortPreparedDraftRegeneration(preparedBridge);
            if (!error?.retryable) state.draftRegenerationKey = "";
            showError(error, () => void regenerateDraft());
        } finally {
            if (!state.session?.demoMode) {
                requestEmbeddedHostView(
                    "sokoban:cocreation-show-lab",
                    state.session?.draftPreview?.regenerationRequestId || ""
                );
            }
            state.busy = false;
            state.draftRegenerationPending = false;
            renderDraftPreview();
            updateControls();
        }
    }

    async function createOrRecoverDraftRegeneration(idempotencyKey) {
        const create = () => api(`/api/sessions/${encodeURIComponent(state.sessionId)}/draft-regenerations`, {
            method: "POST",
            body: { idempotencyKey },
            timeoutMs: DRAFT_REGENERATION_API_TIMEOUT_MS
        });
        try {
            return await create();
        } catch (error) {
            const shouldRecover = Boolean(error?.retryable)
                || ["CLIENT_TIMEOUT", "RETRYABLE_REQUEST_FAILED", "INVALID_RESPONSE"].includes(error?.code);
            if (!shouldRecover) {
                throw error;
            }
            try {
                const latest = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}`, {
                    timeoutMs: DRAFT_REGENERATION_POLL_TIMEOUT_MS
                });
                if (["pending", "claimed"].includes(latest.draftPreview?.regenerationStatus)) {
                    return latest;
                }
            } catch (_recoveryError) {
                // Retry the same idempotency key; the server decides whether this is a replay.
            }
            try {
                return await create();
            } catch (retryError) {
                if (retryError
                    && retryError.retryable === undefined
                    && ["CLIENT_TIMEOUT", "RETRYABLE_REQUEST_FAILED", "INVALID_RESPONSE"].includes(retryError.code)) {
                    retryError.retryable = true;
                }
                throw retryError;
            }
        }
    }

    function draftBridgeFailureMessage(reason) {
        const key = {
            protocol_mismatch: "draftProtocolMismatch",
            session_mismatch: "draftSessionMismatch",
            unity_not_ready: "draftUnityNotReady",
            receiver_not_ready: "draftReceiverNotReady",
            prepare_missing: "draftPrepareExpired",
            prepare_expired: "draftPrepareExpired",
            request_mismatch: "draftPrepareExpired"
        }[reason] || "draftProtocolUnavailable";
        return t(key);
    }

    function prepareUnityForDraftRegeneration() {
        const bridge = activeUnityBridge();
        if (!bridge) return Promise.resolve({ accepted: false, reason: "unity_not_ready" });
        const targetWin = bridge.unityWindow;
        const bridgeRequestId = uniqueId("unity_draft_regeneration_prepare");
        return new Promise(resolve => {
            let settled = false;
            const finish = result => {
                if (settled) return;
                settled = true;
                clearTimeout(timeoutId);
                window.removeEventListener("message", receiveAcknowledgement);
                resolve(result.accepted ? {
                    unityWindow: targetWin,
                    origins: bridge.origins,
                    protocolVersion: bridge.protocolVersion,
                    embedded: bridge.embedded,
                    prepareRequestId: bridgeRequestId,
                    accepted: true,
                    reason: ""
                } : result);
            };
            const receiveAcknowledgement = event => {
                const message = event.data;
                if (event.source !== targetWin || !bridge.origins.includes(event.origin)
                    || message?.type !== "sokoban:cocreation-draft-regenerate-prepare-ack"
                    || message.requestId !== bridgeRequestId
                    || message.sessionId !== state.sessionId) return;
                finish({
                    accepted: message.accepted === true
                        && message.protocolVersion === bridge.protocolVersion,
                    reason: message.reason || (message.protocolVersion === bridge.protocolVersion
                        ? "receiver_not_ready"
                        : "protocol_mismatch")
                });
            };
            const timeoutId = setTimeout(
                () => finish({ accepted: false, reason: "unity_not_ready" }),
                UNITY_PLAY_BRIDGE_TIMEOUT_MS
            );
            window.addEventListener("message", receiveAcknowledgement);
            const message = {
                type: "sokoban:cocreation-draft-regenerate-prepare",
                requestId: bridgeRequestId,
                sessionId: state.sessionId,
                protocolVersion: bridge.protocolVersion
            };
            bridge.origins.forEach(origin => {
                try { targetWin.postMessage(message, origin); } catch (_error) { /* continue */ }
            });
            if (bridge.embedded) {
                requestEmbeddedHostView("sokoban:cocreation-show-unity");
            }
        });
    }

    function sendUnityDraftRegenerationRequest(preparedBridge, requestId) {
        if (!preparedBridge?.unityWindow || preparedBridge.unityWindow.closed) {
            return Promise.resolve({ received: true, accepted: false, reason: "unity_not_ready" });
        }
        const bridgeRequestId = uniqueId("unity_draft_regeneration");
        return new Promise(resolve => {
            let settled = false;
            const finish = result => {
                if (settled) return;
                settled = true;
                clearTimeout(timeoutId);
                window.removeEventListener("message", receiveAcknowledgement);
                resolve(result);
            };
            const receiveAcknowledgement = event => {
                const message = event.data;
                if (event.source !== preparedBridge.unityWindow
                    || !preparedBridge.origins.includes(event.origin)
                    || message?.type !== "sokoban:cocreation-draft-regenerate-ack"
                    || message.requestId !== bridgeRequestId
                    || message.sessionId !== state.sessionId
                    || message.regenerationRequestId !== requestId) return;
                finish({
                    received: true,
                    accepted: message.accepted === true
                        && message.protocolVersion === preparedBridge.protocolVersion,
                    reason: message.reason || ""
                });
            };
            const timeoutId = setTimeout(
                () => finish({ received: false, accepted: false, reason: "" }),
                UNITY_PLAY_BRIDGE_TIMEOUT_MS
            );
            window.addEventListener("message", receiveAcknowledgement);
            const message = {
                type: "sokoban:cocreation-draft-regenerate-request",
                requestId: bridgeRequestId,
                prepareRequestId: preparedBridge.prepareRequestId,
                protocolVersion: preparedBridge.protocolVersion,
                sessionId: state.sessionId,
                regenerationRequestId: requestId
            };
            preparedBridge.origins.forEach(origin => {
                try { preparedBridge.unityWindow.postMessage(message, origin); } catch (_error) { /* continue */ }
            });
        });
    }

    function abortPreparedDraftRegeneration(preparedBridge) {
        if (!preparedBridge?.unityWindow || preparedBridge.unityWindow.closed) return;
        const message = {
            type: "sokoban:cocreation-draft-regenerate-abort",
            protocolVersion: preparedBridge.protocolVersion,
            sessionId: state.sessionId,
            prepareRequestId: preparedBridge.prepareRequestId
        };
        preparedBridge.origins.forEach(origin => {
            try { preparedBridge.unityWindow.postMessage(message, origin); } catch (_error) { /* continue */ }
        });
        requestEmbeddedHostView("sokoban:cocreation-show-lab");
    }

    async function waitForDraftRegeneration(requestId) {
        while (state.sessionId && !state.session?.languageLocked) {
            await new Promise(resolve => setTimeout(resolve, DRAFT_REGENERATION_POLL_MS));
            let latest;
            try {
                latest = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}`, {
                    timeoutMs: DRAFT_REGENERATION_POLL_TIMEOUT_MS
                });
            } catch (_error) {
                showNotice(t("draftReconnecting"));
                requestEmbeddedHostView("sokoban:cocreation-show-lab", requestId);
                await new Promise(resolve => setTimeout(resolve, 2000));
                continue;
            }
            if (latest.draftPreview?.regenerationRequestId !== requestId) return;
            state.session = latest;
            renderDraftPreview();
            updateControls();
            const status = latest.draftPreview?.regenerationStatus;
            if (!["pending", "claimed"].includes(status)) {
                state.draftRegenerationKey = "";
                if (["failed", "cancelled", "timed_out"].includes(status)) {
                    showNotice(draftRegenerationFailureMessage(latest.draftPreview?.failureCode));
                } else {
                    hideNotice();
                }
                return;
            }
        }
    }

    function draftRegenerationFailureMessage(failureCode) {
        const detailKey = {
            blueprint_unavailable: "draftBlueprintUnavailable",
            generation_timed_out: "draftGenerationTimedOut",
            generation_failed: "draftGenerationFailed",
            llm_failed: "draftLlmFailed",
            validation_failed: "draftValidationFailed",
            unity_unavailable: "draftUnityUnavailable"
        }[failureCode] || "draftUnityUnavailable";
        return `${t("draftRegenerationFailed")} ${t(detailKey)}`;
    }

    function ensureDraftRegenerationPollingImpl(requestId) {
        if (!requestId) return Promise.resolve();
        if (state.draftPollPromise && state.draftPollRequestId === requestId) {
            return state.draftPollPromise;
        }
        state.draftPollRequestId = requestId;
        const pollPromise = waitForDraftRegeneration(requestId).finally(() => {
            if (state.draftPollRequestId === requestId) {
                state.draftPollRequestId = "";
                state.draftPollPromise = null;
            }
        });
        state.draftPollPromise = pollPromise;
        return pollPromise;
    }

    function resumeDraftRegenerationPolling() {
        const preview = state.session?.draftPreview;
        if (!["pending", "claimed"].includes(preview?.regenerationStatus)
            || state.draftPollTimerId) return;
        state.draftPollTimerId = setTimeout(async () => {
            state.draftPollTimerId = null;
            try { await ensureDraftRegenerationPolling(preview.regenerationRequestId); }
            catch (error) { showError(error, resumeDraftRegenerationPolling); }
        }, 0);
    }

    function showLanding(mode = "demo") {
        state.landingMode = mode;
        elements.formalStandaloneBlock.hidden = true;
        elements.workspace.hidden = true;
        elements.landing.hidden = false;
        const isFormal = mode === "formal";
        elements.landingEyebrow.dataset.i18n = "sessionSetup";
        elements.landingTitle.dataset.i18n = state.demoCreationPending ? "draftLoadingTitle"
            : state.demoCreationFailed ? "draftInitialFailed"
            : isFormal ? "formalLandingTitle" : "demoLandingTitle";
        elements.landingBody.dataset.i18n = state.demoCreationPending ? "draftLoadingBody"
            : isFormal ? "formalLandingBody" : "demoLandingBody";
        elements.languageLockNotice.dataset.i18n = mode === "formal" ? "formalLanguageLock" : "demoLanguageLock";
        elements.draftActions.hidden = false;
        applyTranslations();
        renderDraftPreview();
        renderLandingStatus();
        updateControls();
    }

    function renderDraftPreview() {
        const preview = state.session?.draftPreview;
        let rows = preview?.rows;
        if (!rows && state.session && !state.session.languageLocked) {
            rows = state.session.versions?.find(item => item.versionId === state.session.currentVersionId)?.rows;
        }
        const hasRows = Array.isArray(rows) && rows.length === 10;
        const isDemoLanding = state.landingMode.startsWith("demo");
        const regenerationStatus = preview?.regenerationStatus;
        const loading = state.demoCreationPending || state.draftRegenerationPending
            || ["pending", "claimed"].includes(regenerationStatus);
        elements.draftPreview.hidden = !hasRows && !isDemoLanding;
        if (elements.draftPreview.hidden) return;

        const isFormal = state.landingMode === "formal";
        elements.draftPreviewTitle.dataset.i18n = isFormal
            ? "formalDraftPreviewTitle"
            : "demoDraftPreviewTitle";
        elements.draftPreviewTitle.textContent = t(elements.draftPreviewTitle.dataset.i18n);
        elements.draftPreviewGrid.hidden = !hasRows;
        elements.draftPreviewLoading.hidden = !loading;
        elements.draftPreviewLoading.classList.toggle("over-map", hasRows);
        elements.draftPreviewLoadingMessage.textContent = state.demoCreationPending
            ? t("draftInitialGenerating")
            : t("draftRegenerating");

        if (hasRows) {
            elements.draftPreviewGrid.textContent = "";
            elements.draftPreviewGrid.setAttribute("aria-label", t(elements.draftPreviewTitle.dataset.i18n));
            const entityLabels = buildEntityLabels(rows);
            rows.forEach((row, y) => [...row].forEach((tile, x) => {
                const cell = document.createElement("span");
                cell.className = `draft-preview-cell ${tileClass(tile)}`;
                const label = entityLabels.get(`${x},${y}`);
                cell.textContent = label || tileLabel(tile);
                markTileHelp(cell, tile, x, y, label, "preview");
                elements.draftPreviewGrid.appendChild(cell);
            }));
        }

        elements.draftGenerationLabel.textContent = hasRows
            ? t("draftGeneration").replace("{generation}", String(preview?.generation || 1))
            : "";
        elements.draftPreviewStatus.textContent = loading
            ? (state.demoCreationPending ? t("draftInitialGenerating") : t("draftRegenerating"))
            : state.demoCreationFailed
                ? t("draftInitialFailed")
                : ["failed", "cancelled", "timed_out"].includes(regenerationStatus)
                    ? t("draftRegenerationFailed")
                    : t("draftReady");
    }

    function renderLandingStatus() {
        if (state.demoCreationPending) {
            setStatus(t("draftInitialGenerating"), "pending");
            return;
        }
        if (state.session && !state.session.languageLocked) {
            setStatus(t("ready"), "ready");
            return;
        }
        setStatus(t("errorNoSession"), "pending");
    }

    return {
        unityHost,
        UNITY_ORIGINS,
        handleUnityHostInit,
        hasEmbeddedFormalHost,
        showFormalStandaloneBlock,
        announceEmbeddedLabReady,
        activeUnityBridge,
        requestEmbeddedHostView: requestEmbeddedHostViewImpl,
        tryOpenPlayInExistingUnity,
        handleUnityBridgeMessage,
        returnToUnity,
        selectSetupLanguage,
        confirmSessionLanguage,
        regenerateDraft,
        createOrRecoverDraftRegeneration,
        draftBridgeFailureMessage,
        prepareUnityForDraftRegeneration,
        sendUnityDraftRegenerationRequest,
        abortPreparedDraftRegeneration,
        waitForDraftRegeneration,
        draftRegenerationFailureMessage,
        ensureDraftRegenerationPolling: ensureDraftRegenerationPollingImpl,
        resumeDraftRegenerationPolling,
        showLanding,
        renderDraftPreview,
        renderLandingStatus,
    };
}
