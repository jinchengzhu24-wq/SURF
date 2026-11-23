"""Durable choice receipts and current-Stage CAS around the existing planning pipeline."""
from __future__ import annotations

import copy
import json
import time
import uuid
from typing import TYPE_CHECKING

from cocreation.domain import binary_discovery as binary
from cocreation.domain.design_requirements import (
    INTERPRETATION_VERSION,
    evaluate_requirements,
    preflight_requirements,
    protected_change_requested,
    validate_requirement_record,
)
from cocreation.domain.proposal_search import parse_revision_plan
from cocreation.domain.proposal_understanding import bind_latest
from cocreation.domain.revision_workflow import SemanticConstraintError
from cocreation.llm.facade import KIMI_MODEL, LLMExecutionResult, LLMServiceError
from cocreation.persistence.repository import get_current_version, get_version, load_design_context

from .config_models import (
    ApiError,
    _llm_failure_details,
    require_active_session,
    require_browser_session,
    require_current_base,
)
from .context_and_discovery import build_llm_context
from .online_sync_and_dashboard import synchronize_turn_with_online_match
from .proposal_and_challenge import _materialize_verified_automatic_offer, _record_reply_outcome

if TYPE_CHECKING:
    from .services import CoCreationServices


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


def _guard(services: CoCreationServices, database, session_id, access_cookie, version_id):
    session = require_active_session(database, session_id, access_cookie, services=services)
    require_current_base(session, version_id)
    design = load_design_context(database, session_id, version_id) or {}
    if (design.get("activeDisagreement") or {}).get("status") == "active":
        raise ApiError(409, "DISAGREEMENT_ACTIVE", "Resolve the current disagreement first.")
    if database.execute("SELECT 1 FROM change_proposals WHERE session_id=? AND status='pending' LIMIT 1", (session_id,)).fetchone():
        raise ApiError(409, "PROPOSAL_PENDING", "Review the pending map before starting another proposal.")
    return session


def _save(services: CoCreationServices, database, session_id, topic):
    services.record_event(database, session_id, "binary_discovery_state", topic, services.utc_now())


