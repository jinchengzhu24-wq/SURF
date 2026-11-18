export const DEFAULT_API_BASE = "https://sokobanaidemo.top";

export function isLoopbackHost(hostname) {
    const normalized = String(hostname || "").trim().toLowerCase();
    return normalized === "localhost"
        || normalized === "127.0.0.1"
        || normalized === "::1"
        || normalized === "[::1]";
}

function tryParseUrl(raw) {
    if (!raw || raw === "null" || typeof raw !== "string") {
        return null;
    }

    try {
        return new URL(raw);
    } catch (error) {
        return null;
    }
}

export function resolveApiBase(locationObj = globalThis.location) {
    const loc = locationObj || {};
    const parsedHref = tryParseUrl(loc.href);
    const parsedOrigin = tryParseUrl(loc.origin);
    const protocol = loc.protocol
        || (parsedHref && parsedHref.protocol)
        || (parsedOrigin && parsedOrigin.protocol)
        || "";
    const origin = (loc.origin && loc.origin !== "null")
        ? loc.origin
        : ((parsedHref && parsedHref.origin) || (parsedOrigin && parsedOrigin.origin) || "");
    const hostname = loc.hostname !== undefined
        ? String(loc.hostname || "")
        : ((parsedHref && parsedHref.hostname) || (parsedOrigin && parsedOrigin.hostname) || "");
    const search = loc.search !== undefined
        ? String(loc.search || "")
        : ((parsedHref && parsedHref.search) || "");

    const queryApi = new URLSearchParams(search).get("api");

    if (queryApi) {
        try {
            const baseHref = ((protocol === "http:" || protocol === "https:") && origin)
                ? origin
                : DEFAULT_API_BASE;
            const parsed = new URL(queryApi, baseHref);

            if (parsed.protocol === "http:" || parsed.protocol === "https:") {
                const sameOrigin = (protocol === "http:" || protocol === "https:")
                    && Boolean(origin)
                    && parsed.origin === origin;
                const localContext = protocol === "file:"
                    || isLoopbackHost(hostname);

                if (sameOrigin || (localContext && isLoopbackHost(parsed.hostname))) {
                    return (parsed.origin + parsed.pathname).replace(/\/+$/, "");
                }
            }
        } catch (error) {
            // Ignore invalid ?api= query overrides and fall back safely.
        }
    }

    if ((protocol === "http:" || protocol === "https:") && origin) {
        return origin.replace(/\/+$/, "");
    }

    return DEFAULT_API_BASE;
}

export function escapeHtml(input) {
    if (input === null || input === undefined) {
        return "";
    }

    return String(input)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

export function safeArray(input) {
    return Array.isArray(input) ? input : [];
}

export function clean(input) {
    if (input === null || input === undefined) {
        return "";
    }

    const text = String(input).trim();
    return text === "-" ? "" : text;
}

export function firstValue(...values) {
    return values.find(item => clean(item)) || "";
}

export function value(input) {
    if (input === null || input === undefined || input === "") {
        return "-";
    }

    return String(input);
}

export function numeric(input) {
    return typeof input === "number" && !Number.isNaN(input) ? input : 0;
}

export function compactId(input) {
    const text = clean(input);
    return text ? text.slice(0, 8) : "-";
}

export function shortId(input) {
    return clean(input).slice(0, 8);
}

export function plural(count, singular) {
    return count + " " + singular + (count === 1 ? "" : "s");
}

export function titleCase(input) {
    return clean(input).replace(/[-_]+/g, " ").replace(/\b\w/g, character => character.toUpperCase()) || "-";
}

export function formatSeconds(input) {
    if (typeof input !== "number" || Number.isNaN(input)) {
        return "-";
    }

    return input.toFixed(1) + "s";
}

export function formatClockDuration(input) {
    if (typeof input !== "number" || Number.isNaN(input) || input < 0) {
        return "-";
    }

    const totalSeconds = Math.min(1200, Math.round(input));
    const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, "0");
    const seconds = String(totalSeconds % 60).padStart(2, "0");
    return minutes + ":" + seconds;
}

export function formatRatio(input) {
    if (typeof input !== "number" || Number.isNaN(input)) {
        return "-";
    }

    return input.toFixed(2) + "x";
}

export function formatPercent(input) {
    if (typeof input !== "number" || Number.isNaN(input)) {
        return "-";
    }

    return Math.round(input * 100) + "%";
}

