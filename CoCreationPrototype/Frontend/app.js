"use strict";

const MAX_MESSAGES = 20;
const MAX_MESSAGE_LENGTH = 2000;

const state = {
    messages: [],
    rows: [],
    busy: false,
    awaitingAssistant: false,
    requestController: null,
};

const elements = {
    chatForm: document.getElementById("chatForm"),
    messageInput: document.getElementById("messageInput"),
    sendButton: document.getElementById("sendButton"),
    restartButton: document.getElementById("restartButton"),
    retryButton: document.getElementById("retryButton"),
    notice: document.getElementById("notice"),
    noticeMessage: document.getElementById("noticeMessage"),
    messageList: document.getElementById("messageList"),
    emptyChat: document.getElementById("emptyChat"),
    typingRow: document.getElementById("typingRow"),
    chatScroll: document.getElementById("chatScroll"),
    mapGrid: document.getElementById("mapGrid"),
    mapLegend: document.getElementById("mapLegend"),
    characterCount: document.getElementById("characterCount"),
    prototypeStatus: document.getElementById("prototypeStatus"),
};

elements.chatForm.addEventListener("submit", handleSubmit);
elements.restartButton.addEventListener("click", restartChat);
elements.retryButton.addEventListener("click", retryLastRequest);
elements.messageInput.addEventListener("input", updateCharacterCount);
elements.messageInput.addEventListener("keydown", handleComposerKeydown);

loadSample();
updateCharacterCount();

async function loadSample() {
    setPrototypeStatus("Loading map...", "pending");

    try {
        const response = await fetch("/api/sample", { cache: "no-store" });
        const payload = await readJson(response);

        if (!response.ok) {
            throw createApiError(payload, response.status);
        }

        state.rows = Array.isArray(payload.rows) ? payload.rows : [];
        renderMap(state.rows);
        renderLegend(payload.legend || {});
        setPrototypeStatus("Local prototype", "ready");
        elements.messageInput.focus();
    } catch (error) {
        setPrototypeStatus("Map unavailable", "error");
        showNotice(error.message || "The sample map could not be loaded.", false);
    }
}

async function handleSubmit(event) {
    event.preventDefault();

    if (state.busy || state.awaitingAssistant) {
        return;
    }

    const content = elements.messageInput.value.trim();

    if (!content) {
        elements.messageInput.focus();
        return;
    }

    if (state.messages.length >= MAX_MESSAGES - 1) {
        showNotice("This test chat has reached its 20-message limit. Restart the chat to continue.", false);
        return;
    }

    state.messages.push({ role: "user", content });
    state.awaitingAssistant = true;
    elements.messageInput.value = "";
    updateCharacterCount();
    renderMessages();
    hideNotice();
    await requestAssistantReply();
}

async function requestAssistantReply() {
    if (state.busy || !state.awaitingAssistant) {
        return;
    }

    setBusy(true);
    state.requestController = new AbortController();

    try {
        const response = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ messages: state.messages }),
            signal: state.requestController.signal,
        });
        const payload = await readJson(response);

        if (!response.ok) {
            throw createApiError(payload, response.status);
        }

        if (!payload.assistantMessage) {
            throw new Error("The assistant returned an empty message.");
        }

        state.messages.push({
            role: "assistant",
            content: String(payload.assistantMessage),
        });
        state.awaitingAssistant = false;
        hideNotice();
        renderMessages();
    } catch (error) {
        if (error.name !== "AbortError") {
            showNotice(error.message || "The assistant could not respond.", true);
        }
    } finally {
        state.requestController = null;
        setBusy(false);
    }
}

function retryLastRequest() {
    hideNotice();
    requestAssistantReply();
}

function restartChat() {
    if (state.requestController) {
        state.requestController.abort();
    }

    state.messages = [];
    state.awaitingAssistant = false;
    hideNotice();
    setBusy(false);
    renderMessages();
    elements.messageInput.value = "";
    updateCharacterCount();
    elements.messageInput.focus();
}

