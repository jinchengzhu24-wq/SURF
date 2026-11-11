"""Replaces clarification generation/review slots; never classifies a button click."""
import asyncio
import copy
import json
import re
import time

import binary_discovery as binary
from component_patches import PatchRequest, Target
from proposal_search import ProposalSearchExhausted


def generate(topic, context, validator, strategy_validator, request_id, deadline, preflight_budget=20.0):
    import llm_client as llm
    from design_requirements import REQUIREMENT_SEMANTICS, validate_requirement_record
    api_key, base_url = llm._llm_credentials()
    if not api_key:
        raise llm.LLMServiceError("CONFIGURATION_ERROR", "Kimi is unavailable.", request_id, False, 0, 503)
    stage = context["stageContext"]
    projection = {key: topic.get(key) for key in ("initialRequest", "questionCount", "understanding")}
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
        "has been changed. After a branch is selected and allSelectedActionsPassed is true, default "
        "to plan unless a genuinely unresolved DESIGNER preference prevents faithful delivery. A simple "
        "request plus a selected area/action usually suffices: do not ask for extra amounts, another area "
        "or a different design dimension merely because questions remain. "
        "has been changed. At three questions return plan or blocked, never ask. For ask, provide "
        "ONE short question, an optional ONE-sentence note explaining the tradeoff, and EXACTLY "
        "two materially different branches. Each label is a clear short action, lightly playful "
        "when natural (e.g. 少一些障碍！/通道宽一点！); no Markdown, letter prefix or long analysis. "
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
        "preserve uses only outer_shell/player/boxes/targets/water/walls/unrelated_areas; never preserve "
        "water while requesting its removal. effect open_route uses remove_wall/remove_water; "
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
        "the designer named a fixed entity. sourceTurnId is the EXACT supplied designer turn id. "
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
            timeout = min(70.0, deadline - time.monotonic() - 20.0)
            if timeout <= 0:
                raise TimeoutError()
            if patch is not None:
                candidate = asyncio.run(llm._request_component_patch(api_key, base_url, patch, messages,
                    request_id, deadline, "proposal_clarification", max_tokens=llm.PLAIN_CHAT_MAX_TOKENS,
                    timeout_limit=timeout))
            else:
                response = asyncio.run(asyncio.wait_for(llm._request_completion(api_key, base_url,
                    llm.KIMI_MODEL, messages, llm.PLAIN_CHAT_MAX_TOKENS, timeout,
                    task="proposal_clarification", response_format_override=binary.response_format()), timeout=timeout))
                if response.choices[0].finish_reason == "length":
                    raise binary.BinaryError("BINARY_RESPONSE_TRUNCATED", "envelope")
                candidate = llm.parse_json_object(response.choices[0].message.content)
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
            for text in (step["question"], step["note"], *[b["label"] for b in step["branches"]]):
                llm._assert_visible_output_language(context["language"], text)
                llm._validate_map_grounding_texts([text], context["rows"], entity_bindings=stage.get("entityBindings"))
            preflight_started = time.monotonic()
            try:
                caches = binary.preflight(step, topic, context["rows"], stage["stageSnapshot"], validator,
                    deadline, strategy_validator, preflight_budget)
            finally:
                preflight_budget -= time.monotonic() - preflight_started
            review_input = {"evidence": evidence, "initialRequirementInterpretation": initial_record,
                "step": {k: v for k, v in step.items() if k != "initialRequirements"},
                "validatedBranchDiffs": [{"promiseResults": cache["promiseResults"],
                    "diff": [{"row": y + 1, "column": x + 1, "before": a, "after": b}
                        for y, (left, right) in enumerate(zip(context["rows"], cache["rows"]))
                        for x, (a, b) in enumerate(zip(left, right)) if a != b]}
                    for cache in caches]}
            review_messages = [{"role": "system", "content": (
                "Independently review binary discovery against exact designer evidence and selected choices. "
                "Return accepted:boolean and issues:string[]. Use short issue CODES only, no analysis or rewritten prose. "
                "For action=ask, verify one new unresolved preference; both labels must describe their actual different strategies and validated diffs, "
                "and every promise is visible in the label/note. Reject hidden permissions, goals, quantities, "
                "locks, invented map facts, unsupported guarantees or previously settled questions. "
                "Only water/internal walls may change; referents never authorize protected edits. "
                "Implementation coordinates/budgets are hypothetical choices, not new user constraints. "
                "plan is valid only when existing evidence suffices; blocked must not claim infeasibility "
                "from missing evidence. Length, humor and sentence counts are advisory, not rejection reasons. "
                "Preflight diff facts are hypothetical validated candidates, never the current map. "
                "Do not rewrite the candidate or reinterpret a button click.")},
                {"role": "user", "content": json.dumps(review_input, ensure_ascii=False)}]
            review_messages[0]["content"] += (" Verify every initial requirement against exact initialDesignerTurns: "
                "reject missing fixed rules or active goals, invented numeric locks, wrong component/relation/evidence, "
                "or branch suggestions treated as designer requirements. The initial record covers ONLY the initial "
                "designer turn and stays locked later; selected interface actions are checked separately, then compiled "
                "by the final requirements pipeline. Do not demand that later choices be inserted into this initial record. "
                "CONDITIONAL PROTOCOL: the question, two labels/strategies/diffs and new-preference rules apply ONLY "
                "to action=ask. For action=plan or blocked, empty question and branches=[] are REQUIRED and legitimate; "
                "do not reject them for having no question/options. For plan, judge readiness from initial evidence, "
                "the explicit selections and selectedFeasibility. An already selected feasible action answering a simple "
                "request can proceed without another preference. blocked cannot invent infeasibility. " + REQUIREMENT_SEMANTICS)
            review_deadline = min(deadline - 20.0, time.monotonic() + 10.0)
            remaining = min(8.0, review_deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError()
            try:
                response = asyncio.run(asyncio.wait_for(llm._request_completion(api_key, base_url, llm.KIMI_MODEL,
                    review_messages, 1000, remaining, task="proposal_clarification_review",
                    response_format_override=binary.review_format()), timeout=remaining))
                verdict = llm.parse_json_object(response.choices[0].message.content) if response.choices[0].finish_reason != "length" else {}
            except (ValueError, TimeoutError, llm.APIConnectionError, llm.APIStatusError):
                verdict = {}
            if not isinstance(verdict, dict) or set(verdict) - {"accepted", "issues"}:
                raise binary.BinaryError("BINARY_REVIEW_INVALID", "review")
            valid = {key: verdict[key] for key in ("accepted", "issues") if key in verdict and
                (type(verdict[key]) is bool if key == "accepted" else isinstance(verdict[key], list)
                 and all(isinstance(x, str) for x in verdict[key]))}
            if set(valid) != {"accepted", "issues"}:
                repair = PatchRequest("clarification_review", valid,
                    [Target(key, (key,), (bool,) if key == "accepted" else (list,))
                        for key in ("accepted", "issues") if key not in valid], {"review_source": review_input})
                try:
                    verdict = asyncio.run(llm._request_component_patch(api_key, base_url, repair, review_messages,
                        request_id, review_deadline, "proposal_clarification_review", max_tokens=1000, timeout_limit=2.0))
                    if (type(verdict["accepted"]) is not bool or not isinstance(verdict["issues"], list)
                            or any(not isinstance(x, str) for x in verdict["issues"])):
                        raise ValueError()
                except (ValueError, TimeoutError, llm.APIConnectionError, llm.APIStatusError):
                    raise binary.BinaryError("BINARY_REVIEW_INVALID", "review")
            if verdict["accepted"] is not True or verdict["issues"]:
                error = binary.BinaryError("BINARY_SEMANTIC_REJECTED", "review")
                error.issue_codes = [x for x in verdict["issues"] if re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", x)][:8]
                raise error
            return step, caches
        except llm.LLMServiceError:
            raise
        except (llm.APIConnectionError, llm.APIStatusError) as error:
            failure = llm.classify_exception(error, request_id, attempt)
            if not failure.retryable or attempt == 2:
                failure.details = {"task": "proposal_clarification", "failureStage": "upstream", "failureKind": "upstream"}
                raise failure from error
            last_error = error
            continue
        except Exception as error:
            last_error = error
            if getattr(error, "field", None) == "review" or attempt == 2:
                break
            if isinstance(candidate, dict) and set(candidate) <= {"action", "question", "note", "branches", "initialRequirements"}:
                field = getattr(error, "field", "branches")
                if field in {"branch-1", "branch-2"} and len(candidate.get("branches", [])) == 2:
                    patch = PatchRequest("clarification_question", candidate,
                        [Target(field, ("branches", int(field[-1]) - 1), (dict,))],
                        {"snapshot": stage["stageSnapshot"], "topic": evidence["topic"]})
                elif field in {"action", "question", "note", "branches", "initialRequirements"}:
                    patch = PatchRequest("clarification_question", candidate,
                        [Target(field, (field,), (list,) if field == "branches" else (dict, type(None)) if field == "initialRequirements" else (str,))],
                        {"snapshot": stage["stageSnapshot"], "topic": evidence["topic"]})
            messages.append({"role": "system", "content": "Correct only this technical component: "
                + str(getattr(error, "code", "BINARY_CANDIDATE_INVALID")) + ". Keep the original permissions and choices. "
                + ("Validator detail: " + str(error.__cause__)[:300] + ". " if error.__cause__ else "") +
                "A failed search is not proof that a direction is impossible. Use the same binary schema."})
            if getattr(error, "code", "") == "BINARY_ALREADY_SELECTED_DIRECTION":
                messages.append({"role": "system", "content": "The proposed question repeats an already selected action. "
                    "Reassess readiness: if the existing request and selection suffice, return action=plan, question='', "
                    "branches=[], initialRequirements=null. Do not invent a fresh preference just to keep asking."})
    code = ("BINARY_PREFLIGHT_BUDGET" if isinstance(last_error, TimeoutError) else
            "BINARY_PREFLIGHT_NOT_FOUND" if isinstance(last_error, ProposalSearchExhausted) else
            getattr(last_error, "code", "BINARY_CANDIDATE_INVALID"))
    failure = llm.LLMServiceError(code, "The two verified directions could not be prepared. Retry or cancel this proposal.",
        request_id, True, attempt, 502)
    failure.details = {"task": "proposal_clarification", "failureStage": "binary_review" if getattr(last_error, "field", None) == "review" else "binary_preflight",
        "failureKind": "budget" if code == "BINARY_PREFLIGHT_BUDGET" else "validation", "maximumAttempts": 2,
        "reasonCode": code}
    if getattr(last_error, "issue_codes", None):
        failure.details["issues"] = last_error.issue_codes
    raise failure from last_error
