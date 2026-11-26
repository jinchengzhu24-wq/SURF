"""Typed session, Stage, entity binding, play attempt, and unified turn guidance models."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from server.domain.blueprints import GRID_HEIGHT, GRID_WIDTH
from server.domain.memory import DesignContextState

TILE_LEGEND: dict[str, str] = {
    " ": "void",
    "#": "wall",
    ".": "floor",
    "@": "water",
    "p": "player",
    "s": "box",
    "t": "target",
}
ENTITY_BINDINGS_SCHEMA_VERSION = 1


class SessionMode(StrEnum):
    UNITY = "unity"
    DEMO = "demo"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    FINALIZED = "finalized"
    COMPLETED = "completed"
    AWAITING_INTENTION = "awaiting_intention"
    LOCKED = "locked"


class InitialDraftMethod(StrEnum):
    PARTIAL_COMPLETION = "partial_completion"
    DESCRIPTION_GENERATION = "description_generation"
    ALGORITHM_DEMO = "algorithm_demo"


class StageSource(StrEnum):
    INITIAL = "initial"
    HUMAN_EDIT = "human_edit"
    AI_PROPOSAL = "ai_proposal"
    RESTORED = "restored"


class EntityKind(StrEnum):
    PLAYER = "player"
    BOX = "box"
    TARGET = "target"


class IdentityConfidence(StrEnum):
    EXACT = "exact"
    UNKNOWN = "unknown"


class PlayAttemptStatus(StrEnum):
    ISSUED = "issued"
    BOOTSTRAPPED = "bootstrapped"
    PLAYING = "playing"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    EXPIRED = "expired"


class DraftRegenerationStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class DraftRegenerationFailureCode(StrEnum):
    GENERATION_FAILED = "generation_failed"
    LLM_FAILED = "llm_failed"
    VALIDATION_FAILED = "validation_failed"
    UNITY_UNAVAILABLE = "unity_unavailable"
    BLUEPRINT_UNAVAILABLE = "blueprint_unavailable"
    GENERATION_TIMED_OUT = "generation_timed_out"


class GridDimensions(BaseModel):
    rows: int = GRID_HEIGHT
    columns: int = GRID_WIDTH


class GridPoint(BaseModel):
    row: int
    column: int


class EntityBinding(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", use_enum_values=True)

    entity_id: str = Field(alias="entityId")
    label: str
    kind: EntityKind
    row: int
    column: int
    identity_confidence: IdentityConfidence = Field(
        default=IdentityConfidence.EXACT,
        alias="identityConfidence",
    )


class EntityBindingsSnapshot(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    schema_version: int = Field(default=ENTITY_BINDINGS_SCHEMA_VERSION, alias="schemaVersion")
    map_fingerprint: str = Field(alias="mapFingerprint")
    identity_status: Literal["exact", "partial", "unknown"] = Field(alias="identityStatus")
    source: str
    entities: list[EntityBinding] = Field(default_factory=list)
    binding_fingerprint: str = Field(alias="bindingFingerprint")


class TileDiff(BaseModel):
    x: int
    y: int
    before: str
    after: str


class StageChangeSummary(BaseModel):
    """Domain summary of which map components changed between two Stages."""

    model_config = ConfigDict(populate_by_name=True)

    components: list[str] = Field(default_factory=list)
    changed_cell_count: int = Field(default=0, alias="changedCellCount")
    component_cell_counts: dict[str, int] = Field(
        default_factory=dict,
        alias="componentCellCounts",
    )

    def to_wire_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


class StageSnapshot(BaseModel):
    """Authoritative current-Stage fact snapshot shared across domain, prompts, and UI."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    schema_version: int = Field(default=1, alias="schemaVersion")
    legend: dict[str, str] = Field(default_factory=lambda: dict(TILE_LEGEND))
    version_id: str | None = Field(default=None, alias="versionId")
    stage_number: int | None = Field(default=None, alias="stageNumber")
    rows: list[str]
    dimensions: GridDimensions = Field(default_factory=GridDimensions)
    coordinate_system: str = Field(
        default="one-based row,column; row 1 is top and column 1 is left",
        alias="coordinateSystem",
    )
    map_fingerprint: str = Field(alias="mapFingerprint")
    entity_binding_fingerprint: str = Field(alias="entityBindingFingerprint")
    identity_status: Literal["exact", "partial", "unknown"] = Field(
        default="unknown",
        alias="identityStatus",
    )
    entities: list[dict[str, Any]] = Field(default_factory=list)
    player: dict[str, Any] = Field(default_factory=dict)
    boxes: list[dict[str, Any]] = Field(default_factory=list)
    targets: list[dict[str, Any]] = Field(default_factory=list)
    water_cells: list[GridPoint] = Field(default_factory=list, alias="waterCells")
    tile_at: dict[str, str] = Field(default_factory=dict, alias="tileAt")


