"""Build explicit offline patch fixtures from the expected repaired document."""
import copy
import json


def patch_fixture(specification, expected):
    if isinstance(specification, str):
        specification = json.loads(specification)
    patches = []
    for field in specification["targets"]:
        value = expected
        for part in specification["targetPaths"][field]:
            if part == "additionalRequirements" and isinstance(value, dict) and part not in value:
                value = []
            else:
                value = value[part]
        patches.append({"componentId": specification["componentId"], "field": field,
                        "valueJson": json.dumps(copy.deepcopy(value), ensure_ascii=False),
                        "evidenceRefs": [next(iter(specification["evidence"]))]})
    return {"patches": patches}
