import { TILE_ORDER } from "./i18n.js?v=binary-discovery-20261123-v2-esm";

export function buildEntityLabels(rows) {
    const labels = new Map();
    const counts = { s: 0, t: 0 };
    (rows || []).forEach((row, y) => [...String(row || "")].forEach((tile, x) => {
        if (tile === "p") labels.set(`${x},${y}`, "P");
        if (tile === "s" || tile === "t") {
            counts[tile] += 1;
            labels.set(`${x},${y}`, `${tile === "s" ? "B" : "T"}${counts[tile]}`);
        }
    }));
    return labels;
}

export function formatEntityCoordinate(entityLabel, x, y) {
    return entityLabel
        ? `${entityLabel} (row ${y + 1}, column ${x + 1})`
        : formatTileCoordinate(x, y);
}

export function tileClass(tile) { return tile === "#" ? "tile-wall" : tile === "@" ? "tile-water" : tile === "p" ? "tile-player" : tile === "s" ? "tile-box" : tile === "t" ? "tile-target" : tile === "." ? "tile-floor" : "tile-empty"; }
export function tileName(tile) { return tile === "#" ? "wall" : tile === "@" ? "water" : tile === "p" ? "player" : tile === "s" ? "box" : tile === "t" ? "target" : tile === "." ? "floor" : "erase"; }
export function formatTileCoordinate(x, y) { return `(${y + 1}, ${x + 1})`; }
export function tileLabel(tile) { return ({ p: "P", s: "B", t: "T", "@": "~" })[tile] || ""; }

