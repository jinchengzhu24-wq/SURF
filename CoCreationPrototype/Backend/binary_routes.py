"""Durable choice receipts and current-Stage CAS around the existing planning pipeline."""
import copy
import json
import time
import uuid

import binary_discovery as binary
import binary_model
import llm_client as llm
from design_requirements import evaluate_requirements, preflight_requirements, protected_change_requested
from level_validation import validate_and_solve
from proposal_search import parse_revision_plan
from revision_workflow import SemanticConstraintError


def should_start(payload, understanding, context):
    stage = context["stageContext"]
    if payload.action != "none" or stage.get("proposalDiscovery") or stage.get("activeDisagreement"):
        return False
    if not payload.requestProposal and "revision_request" not in (understanding or {}).get("acts", []):
        return False
    if protected_change_requested(understanding or {}, False):
        return False
    for item in (understanding or {}).get("changes", []):
        if item.get("operation") == "change" and item.get("component") in {"player", "player_start", "boxes", "targets", "outer_shell"}:
            return False  # Keep the established protected-request explanation.
    if (understanding or {}).get("conversationOnly"):
        return False
    return True


def _guard(B, database, session_id, access_cookie, version_id):
    session = B.require_active_session(database, session_id, access_cookie)
    B.require_current_base(session, version_id)
    design = B.load_design_context(database, session_id, version_id) or {}
    if (design.get("activeDisagreement") or {}).get("status") == "active":
        raise B.ApiError(409, "DISAGREEMENT_ACTIVE", "Resolve the current disagreement first.")
    if database.execute("SELECT 1 FROM change_proposals WHERE session_id=? AND status='pending' LIMIT 1", (session_id,)).fetchone():
        raise B.ApiError(409, "PROPOSAL_PENDING", "Review the pending map before starting another proposal.")
    return session


def _save(B, database, session_id, topic):
    B.record_event(database, session_id, "binary_discovery_state", topic, B.utc_now())


def _receipt(database, session_id, key):
    row = database.execute("SELECT payload_json FROM audit_events WHERE session_id=? AND event_type='binary_discovery_receipt' "
        "AND json_extract(payload_json,'$.key')=? ORDER BY id DESC LIMIT 1", (session_id, key)).fetchone()
    return json.loads(row["payload_json"]) if row else None


def _put_receipt(B, database, session_id, key, data, status):
    B.record_event(database, session_id, "binary_discovery_receipt", {"key": key, "request": data, "status": status}, B.utc_now())


def _context(B, database, session_id, topic):
    version = B.get_version(database, session_id, topic["versionId"])
    context = B.build_llm_context(database, session_id, version)
    context["language"] = database.execute("SELECT language FROM design_sessions WHERE id=?", (session_id,)).fetchone()["language"]
    stage = context["stageContext"]
    stage.update(requirementPolicyVersion=1, aiEditableTilesOnly=True,
        binaryDiscovery=True,
        requirementUserTurns=topic["userTurns"], responseLanguage=context["language"],
        binaryPromises=[choice["promise"] for choice in topic["choices"]],
        binaryChoices=copy.deepcopy(topic["choices"]))
    # Never send the private candidate maps/old snapshot projections to Kimi.
    stage["proposalDiscovery"] = {"topicId": topic["topicId"], "versionId": topic["versionId"],
        "initialRequest": topic["initialRequest"], "understanding": topic["understanding"],
        "userTurns": topic["userTurns"], "clarificationQuestionCount": topic["questionCount"],
        "answers": [{"question": c["question"], "answerText": c["label"], "turnId": c["sourceTurnId"]} for c in topic["choices"]]}
    stage["revisionRouting"] = "proposal"
    stage["authorizedRevisionBrief"] = topic["initialRequest"]
    return context


def _check_binding(B, context, topic):
    actual = binary.binding(context["stageContext"]["stageSnapshot"], topic["initialRequest"],
        context["stageContext"].get("revisionDesignContext") or {})
    if actual != topic["binding"]:
        raise B.ApiError(409, "PROPOSAL_STALE", "The Stage or design constraints changed. Start a new proposal.")


