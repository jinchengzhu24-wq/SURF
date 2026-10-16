"""Synthetic directions, never real-model evidence."""
from dataclasses import replace
import proposal_options


def options():
    return [{"summary": "I would try grouped water edges.", "components": ["water"],
        "focus": "I would focus on the open interior.", "mechanism": "I would gather the water into a repeated contour.",
        "effect": "This may make the composition easier to read.", "tradeoff": "It may leave less open space nearby.",
        "preserve": "I would retain the fixed objects and outer shell."},
        {"summary": "I would try separated water accents.", "components": ["water"],
        "focus": "I would focus on the same interior area.", "mechanism": "I would spread small water groups apart.",
        "effect": "This may create a more open visual rhythm.", "tradeoff": "The outline may feel less unified.",
        "preserve": "I would retain the fixed objects and outer shell."}]


def envelope():
    return {"body": "I see two possible ways to explore this direction.", "options": options(),
        "comparedOptions": ["1", "2"], "discriminant": "grouped contour versus open spacing",
        "question": "Would you prefer a grouped contour or more open spacing?"}


def with_options(execution, stage_context):
    """Give an HTTP-boundary fixture the validated metadata of its mocked generator."""
    discovery = stage_context["proposalDiscovery"]
    option_set = proposal_options.build_set(options(), discovery, stage_context["stageSnapshot"])
    question = envelope()["question"]
    if int(discovery.get("clarificationQuestionCount") or 0):
        question = "Would you accept less nearby space for the grouped contour?"
    body = "\n\n".join(["I see two possible directions.", proposal_options.render_options(option_set), question])
    return replace(execution, assistant_message=body, proposal_diagnostics={
        "optionSet": option_set, "optionQuestion": {"comparedOptionIds": [item["id"] for item in option_set["options"]],
            "discriminant": envelope()["discriminant"]}, "optionOutcome": "clarifying",
        "clarificationQuestion": question, "clarificationQuestionValidated": True})
