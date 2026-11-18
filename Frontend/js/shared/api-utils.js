const DEFAULT_API_BASE = "https://sokobanaidemo.top";

function isLoopbackHost(hostname) {
    const normalized = String(hostname || "").trim().toLowerCase();
    return normalized === "localhost"
        || normalized === "127.0.0.1"
        || normalized === "::1"
        || normalized === "[::1]";
}

function resolveApiBase() {
    const queryApi = new URLSearchParams(window.location.search).get("api");

    if (queryApi) {
        try {
            const baseHref = (window.location.protocol === "http:" || window.location.protocol === "https:")
                ? window.location.origin
                : DEFAULT_API_BASE;
            const parsed = new URL(queryApi, baseHref);

            if (parsed.protocol === "http:" || parsed.protocol === "https:") {
                const sameOrigin = (window.location.protocol === "http:" || window.location.protocol === "https:")
                    && parsed.origin === window.location.origin;
                const localContext = window.location.protocol === "file:"
                    || isLoopbackHost(window.location.hostname);

                if (sameOrigin || (localContext && isLoopbackHost(parsed.hostname))) {
                    return (parsed.origin + parsed.pathname).replace(/\/+$/, "");
                }
            }
        } catch (error) {
            // Ignore invalid ?api= query overrides and fall back safely.
        }
    }

    if (window.location.protocol === "http:" || window.location.protocol === "https:") {
        return window.location.origin;
    }

    return DEFAULT_API_BASE;
}

function apiUrl(path) {
    return state.apiBase + path;
}

function renderMap(container, rows, mini) {
    container.textContent = "";

    if (!rows.length) {
        container.textContent = "No map rows recorded.";
        container.style.gridTemplateColumns = "";
        return;
    }

    const width = rows.reduce((max, row) => Math.max(max, String(row || "").length), 0);
    container.style.gridTemplateColumns = "repeat(" + width + ", " + (mini ? "18px" : "var(--tile-size)") + ")";

    rows.forEach(rowText => {
        const row = String(rowText || "");

        for (let index = 0; index < width; index += 1) {
            const tile = row[index] || " ";
            const cell = document.createElement("div");
            cell.className = "tile " + getTileClass(tile);
            cell.title = getTileName(tile);
            cell.textContent = getTileLabel(tile);
            container.appendChild(cell);
        }
    });
}

function openDeleteDialog(config) {
    state.pendingDelete = config;
    elements.deleteDialogTitle.textContent = config.title;
    elements.deleteDialogDescription.textContent = config.description;
    elements.deleteDialogScope.textContent = config.scope;
    elements.deleteDialogConfirm.textContent = config.confirmLabel || "Delete";
    elements.deletePasswordInput.value = "";
    elements.deleteDialogError.textContent = "";
    setDeleteDialogBusy(false);

    if (!elements.deleteDialog.open) {
        elements.deleteDialog.showModal();
    }

    window.requestAnimationFrame(() => elements.deletePasswordInput.focus());
}

function closeDeleteDialog() {
    if (state.deleteDialogBusy) {
        return;
    }

    if (elements.deleteDialog.open) {
        elements.deleteDialog.close();
    }

    elements.deletePasswordInput.value = "";
    elements.deleteDialogError.textContent = "";
    state.pendingDelete = null;
}

function setDeleteDialogBusy(busy) {
    state.deleteDialogBusy = busy;
    elements.deleteDialogForm.setAttribute("aria-busy", busy ? "true" : "false");
    elements.deletePasswordInput.disabled = busy;
    elements.deleteDialogCancel.disabled = busy;
    elements.deleteDialogConfirm.disabled = busy;

    if (busy) {
        elements.deleteDialogConfirm.textContent = "Deleting...";
    } else if (state.pendingDelete) {
        elements.deleteDialogConfirm.textContent = state.pendingDelete.confirmLabel || "Delete";
    }
}

