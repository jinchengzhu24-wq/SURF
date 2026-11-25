"""Replaces clarification generation/review slots; never classifies a button click."""
import asyncio
import copy
import json
import re

import cocreation.domain.binary_discovery as binary
from cocreation.domain.design_requirements import (
    REQUIREMENT_SEMANTICS,
    normalize_requirement_record,
    validate_requirement_record,
)
from cocreation.domain.proposal_diagnostics import trace_proposal
from cocreation.domain.proposal_search import ProposalSearchExhausted, _connected_outer_shell
from cocreation.reliability.component_patches import PatchRequest, Target
from cocreation.reliability.reply_reliability import parse_json_object

from .config_and_runtime import (
    APIConnectionError,
    APIStatusError,
    LLMServiceError,
    PLAIN_CHAT_MAX_TOKENS,
    _current_service,
    _request_completion,
    _request_component_patch,
    classify_exception,
)
from .snapshot_grounding import _assert_visible_output_language, _validate_map_grounding_texts


class _UpstreamBinaryTimeout(Exception):
    """Distinguishes upstream model completion timeouts from preflight budget timeouts."""


def generate(topic, context, validator, strategy_validator, request_id, deadline, preflight_budget=20.0, *, service=None):
    svc = _current_service(service)
    with svc.activated():
        return _generate_with_service(
            svc,
            topic,
            context,
            validator,
            strategy_validator,
            request_id,
            deadline,
            preflight_budget=preflight_budget,
        )


