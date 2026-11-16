"""Explicit synthetic review deltas; never accepts legacy envelopes at runtime."""
import copy

from understanding_review import review_targets


def review_fixture(base, corrected=None):
    import llm_client as llm
    def normalized(value):
        value = llm._validate_turn_understanding(copy.deepcopy(value), value["evidenceSpan"])
        value.pop("humanEditEngagement", None)
        value.setdefault("optionDecision", None)
        return value
    base = normalized(base)
    corrected = normalized(corrected) if corrected is not None else base
    corrections = []
    for target in review_targets(base):
        value = corrected
        for part in target["path"]:
            value = value[part]
        if target["mode"] == "append":
            old = base
            for part in target["path"]:
                old = old[part]
            value = value[len(old):]
            if not value:
                continue
        elif value == target["currentValue"]:
            continue
        corrections.append({"targetId": target["targetId"], "reason": "Correct the synthetic evidence interpretation.",
            "value": copy.deepcopy(value), "evidenceRefs": ["user:latest"]})
    return {"decision": "CORRECT" if corrections else "APPROVE", "issues": [], "corrections": corrections}
