"""Bounded human-edit state replay, persisted as append-only audit events.

No model prose, map operations, intent confirmations or new conversation turns.
Callers hold a short SQLite write transaction for claims and commits only.
"""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

POLICY_VERSION = "human-edit-engagement-1"
PROMPT_VERSION = "human-edit-engagement-replay-1"
EVENT = "component_recovery"
MAX_ATTEMPTS = 2
LEASE_SECONDS = 15
TERMINAL = {"applied", "completed_no_change", "needs_input", "expired", "exhausted"}


def now():
    return datetime.now(timezone.utc)


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def tasks(database, session_id):
    result = {}
    for row in database.execute(
        "SELECT payload_json FROM audit_events WHERE session_id=? AND event_type=? ORDER BY id",
        (session_id, EVENT),
    ):
        task = json.loads(row["payload_json"])
        result[task["recoveryId"]] = task
    return result


def record(database, session_id, task, status, **fields):
    from repository import record_event
    updated = {**task, **fields, "status": status, "updatedAt": now().isoformat()}
    record_event(database, session_id, EVENT, updated, updated["updatedAt"])
    return updated


def disagreement_identity(database, session_id, version_id, active):
    if not active or active.get("subject") != "human_edit":
        return None
    if active.get("disagreementId"):
        return active["disagreementId"]
    # Read-time compatibility only. A terminal event starts a new identity interval.
    entry = None
    for row in database.execute(
        "SELECT id,event_type,payload_json FROM audit_events WHERE session_id=? "
        "AND event_type IN ('disagreement_started','disagreement_updated',"
        "'disagreement_acknowledged','disagreement_resolved') "
        "AND json_extract(payload_json,'$.versionId')=? ORDER BY id", (session_id, version_id),
    ):
        payload = json.loads(row["payload_json"])
        if row["event_type"] in {"disagreement_acknowledged", "disagreement_resolved"}:
            entry = None
        elif (payload.get("subject") == "human_edit" and entry is None
              and (payload.get("disagreement") or {}).get("displayCard", True) is not False):
            entry = (row["id"], payload)
    if entry is None or not entry[1].get("turnId"):
        return None
    turn = database.execute(
        "SELECT 1 FROM conversation_turns WHERE id=? AND session_id=? AND version_id=? AND role='assistant'",
        (entry[1]["turnId"], session_id, version_id),
    ).fetchone()
    if turn is None:
        return None
    return "legacy-human-edit-" + signature([session_id, version_id, entry[0]])[:32]


def state_stamp(database, session_id, version_id):
    from repository import load_design_context
    active = load_design_context(database, session_id, version_id).get("activeDisagreement")
    identity = disagreement_identity(database, session_id, version_id, active)
    row = database.execute(
        "SELECT COALESCE(MAX(sequence_number),0) AS n FROM conversation_turns "
        "WHERE session_id=? AND version_id=? AND role='user'", (session_id, version_id),
    ).fetchone()
    receipt = database.execute("SELECT COALESCE(MAX(id),0) AS n FROM audit_events WHERE session_id=? "
                               "AND event_type='interaction_message_guard' AND json_extract(payload_json,'$.versionId')=? "
                               "AND json_extract(payload_json,'$.status')='started'", (session_id, version_id)).fetchone()
    return {"disagreementId": identity, "stateSignature": signature(active), "latestUserSequence": row["n"],
            "latestMessageReceipt": receipt["n"]}


def pending_message_until(database, session_id, version_id):
    guards = {}
    for row in database.execute("SELECT payload_json FROM audit_events WHERE session_id=? "
                                "AND event_type='interaction_message_guard' "
                                "AND json_extract(payload_json,'$.versionId')=? ORDER BY id", (session_id, version_id)):
        guard = json.loads(row["payload_json"])
        guards[guard["guardId"]] = guard
    pending = [item["leaseUntil"] for item in guards.values()
               if item["status"] == "started" and _time(item["leaseUntil"]) > now()]
    return max(pending) if pending else None


def snapshot_signature(database, session_id, version_id):
    row = database.execute("SELECT rows_json,entity_bindings_json FROM level_versions WHERE session_id=? AND id=?",
                           (session_id, version_id)).fetchone()
    return signature(list(row) if row else None)


def eligible(database, session, task):
    if (session["status"] != "active" or session["current_version_id"] != task["baseVersionId"]
            or (session["deadline_at"] and _time(session["deadline_at"]) <= now())):
        return False
    stamp = state_stamp(database, session["id"], task["baseVersionId"])
    return bool(stamp["disagreementId"] and stamp["disagreementId"] == task["disagreementId"]
                and task["snapshotSignature"] == snapshot_signature(database, session["id"], task["baseVersionId"]))