def _generate_with_service(svc, topic, context, validator, strategy_validator, request_id, deadline, preflight_budget=20.0):
    api_key, base_url = svc.credentials()
    if not api_key:
        raise LLMServiceError("CONFIGURATION_ERROR", "Kimi is unavailable.", request_id, False, 0, 503)
    model = svc.config.model
    stage = context["stageContext"]
    projection = {key: topic.get(key) for key in ("initialRequest", "questionCount", "understanding")}
    if topic.get("choices"):
        projection["understanding"] = {
            key: value
            for key, value in (topic.get("understanding") or {}).items()
            if key not in {"sufficient", "nextQuestionDimension"}
        }
        projection["mapStateNote"] = (
            "No edits have been applied yet; selectedCandidate is only a hypothetical feasibility proof on the ORIGINAL Stage."
        )
    projection["selectedChoices"] = [{**{key: choice[key] for key in ("question", "note", "label", "promise")},
        "selectionState": "explicit_interface_choice"} for choice in topic.get("choices", [])]
    if topic.get("selectedCandidate"):
        projection["selectedFeasibility"] = {
            "allSelectedActionsPassed": all(x["passed"] for x in topic["selectedCandidate"]["promiseResults"]),
            "verifiedActions": topic["selectedCandidate"]["promiseResults"],
            "candidateIsHypothetical": True}
    system = (
        "You are the Sokoban co-creation chat peer. Return the binary discovery schema in "
        + ("Simplified Chinese. " if context["language"] == "zh-CN" else "English. ")
        + "Choose action ask / plan / protected_refusal / blocked. "
        "ONLY water and internal walls may be edited; player, boxes, targets, and connected outer shell are FIXED by the server. "
        "Use action='protected_refusal' ONLY when the designer explicitly requests moving, adding, removing, or altering the count, "
        "position, or shape of player, boxes, targets, or outer_shell (for example moving a box or shifting the outer wall); "
        "set protectedEntities to a non-empty subset of ['player','boxes','targets','outer_shell'], question='', note='', branches=[], planStrategies=[], initialRequirements=null. "
        "Do NOT confuse spatial landmarks ('between B1 and T1') or keeping entities fixed ('保持玩家和箱子位置不变') with protected edits. "
        "When topic.selectedChoices is non-empty and selectedFeasibility.allSelectedActionsPassed is true, "
        "strongly prefer action='plan' (question='', note='', branches=[], planStrategies=[], protectedEntities=[], initialRequirements=null). "
        "When the designer's initial request on turn 0 ALREADY specifies a concrete editable direction or asks for an edit not covered "
        "by the four ask promises (for example adding internal walls, adding water, adding a detour, or lengthening/complicating the route), "
        "return action='plan' immediately with question='', note='', branches=[], protectedEntities=[], AND populate planStrategies "
        "with 1 to 3 executable editable RevisionPlan strategies ({effect, focus, operators, preserve, editBudget:1..12, metricGoals:[]}). "
        "Use action='ask' ONLY for an unresolved consequential preference on broad wishes: provide ONE short question, an optional ONE-sentence note, "
        "EXACTLY two materially different branches, planStrategies=[], protectedEntities=[]. "
        "Each branch label is a short natural action naming its editable component (water->水域, internal_walls->内墙, obstacles->障碍) "
        "and rough area (e.g. 清理左侧水域！/拆掉右侧内墙！); no Markdown or letter prefix. "
        "Prefer removing obstacles over adding/relocating them when compatible with the request. Both branches may remove obstacles "
        "in different areas or components. Each branch uses one local strategy: effect in open_route/narrow_route/adjust_internal_walls/reshape_water, "
        "focus (null or {row,column,radius:1..3}), operators from add_wall/remove_wall/add_water/remove_water, "
        "preserve from outer_shell/player/boxes/targets/water/walls/unrelated_areas, editBudget:1..12, metricGoals:[]. "
        "Only wall cells listed in editableInternalWallCells may be removed by remove_wall (any '#' connected to the outer border belongs to fixed outer_shell and cannot be removed). "
        "Center remove_wall focus on editableInternalWallCells (or use focus=null); if editableInternalWallCells is empty, do NOT choose remove_wall. "
        "NEVER include 'water' in preserve when editing water or when any prior selectedChoice edited water; NEVER include 'walls' in preserve "
        "when editing walls or when any prior selectedChoice edited internal_walls. "
        "CRITICAL WHEN topic.selectedChoices IS NON-EMPTY: Q2/Q3 branch strategies run on the ORIGINAL Stage and MUST realize ALL prior "
        "selectedChoices promises PLUS the new branch promise together (set strategy.focus=null when prior and new choices touch different areas, "
        "and include ALL needed operators without preserving edited components). "
        "Supported ask promises: reduce_obstacles (default when removing water/walls), widen_corridor (strictly wider walkable cross-section; "
        "avoid unless explicitly asked for one specific narrow corridor and the corridor center is walkable), shorter_solution, reshape_water. "
        "At questionCount>=3 return plan or blocked, never ask. For blocked, give a brief truthful note, question='', branches=[], planStrategies=[], protectedEntities=[]. "
        "When verifiedInitialRequirements is absent and action != 'protected_refusal', provide initialRequirements using ONLY initialDesignerTurns "
        "with exact sourceTurnId/evidenceSpan: NEVER emit requirements or automaticBindings for outer_shell, player, box/boxes, or target/targets "
        "(even when the designer writes '保持玩家和箱子位置不变' — those are platform-protected and belong ONLY in strategy.preserve, never in initialRequirements); "
        "keep scope='all' by default "
        "(use scope='original_cells' ONLY when component is water or wall AND property is count); use value=null, unit=none, priorityEvidenceSpan='', "
        "entities=[] unless explicitly stated. "
        + (
            "CRITICAL FOR THIS TURN: verifiedInitialRequirements is null, so whenever action is 'ask', 'plan', or 'blocked', "
            "initialRequirements MUST be a non-null JSON object {requirements:[...], automaticBindings:[], exactTransitions:[]} (NEVER null). "
            "Only action='protected_refusal' uses initialRequirements=null. "
            if topic.get("requirements") is None
            else "CRITICAL FOR THIS TURN: verifiedInitialRequirements is already set, so initialRequirements MUST be null. "
        )
        + REQUIREMENT_SEMANTICS
        + " EXACT JSON PROTOCOL: root contains ONLY action, question, note, branches, planStrategies, protectedEntities, initialRequirements. "
        "Each branch contains ONLY label, strategy, promise. Each strategy contains ONLY effect, focus, operators, preserve, editBudget, metricGoals. "
        "Each promise contains ONLY kind, component, focus, corridor. Use empty arrays where appropriate, never omit fields."
    )
    shell = _connected_outer_shell(context["rows"])
    editable_internal_walls = [
        {"row": y + 1, "column": x + 1}
        for y, row in enumerate(context["rows"])
        for x, tile in enumerate(row)
        if tile == "#" and (x, y) not in shell
    ]
    evidence = {
        "currentStageSnapshot": stage["stageSnapshot"],
        "editableInternalWallCells": editable_internal_walls,
        "topic": projection,
        "verifiedInitialRequirements": topic.get("requirements"),
        "initialDesignerTurns": topic["userTurns"][:1],
        "designContext": stage.get("revisionDesignContext") or {},
    }
    messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}]
    candidate, patch = None, None
    last_error = None
    for attempt in (1, 2):
        try:
            timeout = min(70.0, deadline - svc.clock.monotonic() - 20.0)
            if timeout <= 0:
                raise TimeoutError()
            try:
                if patch is not None:
                    candidate = asyncio.run(_request_component_patch(api_key, base_url, patch, messages,
                        request_id, deadline, "proposal_clarification", max_tokens=PLAIN_CHAT_MAX_TOKENS,
                        timeout_limit=timeout))
                else:
                    response = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url,
                        model, messages, PLAIN_CHAT_MAX_TOKENS, timeout,
                        task="proposal_clarification", response_format_override=binary.response_format()), timeout=timeout))
                    if not getattr(response, "choices", None):
                        raise binary.BinaryError("BINARY_RESPONSE_EMPTY", "envelope")
                    if response.choices[0].finish_reason == "length":
                        raise binary.BinaryError("BINARY_RESPONSE_TRUNCATED", "envelope")
                    candidate = parse_json_object(response.choices[0].message.content)
            except asyncio.TimeoutError as error:
                raise _UpstreamBinaryTimeout() from error
            step = binary.parse_step(copy.deepcopy(candidate), topic["questionCount"])
            if step["action"] == "protected_refusal":
                return step, []
            previous_promises = {binary.signature(choice["promise"]) for choice in topic.get("choices", [])}
            if any(binary.signature(b["promise"]) in previous_promises for b in step["branches"]):
                raise binary.BinaryError("BINARY_ALREADY_SELECTED_DIRECTION", "envelope")
            if topic.get("requirements") is None:
                try:
                    normalized_req, norm_notes = normalize_requirement_record(
                        step["initialRequirements"], topic["userTurns"][:1], stage["stageSnapshot"]
                    )
                    initial_record = validate_requirement_record(normalized_req, topic["userTurns"][:1], stage["stageSnapshot"])
                    step["initialRequirements"] = normalized_req
                    if norm_notes:
                        trace_proposal("requirements", "requirements", "normalized", reasonCode=",".join(norm_notes)[:80])
                except (ValueError, TypeError, KeyError) as error:
                    raise binary.BinaryError("BINARY_INITIAL_REQUIREMENTS_INVALID", "initialRequirements") from error
            else:
                if step["initialRequirements"] is not None:
                    raise binary.BinaryError("BINARY_INITIAL_REQUIREMENTS_LOCKED", "initialRequirements")
                initial_record = topic["requirements"]
            stage["requirementRecord"] = initial_record
            text_checks = [("question", step["question"]), ("note", step["note"])] + [
                (f"branch-{index}", branch["label"]) for index, branch in enumerate(step["branches"], start=1)
            ]
            for field, text in text_checks:
                try:
                    _assert_visible_output_language(context["language"], text)
                    _validate_map_grounding_texts([text], context["rows"], entity_bindings=stage.get("entityBindings"))
                except ValueError as error:
                    raise binary.BinaryError("BINARY_TEXT_INVALID", field, detail=str(error)) from error
            binary.validate_binary_step_deterministic(step, topic, context["language"])
            preflight_started = svc.clock.monotonic()
            try:
                caches = binary.preflight(step, topic, context["rows"], stage["stageSnapshot"], validator,
                    deadline, strategy_validator, preflight_budget, clock=svc.clock)
            finally:
                preflight_budget -= svc.clock.monotonic() - preflight_started
            return step, caches
        except LLMServiceError:
            raise
        except (APIConnectionError, APIStatusError, _UpstreamBinaryTimeout) as error:
            if isinstance(error, _UpstreamBinaryTimeout):
                failure = LLMServiceError("UPSTREAM_TIMEOUT", "Kimi did not respond before the timeout.", request_id, True, attempt, 504)
                cause = error.__cause__ or error
                failure_kind = "timeout"
            else:
                failure = classify_exception(error, request_id, attempt)
                cause = error
                failure_kind = "timeout" if failure.code == "UPSTREAM_TIMEOUT" else "upstream"
            if not failure.retryable or attempt == 2:
                failure.details = {"task": "proposal_clarification", "failureStage": "upstream", "failureKind": failure_kind}
                raise failure from cause
            patch = None
            last_error = error
            continue
        except Exception as error:
            last_error = error
            if getattr(error, "field", None) == "review" or attempt == 2:
                break
            field = getattr(error, "field", "branches")
            has_prior_choices = bool(topic.get("choices"))
            allow_plan_switch = (
                has_prior_choices
                and isinstance(candidate, dict)
                and candidate.get("action") == "ask"
                and (
                    field in {"branch-1", "branch-2", "branches"}
                    or getattr(error, "code", "")
                    in {"BINARY_CUMULATIVE_STRATEGY_INVALID", "BINARY_PREFLIGHT_NOT_FOUND", "BINARY_CORRIDOR_INVALID"}
                )
            )
            patch = None
            if not allow_plan_switch and isinstance(candidate, dict) and set(candidate) <= {"action", "question", "note", "branches", "planStrategies", "protectedEntities", "initialRequirements"}:
                if field in {"branch-1", "branch-2"} and len(candidate.get("branches", [])) == 2:
                    patch = PatchRequest("clarification_question", candidate,
                        [Target(field, ("branches", int(field[-1]) - 1), (dict,))],
                        {"snapshot": stage["stageSnapshot"], "topic": evidence["topic"]})
                elif field in {"action", "question", "note", "branches", "planStrategies", "protectedEntities", "initialRequirements"}:
                    patch = PatchRequest("clarification_question", candidate,
                        [Target(field, (field,), (list,) if field in {"branches", "planStrategies", "protectedEntities"} else (dict, type(None)) if field == "initialRequirements" else (str,))],
                        {"snapshot": stage["stageSnapshot"], "topic": evidence["topic"], "initialDesignerTurns": topic["userTurns"][:1]})
            detail_parts = [str(part).strip() for part in (getattr(error, "detail", None), error.__cause__) if part]
            if not detail_parts and str(error).strip():
                detail_parts = [str(error).strip()]
            detail_text = "; ".join(detail_parts)[:300]
            messages.append({"role": "system", "content": "Correct only this technical component: "
                + str(getattr(error, "code", "BINARY_CANDIDATE_INVALID")) + ". Keep the original permissions and choices. "
                + ("Validator detail: " + detail_text + ". " if detail_text else "") +
                "A failed search is not proof that a direction is impossible. Use the same binary schema."})
            if allow_plan_switch:
                messages.append({"role": "system", "content": "If the existing request and topic.selectedChoices already suffice to build "
                    "a verified proposal, prefer returning action='plan', question='', note='', branches=[], initialRequirements=null. "
                    "Otherwise repair BOTH branches to satisfy ALL prior selectedChoices promises plus the new promise on the ORIGINAL "
                    "Stage: set strategy.focus=null, include ALL required operators for prior+new choices, and never preserve any "
                    "component edited by prior or new choices."})
            elif field in {"branch-1", "branch-2"}:
                messages.append({"role": "system", "content": "Keep the repaired branch consistent with the frozen question and note "
                    "(keep its intended component/direction when feasible rather than copying the other branch's component). "
                    "Return ONLY the single branch object {label, strategy, promise} inside valueJson, never the outer {action, question, branches} envelope. "
                    "Check preserve strictly: remove_wall MUST NOT include 'walls'; remove_water MUST NOT include 'water'. "
                    "If widen_corridor failed, switch promise.kind to reduce_obstacles (corridor=null) on the same editable "
                    "component (internal_walls or water) with focus=null or a focus enclosing editable tiles of that component. "
                    "Keep its label and promise materially distinct from the other frozen branch, and never preserve a "
                    "component edited by its operators. "
                    "If BINARY_PREFLIGHT_NOT_FOUND occurred, check editableInternalWallCells and currentStageSnapshot.waterCells: "
                    "walls outside editableInternalWallCells belong to fixed outer_shell and construct 0 candidates. "
                    "Set strategy.focus=null and promise.focus=null (or center focus on an actual cell in editableInternalWallCells/waterCells) and use editBudget 6..10."})
            elif field == "initialRequirements":
                messages.append({"role": "system", "content": "When patching initialRequirements, return ONLY "
                    "{requirements, automaticBindings, exactTransitions}. NEVER include player, box, target, or outer_shell items "
                    "(they are platform-protected). Allowed enums: component in water/wall/gameplay; property in "
                    "count/positions/distribution/distance/change_scope/solutionSteps/solutionPushes/boxAlternations/"
                    "longestPushRun/timing/dependency/experience/appearance; relation in increase/decrease/equal/preserve/"
                    "relocate/nearer/farther/early/later/seek/avoid; strength in invariant/goal/preference; unit in "
                    "absolute/delta/none. entities and automaticBindings may ONLY use exact snapshot IDs like P, B1, T1 "
                    "(never 'player' or 'box'); prefer entities=[] and automaticBindings=[]. "
                    "Every item in requirements MUST include ALL 13 fields: component, property, relation, strength, entities, "
                    "value, unit, priorityEvidenceSpan, focused, sourceTurnId, evidenceSpan, statement, scope "
                    "(copy sourceTurnId verbatim from initialDesignerTurns[0].id and evidenceSpan verbatim from initialDesignerTurns[0].content)."})
            if getattr(error, "code", "") == "BINARY_ALREADY_SELECTED_DIRECTION":
                messages.append({"role": "system", "content": "The proposed question repeats an already selected action. "
                    "Reassess readiness: if the existing request and selection suffice, return action=plan, question='', "
                    "branches=[], initialRequirements=null. Do not invent a fresh preference just to keep asking."})
    code = ("BINARY_PREFLIGHT_BUDGET" if isinstance(last_error, TimeoutError) else
            "BINARY_PREFLIGHT_NOT_FOUND" if isinstance(last_error, ProposalSearchExhausted) else
            getattr(last_error, "code", "BINARY_CANDIDATE_INVALID"))
    failure = LLMServiceError(code, "The two verified directions could not be prepared. Retry or cancel this proposal.",
        request_id, True, attempt, 502)
    failure.details = {"task": "proposal_clarification", "failureStage": "binary_review" if getattr(last_error, "field", None) == "review" else "binary_preflight",
        "failureKind": "budget" if code == "BINARY_PREFLIGHT_BUDGET" else "validation", "maximumAttempts": 2,
        "reasonCode": code}
    if getattr(last_error, "issue_codes", None):
        failure.details["issues"] = last_error.issue_codes
    raise failure from last_error