def start(B, session_id, payload, content, understanding, request, access_cookie):
    data = {"kind": "start", "baseVersionId": payload.baseVersionId, "content": content,
        "requestProposal": payload.requestProposal}
    with B.connect(immediate=True) as database:
        B.require_browser_session(database, session_id, access_cookie)
        receipt = _receipt(database, session_id, payload.idempotencyKey)
        if receipt:
            if receipt["request"] != data:
                raise B.ApiError(409, "IDEMPOTENCY_CONFLICT", "This key belongs to another action.")
            if receipt["status"] == "done":
                return B.serialize_session(database, session_id)
        session = _guard(B, database, session_id, access_cookie, payload.baseVersionId)
        topic = binary.current_topic(database, session_id, payload.baseVersionId)
        if receipt and (not topic or topic["initialMessageKey"] != payload.idempotencyKey or topic["status"] not in binary.ACTIVE):
            raise B.ApiError(409, "PROPOSAL_STALE", "This proposal has ended; start with a new message key.")
        if topic and topic["status"] in binary.ACTIVE:
            if topic["initialMessageKey"] != payload.idempotencyKey:
                raise B.ApiError(409, "PROPOSAL_CHOICE_REQUIRED", "Use the current choice buttons or cancel.")
            if topic["status"] == "planning":
                raise B.ApiError(409, "BINARY_BUSY", "This request is still being processed.")
        else:
            prior = database.execute("SELECT id FROM conversation_turns WHERE session_id=? AND request_id=? AND role='user'",
                (session_id, payload.idempotencyKey)).fetchone()
            user_id = prior["id"] if prior else B.insert_turn(database, session, "user", content, payload.baseVersionId, payload.idempotencyKey, None)
            context = B.build_llm_context(database, session_id, B.get_current_version(database, session))
            topic = {"discoveryPolicyVersion": 2, "topicId": "binary-" + uuid.uuid4().hex,
                "versionId": payload.baseVersionId, "initialMessageKey": payload.idempotencyKey,
                "initialRequest": content, "understanding": understanding.get("proposalUnderstanding") or {},
                "userTurns": [{"id": user_id, "content": content}], "choices": [], "questionCount": 0,
                "language": session["language"], "pendingQuestion": None, "status": "planning"}
            topic["binding"] = binary.binding(context["stageContext"]["stageSnapshot"], content,
                context["stageContext"].get("revisionDesignContext") or {})
            B.record_event(database, session_id, "turn_understanding", {"messageKey": payload.idempotencyKey,
                "baseVersionId": payload.baseVersionId, "result": understanding}, B.utc_now())
        topic["status"] = "planning"
        topic["leaseUntil"] = time.time() + max(1, request.state.llm_deadline - time.monotonic()) + 5
        topic["retryAction"] = {"kind": "start", "idempotencyKey": payload.idempotencyKey,
            "baseVersionId": payload.baseVersionId, "content": content, "requestProposal": payload.requestProposal}
        _put_receipt(B, database, session_id, payload.idempotencyKey, data, "pending")
        _save(B, database, session_id, topic)
    return _advance(B, session_id, topic, payload.idempotencyKey, data, request, access_cookie)