def projected_status(database, session, task):
    if task["status"] in TERMINAL:
        return task["status"]
    if (task.get("policyVersion") != POLICY_VERSION or task.get("promptVersion") != PROMPT_VERSION
            or not eligible(database, session, task)):
        return "expired"
    if task["status"] == "claimed":
        if _time(task["leaseUntil"]) > now():
            return "claimed"
    if task["attemptsUsed"] >= MAX_ATTEMPTS:
        return "exhausted"
    if pending_message_until(database, session["id"], task["baseVersionId"]):
        return "waiting_message"
    return "retry_pending" if task["status"] == "claimed" else task["status"]


def enqueue(database, session_id, version_id, user_turn_id, assistant_turn_id, message_key):
    from repository import load_design_context
    active = load_design_context(database, session_id, version_id).get("activeDisagreement") or {}
    identity = disagreement_identity(database, session_id, version_id, active)
    if not identity or active.get("status") != "active":
        return None
    recovery_id = signature([session_id, version_id, user_turn_id, identity, POLICY_VERSION])[:32]
    previous = tasks(database, session_id)
    if recovery_id in previous:
        return previous[recovery_id]
    for old in previous.values():
        if old["status"] not in TERMINAL and old["disagreementId"] == identity:
            record(database, session_id, old, "expired", failureCode="SUPERSEDED_SOURCE")
    user = database.execute("SELECT content,sequence_number FROM conversation_turns WHERE id=?", (user_turn_id,)).fetchone()
    prior = database.execute(
        "SELECT id FROM conversation_turns WHERE session_id=? AND version_id=? AND role='assistant' "
        "AND sequence_number<? ORDER BY sequence_number DESC LIMIT 1",
        (session_id, version_id, user["sequence_number"]),
    ).fetchone()
    task = {"recoveryId": recovery_id, "component": "human_edit_engagement", "policyVersion": POLICY_VERSION,
            "promptVersion": PROMPT_VERSION,
            "baseVersionId": version_id, "disagreementId": identity, "sourceUserTurnId": user_turn_id,
            "sourceAssistantTurnId": prior["id"] if prior else None, "deliveredAssistantTurnId": assistant_turn_id,
            "messageKey": message_key, "sourceSignature": signature(user["content"]),
            "snapshotSignature": snapshot_signature(database, session_id, version_id),
            "stateVersion": state_stamp(database, session_id, version_id), "attemptsUsed": 0,
            "createdAt": now().isoformat()}
    return record(database, session_id, task, "pending")


