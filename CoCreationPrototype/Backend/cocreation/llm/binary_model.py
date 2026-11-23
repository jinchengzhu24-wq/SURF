"""Replaces clarification generation/review slots; never classifies a button click."""
import asyncio
import copy
import json
import re

import cocreation.domain.binary_discovery as binary
from cocreation.domain.design_requirements import REQUIREMENT_SEMANTICS, validate_requirement_record
from cocreation.domain.proposal_diagnostics import trace_proposal
from cocreation.domain.proposal_search import ProposalSearchExhausted
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
        + "Choose action ask/plan/blocked. Ask only about a consequential UNRESOLVED preference; "
        "Every item in topic.selectedChoices has ALREADY BEEN CLICKED by the designer and is locked "
        "interface evidence, not an available option or an unanswered question. Do not ask it again or "
        "reinterpret the click. Implementable cells/amounts are implementation assumptions, not missing preferences. "
        "never ask to fill a quota. plan means the existing request/choices suffice, not that a map "
        "has been changed. When topic.selectedChoices is non-empty and selectedFeasibility.allSelectedActionsPassed is true, "
        "strongly prefer action='plan' (question='', note='', branches=[], initialRequirements=null). "
        "A broad wish (e.g. 'make it easier' / '我想让关卡简单一些') plus ONE selected area/obstacle choice is ALREADY "
        "sufficient to deliver a verified proposal—do NOT ask Q2 about adding a second unrelated obstacle area or extra "
        "edit unless the designer's original request explicitly required multiple separate changes. "
        "At three questions return plan or blocked, never ask. For ask, provide "
        "ONE short question, an optional ONE-sentence note explaining the tradeoff, and EXACTLY "
        "two materially different branches. Each label is a clear short action, lightly playful "
        "when natural (e.g. 少一些水域！/拆掉几堵内墙！/打开中央区域！); no Markdown, letter prefix or long analysis. "
        "Question/note/labels must not repeat a full plan. Length is a preference, not a joke quota. "
        "Use location words rather than coordinate inventories when left/right or opening/central already "
        "identifies the choice. The optional note explains ONLY the key tradeoff. "
        "Only water and internal walls may change. Keep player, boxes, targets and connected outer "
        "shell fixed. Prefer removing obstacles over adding or relocating them when compatible with "
        "the actual request and every preservation constraint: removing blocked terrain retains existing "
        "solutions. Both branches may use removals, but must address genuinely different places or "
        "tradeoffs. Do not reinterpret appearance/redistribution or preservation as permission to reduce "
        "terrain, and do not equate solvability with proven player-perceived difficulty. Keep player, "
        "boxes, targets and connected outer "
        "shell fixed. Initial request, verified requirements and all selected choices stay binding. "
        "Use the current StageSnapshot as the only map authority, never invent play evidence. "
        "Each branch needs one executable local strategy using the existing fields effect, focus "
        "(null or {row,column,radius:1..3}), operators, preserve, editBudget:1..12, metricGoals:[]. "
        "preserve uses only outer_shell/player/boxes/targets/water/walls/unrelated_areas. "
        "NEVER include 'water' in preserve when strategy.operators uses add_water/remove_water OR when any prior choice in topic.selectedChoices edited water. "
        "NEVER include 'walls' in preserve when strategy.operators uses add_wall/remove_wall OR when any prior choice edited internal_walls. "
        "For water-only edits (no prior wall choice): preserve=['outer_shell','player','boxes','targets','walls']. "
        "For wall-only edits (no prior water choice): preserve=['outer_shell','player','boxes','targets','water']. "
        "For CUMULATIVE branches combining prior water + new internal_walls (or vice versa): use effect='open_route', "
        "focus=null, operators=['remove_water','remove_wall'], preserve=['outer_shell','player','boxes','targets']. "
        "When editing with a non-null focus, verify it encloses editable tiles for EVERY active promise; otherwise use focus=null. "
        "CRITICAL WHEN topic.selectedChoices IS NON-EMPTY: the Stage has NOT been modified yet. Every Q2/Q3 branch strategy "
        "runs on the ORIGINAL Stage and MUST realize ALL prior selectedChoices promises PLUS the new branch promise together "
        "(set strategy.focus=null whenever prior and new choices touch different areas, include ALL needed operators for prior+new choices, "
        "and never preserve a component edited by any prior choice). "
        "effect open_route uses remove_wall/remove_water; "
        "reshape_water uses add_water/remove_water; adjust_internal_walls uses add_wall/remove_wall. "
        "Internal coordinates are implementation assumptions, not new user requirements. "
        "Every later strategy must permit the CUMULATIVE patch from the ORIGINAL Stage: use focus=null "
        "when changes are spread out, and editBudget for the entire patch, never a new budget per question. "
        "Ask only about a NEW measurable action, never repeat a selected action or promise 'even more' "
        "using the same original-baseline check. "
        "The promise must match the VISIBLE label/note, not add a hidden purpose or lock. Supported "
        "promises: reduce_obstacles (strictly fewer indicated water/internal walls/obstacles in focus); "
        "widen_corridor (strictly wider WALKABLE cross-section at an existing narrow corridor; give "
        "corridor={row,column,axis:horizontal|vertical} across its WIDTH); shorter_solution (strictly "
        "fewer BFS minimum moves); reshape_water (different water positions with unchanged count). "
        "DEFAULT to reduce_obstacles (component=water with operators=['remove_water'], component=internal_walls with "
        "operators=['remove_wall'], or component=obstacles) whenever a branch removes water or internal walls to open space "
        "or ease a passage; use focus=null unless a specific radius clearly encloses editable tiles of that component. "
        "Avoid widen_corridor unless the designer explicitly asks to widen one specific narrow corridor AND you verify a walkable "
        "cell ('.','p','s','t', NEVER '#' or '@') whose narrow width (horizontal axis for a vertical corridor, vertical axis "
        "for a horizontal corridor; width < perpendicular length >=3) is directly bordered on that same row/column by an "
        "editable tile allowed by operators and inside focus. "
        "promise={kind,component:water|internal_walls|obstacles,focus,corridor}; corridor is null except "
        "for widening. Do not promise aesthetic quality, player difficulty, compulsory routes or "
        "guaranteed experience. Such effects are tentative, while the concrete action must be testable. "
        "Unselected branches and their hidden parameters are hypothetical, never designer evidence. "
        "The server will search and solve BOTH branches before showing buttons. Do not invent a "
        "filler B or two descriptions of the same core change. If there is no pending preference, "
        "plan early. If two responsible directions cannot be offered and a choice is necessary, "
        "use blocked with a short truthful note. For plan/blocked, question='' and branches=[]."
    )
    evidence = {"currentStageSnapshot": stage["stageSnapshot"], "topic": projection,
        "verifiedInitialRequirements": topic.get("requirements"),
        "initialDesignerTurns": topic["userTurns"][:1],
        "designContext": stage.get("revisionDesignContext") or {}}
    system += (" In this SAME output provide initialRequirements using the requirements schema when verifiedInitialRequirements "
        "is absent. Compile only initialDesignerTurns with exact sourceTurnId/evidenceSpan, no branch suggestions as evidence. "
        "automaticBindings only uses snapshot entity labels such as P/B1/T1, never terrain or coordinates; usually []. "
        "For 'make it simpler', use component=gameplay/property=experience/relation=seek/strength=goal. "
        "For 'less water', use component=water/property=count/relation=decrease/strength=goal. "
        "Without a stated number use value=null, unit=none, priorityEvidenceSpan='', entities=[] unless "
        "the designer named a fixed entity. Set priorityEvidenceSpan='' unless the designer explicitly prioritized "
        "one goal over another (e.g. 'most importantly'/'优先'). sourceTurnId is the EXACT supplied designer turn id. "
        "When verifiedInitialRequirements exists, initialRequirements must be null and that record stays locked. "
        "The independent reviewer will verify requirement coverage and meanings together with the branches. " + REQUIREMENT_SEMANTICS)
    system += (" EXACT JSON PROTOCOL, also when the provider falls back to json_object: root contains ONLY "
        "action, question, note, branches, initialRequirements. branches is an array (not an A/B object). "
        "Each branch contains ONLY label, strategy, promise: no id, letter, description, effect or other sibling keys. "
        "strategy contains ONLY effect, focus, operators, preserve, editBudget, metricGoals. "
        "promise contains ONLY kind, component, focus, corridor. The server assigns A/B and IDs. "
        "initialRequirements is null for a locked record, otherwise contains ONLY requirements, automaticBindings, "
        "exactTransitions using the supplied requirements schema. Use empty arrays where appropriate, never omit fields.")
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
            previous_promises = {binary.signature(choice["promise"]) for choice in topic.get("choices", [])}
            if any(binary.signature(b["promise"]) in previous_promises for b in step["branches"]):
                raise binary.BinaryError("BINARY_ALREADY_SELECTED_DIRECTION", "envelope")
            if topic.get("requirements") is None:
                try:
                    initial_record = validate_requirement_record(step["initialRequirements"], topic["userTurns"][:1], stage["stageSnapshot"])
                except (ValueError, TypeError, KeyError) as error:
                    raise binary.BinaryError("BINARY_INITIAL_REQUIREMENTS_INVALID", "initialRequirements") from error
            else:
                if step["initialRequirements"] is not None:
                    raise binary.BinaryError("BINARY_INITIAL_REQUIREMENTS_LOCKED", "initialRequirements")
                initial_record = topic["requirements"]
            # Request-local candidate only; persisted as verified solely after independent review succeeds.
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
            preflight_started = svc.clock.monotonic()
            try:
                caches = binary.preflight(step, topic, context["rows"], stage["stageSnapshot"], validator,
                    deadline, strategy_validator, preflight_budget, clock=svc.clock)
            finally:
                preflight_budget -= svc.clock.monotonic() - preflight_started
            review_input = {"evidence": evidence, "initialRequirementInterpretation": initial_record,
                "step": {k: v for k, v in step.items() if k != "initialRequirements"},
                "validatedBranchDiffs": [{"promiseResults": cache["promiseResults"],
                    "diff": [{"row": y + 1, "column": x + 1, "before": a, "after": b}
                        for y, (left, right) in enumerate(zip(context["rows"], cache["rows"]))
                        for x, (a, b) in enumerate(zip(left, right)) if a != b]}
                    for cache in caches]}
            review_messages = [{"role": "system", "content": (
                "Independently review this binary discovery step against exact initialDesignerTurns and topic.selectedChoices. "
                "Return JSON {accepted:boolean, issues:string[]} using ONLY short uppercase issue CODES from this list when a concrete violation exists, "
                "otherwise return accepted=true and issues=[]. "
                "Allowed codes: ALREADY_SETTLED_CHOICE, DUPLICATE_OR_IDENTICAL_DIRECTIONS, QUESTION_BRANCH_MISMATCH, LABEL_PROMISE_MISMATCH, "
                "PROTECTED_EDIT, UNSUPPORTED_EXPERIENCE_GUARANTEE, INITIAL_REQ_MISSING_STATED_RULE, INITIAL_REQ_INVENTED_LOCK, "
                "INITIAL_REQ_WRONG_EVIDENCE, PLAN_NOT_READY, BLOCKED_UNJUSTIFIED. "
                "WHAT IS VALID (DO NOT REJECT FOR THESE): "
                "(1) For a broad initial request such as 'make it simpler' / '我想让关卡简单一些', a single initialRequirement with "
                "component=gameplay, property=experience, relation=seek, strength=goal, value=null, unit=none is VALID and complete. "
                "Do NOT require fixed/invariant rules in initialRequirementInterpretation unless the designer explicitly stated a preservation or fixed rule in initialDesignerTurns; "
                "platform protections (outer_shell, player, boxes, targets) are enforced by the server and do NOT belong in initialRequirementInterpretation. "
                "(2) For action=ask, proposing two concrete editable water/internal_walls directions to clarify a broad request is the PURPOSE of binary discovery—NOT hidden permission or ungrounded scope. "
                "(3) strategy.focus, strategy.editBudget, strategy.preserve, promise.focus, and validatedBranchDiffs are server-checked HYPOTHETICAL feasibility witnesses (often a minimal 1-cell proof), "
                "NOT numeric locks, NOT user constraints, and labels do not need to state coordinates or cell counts. "
                "(4) A brief note describing spatial/layout differences (e.g. opening water area vs widening wall corridor) is valid as long as it does not guarantee subjective human difficulty/fun or compulsory routes. "
                "(5) Length, tone, and sentence counts are advisory, never rejection reasons. "
                "WHAT TO REJECT: "
                "For action=ask: reject ONLY if a branch repeats topic.selectedChoices, both branches represent the same direction, "
                "question/note clearly contradicts a branch's actual component/action, a label promises an edit to one component/action while its strategy/promise/diff changes a different component/action, "
                "a branch edits protected entities (player/boxes/targets/outer_shell), or visible text makes an unsupported guarantee of player difficulty/compulsory route. "
                "For initialRequirementInterpretation (covering ONLY initialDesignerTurns): reject ONLY if it omits a rule/goal explicitly stated by the designer, "
                "invents a numeric value/lock/invariant not stated in initialDesignerTurns (check value/unit/strength ONLY inside initialRequirementInterpretation, NEVER strategy.editBudget), "
                "misstates component/relation/evidence, or copies branch suggestions into initialRequirementInterpretation. "
                "CONDITIONAL PROTOCOL: the question, two labels/strategies/diffs and new-preference rules apply ONLY to action=ask. "
                "For action=plan or blocked, empty question and branches=[] are REQUIRED and legitimate. "
                "For plan, judge readiness from initial evidence, explicit selections and selectedFeasibility; blocked cannot invent infeasibility. "
                + REQUIREMENT_SEMANTICS)},
                {"role": "user", "content": json.dumps(review_input, ensure_ascii=False)}]
            review_deadline = min(deadline - 20.0, svc.clock.monotonic() + 24.0)
            remaining = min(12.0, review_deadline - svc.clock.monotonic())
            if remaining <= 0:
                raise TimeoutError()
            try:
                response = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url, model,
                    review_messages, 1000, remaining, task="proposal_clarification_review",
                    response_format_override=binary.review_format()), timeout=remaining))
                verdict = (parse_json_object(response.choices[0].message.content)
                    if getattr(response, "choices", None) and response.choices[0].finish_reason != "length" else {})
            except (ValueError, TimeoutError, APIConnectionError, APIStatusError):
                verdict = {}
            if not isinstance(verdict, dict) or set(verdict) - {"accepted", "issues"}:
                raise binary.BinaryError("BINARY_REVIEW_INVALID", "review")
            valid = {key: verdict[key] for key in ("accepted", "issues") if key in verdict and
                (type(verdict[key]) is bool if key == "accepted" else isinstance(verdict[key], list)
                 and all(isinstance(x, str) for x in verdict[key]))}
            if set(valid) != {"accepted", "issues"}:
                retry_remaining = min(12.0, review_deadline - svc.clock.monotonic())
                if retry_remaining <= 0:
                    raise binary.BinaryError("BINARY_REVIEW_INVALID", "review")
                try:
                    if valid and not (valid.get("accepted") is False and "issues" not in valid):
                        repair = PatchRequest("clarification_review", valid,
                            [Target(key, (key,), (bool,) if key == "accepted" else (list,))
                                for key in ("accepted", "issues") if key not in valid], {"review_source": review_input})
                        verdict = asyncio.run(_request_component_patch(api_key, base_url, repair, review_messages,
                            request_id, review_deadline, "proposal_clarification_review", max_tokens=1000,
                            timeout_limit=retry_remaining))
                    else:
                        retry_response = asyncio.run(asyncio.wait_for(_request_completion(api_key, base_url, model,
                            review_messages, 1000, retry_remaining, task="proposal_clarification_review",
                            response_format_override=binary.review_format()), timeout=retry_remaining))
                        if not getattr(retry_response, "choices", None) or retry_response.choices[0].finish_reason == "length":
                            raise ValueError()
                        verdict = parse_json_object(retry_response.choices[0].message.content)
                    if (not isinstance(verdict, dict) or set(verdict) != {"accepted", "issues"}
                            or type(verdict["accepted"]) is not bool or not isinstance(verdict["issues"], list)
                            or any(not isinstance(x, str) for x in verdict["issues"])):
                        raise ValueError()
                except (ValueError, TimeoutError, APIConnectionError, APIStatusError):
                    raise binary.BinaryError("BINARY_REVIEW_INVALID", "review")
            if verdict["accepted"] is not True or verdict["issues"]:
                error = binary.BinaryError("BINARY_SEMANTIC_REJECTED", "review")
                error.issue_codes = [x for x in verdict["issues"] if re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", x)][:8]
                trace_proposal(
                    "binary_review",
                    "review",
                    "rejected",
                    "BINARY_SEMANTIC_REJECTED",
                    attempt=attempt,
                    issues=[
                        {"fieldPath": "review", "reasonCode": code, "sourceKind": "server"}
                        for code in error.issue_codes
                        if re.fullmatch(r"[A-Z_]{1,80}", code)
                    ],
                    issueCount=len(error.issue_codes),
                )
                raise error
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
            if not allow_plan_switch and isinstance(candidate, dict) and set(candidate) <= {"action", "question", "note", "branches", "initialRequirements"}:
                if field in {"branch-1", "branch-2"} and len(candidate.get("branches", [])) == 2:
                    patch = PatchRequest("clarification_question", candidate,
                        [Target(field, ("branches", int(field[-1]) - 1), (dict,))],
                        {"snapshot": stage["stageSnapshot"], "topic": evidence["topic"]})
                elif field in {"action", "question", "note", "branches", "initialRequirements"}:
                    patch = PatchRequest("clarification_question", candidate,
                        [Target(field, (field,), (list,) if field == "branches" else (dict, type(None)) if field == "initialRequirements" else (str,))],
                        {"snapshot": stage["stageSnapshot"], "topic": evidence["topic"]})
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
                    "component edited by its operators."})
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