def choose(B, session_id, payload, request, access_cookie):
    data = {"kind": "choice", **payload.model_dump()}
    with B.connect(immediate=True) as database:
        B.require_browser_session(database, session_id, access_cookie)
        receipt = _receipt(database, session_id, payload.idempotencyKey)
        if receipt:
            if receipt["request"] != data:
                raise B.ApiError(409, "IDEMPOTENCY_CONFLICT", "This key belongs to another choice.")
            if receipt["status"] == "done":
                return B.serialize_session(database, session_id)
        session = _guard(B, database, session_id, access_cookie, payload.baseVersionId)
        topic = binary.current_topic(database, session_id, payload.baseVersionId)
        if not topic or topic["topicId"] != payload.topicId or topic["status"] not in binary.ACTIVE:
            raise B.ApiError(409, "PROPOSAL_STALE", "This topic is no longer active.")
        context = _context(B, database, session_id, topic)
        _check_binding(B, context, topic)
        question = topic.get("pendingQuestion") or {}
        if question.get("questionId") != payload.questionId:
            raise B.ApiError(409, "PROPOSAL_STALE", "Answer the current question.")
        if receipt is None:
            if topic["status"] != "clarifying" or any(c["questionId"] == payload.questionId for c in topic["choices"]):
                raise B.ApiError(409, "PROPOSAL_STALE", "This question was already answered.")
            branch = next((item for item in question["choices"] if item["choiceId"] == payload.choiceId), None)
            if branch is None:
                raise B.ApiError(409, "PROPOSAL_STALE", "This option does not belong to the question.")
            try:
                presentation = displayed_question(database, session_id, question, payload.displayLanguage, topic["language"])
            except ValueError as error:
                raise B.ApiError(409, "PROPOSAL_TRANSLATION_STALE", "Refresh to use the verified source-language buttons.") from error
            label = next(item["label"] for item in presentation["choices"] if item["choiceId"] == payload.choiceId)
            text = ("选择 " if payload.displayLanguage == "zh-CN" else "Selected ") + branch["letter"] + ": " + label
            user_id = B.insert_turn(database, session, "user", text, payload.baseVersionId, payload.idempotencyKey, None)
            cache = copy.deepcopy(branch["candidate"])
            if (cache["topicBinding"] != topic["binding"] or cache["prefix"] != binary.path_signature(topic["choices"])
                    or cache.get("proofSignature") != binary.signature({k: v for k, v in cache.items() if k != "proofSignature"})):
                raise B.ApiError(409, "PROPOSAL_STALE", "The feasibility proof changed.")
            choice = {"questionId": payload.questionId, "choiceId": payload.choiceId,
                "question": presentation["question"], "note": presentation["note"], "label": label,
                "letter": branch["letter"], "promise": branch["promise"], "sourceTurnId": user_id,
                "displayLanguage": payload.displayLanguage}
            topic["choices"].append(choice)
            topic["userTurns"].append({"id": user_id, "content": text})
            topic["selectedCandidate"] = cache
            topic["selectedCandidate"]["selectedPrefix"] = binary.path_signature(topic["choices"])
            B.record_event(database, session_id, "binary_choice_selected", {"topicId": topic["topicId"],
                "questionId": payload.questionId, "choiceId": payload.choiceId, "sourceTurnId": user_id,
                "evidenceKind": "interface_choice", "displayLanguage": payload.displayLanguage}, B.utc_now())
        elif topic["status"] == "planning":
            raise B.ApiError(409, "BINARY_BUSY", "This choice is still being processed.")
        topic["status"] = "planning"
        topic["leaseUntil"] = time.time() + max(1, request.state.llm_deadline - time.monotonic()) + 5
        topic["retryAction"] = {"kind": "choice", **payload.model_dump()}
        _put_receipt(B, database, session_id, payload.idempotencyKey, data, "pending")
        _save(B, database, session_id, topic)
    return _advance(B, session_id, topic, payload.idempotencyKey, data, request, access_cookie)


def cancel(B, session_id, payload, access_cookie):
    data = {"kind": "cancel", **payload.model_dump()}
    with B.connect(immediate=True) as database:
        B.require_browser_session(database, session_id, access_cookie)
        receipt = _receipt(database, session_id, payload.idempotencyKey)
        if receipt:
            if receipt["request"] != data:
                raise B.ApiError(409, "IDEMPOTENCY_CONFLICT", "This key belongs to another action.")
            return B.serialize_session(database, session_id)
        session = _guard(B, database, session_id, access_cookie, payload.baseVersionId)
        topic = binary.current_topic(database, session_id, payload.baseVersionId)
        if not topic or topic["topicId"] != payload.topicId:
            raise B.ApiError(409, "PROPOSAL_STALE", "This topic is no longer active.")
        if topic["status"] == "planning":
            raise B.ApiError(409, "BINARY_BUSY", "Wait for the current request before cancelling.")
        if topic["status"] not in binary.ACTIVE:
            raise B.ApiError(409, "PROPOSAL_STALE", "This topic has ended.")
        topic.update(status="cancelled", retryAction=None)
        _save(B, database, session_id, topic)
        _put_receipt(B, database, session_id, payload.idempotencyKey, data, "done")
        return B.serialize_session(database, session_id)