class StageVersion(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", use_enum_values=True)

    version_id: str = Field(alias="versionId")
    session_id: str = Field(alias="sessionId")
    stage_number: int = Field(alias="stageNumber")
    parent_version_id: str | None = Field(default=None, alias="parentVersionId")
    source: StageSource
    rows: list[str]
    summary: str = ""
    diff: list[TileDiff] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    design_context: DesignContextState | None = Field(default=None, alias="designContext")
    entity_bindings: EntityBindingsSnapshot | None = Field(default=None, alias="entityBindings")
    created_at: str = Field(alias="createdAt")


class PlayAttempt(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", use_enum_values=True)

    attempt_id: str = Field(alias="attemptId")
    session_id: str | None = Field(default=None, alias="sessionId")
    version_id: str | None = Field(default=None, alias="versionId")
    status: PlayAttemptStatus
    issued_at: str = Field(alias="issuedAt")
    loaded_at: str | None = Field(default=None, alias="loadedAt")
    first_move_at: str | None = Field(default=None, alias="firstMoveAt")
    finished_at: str | None = Field(default=None, alias="finishedAt")
    duration_seconds: float = Field(default=0.0, alias="durationSeconds")
    move_count: int = Field(default=0, alias="moveCount")
    push_count: int = Field(default=0, alias="pushCount")
    restart_count: int = Field(default=0, alias="restartCount")
    minimum_moves: int = Field(default=-1, alias="minimumMoves")
    minimum_pushes: int = Field(default=-1, alias="minimumPushes")


class DraftRegeneration(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", use_enum_values=True)

    generation: int = 1
    regeneration_status: DraftRegenerationStatus | None = Field(
        default=None,
        alias="regenerationStatus",
    )
    regeneration_request_id: str | None = Field(default=None, alias="regenerationRequestId")
    regeneration_requested_at: str | None = Field(
        default=None,
        alias="regenerationRequestedAt",
    )
    regeneration_updated_at: str | None = Field(default=None, alias="regenerationUpdatedAt")
    failure_code: DraftRegenerationFailureCode | None = Field(default=None, alias="failureCode")


class SessionRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", use_enum_values=True)

    session_id: str = Field(alias="sessionId")
    mode: SessionMode = SessionMode.UNITY
    status: SessionStatus = SessionStatus.ACTIVE
    language: Literal["en", "zh-CN"] = "zh-CN"
    language_locked: bool = Field(default=False, alias="languageLocked")
    demo_mode: bool = Field(default=False, alias="demoMode")
    initial_draft_method: InitialDraftMethod = Field(alias="initialDraftMethod")
    match_id: str | None = Field(default=None, alias="matchId")
    player_number: int | None = Field(default=None, alias="playerNumber")
    current_version_id: str | None = Field(default=None, alias="currentVersionId")
    final_version_id: str | None = Field(default=None, alias="finalVersionId")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    deadline_started_at: str | None = Field(default=None, alias="deadlineStartedAt")
    deadline_at: str | None = Field(default=None, alias="deadlineAt")


# ---------------------------------------------------------------------------
# Unified PublicTurnGuidance (replaces two conflicting _public_guidance fns)
# ---------------------------------------------------------------------------

_HIDDEN_GUIDANCE_KEYS: frozenset[str] = frozenset(
    {
        "designContextPatch",
        "designContextPatchError",
        "proposalDiscovery",
        "openingPresentation",
        "openingRecovery",
        "manualEditReview",
        "relaxationOffer",
    }
)
_HIDDEN_PROPOSAL_OFFER_KEYS: frozenset[str] = frozenset(
    {"executionBrief", "revisionWorkflow", "revisionPlan"}
)


class PublicTurnGuidance(BaseModel):
    """Canonical public guidance payload exposed on turns and projections."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    move: str | None = None
    intent_hypothesis: str | None = Field(default=None, alias="intentHypothesis")
    intent_confidence: Literal["low", "medium", "high"] | None = Field(
        default=None,
        alias="intentConfidence",
    )
    follow_up_question: str | None = Field(default=None, alias="followUpQuestion")
    proposal_offer: dict[str, Any] | None = Field(default=None, alias="proposalOffer")
    proposal_question: dict[str, Any] | None = Field(default=None, alias="proposalQuestion")
    disagreement: dict[str, Any] | None = None
    ui_cues: list[dict[str, Any]] = Field(default_factory=list, alias="uiCues")
    coordinate_links: list[dict[str, Any]] = Field(default_factory=list, alias="coordinateLinks")
    intent_state: dict[str, Any] | None = Field(default=None, alias="intentState")
    challenge_state: dict[str, Any] | None = Field(default=None, alias="challengeState")
    challenge_card_schema_version: int | None = Field(
        default=None,
        alias="challengeCardSchemaVersion",
    )
    dashboard_node: dict[str, Any] | None = Field(default=None, alias="dashboardNode")

    @classmethod
    def from_raw(
        cls,
        raw: Mapping[str, Any] | None,
        *,
        include_dashboard_node: bool = True,
    ) -> dict[str, Any]:
        """Single authoritative sanitizer replacing both legacy `_public_guidance` functions."""
        if not isinstance(raw, Mapping):
            return {}

        cleaned: dict[str, Any] = {
            str(key): value
            for key, value in raw.items()
            if not str(key).startswith("_") and key not in _HIDDEN_GUIDANCE_KEYS
        }

        if include_dashboard_node:
            dashboard_node = (
                dict(cleaned.get("dashboardNode"))
                if isinstance(cleaned.get("dashboardNode"), Mapping)
                else {}
            )
            if raw.get("_intentHypothesisId"):
                dashboard_node["intentHypothesisId"] = raw["_intentHypothesisId"]
                review_state = raw.get("_intentReviewState")
                if (
                    isinstance(review_state, Mapping)
                    and review_state.get("interactionMode") == "conflict_choice"
                ):
                    dashboard_node["intentConflict"] = True
            discovery = raw.get("proposalDiscovery")
            if isinstance(discovery, Mapping) and discovery.get("topicId"):
                dashboard_node["proposalTopicId"] = discovery["topicId"]
            if dashboard_node:
                cleaned["dashboardNode"] = dashboard_node
            else:
                cleaned.pop("dashboardNode", None)
        else:
            cleaned.pop("dashboardNode", None)

        offer = cleaned.get("proposalOffer")
        if isinstance(offer, Mapping):
            cleaned["proposalOffer"] = {
                str(key): value
                for key, value in offer.items()
                if key not in _HIDDEN_PROPOSAL_OFFER_KEYS and not str(key).startswith("_")
            }

        return cleaned


def to_public_guidance(
    raw: Mapping[str, Any] | None,
    *,
    include_dashboard_node: bool = True,
) -> dict[str, Any]:
    """Sanitize turn guidance into the canonical public dictionary representation."""
    return PublicTurnGuidance.from_raw(raw, include_dashboard_node=include_dashboard_node)


# ---------------------------------------------------------------------------
# Pure Stage diff & change summary helpers (extracted out of solver!)
# ---------------------------------------------------------------------------


def map_fingerprint(rows: Sequence[str] | None) -> str:
    """Return a stable SHA-256 fingerprint for a Stage row tuple."""
    canonical = json.dumps(list(rows or ()), ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()


def describe_diff(before_rows: Sequence[str], after_rows: Sequence[str]) -> list[dict[str, Any]]:
    """Return row-major tile diff entries between two equal-sized grids."""
    changes: list[dict[str, Any]] = []
    for y, (before_row, after_row) in enumerate(zip(before_rows, after_rows, strict=True)):
        for x, (before_tile, after_tile) in enumerate(zip(before_row, after_row, strict=True)):
            if before_tile != after_tile:
                changes.append(
                    {"x": x, "y": y, "before": before_tile, "after": after_tile}
                )
    return changes


def boundary_wall_cells(rows: Sequence[str]) -> set[tuple[int, int]]:
    """Return outer shell `#` coordinates reachable from canvas border or exterior void."""
    height = len(rows)
    if height == 0:
        return set()
    width = len(rows[0])
    exterior_void: deque[tuple[int, int]] = deque()
    visited_void: set[tuple[int, int]] = set()

    for y in range(height):
        for x in range(width):
            if (x in {0, width - 1} or y in {0, height - 1}) and rows[y][x] == " ":
                pos = (x, y)
                if pos not in visited_void:
                    visited_void.add(pos)
                    exterior_void.append(pos)

    while exterior_void:
        x, y = exterior_void.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in visited_void:
                if rows[ny][nx] == " ":
                    visited_void.add((nx, ny))
                    exterior_void.append((nx, ny))

    shell: set[tuple[int, int]] = set()
    for y in range(height):
        for x in range(width):
            if rows[y][x] != "#":
                continue
            if x in {0, width - 1} or y in {0, height - 1} or any(
                (x + dx, y + dy) in visited_void
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1))
            ):
                shell.add((x, y))
    return shell


def summarize_stage_changes(
    before_rows: Sequence[str],
    after_rows: Sequence[str],
) -> StageChangeSummary:
    """Summarize component-level changes between two Stage grids without invoking the solver."""
    before_shell = boundary_wall_cells(before_rows)
    after_shell = boundary_wall_cells(after_rows)
    changed_cells = describe_diff(before_rows, after_rows)

    component_cells: dict[str, set[tuple[int, int]]] = {
        "outerShell": set(),
        "water": set(),
        "internalWalls": set(),
        "boxes": set(),
        "targets": set(),
        "player": set(),
        "floorArea": set(),
    }

    for change in changed_cells:
        position = (int(change["x"]), int(change["y"]))
        before = str(change["before"])
        after = str(change["after"])

        if "#" in {before, after}:
            component = (
                "outerShell"
                if position in before_shell or position in after_shell
                else "internalWalls"
            )
            component_cells[component].add(position)

        for tile, component in (
            ("@", "water"),
            ("s", "boxes"),
            ("t", "targets"),
            ("p", "player"),
        ):
            if tile in {before, after}:
                component_cells[component].add(position)

        if {before, after}.issubset({".", " "}):
            component_cells["floorArea"].add(position)

    order = (
        "outerShell",
        "water",
        "internalWalls",
        "boxes",
        "targets",
        "player",
        "floorArea",
    )
    components = [name for name in order if component_cells[name]]
    return StageChangeSummary(
        components=components,
        changedCellCount=len(changed_cells),
        componentCellCounts={name: len(component_cells[name]) for name in components},
    )