def _receipt(database, session_id, key):
    row = database.execute(
        "SELECT payload_json FROM audit_events WHERE session_id=? AND event_type='binary_discovery_receipt' "
        "AND json_extract(payload_json,'$.key')=? ORDER BY id DESC LIMIT 1",
        (session_id, key),
    ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def _put_receipt(services: CoCreationServices, database, session_id, key, data, status):
    services.record_event(
        database,
        session_id,
        "binary_discovery_receipt",
        {"key": key, "request": data, "status": status},
        services.utc_now(),
    )


def _context(services: CoCreationServices, database, session_id, topic):
    version = get_version(database, session_id, topic["versionId"])
    context = build_llm_context(database, session_id, version, services=services)
    context["language"] = database.execute("SELECT language FROM design_sessions WHERE id=?", (session_id,)).fetchone()["language"]
    stage = context["stageContext"]
    stage.update(
        requirementPolicyVersion=1,
        aiEditableTilesOnly=True,
        binaryDiscovery=True,
        requirementUserTurns=topic["userTurns"],
        responseLanguage=context["language"],
        binaryPromises=[choice["promise"] for choice in topic["choices"]],
        binaryChoices=copy.deepcopy(topic["choices"]),
    )
    # Never send the private candidate maps/old snapshot projections to Kimi.
    stage["proposalDiscovery"] = {
        "topicId": topic["topicId"],
        "versionId": topic["versionId"],
        "initialRequest": topic["initialRequest"],
        "understanding": topic["understanding"],
        "userTurns": topic["userTurns"],
        "clarificationQuestionCount": topic["questionCount"],
        "answers": [{"question": c["question"], "answerText": c["label"], "turnId": c["sourceTurnId"]} for c in topic["choices"]],
    }
    stage["revisionRouting"] = "proposal"
    stage["authorizedRevisionBrief"] = topic["initialRequest"]
    return context


def _check_binding(context, topic):
    actual = binary.binding(
        context["stageContext"]["stageSnapshot"],
        topic["initialRequest"],
        context["stageContext"].get("revisionDesignContext") or {},
    )
    if actual != topic["binding"]:
        raise ApiError(409, "PROPOSAL_STALE", "The Stage or design constraints changed. Start a new proposal.")


def start(services: CoCreationServices, session_id, payload, content, understanding, request, access_cookie):
    data = {
        "kind": "start",
        "baseVersionId": payload.baseVersionId,
        "content": content,
        "requestProposal": payload.requestProposal,
    }
    with services.connect(immediate=True) as database:
        require_browser_session(database, session_id, access_cookie, services=services)
        receipt = _receipt(database, session_id, payload.idempotencyKey)
        if receipt:
            if receipt["request"] != data:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "This key belongs to another action.")
            if receipt["status"] == "done":
                return services.serialize_session(database, session_id)
        session = _guard(services, database, session_id, access_cookie, payload.baseVersionId)
        topic = binary.current_topic(database, session_id, payload.baseVersionId)
        if receipt and (not topic or topic["initialMessageKey"] != payload.idempotencyKey or topic["status"] not in binary.ACTIVE):
            raise ApiError(409, "PROPOSAL_STALE", "This proposal has ended; start with a new message key.")
        if topic and topic["status"] in binary.ACTIVE:
            if topic["initialMessageKey"] != payload.idempotencyKey:
                raise ApiError(409, "PROPOSAL_CHOICE_REQUIRED", "Use the current choice buttons or cancel.")
            if topic["status"] == "planning":
                raise ApiError(409, "BINARY_BUSY", "This request is still being processed.")
        else:
            prior = database.execute(
                "SELECT id FROM conversation_turns WHERE session_id=? AND request_id=? AND role='user'",
                (session_id, payload.idempotencyKey),
            ).fetchone()
            user_id = prior["id"] if prior else services.insert_turn(
                database, session, "user", content, payload.baseVersionId, payload.idempotencyKey, None
            )
            bound_understanding = bind_latest(understanding or {}, user_id, payload.baseVersionId)
            context = build_llm_context(database, session_id, get_current_version(database, session), services=services)
            topic = {
                "discoveryPolicyVersion": 2,
                "topicId": "binary-" + uuid.uuid4().hex,
                "versionId": payload.baseVersionId,
                "initialMessageKey": payload.idempotencyKey,
                "initialRequest": content,
                "understanding": bound_understanding.get("proposalUnderstanding") or {},
                "userTurns": [{"id": user_id, "content": content}],
                "choices": [],
                "questionCount": 0,
                "language": session["language"],
                "pendingQuestion": None,
                "status": "planning",
            }
            topic["binding"] = binary.binding(
                context["stageContext"]["stageSnapshot"],
                content,
                context["stageContext"].get("revisionDesignContext") or {},
            )
            services.record_event(
                database,
                session_id,
                "turn_understanding",
                {"messageKey": payload.idempotencyKey, "baseVersionId": payload.baseVersionId, "result": bound_understanding},
                services.utc_now(),
            )
        topic["status"] = "planning"
        topic["leaseUntil"] = time.time() + max(1, request.state.llm_deadline - services.monotonic()) + 5
        topic["retryAction"] = {
            "kind": "start",
            "idempotencyKey": payload.idempotencyKey,
            "baseVersionId": payload.baseVersionId,
            "content": content,
            "requestProposal": payload.requestProposal,
        }
        _put_receipt(services, database, session_id, payload.idempotencyKey, data, "pending")
        _save(services, database, session_id, topic)
    return _advance(services, session_id, topic, payload.idempotencyKey, data, request, access_cookie)