def displayed_question(database, session_id, question, language, source_language):
    view = {key: question[key] for key in ("question", "note")}
    view["choices"] = [{"choiceId": c["choiceId"], "label": c["label"]} for c in question["choices"]]
    if language == source_language:
        return view
    # Only complete, independently verified turn translations can supply labels.
    row = database.execute("SELECT guidance_json FROM turn_translations WHERE turn_id=? AND language=?",
        (question.get("turnId"), language)).fetchone()
    translated = (json.loads(row["guidance_json"] or "{}") if row else {}).get("proposalQuestion")
    if not translated or translated.get("questionId") != question["questionId"]:
        raise ValueError("BINARY_TRANSLATION_UNAVAILABLE")
    if {c["choiceId"] for c in translated.get("choices", [])} != {c["choiceId"] for c in view["choices"]}:
        raise ValueError("BINARY_TRANSLATION_UNAVAILABLE")
    return translated


def _advance(B, session_id, topic, key, receipt_data, request, access_cookie):
    deadline = request.state.llm_deadline
    phase = "binary_preflight"
    try:
        with B.connect() as database:
            _guard(B, database, session_id, access_cookie, topic["versionId"])
            context = _context(B, database, session_id, topic)
        _check_binding(B, context, topic)
        stage = context["stageContext"]
        stage["requirementRecord"] = topic.get("requirements")
        preflight_started = time.monotonic()
        preflight_cutoff = min(deadline - 20.0, preflight_started + 20.0)
        baseline = validate_and_solve(context["rows"], deadline=preflight_cutoff)
        baseline_features = llm._proposal_route_features(context["rows"], baseline, stage.get("entityBindings"), deadline=preflight_cutoff)
        context["validation"]["minimumPushes"] = baseline_features.get("minimumPushes")
        preflight_budget = 20.0 - (time.monotonic() - preflight_started)
        def validator(rows, cutoff):
            llm.validate_ai_edit_diff(context["rows"], rows)
            validation = validate_and_solve(rows, deadline=cutoff)
            after = llm._proposal_route_features(rows, validation, stage.get("entityBindings"), deadline=cutoff)
            outcomes = evaluate_requirements(context["rows"], rows, stage["requirementRecord"], baseline_features, after)
            if any(not item["passed"] for item in outcomes):
                raise SemanticConstraintError(outcomes)
            binary.check_promises(context["rows"], rows, [c["promise"] for c in topic["choices"]], baseline, validation)
            return validation
        def strategy_validator(rows, strategy):
            plan = parse_revision_plan({"strategies": [strategy]})
            contract = llm._build_revision_execution_contract(plan, topic["initialRequest"], stage)
            operations = _operations(context["rows"], rows)
            if list(llm.execute_revision_operations(context["rows"], operations, contract, 1)) != list(rows):
                raise ValueError("BINARY_EXECUTION_MISMATCH")
        if topic["questionCount"] >= 3:
            step, caches = {"action": "plan", "note": "", "question": "", "branches": []}, []
        else:
            step, caches = binary_model.generate(topic, context, validator, strategy_validator, request.state.request_id, deadline, preflight_budget)
            # The generator's existing independent review has accepted this record and both directions.
            if topic.get("requirements") is None:
                from design_requirements import validate_requirement_record
                topic["requirements"] = validate_requirement_record(step["initialRequirements"], topic["userTurns"][:1], stage["stageSnapshot"])
            stage["requirementRecord"] = topic["requirements"]
        if preflight_requirements(context["rows"], topic["requirements"]):
            raise B.ApiError(422, "REQUIREMENT_PERMISSION_CONFLICT", "The request conflicts with fixed map permissions.")
        if step["action"] == "ask":
            question_id = "question-" + uuid.uuid4().hex
            topic["questionCount"] += 1
            question = {"questionId": question_id, "number": topic["questionCount"],
                "question": step["question"], "note": step["note"], "choices": []}
            for index, (branch, cache) in enumerate(zip(step["branches"], caches)):
                cache["proofSignature"] = binary.signature(cache)
                question["choices"].append({"choiceId": "choice-" + uuid.uuid4().hex, "letter": "AB"[index],
                    "label": branch["label"], "promise": branch["promise"], "candidate": cache})
            topic.update(status="clarifying", pendingQuestion=question, retryAction=None, failureCode=None)
            public = binary.public_question(topic)
            body = "\n\n".join(part for part in [question["question"], question["note"],
                "\n".join(c["letter"] + ": " + c["label"] for c in question["choices"])] if part)
            execution = llm.LLMExecutionResult(body, 1, request.state.request_id, model=llm.KIMI_MODEL,
                guidance={"proposalQuestion": public, "move": "clarify_intent"})
        elif step["action"] == "blocked":
            raise binary.BinaryError("BINARY_DIRECTIONS_UNAVAILABLE")
        else:
            phase = "binary_finalization"
            execution = _finalize(B, topic, context, validator, request, deadline)
            topic.update(status="proposal_ready", retryAction=None, failureCode=None)
        with B.connect(immediate=True) as database:
            session = _guard(B, database, session_id, access_cookie, topic["versionId"])
            current = binary.current_topic(database, session_id, topic["versionId"])
            if not current or current["topicId"] != topic["topicId"] or current["status"] != "planning" or current.get("retryAction", {}).get("idempotencyKey") != key:
                raise B.ApiError(409, "PROPOSAL_STALE", "The active question changed while preparing the reply.")
            _check_binding(B, _context(B, database, session_id, topic), topic)
            turn_id = B.insert_turn(database, session, "assistant", execution.assistant_message, topic["versionId"], key, execution)
            if step["action"] == "ask":
                topic["pendingQuestion"]["turnId"] = turn_id
            _save(B, database, session_id, topic)
            B.record_event(database, session_id, "binary_question_verified" if step["action"] == "ask" else "binary_offer_verified", {
                "topicId": topic["topicId"], "baseVersionId": topic["versionId"], "questionCount": topic["questionCount"],
                "selectedActions": len(topic["choices"]), "branchPreflightMs": [c["preflightMs"] for c in caches],
                "branchCount": len(caches), "policyVersion": binary.POLICY_VERSION}, B.utc_now())
            _put_receipt(B, database, session_id, key, receipt_data, "done")
            result = B.serialize_session(database, session_id)
        try:
            B.synchronize_turn_with_online_match(session_id, key)
        except Exception:
            # The committed receipt remains authoritative; synchronization is retried separately.
            pass
        return result
    except Exception as error:
        if isinstance(error, B.ApiError) and error.status_code == 409:
            raise
        if isinstance(error, (B.ApiError, llm.LLMServiceError)):
            failure = error
        else:
            code = ("BINARY_PREFLIGHT_BUDGET" if phase == "binary_preflight" else "BINARY_PLANNING_BUDGET") if isinstance(error, TimeoutError) else getattr(error, "code", "BINARY_VALIDATION_FAILED")
            failure = llm.LLMServiceError(code, "Verified directions are not ready. Retry or cancel this proposal.",
                request.state.request_id, True, 1, 502)
            failure.details = {"task": "proposal_clarification" if phase == "binary_preflight" else "revision_plan",
                "failureStage": phase, "reasonCode": code}
        with B.connect(immediate=True) as database:
            session = B.require_browser_session(database, session_id, access_cookie)
            current = binary.current_topic(database, session_id, topic["versionId"])
            if session["current_version_id"] == topic["versionId"] and current and current["topicId"] == topic["topicId"] and current["status"] == "planning":
                current.update(status="retry_pending" if getattr(failure, "retryable", False) else "revision_needed", failureCode=failure.code)
                if topic.get("requirements") is not None:
                    current["requirements"] = topic["requirements"]
                _save(B, database, session_id, current)
                B.record_event(database, session_id, "message_generation_failed", {"messageKey": key,
                    "baseVersionId": topic["versionId"], "code": failure.code, "attemptsUsed": getattr(failure, "attempts_used", 1),
                    "requestId": request.state.request_id, "proposalRequested": True, "retryable": getattr(failure, "retryable", False),
                    **{field: (getattr(failure, "details", None) or {}).get(field) for field in ("task", "failureStage", "reasonCode")}}, B.utc_now())
        raise failure


