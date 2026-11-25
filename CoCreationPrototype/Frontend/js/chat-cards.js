import { GUIDANCE_CUE_LABELS, LLM_REQUEST_TIMEOUT_MS, REQUEST_COPY } from "./i18n.js?v=binary-discovery-20261123-v3-esm";
import { tileName } from "./map-editor.js?v=binary-discovery-20261123-v3-esm";
import { setButtonDisabled } from "./state-storage.js?v=binary-discovery-20261123-v3-esm";

export function isInternalProposalFailureCue(cue) {
    if (!cue || cue.type !== "clarification") return false;
    const text = String(cue.text || "").toLowerCase();
    return text.includes("无法形成")
        || text.includes("可验证的方案")
        || text.includes("could not form a proposal")
        || text.includes("fully consistent with the saved map");
}

export function assistantBodyWithoutCues(content, uiCues, question) {
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

export function formatCoordinatePoint(point) {
    return `row ${point?.row}, column ${point?.column}`;
}

export function normalizedCoordinatePoint(point) {
    if (!point || !Number.isInteger(point.row) || !Number.isInteger(point.column)) return null;
    if (point.row < 1 || point.row > 10 || point.column < 1 || point.column > 12) return null;
    return { row: point.row, column: point.column };
}

export function findCoordinateRoute(rows, from, to) {
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

const defaultAssistantBodyWithoutCues = assistantBodyWithoutCues;

export function createChatCards(ctx) {
    const { state, elements, window, document, localStorage, setTimeout, requestAnimationFrame, ports } = ctx;

    const t = (...args) => ports.t(...args);
    const recordReplyDiagnostic = (...args) => ports.recordReplyDiagnostic(...args);
    const turnReplyIdentity = (...args) => ports.turnReplyIdentity(...args);
    const progressPanelKey = (...args) => ports.progressPanelKey(...args);
    const progressPanelStorageKey = (...args) => ports.progressPanelStorageKey(...args);
    const makeButton = (...args) => ports.makeButton(...args);
    const uniqueId = (...args) => ports.uniqueId(...args);
    const api = (...args) => ports.api(...args);
    const withBusy = (...args) => ports.withBusy(...args);
    const showError = (...args) => ports.showError(...args);
    const escapeHtml = (...args) => ports.escapeHtml(...args);
    const persistPendingMessage = (...args) => ports.persistPendingMessage(...args);
    const handleComposerInput = (...args) => ports.handleComposerInput(...args);
    const canEditSelected = (...args) => ports.canEditSelected(...args);
    const drawActiveCoordinateRoute = (...args) => ports.drawActiveCoordinateRoute(...args);
    const currentPendingProposal = (...args) => ports.currentPendingProposal(...args);
    const createMiniMap = (...args) => ports.createMiniMap(...args);
    const selectVersion = (...args) => ports.selectVersion(...args);
    const selectedVersion = (...args) => ports.selectedVersion(...args);
    const activeChallengeComposerState = (...args) => ports.activeChallengeComposerState(...args);
    const submitPendingMessage = (...args) => ports.submitPendingMessage(...args);
    const decideProposal = (...args) => ports.decideProposal(...args);
    const submitBinaryAction = (...args) => ports.submitBinaryAction(...args);
    const render = (...args) => ports.render(...args);
    const updateControls = (...args) => ports.updateControls(...args);
    const deadlineExpired = (...args) => ports.deadlineExpired(...args);
    const showReplyNotice = (...args) => ports.showReplyNotice(...args);
    const renderTranslationStatus = (...args) => ports.renderTranslationStatus(...args);
    const localizedAssistantTurn = (...args) => ports.localizedAssistantTurn(...args);
    const proposalForTurn = (...args) => ports.proposalForTurn(...args);
    const renderAssistantBody = (...args) => ports.renderAssistantBody(...args);
    const assistantBodyWithoutCues = (...args) => ports.assistantBodyWithoutCues(...args);
    const isLatestRevisionOfferTurn = (...args) => ports.isLatestRevisionOfferTurn(...args);
    const ensureVisibleTranslations = (...args) => ports.ensureVisibleTranslations(...args);

function renderProgressContext() {
    if (!elements.progressPanel) return;
    if (!state.session || !Array.isArray(state.session.progressContexts)) {
        elements.progressPanel.hidden = true;
        return;
    }

    const key = progressPanelKey();
    if (elements.progressPanel.dataset.progressKey !== key) {
        elements.progressPanel.dataset.progressKey = key;
        const saved = localStorage.getItem(progressPanelStorageKey());
        elements.progressPanel.open = saved === "open";
    }
    elements.progressSummary.setAttribute(
        "aria-expanded",
        elements.progressPanel.open ? "true" : "false",
    );

    const context = (state.session.progressContexts || []).find(
        item => item.versionId === state.selectedVersionId
    ) || { questionRecords: [], designInclinations: [] };

    elements.progressPanel.hidden = false;
    const questions = Array.isArray(context.questionRecords) ? context.questionRecords : [];
    renderQuestionRecords(
        elements.unresolvedQuestionsList,
        questions.filter(item => item?.status === "unanswered"),
        false,
    );
    renderQuestionRecords(
        elements.answeredQuestionsList,
        questions.filter(item => item?.status === "answered" || item?.status === "ignored"),
        true,
    );
    if (elements.answeredQuestionsPanel) {
        elements.answeredQuestionsPanel.hidden = !questions.some(
            item => item?.status === "answered" || item?.status === "ignored"
        );
    }
    renderDesignInclinations(context.designInclinations);
    requestAnimationFrame(updateProgressPanelMaxHeight);
}

function renderQuestionRecords(container, items, answered) {
    if (!container) return;
    container.textContent = "";
    const entries = Array.isArray(items) ? items : [];
    if (!entries.length) {
        const empty = document.createElement("p");
        empty.className = "progress-empty";
        empty.textContent = t(answered ? "noAnsweredQuestions" : "noUnresolvedQuestions");
        container.appendChild(empty);
        return;
    }
    entries.forEach(item => {
        const record = document.createElement("article");
        const ignored = item?.status === "ignored";
        record.className = `progress-item progress-item-question ${ignored ? "ignored" : answered ? "answered" : "unanswered"}`;
        record.setAttribute("aria-busy", "false");
        const text = document.createElement("p");
        text.className = "progress-item-title progress-item-text";
        text.textContent = String(item?.question || "").trim();
        record.appendChild(text);
        const footer = document.createElement("footer");
        footer.className = "progress-item-footer";
        const meta = document.createElement("small");
        meta.className = "progress-item-meta";
        const asked = Number(item?.askedAtStageNumber);
        const answeredAt = Number(item?.answeredAtStageNumber);
        const ignoredAt = Number(item?.ignoredAtStageNumber);
        const values = [ignored ? t("ignoredLabel") : answered ? t("answeredLabel") : t("unresolvedLabel")];
        if (Number.isInteger(asked)) values.push(t("askedAtStage").replace("{stage}", String(asked)));
        if (answered && Number.isInteger(answeredAt)) {
            values.push(t("answeredAtStage").replace("{stage}", String(answeredAt)));
        }
        meta.textContent = values.join(" \u00b7 ");
        footer.appendChild(meta);
        if (ignored && Number.isInteger(ignoredAt)) {
            meta.textContent += ` \u00b7 ${t("ignoredAtStage").replace("{stage}", String(ignoredAt))}`;
        }
        if (item?.questionId && (!answered || ignored)) {
            const action = ignored ? "restore" : "ignore";
            const feedbackKey = `${item.questionId}:${action}`;
            const button = makeButton(
                t(ignored ? "restoreQuestion" : "ignoreQuestion"),
                "secondary-button guidance-cue-button question-feedback-button",
                () => void submitQuestionFeedback(action, item, record),
            );
            button.dataset.feedbackKey = feedbackKey;
            button.disabled = state.busy || !canEditSelected()
                || state.questionFeedbackBusy.has(feedbackKey);
            footer.appendChild(button);
        }
        record.appendChild(footer);
        container.appendChild(record);
    });
}

async function submitQuestionFeedback(action, item, record, idempotencyKey = null, baseVersionId = null) {
    if (state.busy || !canEditSelected() || !item?.questionId) return;
    const feedbackKey = `${item.questionId}:${action}`;
    if (state.questionFeedbackBusy.has(feedbackKey)) return;
    const requestKey = idempotencyKey || uniqueId(`question-${action}`);
    const targetVersionId = baseVersionId || state.session.currentVersionId;
    state.questionFeedbackBusy.add(feedbackKey);
    record.setAttribute("aria-busy", "true");
    record.querySelectorAll("button").forEach(button => { button.disabled = true; });
    try {
        await withBusy(async () => {
            const result = await api(
                `/api/sessions/${state.sessionId}/questions/${item.questionId}/feedback`,
                {
                    method: "POST",
                    body: {
                        action,
                        baseVersionId: targetVersionId,
                        idempotencyKey: requestKey,
                    },
                },
            );
            state.session = result.session;
            selectVersion(state.session.currentVersionId, false, { preserveSameStageDraft: true });
            render();
        });
    } finally {
        state.questionFeedbackBusy.delete(feedbackKey);
        if (record.isConnected) {
            record.setAttribute("aria-busy", "false");
            record.querySelectorAll("button").forEach(button => {
                button.disabled = state.busy || !canEditSelected();
            });
        }
        updateControls();
    }
}

function updateProgressPanelMaxHeight() {
    if (!elements.progressPanel) return;
    const chatPanel = elements.progressPanel.closest(".chat-panel");
    const panelHeight = chatPanel?.getBoundingClientRect().height || 0;
    if (panelHeight <= 0) return;
    const progressHeight = elements.progressPanel.open
        ? elements.progressPanel.getBoundingClientRect().height
        : 0;
    const summaryHeight = elements.progressSummary?.getBoundingClientRect().height || 0;
    const referenceHeight = window.matchMedia?.("(min-width: 1201px)")?.matches
        ? panelHeight
        : panelHeight - Math.max(0, progressHeight - summaryHeight);
    const progressMaxHeight = Math.max(
        summaryHeight,
        Math.floor(Math.max(summaryHeight, referenceHeight) * 0.44),
    );
    elements.progressPanel.style.setProperty(
        "--progress-panel-max-height",
        `${progressMaxHeight}px`,
    );
    elements.progressPanel.style.setProperty(
        "--progress-panel-body-max-height",
        `${Math.max(0, progressMaxHeight - Math.ceil(summaryHeight))}px`,
    );
}

function renderProgressItems(container, items, type, emptyKey) {
    if (!container) return;
    container.textContent = "";
    const entries = Array.isArray(items) ? items : [];
    if (!entries.length) {
        const empty = document.createElement("p");
        empty.className = "progress-empty";
        empty.textContent = t(emptyKey);
        container.appendChild(empty);
        return;
    }

    entries.forEach(item => {
        const record = document.createElement("article");
        record.className = `progress-item progress-item-${type}`;

        const text = document.createElement("p");
        text.className = "progress-item-text";
        text.textContent = String(item?.text || item?.decision || item?.question || "").trim();
        record.appendChild(text);

        if (type === "decision" && item?.reason) {
            const reason = document.createElement("p");
            reason.className = "progress-item-reason";
            reason.textContent = String(item.reason);
            record.appendChild(reason);
        }

        const meta = document.createElement("small");
        const stageNumber = Number(item?.sourceStageNumber);
        const source = Number.isInteger(stageNumber)
            ? t("fromStage").replace("{stage}", String(stageNumber))
            : "";
        const label = type === "decision"
            ? t("confirmedLabel")
            : type === "direction" ? t("explicitLabel") : t("unresolvedLabel");
        const updated = formatProgressTime(item?.updatedAt);
        const updatedLabel = updated
            ? t("updatedAt").replace("{time}", updated)
            : "";
        meta.textContent = [label, source, updatedLabel].filter(Boolean).join(" \u00b7 ");
        record.appendChild(meta);
        container.appendChild(record);
    });
}

function renderDesignInclinations(items) {
    const container = elements.designInclinationsList;
    if (!container) return;
    container.textContent = "";
    const entries = Array.isArray(items) ? items : [];
    if (!entries.length) {
        const empty = document.createElement("p");
        empty.className = "progress-empty";
        empty.textContent = t("noDesignInclinations");
        container.appendChild(empty);
        return;
    }
    entries.forEach(item => {
        const record = document.createElement("article");
        record.className = "progress-item progress-item-inclination";
        const statement = document.createElement("p");
        statement.className = "progress-item-title progress-item-text";
        statement.textContent = String(item?.statement || "").trim();
        record.appendChild(statement);
        const evidence = document.createElement("div");
        evidence.className = "progress-evidence";
        const why = document.createElement("strong");
        why.className = "inclination-why";
        why.textContent = t("whyUnderstand");
        evidence.appendChild(why);
        const trail = document.createElement("ol");
        trail.className = "inclination-evidence";
        (Array.isArray(item?.evidenceTrail) ? item.evidenceTrail : []).forEach(evidence => {
            const entry = document.createElement("li");
            const stage = document.createElement("span");
            stage.className = "progress-evidence-stage";
            stage.textContent = `${t("stage")} ${evidence.stageNumber}`;
            const summary = document.createElement("span");
            summary.className = "progress-evidence-text";
            summary.textContent = String(evidence.text || "");
            entry.append(stage, summary);
            if (evidence?.kind === "manual_edit" && evidence?.detailedText) {
                const detail = document.createElement("details");
                detail.className = "manual-observation-detail";
                const heading = document.createElement("summary");
                heading.textContent = t("viewDetailedObservation");
                const body = document.createElement("p");
                body.textContent = String(evidence.detailedText);
                detail.append(heading, body);
                entry.appendChild(detail);
            }
            trail.appendChild(entry);
        });
        evidence.appendChild(trail);
        record.appendChild(evidence);
        const footer = document.createElement("footer");
        footer.className = "progress-item-footer progress-item-footer-confirmed";
        const meta = document.createElement("small");
        meta.className = "progress-item-meta";
        meta.textContent = t("confirmedAtStage").replace(
            "{stage}", String(item?.confirmedAtStageNumber || "")
        );
        footer.appendChild(meta);
        record.appendChild(footer);
        container.appendChild(record);
    });
}

function formatProgressTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString(state.language === "zh-CN" ? "zh-CN" : "en-US", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
    });
}

function renderMessages() {
    const previousStageId = state.renderedMessageStageId;
    const previousMessageCount = state.renderedMessageCount;
    const previousScrollTop = elements.chatScroll.scrollTop;
    const previousScrollHeight = elements.chatScroll.scrollHeight;
    const previousClientHeight = elements.chatScroll.clientHeight;
    const wasNearBottom = previousScrollHeight - previousScrollTop - previousClientHeight <= 48;
    elements.messageList.textContent = "";
    const turns = selectedStageTurns();
    const stageChanged = previousStageId !== null && previousStageId !== state.selectedVersionId;
    const initialRender = previousStageId === null;
    const hasNewTurns = turns.length > previousMessageCount;
    elements.emptyChat.hidden = turns.length > 0 || state.selectedVersionId === state.session.currentVersionId;

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
    state.renderedMessageStageId = state.selectedVersionId;
    state.renderedMessageCount = turns.length;
    requestAnimationFrame(() => {
        if (initialRender || stageChanged || (hasNewTurns && wasNearBottom)) {
            elements.chatScroll.scrollTop = elements.chatScroll.scrollHeight;
            return;
        }
        elements.chatScroll.scrollTop = Math.min(
            previousScrollTop,
            Math.max(0, elements.chatScroll.scrollHeight - elements.chatScroll.clientHeight),
        );
    });
}

function renderAssistantBubble(turn, bubble) {
    try { return renderAssistantBubbleContent(turn, bubble); }
    catch (error) {
        recordReplyDiagnostic("render", "failed", { ...turnReplyIdentity(turn), code: "RENDER_EXCEPTION" });
        throw error;
    }
}

function renderAssistantBubbleContent(turn, bubble) {
    const localized = localizedAssistantTurn(turn);
    if (localized.guidance?.proposalQuestion) {
        renderBinaryQuestion(localized.guidance.proposalQuestion, bubble, turn);
        return;
    }
    const proposal = proposalForTurn(turn.turnId);
    const guidance = guidanceForDisplay(localized.guidance || {}, proposal);
    let renderedIntent = false;
    const receivedIntent = Boolean(localized.guidance?.intentHypothesis);
    const filterCode = receivedIntent && !guidance.intentHypothesis
        ? (proposal || localized.guidance?.proposalOffer ? "ACTION_CARD_FAMILY"
            : (localized.guidance?.uiCues || []).some(cue => cue?.type === "manual_edit") ? "MANUAL_CARD_FAMILY" : "CLARIFICATION_CARD_FAMILY")
        : "";
    recordReplyDiagnostic("display_policy", filterCode ? "filtered" : "passed", {
        ...turnReplyIdentity(turn), code: filterCode, receivedCard: receivedIntent, displayCard: Boolean(guidance.intentHypothesis) });
    const question = String(guidance.followUpQuestion || "").trim();
    const uiCues = Array.isArray(guidance.uiCues) ? guidance.uiCues : [];
    const activeDisagreement = guidance.disagreement?.status === "active"
        && guidance.disagreement?.displayCard !== false
        ? guidance.disagreement
        : null;
    const bodyNode = document.createElement("div");
    bodyNode.className = "message-body";
    const body = assistantBodyWithoutCues(localized.content, uiCues, question);
    renderAssistantBody(bodyNode, body, guidance.coordinateLinks, turn);
    bubble.appendChild(bodyNode);
    const isTranslatedDisplay = Boolean(
        turn.role === "assistant"
        && turn.language
        && turn.language !== state.language
        && turn.translations?.[state.language]
    );
    if (isTranslatedDisplay) {
        const translatedLabel = document.createElement("small");
        translatedLabel.className = "translation-label";
        translatedLabel.textContent = t("translatedDisplay");
        bubble.appendChild(translatedLabel);
    }

    const cueList = document.createElement("div");
    cueList.className = "guidance-cues";

    const offer = guidance.proposalOffer;

    const presentation = offer?.proposalPresentation;
    const v2Presentation = presentation?.schemaVersion === 2;
    const proposalSummary = presentation?.summary || offer?.summary;
    const proposalRationale = presentation?.rationale || offer?.rationale || "";
    if (offer && (v2Presentation || proposalSummary)) {
        const revisionCue = createGuidanceCue(
            "revision",
            v2Presentation ? "" : proposalSummary,
            v2Presentation ? "" : proposalRationale,
        );
        if (presentation) {
            revisionCue.appendChild(
                createProposalPresentation(presentation)
            );
        }
        const proposalState = turn.proposalState || null;
        const proposalStatus = proposalState?.status || (
            isLatestRevisionOfferTurn(turn) ? "active" : "stale"
        );
        const actionable = proposalStatus === "active"
            && isLatestRevisionOfferTurn(turn)
            && proposalState?.actionable !== false;
        if (canEditSelected() && !selectedStageHasActiveDisagreement()) {
            if (proposalStatus !== "already_satisfied" && proposalStatus !== "unbound") {
                [
                    ["execute_revision", "draftSuggestedRevision"],
                    ["challenge_revision", "challengeRevision"],
                    ["alternative_revision", "alternativeRevision"]
                ].filter(([action]) => !Array.isArray(offer.availableActions) || offer.availableActions.includes(action)).forEach(([action, labelKey]) => {
                    const stale = !actionable;
                    const button = makeButton(
                        t(labelKey),
                        `secondary-button guidance-cue-button revision-action-button${stale ? " guidance-cue-button-stale" : ""}`,
                        actionable
                            ? () => sendRevisionCardAction(action, turn, offer)
                            : null,
                        {
                            disabled: stale,
                            title: actionable ? "" : proposalStateMessage(proposalStatus)
                        }
                    );
                    button.dataset.actionable = String(actionable);
                    setButtonDisabled(button, stale || state.busy || binaryFlowActive());
                    revisionCue.appendChild(button);
                });
            }
            if (!actionable && proposalStatus !== "already_satisfied" && proposalStatus !== "unbound") {
                const staleNote = document.createElement("small");
                staleNote.className = "guidance-cue-stale-note";
                staleNote.textContent = proposalStateMessage(proposalStatus);
                revisionCue.appendChild(staleNote);
            }
        }
        if (proposalStatus === "already_satisfied" || proposalStatus === "unbound") {
            const statusNote = document.createElement("small");
            statusNote.className = "guidance-cue-stale-note";
            statusNote.textContent = proposalStateMessage(proposalStatus);
            revisionCue.appendChild(statusNote);
        }
        cueList.appendChild(revisionCue);
    } else if (proposal && proposal.summary) {
        cueList.appendChild(createGuidanceCue(
            "revision",
            localizedProposalSummary(proposal)
        ));
    } else if (guidance.intentHypothesis) {
        renderedIntent = true;
        if (guidance.intentState?.interactionMode === "conflict_choice") {
            cueList.appendChild(createIntentConflictChoiceCue(
                guidance.intentHypothesis,
                guidance.intentState,
                turn,
            ));
        } else {
            cueList.appendChild(createIntentGuidanceCue(
                guidance.intentHypothesis,
                guidance.intentState,
                turn,
            ));
        }
    }

    uiCues.forEach(cue => {
        if (cue && ["manual_edit", "warning", "tradeoff", "clarification"].includes(cue.type) && cue.text) {
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
    recordReplyDiagnostic("render", "completed", { ...turnReplyIdentity(turn), renderedCard: renderedIntent,
        code: receivedIntent && !renderedIntent ? "CARD_NOT_RENDERED" : "" });
}

function proposalStateMessage(status) {
    if (status === "already_satisfied") return t("proposalAlreadySatisfied");
    if (status === "unbound") return t("proposalUnbound");
    if (status === "stale") return t("proposalStale");
    if (status === "precondition_failed") return t("proposalPreconditionFailed");
    if (status === "disagreement_active") return t("proposalDisagreementActive");
    if (status === "challenged") return t("proposalChallenged");
    return t("staleRevisionCard");
}

function selectedStageHasActiveDisagreement() {
    const canonical = state.session?.interactionState;
    if (state.selectedVersionId === state.session?.currentVersionId
        && canonical?.versionId === state.selectedVersionId) {
        return Boolean(canonical.proposalBlocked);
    }
    let latestStatus = null;
    selectedStageTurns().forEach(turn => {
        if (turn.role !== "assistant") return;
        const disagreement = turn.guidance?.disagreement;
        if (disagreement && typeof disagreement === "object") {
            latestStatus = disagreement.status || null;
        }
    });
    return latestStatus === "active";
}

function selectedStageHasPendingIntentConflict() {
    return selectedStageTurns().some(turn => (
        turn.role === "assistant"
        && turn.guidance?.intentState?.interactionMode === "conflict_choice"
        && turn.guidance.intentState.actionable === true
    ));
}

function isRevisionOfferTurn(turn) {
    const offer = turn?.guidance?.proposalOffer;
    return Boolean(turn?.role === "assistant"
        && offer
        && typeof offer === "object"
        && (String(offer.summary || "").trim()
            || (offer.proposalPresentation && typeof offer.proposalPresentation === "object")));
}

function latestRevisionOfferTurn() {
    return selectedStageTurns().slice().reverse().find(isRevisionOfferTurn) || null;
}

function isLatestRevisionOfferTurnImpl(turn) {
    return latestRevisionOfferTurn()?.turnId === turn?.turnId;
}

function guidanceForDisplay(source, proposal = null) {
    const guidance = { ...source };
    const cues = (Array.isArray(source.uiCues) ? source.uiCues : [])
        .filter(Boolean)
        .filter(cue => !isInternalProposalFailureCue(cue));
    const warning = cues.find(cue => ["warning", "tradeoff"].includes(cue.type) && cue.text);
    let manual = cues.find(cue => cue.type === "manual_edit" && cue.text);
    const clarification = cues.find(cue => cue.type === "clarification" && cue.text);

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
    if (clarification) {
        guidance.intentHypothesis = null;
        guidance.followUpQuestion = null;
        guidance.uiCues = [clarification, warning].filter(Boolean);
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
    label.textContent = GUIDANCE_CUE_LABELS[state.language].question;
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
    label.textContent = GUIDANCE_CUE_LABELS[state.language].question;
    cue.appendChild(label);

    const fields = [
        ["discussionUser", disagreement.userPosition],
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
    if (disagreement.subject === "ai_revision_challenge" && disagreement.phase === "choice_pending") {
        const actions = document.createElement("div");
        actions.className = "proposal-actions";
        [["ai", state.language === "zh-CN" ? "是" : "Yes"],
         ["user", state.language === "zh-CN" ? "否" : "No"]].forEach(([choice, label]) => {
            const button = makeButton(
                label,
                "secondary-button guidance-cue-button challenge-choice-button",
                () => sendChallengeChoice(disagreement.challengeId, choice),
            );
            button.dataset.challengeId = disagreement.challengeId || "";
            setButtonDisabled(button, !canUseChallengeChoice(disagreement.challengeId));
            actions.appendChild(button);
        });
        cue.appendChild(actions);
    }
    return cue;
}

function canUseChallengeChoice(challengeId) {
    const active = activeChallengeComposerState();
    return Boolean(challengeId && canEditSelected() && !state.busy
        && active?.status === "choice_pending"
        && active.challengeId === challengeId);
}

function sendChallengeChoice(challengeId, choice) {
    if (!canUseChallengeChoice(challengeId)) return;
    const content = state.language === "zh-CN"
        ? (choice === "ai" ? "是" : "否")
        : (choice === "ai" ? "Yes" : "No");
    state.pendingMessage = {
        content,
        baseVersionId: state.session.currentVersionId,
        idempotencyKey: uniqueId("challenge-choice"),
        requestProposal: false,
        action: "continue_challenge",
        challengeId,
        challengeChoice: choice
    };
    persistPendingMessage();
    void submitPendingMessage();
}

function renderAssistantBodyImpl(node, body, links, turn) {
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
        const button = document.createElement("span");
        button.className = "coordinate-link";
        button.textContent = candidate.text;
        button.dataset.coordinateLinkIndex = String(candidate.linkIndex);
        button.setAttribute("role", "button");
        button.setAttribute("tabindex", "0");
        button.setAttribute(
            "aria-label",
            t("routeAriaLabel").replace("{fromRow}", candidate.link.from.row).replace("{fromColumn}", candidate.link.from.column).replace("{toRow}", candidate.link.to.row).replace("{toColumn}", candidate.link.to.column)
        );
        button.addEventListener("click", () => toggleCoordinateLink(turn, candidate.linkIndex, candidate.link));
        button.addEventListener("keydown", event => {
            if (event.key !== "Enter" && event.key !== " ") return;
            event.preventDefault();
            toggleCoordinateLink(turn, candidate.linkIndex, candidate.link);
        });
        node.appendChild(button);
        cursor = candidate.end;
    });
    if (cursor < text.length) node.appendChild(document.createTextNode(text.slice(cursor)));
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

function proposalForTurnImpl(turnId) {
    return state.session.proposals.find(proposal => proposal.assistantTurnId === turnId);
}

function createGuidanceCue(type, text, detail = "") {
    const cue = document.createElement("section");
    cue.className = `guidance-cue guidance-cue-${type.replace("_", "-")}`;
    const label = document.createElement("span");
    label.className = "guidance-cue-label";
    label.textContent = GUIDANCE_CUE_LABELS[state.language]?.[type]
        || GUIDANCE_CUE_LABELS.en[type];
    cue.appendChild(label);
    if (text) {
        const message = document.createElement("strong");
        message.textContent = text;
        cue.appendChild(message);
    }

    if (detail) {
        const rationale = document.createElement("p");
        rationale.textContent = detail;
        cue.appendChild(rationale);
    }

    return cue;
}

function createIntentGuidanceCue(text, intentState, turn) {
    const cue = createGuidanceCue("intent", text);
    const cardState = intentState && typeof intentState === "object"
        ? intentState
        : { status: "tentative", interactionMode: "resolved", actionable: false };
    cue.classList.add("intent-feedback-card");
    cue.setAttribute("aria-busy", "false");

    if (!cardState.actionable || !canEditSelected()) {
        const status = document.createElement("small");
        status.className = "guidance-cue-stale-note intent-resolution";
        if (cardState.status === "confirmed") {
            status.textContent = cardState.resolvedStatement
                && String(cardState.resolvedStatement).trim() !== String(text).trim()
                ? t("intentRevised")
                : t("intentConfirmed");
        } else if (cardState.status === "rejected") {
            status.textContent = t("intentRejected");
        } else if (cardState.status === "superseded") {
            status.textContent = t("intentSuperseded");
        } else {
            status.textContent = t("intentStale");
        }
        cue.appendChild(status);
        return cue;
    }

    const actions = document.createElement("div");
    actions.className = "intent-card-actions";
    const openEditor = () => {
        if (state.busy || cue.querySelector(".intent-edit-form")) return;
        actions.hidden = true;
        const form = document.createElement("form");
        form.className = "intent-edit-form";
        const label = document.createElement("label");
        label.className = "sr-only";
        label.textContent = t("intentModify");
        const input = document.createElement("textarea");
        input.rows = 3;
        input.maxLength = 1200;
        input.value = String(text || "");
        label.htmlFor = `intent-edit-${turn.turnId}`;
        input.id = label.htmlFor;
        const counter = document.createElement("small");
        counter.className = "intent-character-count";
        const updateCount = () => {
            counter.textContent = t("intentCharacters").replace(
                "{count}", String(input.value.length)
            );
        };
        input.addEventListener("input", updateCount);
        updateCount();
        const editActions = document.createElement("div");
        editActions.className = "intent-edit-actions";
        editActions.append(
            makeButton(t("intentSave"), "secondary-button guidance-cue-button", null),
            makeButton(t("intentCancel"), "secondary-button guidance-cue-button", () => {
                form.remove();
                actions.hidden = false;
            })
        );
        const saveButton = editActions.firstElementChild;
        form.addEventListener("submit", event => {
            event.preventDefault();
            const candidate = input.value.trim();
            if (candidate.length < 4) {
                input.focus();
                return;
            }
            void submitIntentFeedback("revise", turn, cardState, candidate, cue);
        });
        saveButton.addEventListener("click", event => {
            event.preventDefault();
            form.requestSubmit();
        });
        form.append(label, input, counter, editActions);
        cue.appendChild(form);
        input.focus();
        input.select();
    };

    if (cardState.interactionMode === "full") {
        actions.append(
            makeButton(t("intentMatches"), "secondary-button guidance-cue-button", () => {
                void submitIntentFeedback("confirm", turn, cardState, null, cue);
            }),
            makeButton(t("intentReject"), "secondary-button guidance-cue-button", () => {
                void submitIntentFeedback("reject", turn, cardState, null, cue);
            }),
            makeButton(t("intentAdjust"), "secondary-button guidance-cue-button", openEditor),
        );
    } else {
        actions.appendChild(makeButton(
            t("intentModify"),
            "secondary-button guidance-cue-button",
            openEditor,
        ));
    }
    cue.appendChild(actions);
    return cue;
}

function createIntentConflictChoiceCue(text, intentState, turn) {
    const choice = intentState?.conflictChoice;
    const wrapper = document.createElement("section");
    wrapper.className = "intent-conflict-choice";
    if (!choice || !Array.isArray(choice.options) || choice.options.length !== 2) {
        wrapper.appendChild(createIntentGuidanceCue(text, intentState, turn));
        return wrapper;
    }

    choice.options.forEach(option => {
        const statement = String(
            option.role === "new"
                ? (text || option.statement || "")
                : (option.statement || "")
        ).trim();
        const card = createGuidanceCue("intent", statement);
        card.classList.add("intent-feedback-card", "intent-conflict-option");
        card.setAttribute("aria-busy", "false");

        const role = document.createElement("small");
        role.className = "intent-conflict-option-role";
        role.textContent = option.role === "new"
            ? t("intentNewOption")
            : t("intentExistingOption");
        card.insertBefore(role, card.querySelector("strong"));

        const evidenceText = String(option.evidenceText || "").trim();
        if (evidenceText) {
            const evidence = document.createElement("small");
            evidence.className = "intent-conflict-evidence";
            evidence.textContent = `${t("intentEvidence")}：“${evidenceText}”`;
            const actionsAnchor = card.querySelector(".guidance-cue-text");
            if (actionsAnchor) {
                actionsAnchor.insertAdjacentElement("afterend", evidence);
            } else {
                card.appendChild(evidence);
            }
        }

        if (intentState.actionable && canEditSelected()) {
            const actions = document.createElement("div");
            actions.className = "intent-card-actions";
            actions.appendChild(makeButton(
                t("intentKeepThis"),
                "secondary-button guidance-cue-button",
                () => void submitIntentFeedback(
                    "keep",
                    turn,
                    intentState,
                    null,
                    card,
                    option.hypothesisId,
                ),
            ));
            card.appendChild(actions);
        } else {
            const status = document.createElement("small");
            status.className = "guidance-cue-stale-note intent-resolution";
            if (choice.selectedHypothesisId === option.hypothesisId) {
                status.textContent = t("intentKept");
            } else if (option.status === "superseded") {
                status.textContent = t("intentSuperseded");
            } else if (option.status === "rejected") {
                status.textContent = t("intentRejected");
            } else {
                status.textContent = t("intentStale");
            }
            card.appendChild(status);
        }
        wrapper.appendChild(card);
    });
    return wrapper;
}

async function submitIntentFeedback(
    action,
    turn,
    intentState,
    candidateText,
    cue,
    selectedHypothesisId = null,
) {
    if (state.busy || !canEditSelected() || !intentState?.actionable) return;
    const baseVersionId = state.session.currentVersionId;
    const idempotencyKey = uniqueId(`intent-${action}`);
    cue.setAttribute("aria-busy", "true");
    cue.querySelectorAll("button, textarea").forEach(control => {
        control.disabled = true;
    });
    await withBusy(async () => {
        const result = await api(
            `/api/sessions/${state.sessionId}/intent-hypotheses/${intentState.hypothesisId}/feedback`,
            {
                method: "POST",
                body: {
                    action,
                    candidateText: action === "revise" ? candidateText : null,
                    selectedHypothesisId: action === "keep" ? selectedHypothesisId : null,
                    sourceTurnId: turn.turnId,
                    baseVersionId,
                    idempotencyKey,
                },
                timeoutMs: LLM_REQUEST_TIMEOUT_MS,
            },
        );
        state.session = result.session;
        selectVersion(state.session.currentVersionId, false, { preserveSameStageDraft: true });
        render();
    }, () => {
        cue.setAttribute("aria-busy", "false");
        cue.querySelectorAll("button, textarea").forEach(control => {
            control.disabled = false;
        });
    });
}

function proposalPresentationTileLabel(tile) {
    const label = t(tileName(tile));
    return `${label} (${tile})`;
}

function proposalPreservedLabel(value) {
    const labels = {
        outer_shell: state.language === "zh-CN" ? "外壳" : "outer shell",
        unrelated_areas: state.language === "zh-CN" ? "其他区域" : "unrelated areas",
        player: t("player"),
        boxes: t("box"),
        targets: t("target"),
        water: t("water"),
        walls: state.language === "zh-CN" ? "墙体" : "walls"
    };
    return labels[value] || String(value || "");
}

function createProposalPresentation(presentation) {
    if (presentation?.schemaVersion === 2) {
        return createProposalPresentationV2(presentation);
    }
    const section = document.createElement("div");
    section.className = "proposal-plan-details";

    const appendTextBlock = (labelKey, value) => {
        const text = String(value || "").trim();
        if (!text) return;
        const block = document.createElement("p");
        block.className = "proposal-plan-contract-item";
        const label = document.createElement("strong");
        label.textContent = `${t(labelKey)}: `;
        block.append(label, document.createTextNode(text));
        section.appendChild(block);
    };
    const appendListBlock = (labelKey, values) => {
        const items = Array.isArray(values) ? values.map(value => String(value || "").trim()).filter(Boolean) : [];
        if (!items.length) return;
        const heading = document.createElement("h4");
        heading.textContent = t(labelKey);
        section.appendChild(heading);
        const list = document.createElement("ul");
        items.forEach(value => {
            const item = document.createElement("li");
            item.textContent = value;
            list.appendChild(item);
        });
        section.appendChild(list);
    };

    appendTextBlock("proposalObjective", presentation?.objective);
    appendListBlock("proposalMustSatisfy", presentation?.mustSatisfy);
    appendListBlock("proposalTryToAchieve", presentation?.tryToAchieve);

    const title = document.createElement("h4");
    title.textContent = presentation?.objective
        ? t("proposalActualChanges")
        : t("proposalConcreteChanges");
    section.appendChild(title);

    const changes = Array.isArray(presentation?.changes)
        ? presentation.changes
        : [];
    if (changes.length) {
        const list = document.createElement("ul");
        changes.forEach(change => {
            if (!Number.isInteger(change?.row) || !Number.isInteger(change?.column)) return;
            const item = document.createElement("li");
            item.className = "proposal-plan-detail-item";
            const before = proposalPresentationTileLabel(change.before);
            const after = proposalPresentationTileLabel(change.after);
            item.textContent = `(${change.row}, ${change.column})：${t("proposalBefore")} ${before} → ${t("proposalAfter")} ${after}`;
            list.appendChild(item);
        });
        if (list.childElementCount) section.appendChild(list);
    }

    const preserved = Array.isArray(presentation?.preserved)
        ? presentation.preserved
            .map(proposalPreservedLabel)
            .filter(Boolean)
        : [];
    if (preserved.length) {
        const preservedText = document.createElement("p");
        preservedText.className = "proposal-plan-preserved";
        preservedText.textContent = `${t("proposalPreserved")}：${preserved.join("、")}`;
        section.appendChild(preservedText);
    }
    return section;
}

function createProposalPresentationV2(presentation) {
    const container = document.createElement("div");
    container.className = "proposal-plan-details proposal-plan-details-v2";
    if (presentation.binaryCompact) {
        const title = document.createElement("strong");
        title.textContent = state.language === "zh-CN" ? "局部调整方案" : "Local revision plan";
        const summary = document.createElement("p");
        const groups = new Map();
        (presentation.changes || []).forEach(change => {
            const label = `${proposalPresentationTileLabel(change.before)} → ${proposalPresentationTileLabel(change.after)}`;
            groups.set(label, (groups.get(label) || 0) + 1);
        });
        summary.textContent = [...groups].map(([label, count]) => `${label}: ${count}${state.language === "zh-CN" ? "格" : " tiles"}`).join("; ");
        const actions = document.createElement("p");
        actions.textContent = (state.language === "zh-CN" ? "回应你的选择：" : "Your selected actions: ")
            + (presentation.verifiedActions || []).join(" / ");
        const verified = document.createElement("p");
        verified.textContent = state.language === "zh-CN"
            ? "修改权限、所选动作与可解性已验证；实际体验等你试玩。"
            : "Permissions, selected actions and solvability verified; experience needs playtesting.";
        const details = document.createElement("details");
        const caption = document.createElement("summary");
        caption.textContent = state.language === "zh-CN" ? "逐格变化与选择记录" : "Tile changes and choice history";
        details.append(caption, createProposalPresentationV2({ ...presentation, binaryCompact: false }));
        container.append(title, summary);
        if ((presentation.verifiedActions || []).length) container.appendChild(actions);
        if (presentation.reason) {
            const limits = document.createElement("p");
            limits.textContent = presentation.reason;
            container.appendChild(limits);
        }
        container.append(verified, details);
        return container;
    }

    const appendSection = (labelKey, values) => {
        const items = (Array.isArray(values) ? values : [values])
            .map(value => String(value ?? "").trim())
            .filter(Boolean);
        if (!items.length) return;
        const section = document.createElement("section");
        section.className = "proposal-v2-section";
        const title = document.createElement("strong");
        title.className = "proposal-v2-title";
        title.textContent = `${t(labelKey)}${state.language === "zh-CN" ? "：" : ":"}`;
        section.appendChild(title);
        const list = document.createElement("ul");
        list.className = "proposal-v2-list";
        items.forEach(value => {
            const item = document.createElement("li");
            item.className = "proposal-v2-item";
            item.textContent = value;
            list.appendChild(item);
        });
        section.appendChild(list);
        container.appendChild(section);
    };

    appendSection("proposalRequestText", presentation.requestText);
    appendSection("proposalAnswerTexts", presentation.answerTexts);

    const changes = (Array.isArray(presentation.changes) ? presentation.changes : [])
        .filter(change => Number.isInteger(change?.row) && Number.isInteger(change?.column))
        .map(change => {
            const before = proposalPresentationTileLabel(change.before);
            const after = proposalPresentationTileLabel(change.after);
            return `(${change.row}, ${change.column})：${before} → ${after}`;
        });
    appendSection("proposalChangeList", changes);
    appendSection("proposalReason", presentation.reason);

    const solution = presentation.solution || {};
    if (
        Number.isInteger(solution.minimumMoves)
        && Number.isInteger(solution.pushesOnMinimumMoveSolution)
    ) {
        const text = state.language === "zh-CN"
            ? `最少移动 ${solution.minimumMoves} 步，其中推动 ${solution.pushesOnMinimumMoveSolution} 次。`
            : `Minimum ${solution.minimumMoves} moves, including ${solution.pushesOnMinimumMoveSolution} pushes.`;
        appendSection("proposalSolution", text);
    }
    return container;
}

function prefillProposalConsent(offer) {
    // Legacy helper kept for integrations that call it directly. Purple-card
    // Historical callback shape was: () => prefillProposalConsent(offer)
    // clicks now go through sendRevisionCardAction so the server receives the
    // explicit execute_revision action and source turn.
    const summary = String(offer.summary || "").trim();
    elements.messageInput.value = REQUEST_COPY[state.language].proposalConsent.replace("{summary}", summary);
    handleComposerInput();
    elements.chatForm.requestSubmit();
}

function sendRevisionCardAction(action, turn, offer) {
    if (!canEditSelected() || state.busy || binaryFlowActive() || selectedStageHasActiveDisagreement()) return;
    if (turn?.proposalState && turn.proposalState.actionable === false) {
        showReplyNotice(proposalStateMessage(turn.proposalState.status));
        render();
        return;
    }
    if (!isLatestRevisionOfferTurn(turn)) {
        showReplyNotice(t("staleRevisionCard"));
        render();
        return;
    }
    const content = action === "execute_revision"
        ? REQUEST_COPY[state.language].executeBoundProposal
        : action === "challenge_revision"
            ? REQUEST_COPY[state.language].challengeRevision
            : REQUEST_COPY[state.language].alternativeRevision;
    state.pendingMessage = {
        content,
        baseVersionId: state.session.currentVersionId,
        idempotencyKey: uniqueId(`card-${action}`),
        requestProposal: false,
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
    const changes = document.createElement("ul");
    changes.className = "proposal-plan-detail-list";
    (proposal.diff || []).forEach(change => {
        const item = document.createElement("li");
        item.className = "proposal-plan-detail-item";
        const row = change.y + 1;
        const column = change.x + 1;
        item.textContent = `(${row}, ${column})：${proposalPresentationTileLabel(change.before)} → ${proposalPresentationTileLabel(change.after)}`;
        changes.appendChild(item);
    });
    card.appendChild(changes);
    card.appendChild(createMiniMap(proposal.proposedRows, proposal.diff, "proposal-map"));
    const actions = document.createElement("div");
    actions.className = "proposal-actions";
    const reject = makeButton(t("reject"), "secondary-button", () => decideProposal(proposal, "reject"));
    const accept = makeButton(t("accept"), "primary-button", () => decideProposal(proposal, "accept"));
    actions.append(reject, accept);
    card.appendChild(actions);
    elements.proposalArea.appendChild(card);
}

function proposalFlowActive() {
    return Boolean(state.session?.proposalFlowState?.active);
}

function binaryFlowActive() {
    return state.session?.proposalFlowState?.interactionMode === "binary_choice" && proposalFlowActive();
}

function canChooseBinary(questionId) {
    const flow = state.session?.proposalFlowState;
    return binaryFlowActive() && flow.status === "clarifying" && !state.busy && !state.unityPlayActive
        && !state.dirty && !deadlineExpired() && canEditSelected()
        && state.selectedVersionId === state.session.currentVersionId
        && flow.pendingQuestion?.questionId === questionId && !flow.pendingQuestion.selectedChoiceId;
}

function renderBinaryQuestion(question, bubble, turn) {
    const heading = document.createElement("p");
    heading.className = "binary-question";
    heading.textContent = question.question;
    bubble.appendChild(heading);
    if (question.note) {
        const note = document.createElement("p");
        note.className = "binary-note";
        note.textContent = question.note;
        bubble.appendChild(note);
    }
    const choices = document.createElement("div");
    choices.className = "binary-choices";
    question.choices.forEach(choice => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary-button binary-choice-button";
        button.dataset.questionId = question.questionId;
        button.dataset.questionStatus = question.status || "";
        button.textContent = `${choice.letter}: ${choice.label}`;
        const selected = question.selectedChoiceId === choice.choiceId;
        button.classList.toggle("is-selected", selected);
        button.setAttribute("aria-pressed", String(selected));
        button.disabled = !canChooseBinary(question.questionId) || question.status !== "pending";
        button.addEventListener("click", () => {
            if (!canChooseBinary(question.questionId) || question.status !== "pending") return;
            const payload = { baseVersionId: question.baseVersionId, topicId: question.topicId,
                questionId: question.questionId, choiceId: choice.choiceId,
                displayLanguage: question.sourceLanguage || turn.language,
                idempotencyKey: uniqueId("binary-choice") };
            submitBinaryAction({ kind: "choice", ...payload });
        });
        choices.appendChild(button);
    });
    bubble.appendChild(choices);
}

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

function localizedAssistantTurnImpl(turn) {
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
                timeoutMs: LLM_REQUEST_TIMEOUT_MS
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
            const key = `${turnId}:${targetLanguage}`;
            state.translationFailures.delete(key);
            state.translationRetryAttempts.delete(key);
        });
    } catch (error) {
        let remaining = error.details?.pendingTurnIds || turnIds;
        try {
            const latest = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}`);
            const byId = new Map(latest.turns.map(turn => [turn.turnId, turn]));
            state.session.turns = state.session.turns.map(turn => {
                const updated = byId.get(turn.turnId);
                return updated ? { ...turn, translations: updated.translations } : turn;
            });
            remaining = remaining.filter(turnId => !byId.get(turnId)?.translations?.[targetLanguage]);
        } catch (_refreshError) { /* Keep source text and the original retry target. */ }
        let shouldScheduleRetry = false;
        turnIds.forEach(turnId => {
            const key = `${turnId}:${targetLanguage}`;
            if (remaining.includes(turnId)) {
                state.translationFailures.add(key);
                const nextAttempt = (state.translationRetryAttempts.get(key) || 0) + 1;
                state.translationRetryAttempts.set(key, nextAttempt);
                if (nextAttempt < 3) {
                    shouldScheduleRetry = true;
                }
            } else {
                state.translationFailures.delete(key);
                state.translationRetryAttempts.delete(key);
            }
        });
        if (shouldScheduleRetry && state.translationRetryTimerId === null) {
            state.translationRetryTimerId = setTimeout(() => {
                state.translationRetryTimerId = null;
                ensureVisibleTranslations();
            }, 5000);
        }
    } finally {
        state.translating.delete(requestKey);
        state.translationRemainingCount = Math.max(0, state.translationRemainingCount - batch.length);
        render();
    }
}

async function ensureVisibleTranslationsImpl() {
    if (!state.session) return;
    if (state.translationInProgress) {
        state.translationPendingRerun = true;
        return;
    }
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

    try {
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
    } finally {
        state.translationInProgress = false;
        state.translationRemainingCount = 0;
        const shouldRerun = state.translationPendingRerun;
        state.translationPendingRerun = false;
        render();
        if (shouldRerun) {
            void ensureVisibleTranslations();
        }
    }
}

    return {
        renderProgressContext,
        renderQuestionRecords,
        submitQuestionFeedback,
        updateProgressPanelMaxHeight,
        renderProgressItems,
        renderDesignInclinations,
        formatProgressTime,
        renderMessages,
        renderAssistantBubble,
        renderAssistantBubbleContent,
        proposalStateMessage,
        selectedStageHasActiveDisagreement,
        selectedStageHasPendingIntentConflict,
        isRevisionOfferTurn,
        latestRevisionOfferTurn,
        isLatestRevisionOfferTurn: isLatestRevisionOfferTurnImpl,
        guidanceForDisplay,
        isInternalProposalFailureCue,
        proposalCompanionManualText,
        createDiscussionFocus,
        createDisagreementCard,
        canUseChallengeChoice,
        sendChallengeChoice,
        assistantBodyWithoutCues: defaultAssistantBodyWithoutCues,
        renderAssistantBody: renderAssistantBodyImpl,
        formatCoordinatePoint,
        normalizedCoordinatePoint,
        findCoordinateRoute,
        toggleCoordinateLink,
        proposalForTurn: proposalForTurnImpl,
        createGuidanceCue,
        createIntentGuidanceCue,
        createIntentConflictChoiceCue,
        submitIntentFeedback,
        proposalPresentationTileLabel,
        proposalPreservedLabel,
        createProposalPresentation,
        createProposalPresentationV2,
        prefillProposalConsent,
        sendRevisionCardAction,
        renderProposal,
        proposalFlowActive,
        binaryFlowActive,
        canChooseBinary,
        renderBinaryQuestion,
        selectedStageTurns,
        localizedAssistantTurn: localizedAssistantTurnImpl,
        localizedProposalSummary,
        verifiedProposalMessage,
        verifiedProposalSummary,
        translateVisibleBatch,
        ensureVisibleTranslations: ensureVisibleTranslationsImpl,
    };
}