def choose(services: CoCreationServices, session_id, payload, request, access_cookie):
    data = {"kind": "choice", **payload.model_dump()}
    with services.connect(immediate=True) as database:
        require_browser_session(database, session_id, access_cookie, services=services)
        receipt = _receipt(database, session_id, payload.idempotencyKey)
        if receipt:
            if receipt["request"] != data:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "This key belongs to another choice.")
            if receipt["status"] == "done":
                return services.serialize_session(database, session_id)
        session = _guard(services, database, session_id, access_cookie, payload.baseVersionId)
        topic = binary.current_topic(database, session_id, payload.baseVersionId)
        if not topic or topic["topicId"] != payload.topicId or topic["status"] not in binary.ACTIVE:
            raise ApiError(409, "PROPOSAL_STALE", "This topic is no longer active.")
        context = _context(services, database, session_id, topic)
        _check_binding(context, topic)
        question = topic.get("pendingQuestion") or {}
        if question.get("questionId") != payload.questionId:
            raise ApiError(409, "PROPOSAL_STALE", "Answer the current question.")
        if receipt is None:
            if topic["status"] != "clarifying" or any(c["questionId"] == payload.questionId for c in topic["choices"]):
                raise ApiError(409, "PROPOSAL_STALE", "This question was already answered.")
            branch = next((item for item in question["choices"] if item["choiceId"] == payload.choiceId), None)
            if branch is None:
                raise ApiError(409, "PROPOSAL_STALE", "This option does not belong to the question.")
            try:
                presentation = displayed_question(database, session_id, question, payload.displayLanguage, topic["language"])
            except ValueError as error:
                raise ApiError(409, "PROPOSAL_TRANSLATION_STALE", "Refresh to use the verified source-language buttons.") from error
            label = next(item["label"] for item in presentation["choices"] if item["choiceId"] == payload.choiceId)
            text = ("选择 " if payload.displayLanguage == "zh-CN" else "Selected ") + branch["letter"] + ": " + label
            user_id = services.insert_turn(database, session, "user", text, payload.baseVersionId, payload.idempotencyKey, None)
            cache = copy.deepcopy(branch["candidate"])
            if (
                cache["topicBinding"] != topic["binding"]
                or cache["prefix"] != binary.path_signature(topic["choices"])
                or cache.get("proofSignature") != binary.signature({k: v for k, v in cache.items() if k != "proofSignature"})
            ):
                raise ApiError(409, "PROPOSAL_STALE", "The feasibility proof changed.")
            choice = {
                "questionId": payload.questionId,
                "choiceId": payload.choiceId,
                "question": presentation["question"],
                "note": presentation["note"],
                "label": label,
                "letter": branch["letter"],
                "promise": branch["promise"],
                "sourceTurnId": user_id,
                "displayLanguage": payload.displayLanguage,
            }
            topic["choices"].append(choice)
            topic["userTurns"].append({"id": user_id, "content": text})
            topic["selectedCandidate"] = cache
            topic["selectedCandidate"]["selectedPrefix"] = binary.path_signature(topic["choices"])
            services.record_event(
                database,
                session_id,
                "binary_choice_selected",
                {
                    "topicId": topic["topicId"],
                    "questionId": payload.questionId,
                    "choiceId": payload.choiceId,
                    "sourceTurnId": user_id,
                    "evidenceKind": "interface_choice",
                    "displayLanguage": payload.displayLanguage,
                },
                services.utc_now(),
            )
        elif topic["status"] == "planning":
            raise ApiError(409, "BINARY_BUSY", "This choice is still being processed.")
        topic["status"] = "planning"
        topic["leaseUntil"] = time.time() + max(1, request.state.llm_deadline - services.monotonic()) + 5
        topic["retryAction"] = {"kind": "choice", **payload.model_dump()}
        _put_receipt(services, database, session_id, payload.idempotencyKey, data, "pending")
        _save(services, database, session_id, topic)
    return _advance(services, session_id, topic, payload.idempotencyKey, data, request, access_cookie)


def cancel(services: CoCreationServices, session_id, payload, access_cookie):
    data = {"kind": "cancel", **payload.model_dump()}
    with services.connect(immediate=True) as database:
        require_browser_session(database, session_id, access_cookie, services=services)
        receipt = _receipt(database, session_id, payload.idempotencyKey)
        if receipt:
            if receipt["request"] != data:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "This key belongs to another action.")
            return services.serialize_session(database, session_id)
        _guard(services, database, session_id, access_cookie, payload.baseVersionId)
        topic = binary.current_topic(database, session_id, payload.baseVersionId)
        if not topic or topic["topicId"] != payload.topicId:
            raise ApiError(409, "PROPOSAL_STALE", "This topic is no longer active.")
        if topic["status"] == "planning":
            raise ApiError(409, "BINARY_BUSY", "Wait for the current request before cancelling.")
        if topic["status"] not in binary.ACTIVE:
            raise ApiError(409, "PROPOSAL_STALE", "This topic has ended.")
        topic.update(status="cancelled", retryAction=None)
        _save(services, database, session_id, topic)
        _put_receipt(services, database, session_id, payload.idempotencyKey, data, "done")
        return services.serialize_session(database, session_id)