export function createMapEditor(ctx) {
    const { state, elements, document, requestAnimationFrame, ports } = ctx;
    const t = (...args) => ports.t(...args);
    const markTileHelp = (...args) => ports.markTileHelp(...args);
    const isLevelValidationError = (...args) => ports.isLevelValidationError(...args);
    const escapeHtml = (...args) => ports.escapeHtml(...args);
    const localizedErrorMessage = (...args) => ports.localizedErrorMessage(...args);
    const technicalDetailsMarkup = (...args) => ports.technicalDetailsMarkup(...args);
    const technicalDetailsLabel = (...args) => ports.technicalDetailsLabel(...args);
    const persistUnsavedMapDraft = (...args) => ports.persistUnsavedMapDraft(...args);
    const findCoordinateRoute = (...args) => ports.findCoordinateRoute(...args);
    const selectedVersion = (...args) => ports.selectedVersion(...args);
    const updateControls = (...args) => ports.updateControls(...args);
    const deadlineExpired = (...args) => ports.deadlineExpired(...args);
    const canEditSelected = (...args) => ports.canEditSelected(...args);
    const renderMap = (...args) => ports.renderMap(...args);

    function clearMapOverlay() {
        elements.mapOverlay.textContent = "";
        elements.mapOverlay.removeAttribute("viewBox");
    }

    function drawActiveCoordinateRoute() {
        clearMapOverlay();
        const active = state.activeCoordinateLink;
        if (!active || active.versionId !== state.selectedVersionId) return;

        const route = findCoordinateRoute(state.draftRows, active.from, active.to);
        const board = elements.mapBoard;
        if (!route || !board) {
            state.activeCoordinateLink = null;
            return;
        }

        const boardRect = board.getBoundingClientRect();
        const points = route.map(point => {
            const cell = elements.mapGrid.querySelector(
                `[data-row="${point.row}"][data-column="${point.column}"]`
            );
            if (!cell) return null;
            const cellRect = cell.getBoundingClientRect();
            return `${cellRect.left + cellRect.width / 2 - boardRect.left},${cellRect.top + cellRect.height / 2 - boardRect.top}`;
        });
        if (points.some(point => !point) || !boardRect.width || !boardRect.height) {
            state.activeCoordinateLink = null;
            return;
        }

        elements.mapOverlay.setAttribute("viewBox", `0 0 ${boardRect.width} ${boardRect.height}`);
        const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
        const marker = document.createElementNS("http://www.w3.org/2000/svg", "marker");
        marker.setAttribute("id", "coordinate-route-arrowhead");
        marker.setAttribute("viewBox", "0 0 20 20");
        marker.setAttribute("refX", "18");
        marker.setAttribute("refY", "10");
        marker.setAttribute("markerWidth", "20");
        marker.setAttribute("markerHeight", "20");
        marker.setAttribute("orient", "auto");
        marker.setAttribute("markerUnits", "userSpaceOnUse");
        const arrowhead = document.createElementNS("http://www.w3.org/2000/svg", "path");
        arrowhead.setAttribute("d", "M 1 1 L 19 10 L 1 19 L 5.5 10 Z");
        arrowhead.setAttribute("fill", "#e03131");
        arrowhead.setAttribute("stroke", "#ffffff");
        arrowhead.setAttribute("stroke-width", "1.5");
        arrowhead.setAttribute("stroke-linejoin", "round");
        marker.appendChild(arrowhead);
        defs.appendChild(marker);
        elements.mapOverlay.appendChild(defs);

        const polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
        polyline.setAttribute("points", points.join(" "));
        polyline.setAttribute("fill", "none");
        polyline.setAttribute("stroke", "#e03131");
        polyline.setAttribute("stroke-width", "5");
        polyline.setAttribute("stroke-linecap", "round");
        polyline.setAttribute("stroke-linejoin", "round");
        polyline.setAttribute("stroke-dasharray", "7 5");
        polyline.setAttribute("marker-end", "url(#coordinate-route-arrowhead)");
        elements.mapOverlay.appendChild(polyline);
    }

    function renderMapImpl() {
        if (state.activeCoordinateLink && state.activeCoordinateLink.versionId !== state.selectedVersionId) {
            state.activeCoordinateLink = null;
        }
        const version = selectedVersion();
        clearMapOverlay();
        if (!version) return;
        elements.selectedStageEyebrow.textContent = `${t("stage").toUpperCase()} ${version.stageNumber}`;
        elements.mapGrid.textContent = "";
        elements.mapGrid.style.gridTemplateColumns = "repeat(12, var(--tile-size))";
        const entityLabels = buildEntityLabels(state.draftRows);
        state.draftRows.forEach((row, y) => {
            [...row].forEach((tile, x) => {
                const cell = document.createElement("button");
                cell.type = "button";
                cell.className = `tile ${tileClass(tile)}`;
                const entityLabel = entityLabels.get(`${x},${y}`);
                cell.textContent = entityLabel || tileLabel(tile);
                markTileHelp(cell, tile, x, y, entityLabel, "map");
                cell.dataset.row = String(y + 1);
                cell.dataset.column = String(x + 1);
                if (entityLabel) cell.dataset.entityId = entityLabel;
                cell.setAttribute(
                    "aria-label",
                    entityLabel
                        ? formatEntityCoordinate(entityLabel, x, y)
                        : `${t(tileName(tile))} ${formatTileCoordinate(x, y)}`,
                );
                cell.disabled = !canEditSelected() || state.busy;
                if (version.diff.some(change => change.x === x && change.y === y)) cell.classList.add("changed");
                cell.addEventListener("click", () => editTile(x, y));
                elements.mapGrid.appendChild(cell);
            });
        });
        renderToolbar();
        const validation = version.validation;
        const failedValidation = state.dirty && isLevelValidationError(state.validationError);
        elements.validationCard.className = `validation-card${failedValidation ? " invalid" : ""}`;
        elements.validationCard.setAttribute("role", failedValidation ? "alert" : "status");
        elements.validationCard.innerHTML = failedValidation
            ? `<strong>✗ ${escapeHtml(t("validationFailed"))}</strong><span>${escapeHtml(localizedErrorMessage(state.validationError))}</span>${technicalDetailsMarkup(state.validationError)}`
            : `<strong>✓ ${escapeHtml(t(state.dirty ? "previousVerified" : "verified"))}</strong>
               ${state.dirty ? `<span>${escapeHtml(t("draftUnchecked"))}</span>` : ""}
               <span>${escapeHtml(t(state.dirty ? "savedSolution" : "solutionSummary").replace("{steps}", validation.solutionSteps).replace("{pushes}", validation.solutionPushes))}</span>
               <details class="technical-details"><summary>${escapeHtml(technicalDetailsLabel())}</summary><p>${escapeHtml(String(validation.searchedStates))} states</p></details>`;

        elements.mapMode.textContent = state.session.status !== "active" ? t("lockedMode") : version.versionId === state.session.currentVersionId ? t("editMode") : t("readOnlyMode");
        if (state.dirty) elements.mapMode.textContent += ` · ${t("unsaved")}`;
        renderAttempts(version);
        drawActiveCoordinateRoute();
        requestAnimationFrame(drawActiveCoordinateRoute);
    }

    function renderToolbar() {
        elements.mapToolbar.textContent = "";
        TILE_ORDER.forEach(tile => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = `palette-button ${tileClass(tile)}`;
            button.dataset.uiHelpTile = tile;
            button.dataset.uiHelpKind = "palette";
            if (state.selectedTile === tile) button.classList.add("selected");
            button.disabled = !canEditSelected() || state.busy;
            const toolSymbol = ({ ".": "·", "#": "", "@": "~", p: "P", s: "B", t: "T", " ": "×" })[tile];
            button.innerHTML = `<span aria-hidden="true">${escapeHtml(toolSymbol)}</span><small>${escapeHtml(t(tileName(tile)))}</small>`;
            button.addEventListener("click", () => {
                if (!canEditSelected() || state.busy) return;
                state.selectedTile = tile;
                renderToolbar();
            });
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

    function resetDraftFromSelection() {
        const version = selectedVersion();
        state.draftRows = version ? version.rows.slice() : [];
        state.dirty = false;
        state.validationError = null;
    }

    function editTile(x, y) {
        if (!canEditSelected() || state.busy) return;
        state.activeCoordinateLink = null;
        const row = [...state.draftRows[y]];
        row[x] = state.selectedTile;
        state.draftRows[y] = row.join("");
        state.dirty = state.draftRows.some((value, index) => value !== selectedVersion().rows[index]);
        persistUnsavedMapDraft();
        state.validationError = null;
        renderMap();
        updateControls();
    }

    function canEditSelectedImpl() {
        return state.session
            && state.session.status === "active"
            && !state.unityPlayActive
            && !deadlineExpired()
            && state.selectedVersionId === state.session.currentVersionId;
    }

    function currentPendingProposal() {
        return state.session && state.session.proposals.slice().reverse().find(item => item.status === "pending" && item.baseVersionId === state.session.currentVersionId);
    }

    function createMiniMap(rows, diff, extraClass = "") {
        const map = document.createElement("div");
        map.className = `mini-map ${extraClass}`;
        map.style.gridTemplateColumns = "repeat(12, 1fr)";
        const entityLabels = buildEntityLabels(rows);
        const changed = new Set((diff || []).map(item => `${item.x},${item.y}`));
        rows.forEach((row, y) => [...row].forEach((tile, x) => {
            const cell = document.createElement("i");
            cell.className = tileClass(tile);
            const entityLabel = entityLabels.get(`${x},${y}`);
            markTileHelp(cell, tile, x, y, entityLabel, "preview");
            if (entityLabel) {
                cell.dataset.entityId = entityLabel;
                cell.setAttribute("role", "img");
                cell.setAttribute("aria-label", formatEntityCoordinate(entityLabel, x, y));
            }
            if (changed.has(`${x},${y}`)) cell.classList.add("changed");
            map.appendChild(cell);
        }));
        return map;
    }

    function formatAttempt(attempt) {
        const statusKey = attempt.status === "completed" ? "attemptCompleted" : attempt.status === "abandoned" ? "attemptAbandoned" : attempt.status === "interrupted" ? "attemptInterrupted" : "attemptStarted";
        return `${t(statusKey)} · ${attempt.moveCount} ${t("moves")} · ${attempt.pushCount} ${t("pushes")} · ${attempt.restartCount} ${t("restarts")} · ${Number(attempt.durationSeconds || 0).toFixed(1)} ${t("seconds")}`;
    }

    return {
        clearMapOverlay,
        drawActiveCoordinateRoute,
        renderMap: renderMapImpl,
        renderToolbar,
        renderAttempts,
        resetDraftFromSelection,
        editTile,
        canEditSelected: canEditSelectedImpl,
        currentPendingProposal,
        createMiniMap,
        formatAttempt,
        buildEntityLabels,
        formatEntityCoordinate,
        tileClass,
        tileName,
        formatTileCoordinate,
        tileLabel,
    };
}
