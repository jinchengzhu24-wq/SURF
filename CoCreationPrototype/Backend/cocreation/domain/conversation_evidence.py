"""Request-local dialogue provenance, separate from authoritative map facts."""
import copy
import hashlib
import json


CONTEXT_VERSION = "dialogue-evidence-3-engagement"


def build_dialogue_evidence(turns, version_id, topic=None):
    """Keep the current window plus every user source of the active topic."""
    ordered = [dict(turn) for turn in turns
               if turn.get("version_id", version_id) == version_id]
    ordered.sort(key=lambda turn: turn.get("sequence_number", 0))
    selected = {turn["id"]: turn for turn in ordered[-24:]}
    pinned = {item["id"] for item in (topic or {}).get("userTurns") or []}
    for turn in ordered:
        if turn["id"] in pinned and turn["role"] == "user":
            selected[turn["id"]] = turn
    records = [{"id": turn["id"], "role": turn["role"],
                "versionId": version_id, "sequence": turn.get("sequence_number", 0),
                "content": turn["content"]}
               for turn in sorted(selected.values(), key=lambda turn: turn.get("sequence_number", 0))]
    return {"contextVersion": CONTEXT_VERSION, "versionId": version_id, "turns": records}


def with_latest(evidence, latest):
    result = copy.deepcopy(evidence or {"contextVersion": CONTEXT_VERSION, "turns": []})
    result["turns"] = [turn for turn in result["turns"] if turn["id"] != "latest"]
    result["turns"].append({"id": "latest", "role": "user",
                            "versionId": result.get("versionId"), "content": latest})
    return result


def user_sources(evidence):
    return {turn["id"]: turn["content"] for turn in (evidence or {}).get("turns", [])
            if turn["role"] == "user"
            and turn.get("versionId") == (evidence or {}).get("versionId")}


def evidence_signature(evidence):
    return hashlib.sha256(json.dumps(evidence or {}, ensure_ascii=False,
                                     sort_keys=True).encode("utf-8")).hexdigest()