def displayed_question(database, session_id, question, language, source_language):
    view = {key: question[key] for key in ("question", "note")}
    view["choices"] = [{"choiceId": c["choiceId"], "label": c["label"]} for c in question["choices"]]
    if language == source_language:
        return view
    # Only complete, independently verified turn translations can supply labels.
    row = database.execute(
        "SELECT guidance_json FROM turn_translations WHERE turn_id=? AND language=?",
        (question.get("turnId"), language),
    ).fetchone()
    translated = (json.loads(row["guidance_json"] or "{}") if row else {}).get("proposalQuestion")
    if not translated or translated.get("questionId") != question["questionId"]:
        raise ValueError("BINARY_TRANSLATION_UNAVAILABLE")
    if {c["choiceId"] for c in translated.get("choices", [])} != {c["choiceId"] for c in view["choices"]}:
        raise ValueError("BINARY_TRANSLATION_UNAVAILABLE")
    return translated


def _advance(services: CoCreationServices, session_id, topic, key, receipt_data, request, access_cookie):
    deadline = request.state.llm_deadline
    phase = "binary_preflight"
    try:
        with services.connect() as database:
            _guard(services, database, session_id, access_cookie, topic["versionId"])
            context = _context(services, database, session_id, topic)
        _check_binding(context, topic)
        stage = context["stageContext"]
        stage["requirementRecord"] = topic.get("requirements")
        preflight_started = services.monotonic()
        preflight_cutoff = min(deadline - 20.0, preflight_started + 20.0)
        baseline = services.llm.solve_stage(context["rows"], deadline=preflight_cutoff)
        baseline_features = services.llm.proposal_route_features(
            context["rows"],
            baseline,
            stage.get("entityBindings"),
            deadline=preflight_cutoff,
        )
        context["validation"]["minimumPushes"] = baseline_features.get("minimumPushes")
        preflight_budget = 20.0 - (services.monotonic() - preflight_started)

        def validator(rows, cutoff):
            services.llm.validate_ai_edit_diff(context["rows"], rows)
            validation = services.llm.solve_stage(rows, deadline=cutoff)
            after = services.llm.proposal_route_features(
                rows,
                validation,
                stage.get("entityBindings"),
                deadline=cutoff,
            )
            outcomes = evaluate_requirements(context["rows"], rows, stage["requirementRecord"], baseline_features, after)
            if any(not item["passed"] for item in outcomes):
                raise SemanticConstraintError(outcomes)
            binary.check_promises(context["rows"], rows, [c["promise"] for c in topic["choices"]], baseline, validation)
            return validation

        def strategy_validator(rows, strategy):
            plan = parse_revision_plan({"strategies": [strategy]})
            contract = services.llm.build_revision_execution_contract(plan, topic["initialRequest"], stage)
            operations = _operations(context["rows"], rows)
            if list(services.llm.execute_revision_operations(context["rows"], operations, contract, 1)) != list(rows):
                raise ValueError("BINARY_EXECUTION_MISMATCH")

        if topic["questionCount"] >= 3:
            step, caches = {"action": "plan", "note": "", "question": "", "branches": []}, []
        else:
            step, caches = services.llm.generate_binary_step(
                topic,
                context,
                validator,
                strategy_validator,
                request.state.request_id,
                deadline,
                preflight_budget,
            )
            # The generator's existing independent review has accepted this record and both directions.
            if topic.get("requirements") is None:
                topic["requirements"] = validate_requirement_record(
                    step["initialRequirements"],
                    topic["userTurns"][:1],
                    stage["stageSnapshot"],
                )
            stage["requirementRecord"] = topic["requirements"]
        if preflight_requirements(context["rows"], topic["requirements"]):
            raise ApiError(422, "REQUIREMENT_PERMISSION_CONFLICT", "The request conflicts with fixed map permissions.")
        if step["action"] == "ask":
            question_id = "question-" + uuid.uuid4().hex
            topic["questionCount"] += 1
            question = {
                "questionId": question_id,
                "number": topic["questionCount"],
                "question": step["question"],
                "note": step["note"],
                "choices": [],
            }
            for index, (branch, cache) in enumerate(zip(step["branches"], caches)):
                cache["proofSignature"] = binary.signature(cache)
                question["choices"].append(
                    {
                        "choiceId": "choice-" + uuid.uuid4().hex,
                        "letter": "AB"[index],
                        "label": branch["label"],
                        "promise": branch["promise"],
                        "candidate": cache,
                    }
                )
            topic.update(status="clarifying", pendingQuestion=question, retryAction=None, failureCode=None)
            public = binary.public_question(topic)
            body = "\n\n".join(
                part
                for part in [
                    question["question"],
                    question["note"],
                    "\n".join(c["letter"] + ": " + c["label"] for c in question["choices"]),
                ]
                if part
            )
            execution = LLMExecutionResult(
                body,
                1,
                request.state.request_id,
                model=KIMI_MODEL,
                guidance={"proposalQuestion": public, "move": "clarify_intent"},
            )
        elif step["action"] == "blocked":
            raise binary.BinaryError("BINARY_DIRECTIONS_UNAVAILABLE")
        else:
            phase = "binary_finalization"
            execution = _finalize(services, topic, context, validator, request, deadline)
            topic.update(status="proposal_ready", retryAction=None, failureCode=None)
        with services.connect(immediate=True) as database:
            session = _guard(services, database, session_id, access_cookie, topic["versionId"])
            current = binary.current_topic(database, session_id, topic["versionId"])
            if (
                not current
                or current["topicId"] != topic["topicId"]
                or current["status"] != "planning"
                or current.get("retryAction", {}).get("idempotencyKey") != key
            ):
                raise ApiError(409, "PROPOSAL_STALE", "The active question changed while preparing the reply.")
            _check_binding(_context(services, database, session_id, topic), topic)
            turn_id = services.insert_turn(
                database,
                session,
                "assistant",
                execution.assistant_message,
                topic["versionId"],
                key,
                execution,
            )
            _record_reply_outcome(
                database,
                session_id,
                key,
                topic["versionId"],
                execution,
                "proposal_clarification" if step["action"] == "ask" else "message",
                proposal_requested=True,
                services=services,
            )
            if step["action"] == "ask":
                topic["pendingQuestion"]["turnId"] = turn_id
            _save(services, database, session_id, topic)
            services.record_event(
                database,
                session_id,
                "binary_question_verified" if step["action"] == "ask" else "binary_offer_verified",
                {
                    "topicId": topic["topicId"],
                    "baseVersionId": topic["versionId"],
                    "questionCount": topic["questionCount"],
                    "selectedActions": len(topic["choices"]),
                    "branchPreflightMs": [c["preflightMs"] for c in caches],
                    "branchCount": len(caches),
                    "policyVersion": binary.POLICY_VERSION,
                },
                services.utc_now(),
            )
            _put_receipt(services, database, session_id, key, receipt_data, "done")
            result = services.serialize_session(database, session_id)
        try:
            synchronize_turn_with_online_match(session_id, key, services=services)
        except Exception:
            # The committed receipt remains authoritative; synchronization is retried separately.
            pass
        return result
    except Exception as error:
        if isinstance(error, ApiError) and error.status_code == 409:
            raise
        if isinstance(error, (ApiError, LLMServiceError)):
            failure = error
        else:
            code = (
                ("BINARY_PREFLIGHT_BUDGET" if phase == "binary_preflight" else "BINARY_PLANNING_BUDGET")
                if isinstance(error, TimeoutError)
                else getattr(error, "code", "BINARY_VALIDATION_FAILED")
            )
            failure = LLMServiceError(
                code,
                "Verified directions are not ready. Retry or cancel this proposal.",
                request.state.request_id,
                True,
                1,
                502,
            )
            failure.details = {
                "task": "proposal_clarification" if phase == "binary_preflight" else "revision_plan",
                "failureStage": phase,
                "reasonCode": code,
            }
        with services.connect(immediate=True) as database:
            session = require_browser_session(database, session_id, access_cookie, services=services)
            current = binary.current_topic(database, session_id, topic["versionId"])
            if session["current_version_id"] == topic["versionId"] and current and current["topicId"] == topic["topicId"] and current["status"] == "planning":
                retryable = bool(getattr(failure, "retryable", False))
                current.update(
                    status="retry_pending" if retryable else "revision_needed",
                    failureCode=failure.code,
                    **({} if retryable else {"retryAction": None}),
                )
                if topic.get("requirements") is not None:
                    current["requirements"] = topic["requirements"]
                _save(services, database, session_id, current)
                failure_details = {
                    **(_llm_failure_details(failure) if isinstance(failure, LLMServiceError) else {}),
                    **(getattr(failure, "details", None) or {}),
                }
                services.record_event(
                    database,
                    session_id,
                    "message_generation_failed",
                    {
                        "messageKey": key,
                        "baseVersionId": topic["versionId"],
                        "code": failure.code,
                        "attemptsUsed": getattr(failure, "attempts_used", 1),
                        "requestId": request.state.request_id,
                        "proposalRequested": True,
                        "retryable": retryable,
                        "details": failure_details,
                        **{field: failure_details.get(field) for field in ("task", "failureStage", "reasonCode")},
                    },
                    services.utc_now(),
                )
        raise failure