function renderMessages() {
    elements.messageList.textContent = "";
    elements.emptyChat.hidden = state.messages.length > 0;

    state.messages.forEach(message => {
        const row = document.createElement("div");
        row.className = "message-row " + message.role;

        if (message.role === "assistant") {
            const avatar = document.createElement("div");
            avatar.className = "message-avatar";
            avatar.textContent = "AI";
            avatar.setAttribute("aria-hidden", "true");
            row.appendChild(avatar);
        }

        const bubble = document.createElement("div");
        bubble.className = "message-bubble";
        bubble.textContent = message.content;
        row.appendChild(bubble);
        elements.messageList.appendChild(row);
    });

    scrollChatToBottom();
}

function renderMap(rows) {
    elements.mapGrid.textContent = "";

    if (!rows.length) {
        elements.mapGrid.textContent = "No map available.";
        elements.mapGrid.style.gridTemplateColumns = "";
        return;
    }

    const width = rows.reduce((maximum, row) => Math.max(maximum, String(row).length), 0);
    elements.mapGrid.style.gridTemplateColumns = "repeat(" + width + ", var(--tile-size))";

    rows.forEach(rowText => {
        const row = String(rowText);

        for (let index = 0; index < width; index += 1) {
            const tile = row[index] || " ";
            const cell = document.createElement("div");
            cell.className = "tile " + getTileClass(tile);
            cell.title = getTileName(tile);
            cell.textContent = getTileLabel(tile);
            elements.mapGrid.appendChild(cell);
        }
    });

    elements.mapGrid.setAttribute("aria-label", "Fixed 12 by 10 Sokoban map");
}

function renderLegend(legend) {
    elements.mapLegend.textContent = "";
    const orderedTiles = [".", "#", "@", "p", "s", "t"];

    orderedTiles.forEach(tile => {
        if (!legend[tile]) {
            return;
        }

        const item = document.createElement("span");
        item.className = "legend-item";

        const swatch = document.createElement("i");
        swatch.className = "swatch " + getTileClass(tile);
        swatch.setAttribute("aria-hidden", "true");

        const label = document.createElement("span");
        label.textContent = legend[tile];

        item.appendChild(swatch);
        item.appendChild(label);
        elements.mapLegend.appendChild(item);
    });
}

function setBusy(busy) {
    state.busy = busy;
    elements.sendButton.disabled = busy || state.awaitingAssistant;
    elements.messageInput.disabled = busy || state.awaitingAssistant;
    elements.typingRow.hidden = !busy;
    elements.sendButton.textContent = busy ? "Sending..." : "Send";

    if (busy) {
        scrollChatToBottom();
    } else if (!state.awaitingAssistant) {
        elements.messageInput.focus();
    }
}

function showNotice(message, retryable) {
    elements.noticeMessage.textContent = message;
    elements.retryButton.hidden = !retryable;
    elements.notice.classList.add("visible");
}

function hideNotice() {
    elements.notice.classList.remove("visible");
    elements.retryButton.hidden = true;
    elements.noticeMessage.textContent = "";
}

function setPrototypeStatus(label, stateName) {
    elements.prototypeStatus.className = "prototype-status " + stateName;
    elements.prototypeStatus.querySelector("span:last-child").textContent = label;
}

function updateCharacterCount() {
    const count = elements.messageInput.value.length;
    elements.characterCount.textContent = count + " / " + MAX_MESSAGE_LENGTH;
}

function handleComposerKeydown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        elements.chatForm.requestSubmit();
    }
}

function scrollChatToBottom() {
    requestAnimationFrame(() => {
        elements.chatScroll.scrollTop = elements.chatScroll.scrollHeight;
    });
}

async function readJson(response) {
    try {
        return await response.json();
    } catch (error) {
        return {};
    }
}

function createApiError(payload, status) {
    const error = new Error(payload.message || "Request failed with HTTP " + status + ".");
    error.code = payload.code || "REQUEST_FAILED";
    error.requestId = payload.requestId || "";
    error.retryable = Boolean(payload.retryable);
    return error;
}

function getTileClass(tile) {
    if (tile === "#") return "tile-wall";
    if (tile === "@") return "tile-water";
    if (tile === "p") return "tile-player";
    if (tile === "s") return "tile-box";
    if (tile === "t") return "tile-target";
    if (tile === ".") return "tile-floor";
    return "tile-empty";
}

function getTileName(tile) {
    return ({ "#": "wall", "@": "water", p: "player", s: "box", t: "target", ".": "floor" })[tile] || "empty";
}

function getTileLabel(tile) {
    return ({ p: "P", s: "B", t: "T", "@": "~" })[tile] || "";
}