def claim(database, session, recovery_id, base_version_id, key):
    task = tasks(database, session["id"]).get(recovery_id)
    if task is None:
        raise ValueError("UNKNOWN_RECOVERY")
    if base_version_id != task["baseVersionId"]:
        raise ValueError("INVALID_RECOVERY_SOURCE")
    if key in task.get("requestKeys", []):
        return None  # Replay only: never spend another model attempt on the same key.
    expected_key = recovery_id + "-" + str(task["attemptsUsed"] + 1)
    if key != expected_key:
        raise ValueError("IDEMPOTENCY_CONFLICT")
    status = projected_status(database, session, task)
    if status in TERMINAL:
        if task["status"] != status:
            record(database, session["id"], task, status)
        return None
    if status in {"claimed", "waiting_message"}:
        return None
    token = uuid.uuid4().hex
    return record(database, session["id"], task, "claimed", claimToken=token,
                  attemptsUsed=task["attemptsUsed"] + 1, requestKeys=[*task.get("requestKeys", []), key],
                  leaseUntil=(now() + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                  reviewStamp=state_stamp(database, session["id"], base_version_id))


def replay_sources(database, session_id, task):
    source = database.execute(
        "SELECT id,content,sequence_number FROM conversation_turns WHERE id=? AND session_id=? "
        "AND version_id=? AND role='user'", (task["sourceUserTurnId"], session_id, task["baseVersionId"]),
    ).fetchone()
    if source is None or signature(source["content"]) != task["sourceSignature"]:
        raise ValueError("INVALID_RECOVERY_EVIDENCE")
    later = list(database.execute(
        "SELECT id,role,content,sequence_number FROM conversation_turns WHERE session_id=? AND version_id=? "
        "AND sequence_number>? ORDER BY sequence_number", (session_id, task["baseVersionId"], source["sequence_number"]),
    ))
    if len(later) > 24:
        raise ValueError("CONTEXT_OUT_OF_WINDOW")
    assistant = database.execute(
        "SELECT content FROM conversation_turns WHERE id=? AND session_id=? AND version_id=? AND role='assistant'",
        (task.get("sourceAssistantTurnId"), session_id, task["baseVersionId"]),
    ).fetchone()
    # Historical assistant coordinates are not current map evidence. Caller uses
    # the shared role projection to sanitize the original question/background.
    return {"originalUserMessage": source["content"],
            "boundAssistantTurnId": task.get("sourceAssistantTurnId"),
            "boundAssistantMessage": assistant["content"] if assistant else "",
            "subsequentTurns": [dict(row) for row in later if row["id"] != task["deliveredAssistantTurnId"]]}


def finish(database, session, task, decision=None, failure_code=None):
    current = tasks(database, session["id"]).get(task["recoveryId"])
    if not current or current.get("claimToken") != task["claimToken"] or current["status"] != "claimed":
        return "expired"
    if not eligible(database, session, task):
        return record(database, session["id"], current, "expired", failureCode="STALE_TARGET")["status"]
    source = database.execute("SELECT content FROM conversation_turns WHERE id=? AND session_id=? AND version_id=? AND role='user'",
                              (task["sourceUserTurnId"], session["id"], task["baseVersionId"])).fetchone()
    if not source or signature(source["content"]) != task["sourceSignature"]:
        return record(database, session["id"], current, "expired", failureCode="INVALID_RECOVERY_EVIDENCE")["status"]
    if not failure_code:
        from proposal_understanding import validate_engagement
        try:
            decision = validate_engagement(decision, source["content"])
        except ValueError:
            failure_code = "STATE_UNVERIFIED"
    if (state_stamp(database, session["id"], task["baseVersionId"]) != task["reviewStamp"]
            or pending_message_until(database, session["id"], task["baseVersionId"])):
        failure_code = "CONCURRENT_CHANGE"
        decision = None
    if failure_code:
        status = "retry_pending" if task["attemptsUsed"] < MAX_ATTEMPTS else "exhausted"
        return record(database, session["id"], current, status, failureCode=failure_code)["status"]
    if decision["decision"] == "unclear":
        status = "needs_input"
    elif decision["decision"] == "unchanged":
        status = "completed_no_change"
    else:
        from repository import load_design_context, save_design_context, record_event
        from design_context import set_active_disagreement
        context = load_design_context(database, session["id"], task["baseVersionId"])
        active = context["activeDisagreement"]
        context = set_active_disagreement(context, None, task["baseVersionId"], task["sourceUserTurnId"])
        save_design_context(database, task["baseVersionId"], context)
        record_event(database, session["id"], "disagreement_acknowledged", {
            "versionId": task["baseVersionId"], "turnId": task["sourceUserTurnId"], "subject": "human_edit",
            "resolution": None, "disagreement": {**active, "status": "acknowledged", "resolution": None,
                "displayCard": False, "disagreementId": task["disagreementId"]},
            "source": "component_recovery", "recoveryId": task["recoveryId"],
        }, now().isoformat())
        status = "applied"
    return record(database, session["id"], current, status, failureCode=None)["status"]


def interaction_state(database, session):
    from repository import load_design_context
    version_id = session["current_version_id"]
    active = load_design_context(database, session["id"], version_id).get("activeDisagreement") if version_id else None
    revision = database.execute(
        "SELECT COALESCE(MAX(id),0) AS n FROM audit_events WHERE session_id=? AND "
        "(event_type LIKE 'disagreement_%' OR event_type=?)", (session["id"], EVENT),
    ).fetchone()["n"]
    recoveries = []
    for task in tasks(database, session["id"]).values():
        if task["baseVersionId"] != version_id:
            continue
        status = projected_status(database, session, task)
        recoveries.append({"recoveryId": task["recoveryId"], "component": task["component"], "status": status,
                           "attemptsUsed": task["attemptsUsed"], "baseVersionId": version_id,
                           "nextAttemptKey": task["recoveryId"] + "-" + str(task["attemptsUsed"] + 1),
                           "leaseUntil": (pending_message_until(database, session["id"], version_id)
                                          if status == "waiting_message" else task.get("leaseUntil"))})
    return {"versionId": version_id, "revision": revision,
            "disagreementId": disagreement_identity(database, session["id"], version_id, active),
            "disagreementStatus": "active" if active else None,
            "proposalBlocked": bool(active and active.get("status") == "active"), "recoveries": recoveries}