def _operations(base, rows):
    return [
        {"row": y + 1, "column": x + 1, "to": b}
        for y, (left, right) in enumerate(zip(base, rows))
        for x, (a, b) in enumerate(zip(left, right))
        if a != b
    ]


def _finalize(services: CoCreationServices, topic, context, validator, request, deadline):
    stage = context["stageContext"]
    stage.setdefault("_reliabilityState", {})
    stage["binaryChoices"] = topic["choices"]
    stage["binaryPromises"] = [c["promise"] for c in topic["choices"]]
    if isinstance(topic.get("requirements"), dict):
        cached_initial = dict(topic["requirements"])
        cached_initial.setdefault("interpretationVersion", INTERPRETATION_VERSION)
        stage["verifiedRequirementCache"] = [
            *(stage.get("verifiedRequirementCache") or []),
            {"baseVersionId": topic["versionId"], "record": cached_initial},
        ]
    try:
        execution = services.llm.generate_revision_search_proposal_sync(
            conversation=[{"role": "user", "content": t["content"]} for t in topic["userTurns"]],
            rows=context["rows"],
            request_id=request.state.request_id,
            language=context["language"],
            proposal_validator=lambda rows: validator(rows, deadline),
            stage_context=stage,
            baseline_metrics=context["validation"],
            deadline=deadline,
        )
    except LLMServiceError as error:
        if error.code not in {
            "PROPOSAL_SEARCH_EXHAUSTED",
            "DETERMINISTIC_SEARCH_EXHAUSTED",
            "CANDIDATE_UNSOLVABLE",
            "SEMANTIC_CONSTRAINT_NOT_MET",
            "HARD_OBJECTIVE_NOT_MET",
        }:
            raise
        verified = stage.get("_reliabilityState", {}).get("verifiedRequirements")
        cache = topic.get("selectedCandidate")
        if (
            not verified
            or not cache
            or cache.get("topicBinding") != topic["binding"]
            or cache.get("selectedPrefix") != binary.path_signature(topic["choices"])
        ):
            raise
        stage["requirementRecord"] = verified["record"]
        plan = parse_revision_plan({"strategies": [cache["strategy"]]})
        contract = services.llm.build_revision_execution_contract(plan, topic["initialRequest"], stage)
        execution = LLMExecutionResult(
            "",
            error.attempts_used,
            request.state.request_id,
            model="verified-binary-cache",
            proposed_rows=cache["rows"],
            revision_contract=contract,
            revision_plan=plan.as_dict(),
            revision_operations=_operations(context["rows"], cache["rows"]),
            proposal_diagnostics={"selectedStrategyIndex": 1},
        )
    validation = validator(execution.proposed_rows, deadline)
    baseline = services.llm.solve_stage(context["rows"], deadline=deadline)
    stage["validation"] = baseline
    binary.check_promises(context["rows"], execution.proposed_rows, stage["binaryPromises"], baseline, validation)
    # Also check the freshly compiled final record, including selected interface evidence.
    workflow = execution.revision_contract.get("revisionWorkflow") or {}
    services.llm.validate_frozen_requirements(
        context["rows"],
        execution.proposed_rows,
        workflow,
        baseline,
        validation,
        stage.get("entityBindings"),
        deadline=deadline,
    )
    frozen = _materialize_verified_automatic_offer(
        execution,
        context["rows"],
        context["language"],
        stage,
        deadline=deadline,
        services=services,
    )
    offer = frozen.guidance["proposalOffer"]
    presentation = offer.get("proposalPresentation") or {}
    presentation.update(
        binaryCompact=True,
        verifiedActions=[c["label"] for c in topic["choices"]],
        verification=(
            "通过修改权限、动作兑现及可解性检查；体验仍需试玩。"
            if context["language"] == "zh-CN"
            else "Permissions, selected actions and solvability verified; experience still needs playtesting."
        ),
    )
    offer["proposalPresentation"] = presentation
    return frozen