export function formatTimestamp(input) {
    if (!input) {
        return "-";
    }

    const date = new Date(input);
    return Number.isNaN(date.getTime()) ? input : date.toLocaleString();
}

export function formatShortDate(input) {
    if (!input) {
        return "No time";
    }

    const date = new Date(input);

    if (Number.isNaN(date.getTime())) {
        return String(input).slice(0, 10);
    }

    return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function getTileClass(tile) {
    if (tile === "#") return "tile-wall";
    if (tile === "@") return "tile-water";
    if (tile === "p") return "tile-player";
    if (tile === "s") return "tile-box";
    if (tile === "t") return "tile-target";
    if (tile === ".") return "tile-floor";
    return "tile-empty";
}

export function getTileName(tile) {
    return ({ "#": "wall", "@": "water", p: "player", s: "box", t: "target", ".": "floor" })[tile] || "empty";
}

export function getTileLabel(tile) {
    return ({ p: "P", s: "B", t: "T", "@": "~" })[tile] || "";
}

export function getDeleteDialogError(error) {
    if (error && error.status === 401) {
        return "Incorrect password. Please try again.";
    }

    if (error && error.status === 503) {
        return "Deletion password is not configured on the server.";
    }

    return (error && error.message) || "Deletion failed. Please try again.";
}

export function formatDetailValue(detail) {
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

export async function getResponseError(response) {
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

export function textNode(tag, text, className, doc = globalThis.document) {
    const node = doc.createElement(tag);
    node.textContent = text === null || text === undefined ? "" : String(text);
    if (className) {
        node.className = className;
    }
    return node;
}

export function emptyNode(text, doc = globalThis.document) {
    return textNode("div", text, "empty-state", doc);
}

export function createSection(title, doc = globalThis.document) {
    const section = doc.createElement("section");
    section.className = "record-section";
    section.appendChild(textNode("h3", title, undefined, doc));
    return section;
}

export function renderMap(container, rows, mini, doc = (container && container.ownerDocument) || globalThis.document) {
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
            const cell = doc.createElement("div");
            cell.className = "tile " + getTileClass(tile);
            cell.title = getTileName(tile);
            cell.textContent = getTileLabel(tile);
            container.appendChild(cell);
        }
    });
}

export function createDomHelpers({ document: doc } = {}) {
    const resolveDoc = () => doc || globalThis.document;
    return {
        textNode(tag, text, className) {
            return textNode(tag, text, className, resolveDoc());
        },
        emptyNode(text) {
            return emptyNode(text, resolveDoc());
        },
        createSection(title) {
            return createSection(title, resolveDoc());
        },
        renderMap(container, rows, mini) {
            return renderMap(container, rows, mini, resolveDoc());
        }
    };
}

export function createApiClient({ fetchImpl, locationObj, apiBase } = {}) {
    const resolvedBase = clean(apiBase)
        ? clean(apiBase).replace(/\/+$/, "")
        : resolveApiBase(locationObj);

    function getFetch() {
        if (typeof fetchImpl === "function") {
            return fetchImpl;
        }
        if (typeof globalThis.fetch === "function") {
            return globalThis.fetch.bind(globalThis);
        }
        throw new Error("No fetch implementation provided.");
    }

    function apiUrl(path) {
        return resolvedBase + path;
    }

    async function request(path, options) {
        return getFetch()(apiUrl(path), options);
    }

    async function getJson(path, options = { cache: "no-store" }) {
        const response = await request(path, options);
        if (!response.ok) {
            throw Object.assign(new Error("HTTP " + response.status), { status: response.status });
        }
        return response.json();
    }

    async function postProtected(endpoint, payload, password) {
        const response = await request(endpoint, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Delete-Password": password
            },
            body: JSON.stringify(payload)
        });
        if (!response.ok) {
            const error = new Error(await getResponseError(response));
            error.status = response.status;
            throw error;
        }
        return response;
    }

    return {
        apiBase: resolvedBase,
        apiUrl,
        request,
        getJson,
        postProtected
    };
}

export function createStatusController({ elements, window: win } = {}) {
    function setStatus(text) {
        if (elements && elements.statusLine) {
            elements.statusLine.textContent = text;
        }
    }

    function showNotice(text) {
        if (!elements || !elements.notice) {
            return;
        }
        elements.notice.textContent = text;
        elements.notice.classList?.add?.("visible");
        const clearVisible = () => elements.notice.classList?.remove?.("visible");
        if (win && typeof win.setTimeout === "function") {
            win.setTimeout(clearVisible, 4500);
        } else {
            const timer = setTimeout(clearVisible, 4500);
            timer?.unref?.();
        }
    }

    return { setStatus, showNotice };
}

