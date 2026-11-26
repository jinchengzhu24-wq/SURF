"""Policy 2 binary discovery models, step parsing, promise verification, and refusal text."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.domain.blueprints import GRID_HEIGHT, GRID_WIDTH
from server.i18n.messages import format_protected_refusal

from .models import _WALKABLE_TILES, EDITABLE_OPERATORS, BinaryError
from .plan_parser import parse_revision_plan

BINARY_POLICY_VERSION = 2
BINARY_PROMISE_VERSION = 1
BINARY_KINDS: frozenset[str] = frozenset(
    {"reduce_obstacles", "widen_corridor", "shorter_solution", "reshape_water"}
)
BINARY_ACTIVE_STATUSES: frozenset[str] = frozenset(
    {"clarifying", "planning", "retry_pending", "revision_needed"}
)
PROTECTED_ENTITY_KEYS: tuple[str, ...] = ("player", "boxes", "targets", "outer_shell")


class FocusModel(BaseModel):
    row: int = Field(ge=1, le=GRID_HEIGHT)
    column: int = Field(ge=1, le=GRID_WIDTH)
    radius: int = Field(ge=1, le=3)


class CorridorSpec(BaseModel):
    row: int = Field(ge=1, le=GRID_HEIGHT)
    column: int = Field(ge=1, le=GRID_WIDTH)
    axis: Literal["horizontal", "vertical"]


class BinaryPromise(BaseModel):
    kind: Literal["reduce_obstacles", "widen_corridor", "shorter_solution", "reshape_water"]
    component: Literal["water", "internal_walls", "obstacles"]
    focus: FocusModel | None = None
    corridor: CorridorSpec | None = None

    @field_validator("corridor")
    @classmethod
    def _validate_corridor_presence(
        cls,
        value: CorridorSpec | None,
        info: Any,
    ) -> CorridorSpec | None:
        kind = info.data.get("kind")
        if kind == "widen_corridor" and value is None:
            raise BinaryError("BINARY_CORRIDOR_INVALID")
        if kind != "widen_corridor" and value is not None:
            raise BinaryError("BINARY_CORRIDOR_UNEXPECTED")
        return value


class BinaryBranch(BaseModel):
    label: str
    strategy: dict[str, Any]
    promise: BinaryPromise


class BinaryChoice(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    question_id: str = Field(alias="questionId")
    choice_id: str = Field(alias="choiceId")
    question: str = ""
    note: str = ""
    label: str
    letter: str = ""
    promise: BinaryPromise
    source_turn_id: str = Field(default="", alias="sourceTurnId")
    display_language: Literal["en", "zh-CN"] | None = Field(default=None, alias="displayLanguage")


class BinaryQuestion(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    question_id: str = Field(alias="questionId")
    topic_id: str = Field(default="", alias="topicId")
    base_version_id: str = Field(default="", alias="baseVersionId")
    question: str
    note: str = ""
    number: int = 1
    choices: list[dict[str, Any]] = Field(default_factory=list)
    selected_choice_id: str | None = Field(default=None, alias="selectedChoiceId")
    status: Literal["pending", "selected", "expired"] = "pending"


class BinaryStep(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    action: Literal["ask", "plan", "protected_refusal", "blocked"]
    question: str = ""
    note: str = ""
    branches: list[BinaryBranch] = Field(default_factory=list, max_length=2)
    plan_strategies: list[dict[str, Any]] = Field(
        default_factory=list, max_length=3, alias="planStrategies"
    )
    protected_entities: list[str] = Field(
        default_factory=list, max_length=4, alias="protectedEntities"
    )
    initial_requirements: dict[str, Any] | None = Field(default=None, alias="initialRequirements")


class BinaryDiscoveryState(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    topic_id: str = Field(alias="topicId")
    session_id: str = Field(alias="sessionId")
    version_id: str = Field(alias="versionId")
    binding: str
    language: Literal["en", "zh-CN"] = "en"
    status: Literal[
        "clarifying",
        "planning",
        "retry_pending",
        "revision_needed",
        "proposal_ready",
        "cancelled",
    ] = "clarifying"
    question_count: int = Field(default=0, alias="questionCount")
    user_turns: list[dict[str, str]] = Field(default_factory=list, alias="userTurns")
    choices: list[BinaryChoice] = Field(default_factory=list)
    pending_question: dict[str, Any] | None = Field(default=None, alias="pendingQuestion")
    selected_candidate: dict[str, Any] | None = Field(default=None, alias="selectedCandidate")
    retry_action: dict[str, Any] | None = Field(default=None, alias="retryAction")
    failure_code: str | None = Field(default=None, alias="failureCode")
    initial_message_key: str | None = Field(default=None, alias="initialMessageKey")
    lease_until: float = Field(default=0.0, alias="leaseUntil")

    def public_question(self) -> dict[str, Any] | None:
        question = self.pending_question
        if not question:
            return None
        selected = next(
            (item.choice_id for item in self.choices if item.question_id == question["questionId"]),
            None,
        )
        return {
            "questionId": question["questionId"],
            "topicId": self.topic_id,
            "baseVersionId": self.version_id,
            "question": question["question"],
            "note": question["note"],
            "number": question["number"],
            "sourceLanguage": self.language,
            "choices": [
                {"choiceId": item["choiceId"], "label": item["label"], "letter": item["letter"]}
                for item in question["choices"]
            ],
            "selectedChoiceId": selected,
            "status": (
                "selected"
                if selected
                else "pending"
                if self.status == "clarifying"
                else "expired"
            ),
        }

    def public_flow(self) -> dict[str, Any]:
        return {
            "active": self.status in BINARY_ACTIVE_STATUSES,
            "status": self.status,
            "topicId": self.topic_id,
            "interactionMode": "binary_choice",
            "clarificationQuestionCount": self.question_count,
            "recordedUserTurnIds": [turn["id"] for turn in self.user_turns if "id" in turn],
            "pendingQuestion": self.public_question(),
            "retryAction": self.retry_action,
            "retryable": self.status == "retry_pending",
            "failureCategory": self.failure_code,
            "canSupplement": False,
            "recoverySuggestions": [],
        }


def binary_signature(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def parse_binary_step(value: Any, question_count: int) -> dict[str, Any]:
    core_keys = {"action", "question", "note", "branches", "initialRequirements"}
    allowed_keys = core_keys | {"planStrategies", "protectedEntities"}
    if not isinstance(value, Mapping) or not core_keys.issubset(value) or set(value) - allowed_keys:
        raise BinaryError("BINARY_ENVELOPE_INVALID", "envelope")
    data = copy.deepcopy(dict(value))
    data.setdefault("planStrategies", [])
    data.setdefault("protectedEntities", [])
    if (
        data["action"] not in {"ask", "plan", "protected_refusal", "blocked"}
        or not isinstance(data["branches"], list)
        or not isinstance(data["planStrategies"], list)
        or not isinstance(data["protectedEntities"], list)
    ):
        raise BinaryError("BINARY_ACTION_INVALID", "action")

    for field_name in ("question", "note"):
        if not isinstance(data[field_name], str) or len(data[field_name]) > 1200:
            raise BinaryError("BINARY_TEXT_INVALID", field_name)
        data[field_name] = " ".join(data[field_name].split())

    if data["action"] == "protected_refusal":
        if data["question"] or data["branches"] or data["planStrategies"]:
            raise BinaryError("BINARY_UNUSED_FIELDS", "envelope")
        entities = data["protectedEntities"]
        if (
            not entities
            or len(entities) > 4
            or any(
                not isinstance(item, str) or item not in PROTECTED_ENTITY_KEYS
                for item in entities
            )
        ):
            raise BinaryError("BINARY_PROTECTED_ENTITIES_REQUIRED", "protectedEntities")
        data["protectedEntities"] = list(dict.fromkeys(entities))
        return data

    if data["action"] == "plan":
        if data["question"] or data["branches"] or data["protectedEntities"]:
            raise BinaryError("BINARY_UNUSED_FIELDS", "envelope")
        if len(data["planStrategies"]) > 3:
            raise BinaryError("BINARY_PLAN_STRATEGY_INVALID", "planStrategies")
        for item in data["planStrategies"]:
            try:
                plan = parse_revision_plan({"strategies": [item]})
                if (
                    set(plan.strategies[0].operators) - EDITABLE_OPERATORS
                    or plan.strategies[0].metric_goals
                ):
                    raise BinaryError("BINARY_PLAN_STRATEGY_INVALID", "planStrategies")
            except (ValueError, TypeError, KeyError) as error:
                raise BinaryError("BINARY_PLAN_STRATEGY_INVALID", "planStrategies") from error
        return data

    if data["action"] == "blocked":
        if (
            data["question"]
            or data["branches"]
            or data["planStrategies"]
            or data["protectedEntities"]
        ):
            raise BinaryError("BINARY_UNUSED_FIELDS", "envelope")
        if not data["note"]:
            raise BinaryError("BINARY_TEXT_INVALID", "note")
        return data

    if data["planStrategies"] or data["protectedEntities"]:
        raise BinaryError("BINARY_UNUSED_FIELDS", "envelope")
    if question_count >= 3:
        raise BinaryError("BINARY_QUESTION_LIMIT", "action")
    if not data["question"] or len(data["branches"]) != 2:
        raise BinaryError("BINARY_TWO_BRANCHES_REQUIRED")

    for index, branch in enumerate(data["branches"]):
        try:
            if not isinstance(branch, dict) or set(branch) != {"label", "strategy", "promise"}:
                raise BinaryError("BINARY_BRANCH_INVALID")
            if (
                not isinstance(branch["label"], str)
                or not branch["label"].strip()
                or len(branch["label"]) > 240
            ):
                raise BinaryError("BINARY_LABEL_INVALID")
            branch["label"] = " ".join(branch["label"].split())
            plan = parse_revision_plan({"strategies": [branch["strategy"]]})
            if (
                set(plan.strategies[0].operators) - EDITABLE_OPERATORS
                or plan.strategies[0].metric_goals
            ):
                raise BinaryError("BINARY_PROTECTED_STRATEGY")
            branch["promise"] = BinaryPromise.model_validate(branch["promise"]).model_dump()
        except (ValueError, TypeError, KeyError) as error:
            raise BinaryError(
                getattr(error, "code", "BINARY_STRATEGY_INVALID"), f"branch-{index + 1}"
            ) from error

    if (
        data["branches"][0]["label"] == data["branches"][1]["label"]
        or binary_signature(data["branches"][0]["promise"])
        == binary_signature(data["branches"][1]["promise"])
    ):
        raise BinaryError("BINARY_DUPLICATE_BRANCHES")
    return data


def _focus_cells(
    rows: Sequence[str],
    focus: Mapping[str, Any] | None,
) -> list[tuple[int, int, str]]:
    return [
        (y, x, tile)
        for y, row in enumerate(rows)
        for x, tile in enumerate(row)
        if focus is None
        or max(abs(y + 1 - focus["row"]), abs(x + 1 - focus["column"])) <= focus["radius"]
    ]


def _corridor_span(rows: Sequence[str], row: int, col: int, axis: str) -> int:
    y, x = row - 1, col - 1
    if rows[y][x] not in _WALKABLE_TILES:
        return 0
    dx, dy = (1, 0) if axis == "horizontal" else (0, 1)
    size = 1
    for sign in (-1, 1):
        a, b = x + sign * dx, y + sign * dy
        while 0 <= a < GRID_WIDTH and 0 <= b < GRID_HEIGHT and rows[b][a] in _WALKABLE_TILES:
            size += 1
            a, b = a + sign * dx, b + sign * dy
    return size


def check_promises(
    base: Sequence[str],
    candidate: Sequence[str],
    promises: Sequence[Mapping[str, Any]],
    baseline: Any = None,
    validation: Any = None,
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for raw_promise in promises:
        promise = BinaryPromise.model_validate(raw_promise).model_dump()
        kind, focus = promise["kind"], promise["focus"]
        if kind == "reduce_obstacles":
            tiles = (
                {"@"}
                if promise["component"] == "water"
                else {"#"}
                if promise["component"] == "internal_walls"
                else {"#", "@"}
            )
            before: Any = sum(tile in tiles for _, _, tile in _focus_cells(base, focus))
            after: Any = sum(tile in tiles for _, _, tile in _focus_cells(candidate, focus))
            passed = after < before
        elif kind == "reshape_water":
            before_set = {(y, x) for y, x, tile in _focus_cells(base, focus) if tile == "@"}
            after_set = {(y, x) for y, x, tile in _focus_cells(candidate, focus) if tile == "@"}
            passed = (
                before_set != after_set
                and len(before_set) == len(after_set)
                and sum(r.count("@") for r in base) == sum(r.count("@") for r in candidate)
            )
            before, after = len(before_set), len(after_set)
        elif kind == "widen_corridor":
            c = promise["corridor"]
            before = _corridor_span(base, c["row"], c["column"], c["axis"])
            after = _corridor_span(candidate, c["row"], c["column"], c["axis"])
            along = _corridor_span(
                base,
                c["row"],
                c["column"],
                "vertical" if c["axis"] == "horizontal" else "horizontal",
            )
            passed = before > 0 and along >= 3 and before < along and after > before
        else:
            if baseline is None or validation is None:
                raise BinaryError("BINARY_METRIC_UNVERIFIED")
            before = (
                baseline.get("solutionSteps")
                if isinstance(baseline, Mapping)
                else baseline.solution_steps
            )
            after = (
                validation.get("solutionSteps")
                if isinstance(validation, Mapping)
                else validation.solution_steps
            )
            if type(before) is not int or type(after) is not int:
                raise BinaryError("BINARY_METRIC_UNVERIFIED")
            passed = after < before
        outcomes.append({"kind": kind, "passed": bool(passed), "before": before, "after": after})

    if any(not item["passed"] for item in outcomes):
        raise BinaryError("BINARY_PROMISE_NOT_REALIZED", retryable=False)
    return outcomes


def compile_choice_requirements_deterministic(
    choices: Sequence[Mapping[str, Any]],
    user_turns: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Compile selected binary button choices into validated typed requirement items."""
    sources = {str(item["id"]): str(item["content"]) for item in user_turns or ()}
    compiled: list[dict[str, Any]] = []
    for choice in choices or ():
        turn_id = choice["sourceTurnId"]
        span = sources.get(turn_id) or str(choice.get("label") or "")
        promise = choice["promise"]
        kind = promise["kind"]
        comp = promise["component"]
        if kind in {"reduce_obstacles", "widen_corridor"}:
            req_component = "water" if comp == "water" else "wall"
            req_property, req_relation = "count", "decrease"
        elif kind == "shorter_solution":
            req_component, req_property, req_relation = "gameplay", "solutionSteps", "decrease"
        else:
            req_component, req_property, req_relation = "water", "positions", "relocate"
        compiled.append(
            {
                "component": req_component,
                "property": req_property,
                "relation": req_relation,
                "strength": "goal",
                "entities": [],
                "value": None,
                "unit": "none",
                "priorityEvidenceSpan": "",
                "focused": True,
                "sourceTurnId": turn_id,
                "evidenceSpan": span,
                "statement": str(choice.get("label") or span).strip()[:400],
                "scope": "all",
            }
        )
    return compiled


def render_protected_refusal(
    protected_entities: Sequence[str] | None,
    initial_request: str | None,
    language: str | None,
) -> str:
    """Delegate protected entity refusal formatting to server.i18n.messages."""
    return format_protected_refusal(protected_entities, initial_request, language)
