"""Request-local, evidence-bound repairs. This module never writes session state."""
import copy
import hashlib
import json
from dataclasses import dataclass


PATCH_VERSION = "minimal-evidence-patch-1"
DEPENDENCIES = {
    "requirement_review": ("requirements",),
    "understanding_review": ("understanding",),
    "translation": ("translation_validation",),
    "clarification_question": ("clarification_question_validation",),
    "understanding": ("requirements",),
    "requirements": ("plan",),
    "plan": ("contract",),
    "contract": ("operations",),
    "operations": ("validation",),
    "validation": ("brief",),
    "brief": ("presentation", "execution"),
    "presentation": (),
    "execution": (),
    "clarification_review": ("clarification_body", "clarification_question"),
}


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def dependent_components(component):
    result = []
    pending = list(DEPENDENCIES.get(component, ()))
    while pending:
        name = pending.pop(0)
        if name not in result:
            result.append(name)
            pending.extend(DEPENDENCIES.get(name, ()))
    return result


class PatchError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Target:
    field: str
    path: tuple
    types: tuple


class PatchRequest:
    """Only the server declares targets and evidence; all other values stay locked."""
    def __init__(self, component, value, targets, evidence):
        self.component = component
        self._live_value = value
        self._live_evidence = evidence
        self.value = copy.deepcopy(value)
        self.targets = {target.field: target for target in targets}
        if len(self.targets) != len(targets) or not self.targets:
            raise PatchError("PATCH_TARGETS_INVALID")
        paths = [target.path for target in targets]
        if any(a == b or a[:len(b)] == b or b[:len(a)] == a
               for index, a in enumerate(paths) for b in paths[index + 1:]):
            raise PatchError("PATCH_TARGETS_OVERLAP")
        self.evidence = copy.deepcopy(evidence)
        if not evidence:
            raise PatchError("PATCH_EVIDENCE_MISSING")
        self.binding = signature({"component": component, "value": value, "evidence": evidence})

    def prompt(self):
        return {"componentId": self.component, "targets": list(self.targets),
                "targetTypes": {name: [kind.__name__ for kind in target.types] for name, target in self.targets.items()},
                "targetPaths": {name: list(target.path) for name, target in self.targets.items()},
                "frozenCandidate": self.value, "evidence": self.evidence,
                "instruction": "Return patches only for the listed targets. Each valueJson encodes one JSON value. Cite only supplied evidence IDs. Do not change locked fields, create permission, user intent, state transitions or verification flags."}

    def merge(self, payload, current_evidence=None, current_value=None):
        if signature({"component": self.component,
                      "value": self._live_value if current_value is None else current_value,
                      "evidence": self._live_evidence if current_evidence is None else current_evidence}) != self.binding:
            raise PatchError("PATCH_BINDING_CHANGED")
        if not isinstance(payload, dict) or set(payload) != {"patches"} or not isinstance(payload["patches"], list):
            raise PatchError("PATCH_ENVELOPE_INVALID")
        result = copy.deepcopy(self.value)
        seen = set()
        for patch in payload["patches"]:
            if not isinstance(patch, dict) or set(patch) != {"componentId", "field", "valueJson", "evidenceRefs"}:
                raise PatchError("PATCH_FIELDS_INVALID")
            field = patch["field"]
            if not isinstance(field, str) or patch["componentId"] != self.component or field not in self.targets or field in seen:
                raise PatchError("PATCH_TARGET_NOT_ALLOWED")
            refs = patch["evidenceRefs"]
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in self.evidence for ref in refs):
                raise PatchError("PATCH_EVIDENCE_INVALID")
            if not isinstance(patch["valueJson"], str):
                raise PatchError("PATCH_VALUE_INVALID")
            try:
                value = json.loads(patch["valueJson"])
            except (TypeError, ValueError) as error:
                raise PatchError("PATCH_VALUE_INVALID") from error
            target = self.targets[field]
            if type(value) not in target.types:
                raise PatchError("PATCH_TYPE_INVALID")
            parent = result
            try:
                for part in target.path[:-1]:
                    parent = parent[part]
                leaf = target.path[-1]
                if isinstance(parent, list) and isinstance(leaf, int) and leaf == len(parent):
                    parent.append(value)
                else:
                    parent[leaf] = value
            except (IndexError, KeyError, TypeError) as error:
                raise PatchError("PATCH_PATH_INVALID") from error
            seen.add(field)
        if seen != set(self.targets):
            raise PatchError("PATCH_TARGETS_MISSING")
        return result


def patch_response_format():
    properties = {"componentId": {"type": "string"}, "field": {"type": "string"},
                  "valueJson": {"type": "string"},
                  "evidenceRefs": {"type": "array", "items": {"type": "string"}}}
    return {"type": "json_schema", "json_schema": {"name": "cocreation_component_patch", "strict": True,
        "schema": {"type": "object", "additionalProperties": False, "required": ["patches"],
                   "properties": {"patches": {"type": "array", "items": {"type": "object",
                       "additionalProperties": False, "properties": properties, "required": list(properties)}}}}}}


def restore_operation_sources(operations, rows):
    """Recover current facts only; never choose a destination or an operation."""
    result = copy.deepcopy(operations)
    if not isinstance(result, list):
        return result
    for operation in result:
        if not isinstance(operation, dict) or "from" in operation:
            continue
        row, column = operation.get("row"), operation.get("column")
        if (type(row) is int and type(column) is int and 1 <= row <= len(rows)
                and 1 <= column <= len(rows[row - 1])):
            operation["from"] = rows[row - 1][column - 1]
    return result