export function createDeleteDialogController({
    state,
    elements,
    window: win,
    apiClient,
    setStatus,
    showNotice,
    onReload,
    errorStatusPrefix = "Could not delete records: ",
    defaultSuccessText = "Record deleted successfully.",
    selectOnError = false
} = {}) {
    function setDeleteDialogBusy(busy) {
        state.deleteDialogBusy = busy;
        elements.deleteDialogForm?.setAttribute?.("aria-busy", busy ? "true" : "false");
        if (elements.deletePasswordInput) elements.deletePasswordInput.disabled = busy;
        if (elements.deleteDialogCancel) elements.deleteDialogCancel.disabled = busy;
        if (elements.deleteDialogConfirm) {
            elements.deleteDialogConfirm.disabled = busy;
            if (busy) {
                elements.deleteDialogConfirm.textContent = "Deleting...";
            } else if (state.pendingDelete) {
                elements.deleteDialogConfirm.textContent = state.pendingDelete.confirmLabel || "Delete";
            }
        }
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
            if (typeof elements.deleteDialog.showModal === "function") {
                elements.deleteDialog.showModal();
            } else {
                elements.deleteDialog.open = true;
            }
        }

        const scheduleFrame = win && typeof win.requestAnimationFrame === "function"
            ? win.requestAnimationFrame.bind(win)
            : callback => callback();
        scheduleFrame(() => elements.deletePasswordInput.focus?.());
    }

    function closeDeleteDialog() {
        if (state.deleteDialogBusy) {
            return;
        }

        if (elements.deleteDialog.open) {
            if (typeof elements.deleteDialog.close === "function") {
                elements.deleteDialog.close();
            } else {
                elements.deleteDialog.open = false;
            }
        }

        elements.deletePasswordInput.value = "";
        elements.deleteDialogError.textContent = "";
        state.pendingDelete = null;
    }

    async function submitDeleteDialog(event) {
        event?.preventDefault?.();
        const config = state.pendingDelete;
        const password = elements.deletePasswordInput.value;

        if (!config || state.deleteDialogBusy) {
            return;
        }

        if (!password) {
            elements.deleteDialogError.textContent = "Enter the deletion password.";
            elements.deletePasswordInput.focus?.();
            return;
        }

        elements.deleteDialogError.textContent = "";
        setDeleteDialogBusy(true);
        setStatus?.(config.progressText);

        try {
            await apiClient.postProtected(config.endpoint, config.payload, password);
            if (config.afterSuccess) {
                config.afterSuccess();
            }
            setDeleteDialogBusy(false);
            closeDeleteDialog();
            showNotice?.(config.successText || defaultSuccessText);
            if (onReload) {
                await onReload(true);
            }
        } catch (error) {
            const message = getDeleteDialogError(error);
            elements.deleteDialogError.textContent = message;
            setStatus?.(errorStatusPrefix + message);
            setDeleteDialogBusy(false);
            if (selectOnError) {
                elements.deletePasswordInput.focus?.();
                elements.deletePasswordInput.select?.();
            }
        }
    }

    function requiresDashboardAccess() {
        const search = (win && win.location && win.location.search) || "";
        return new URLSearchParams(search).get("access") === "1";
    }

    function clearDashboardAccessParameter() {
        if (!win || !win.location || !win.history || typeof win.history.replaceState !== "function") {
            return;
        }
        const fallbackHref = ((win.location.origin && win.location.origin !== "null") ? win.location.origin : apiClient.apiBase)
            + (win.location.pathname || "/frontend/")
            + (win.location.search || "");
        const url = new URL(win.location.href || fallbackHref);
        url.searchParams.delete("access");
        win.history.replaceState(null, "", url.toString());
    }

    function requestDashboardAccess() {
        openDeleteDialog({
            title: "Dashboard access required",
            description: "Enter the dashboard password to continue.",
            scope: "This opens the 8000 study dashboard.",
            confirmLabel: "Continue",
            endpoint: "/verify-dashboard-password",
            payload: {},
            progressText: "Checking dashboard password...",
            successText: "Dashboard access granted.",
            afterSuccess: clearDashboardAccessParameter
        });
    }

    return {
        openDeleteDialog,
        closeDeleteDialog,
        setDeleteDialogBusy,
        submitDeleteDialog,
        requiresDashboardAccess,
        clearDashboardAccessParameter,
        requestDashboardAccess
    };
}