def _operations(base, rows):
    return [{"row": y + 1, "column": x + 1, "to": b} for y, (left, right) in enumerate(zip(base, rows))
        for x, (a, b) in enumerate(zip(left, right)) if a != b]


def _finalize(B, topic, context, validator, request, deadline):
    stage = context["stageContext"]
    stage.setdefault("_reliabilityState", {})
    stage["binaryChoices"] = topic["choices"]
    stage["binaryPromises"] = [c["promise"] for c in topic["choices"]]
    try:
        execution = llm._generate_revision_search_proposal_sync(api_key=llm._llm_credentials()[0], base_url=llm._llm_credentials()[1],
            conversation=[{"role": "user", "content": t["content"]} for t in topic["userTurns"]],
            rows=context["rows"], request_id=request.state.request_id, language=context["language"],
            proposal_validator=lambda rows: validator(rows, deadline), stage_context=stage,
            baseline_metrics=context["validation"], deadline=deadline)
    except llm.LLMServiceError as error:
        if error.code not in {"PROPOSAL_SEARCH_EXHAUSTED", "DETERMINISTIC_SEARCH_EXHAUSTED", "CANDIDATE_UNSOLVABLE", "SEMANTIC_CONSTRAINT_NOT_MET", "HARD_OBJECTIVE_NOT_MET"}:
            raise
        verified = stage.get("_reliabilityState", {}).get("verifiedRequirements")
        cache = topic.get("selectedCandidate")
        if not verified or not cache or cache.get("topicBinding") != topic["binding"] or cache.get("selectedPrefix") != binary.path_signature(topic["choices"]):
            raise
        stage["requirementRecord"] = verified["record"]
        plan = parse_revision_plan({"strategies": [cache["strategy"]]})
        contract = llm._build_revision_execution_contract(plan, topic["initialRequest"], stage)
        execution = llm.LLMExecutionResult("", error.attempts_used, request.state.request_id, model="verified-binary-cache",
            proposed_rows=cache["rows"], revision_contract=contract, revision_plan=plan.as_dict(),
            revision_operations=_operations(context["rows"], cache["rows"]), proposal_diagnostics={"selectedStrategyIndex": 1})
    validation = validator(execution.proposed_rows, deadline)
    baseline = validate_and_solve(context["rows"], deadline=deadline)
    stage["validation"] = baseline
    binary.check_promises(context["rows"], execution.proposed_rows, stage["binaryPromises"], baseline, validation)
    # Also check the freshly compiled final record, including selected interface evidence.
    workflow = execution.revision_contract.get("revisionWorkflow") or {}
    llm.validate_frozen_requirements(context["rows"], execution.proposed_rows, workflow, baseline, validation, stage.get("entityBindings"), deadline=deadline)
    frozen = B._materialize_verified_automatic_offer(execution, context["rows"], context["language"], stage, deadline=deadline)
    offer = frozen.guidance["proposalOffer"]
    presentation = offer.get("proposalPresentation") or {}
    presentation.update(binaryCompact=True, verifiedActions=[c["label"] for c in topic["choices"]],
        verification="通过修改权限、动作兑现及可解性检查；体验仍需试玩。" if context["language"] == "zh-CN" else
            "Permissions, selected actions and solvability verified; experience still needs playtesting.")
    offer["proposalPresentation"] = presentation
    return frozen