function getDeleteDialogError(error) {
    if (error.status === 401) {
        return "Incorrect password. Please try again.";
    }

    if (error.status === 503) {
        return "Deletion password is not configured on the server.";
    }

    return error.message || "Deletion failed. Please try again.";
}

function createSection(title) {
    const section = document.createElement("section");
    section.className = "record-section";
    section.appendChild(textNode("h3", title));
    return section;
}

function textNode(tag, text, className) {
    const node = document.createElement(tag);
    node.textContent = text === null || text === undefined ? "" : String(text);
    if (className) {
        node.className = className;
    }
    return node;
}

function emptyNode(text) {
    return textNode("div", text, "empty-state");
}

function safeArray(input) {
    return Array.isArray(input) ? input : [];
}

function firstValue(...values) {
    return values.find(item => clean(item)) || "";
}

function clean(input) {
    if (input === null || input === undefined) {
        return "";
    }

    const text = String(input).trim();
    return text === "-" ? "" : text;
}

function value(input) {
    if (input === null || input === undefined || input === "") {
        return "-";
    }

    return String(input);
}

function numeric(input) {
    return typeof input === "number" && !Number.isNaN(input) ? input : 0;
}

function compactId(input) {
    const text = clean(input);
    return text ? text.slice(0, 8) : "-";
}

function shortId(input) {
    return clean(input).slice(0, 8);
}

function plural(count, singular) {
    return count + " " + singular + (count === 1 ? "" : "s");
}

function titleCase(input) {
    return clean(input).replace(/[-_]+/g, " ").replace(/\b\w/g, character => character.toUpperCase()) || "-";
}

function formatSeconds(input) {
    if (typeof input !== "number" || Number.isNaN(input)) {
        return "-";
    }

    return input.toFixed(1) + "s";
}

function formatClockDuration(input) {
    if (typeof input !== "number" || Number.isNaN(input) || input < 0) {
        return "-";
    }

    const totalSeconds = Math.min(1200, Math.round(input));
    const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, "0");
    const seconds = String(totalSeconds % 60).padStart(2, "0");
    return minutes + ":" + seconds;
}

function formatRatio(input) {
    if (typeof input !== "number" || Number.isNaN(input)) {
        return "-";
    }

    return input.toFixed(2) + "x";
}

function formatPercent(input) {
    if (typeof input !== "number" || Number.isNaN(input)) {
        return "-";
    }

    return Math.round(input * 100) + "%";
}

function formatTimestamp(input) {
    if (!input) {
        return "-";
    }

    const date = new Date(input);
    return Number.isNaN(date.getTime()) ? input : date.toLocaleString();
}

function formatShortDate(input) {
    if (!input) {
        return "No time";
    }

    const date = new Date(input);

    if (Number.isNaN(date.getTime())) {
        return String(input).slice(0, 10);
    }

    return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
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

function setStatus(text) {
    elements.statusLine.textContent = text;
}

function showNotice(text) {
    elements.notice.textContent = text;
    elements.notice.classList.add("visible");
    window.setTimeout(() => elements.notice.classList.remove("visible"), 4500);
}

function formatDetailValue(detail) {
    if (!detail) {
        return "";
    }

    if (typeof detail === "string") {
        return detail;
    }

    if (Array.isArray(detail)) {
        return detail.map(item => formatDetailValue(item)).filter(Boolean).join("; ");
    }

    if (typeof detail === "object") {
        if (typeof detail.message === "string" && detail.message) {
            return detail.message;
        }
        if (typeof detail.msg === "string" && detail.msg) {
            return detail.msg;
        }
        if (typeof detail.code === "string" && detail.code) {
            return detail.code;
        }
        try {
            return JSON.stringify(detail);
        } catch (error) {
            return String(detail);
        }
    }

    return String(detail);
}

async function getResponseError(response) {
    try {
        const data = await response.json();
        if (data && data.detail) {
            const formatted = formatDetailValue(data.detail);
            if (formatted) {
                return formatted;
            }
        }
    } catch (error) {
        // Use the status fallback.
    }

    return "HTTP " + response.status;
}
