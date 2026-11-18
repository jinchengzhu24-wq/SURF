import re

from . import __name__  # noqa: F401
from ..config import SHORT_LLM_TIMEOUT_SECONDS
from ..llm.prompts import build_dg_guide_summary_messages
from ..llm.runtime import LLMServiceError
from ..runtime_state import call_app_hook


DG_DIFFICULTY_ANSWERS = {
    "quick_start": 0,
    "observe_then_decide": 1,
    "plan_ahead": 2,
    "easy_to_adjust": 0,
    "consider_order": 1,
    "connected_pushes": 2,
    "no_preference": None,
}
DG_LAYOUT_ANSWERS = {
    "focused_area": 0,
    "connected_areas": 1,
    "wide_area": 2,
    "short_routes": 0,
    "occasional_detours": 1,
    "long_routes": 2,
    "no_preference": None,
}
DG_FIRST_PERSON_PREFIXES = (
    "got it",
    "let's try",
    "let us try",
    "i'd try",
    "how about",
    "i picture",
    "i'm picturing",
    "your choices suggest",
    "i get the sense that",
    "it sounds like",
    "my read is that",
    "i'm hearing that",
    "my guess is that",
    "i think",
    "i feel",
    "from my perspective",
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_TERMINATOR_RE = re.compile(r"[.!?](?:\s|$)")


def _fit_within_limit(text, maximum_length, preserve_terminal_punct=False):
    text = str(text or "").strip()
    if len(text) <= maximum_length:
        return text
    if preserve_terminal_punct:
        terminal = text[-1] if text[-1] in ".!?" else "."
        cutoff = max(1, maximum_length - 1)
        trimmed = text[:cutoff].rstrip(" ,;:-")
        if " " in trimmed:
            candidate = trimmed.rsplit(" ", 1)[0].rstrip(" ,;:-")
            if len(candidate) >= 24:
                trimmed = candidate
        return trimmed.rstrip(".!?") + terminal

    sentences = _SENTENCE_SPLIT_RE.split(text)
    if len(sentences) >= 3:
        kept = []
        for sentence in sentences:
            candidate = " ".join(kept + [sentence]).strip()
            if len(candidate) <= maximum_length:
                kept.append(sentence)
            else:
                break
        if len(kept) >= 2:
            return " ".join(kept)

    cutoff = max(1, maximum_length - 1)
    trimmed = text[:cutoff].rstrip(" ,;:-")
    if " " in trimmed:
        candidate = trimmed.rsplit(" ", 1)[0].rstrip(" ,;:-")
        if len(candidate) >= 24:
            trimmed = candidate
    return trimmed.rstrip(".!?") + "."


def normalize_dg_reflection(summary):
    summary = str(summary or "").strip()
    if not summary:
        return ""
    if summary.lower().startswith(DG_FIRST_PERSON_PREFIXES):
        normalized = summary
    else:
        normalized = "My read is that " + summary
    normalized = _fit_within_limit(normalized, 480, preserve_terminal_punct=False)
    sentence_count = len(_TERMINATOR_RE.findall(normalized))
    starts_with_short_interjection = bool(
        re.match(r"^(?:Got it|Nice|Sure|Okay|Alright|Sounds good)!\s+", normalized, re.IGNORECASE)
    )
    max_sentences = 4 if starts_with_short_interjection else 3
    if sentence_count < 2 or sentence_count > max_sentences:
        raise ValueError("DG guide summary must be 2 or 3 sentences")
    return normalized


def normalize_dg_rationale(rationale, label):
    normalized = str(rationale or "").strip()
    sentence_count = len(_TERMINATOR_RE.findall(normalized))

    if len(normalized) < 30 or sentence_count != 1:
        raise ValueError(
            f"DG guide {label} rationale must explain its recommendation in one sentence"
        )
    if not re.search(r"\bI(?:['’](?:d|m|ve|ll))?\b", normalized, re.IGNORECASE):
        normalized = "I read this as: " + normalized
    if len(normalized) > 320:
        normalized = _fit_within_limit(normalized, 320, preserve_terminal_punct=True)
    return normalized


def dg_preference_score(first, second, choices):
    explicit = [choices[first], choices[second]]
    explicit = [value for value in explicit if value is not None]
    if not explicit:
        # Both answers are no_preference. Preserve that uncertainty as Random
        # and let Confirm resolve it to a concrete value.
        return None
    if len(explicit) == 1 or explicit[0] == explicit[-1]:
        return explicit[0]
    # Adjacent directions use the ordered endpoints from the neutral rule:
    # low+middle stays low, while middle+high stays high.  A two-step
    # disagreement stays at the middle so neither extreme wins.
    low, high = sorted(explicit)
    if high - low == 1:
        return low if low == 0 else high
    return 1


def dg_preference_allowed_scores(first, second, choices):
    """Return score values an LLM may recommend for one parameter group."""
    values = [choices[first], choices[second]]
    explicit = [value for value in values if value is not None]
    if not explicit:
        return None
    if len(explicit) == 1 or explicit[0] == explicit[-1]:
        return {explicit[0]}
    baseline = dg_preference_score(first, second, choices)
    return {
        baseline,
        max(0, baseline - 1),
        min(2, baseline + 1),
    }


def dg_choice_phrase(value, phrases, empty_phrase):
    return phrases.get(value) or empty_phrase


def build_dg_fallback_summary(context, reason=""):
    difficulty_score = dg_preference_score(
        context["firstMovePreference"],
        context["pushPlanningPreference"],
        DG_DIFFICULTY_ANSWERS,
    )
    layout_score = dg_preference_score(
        context["spacePreference"],
        context["routeRhythmPreference"],
        DG_LAYOUT_ANSWERS,
    )
    difficulty = "Random" if difficulty_score is None else ("Easy", "Medium", "Hard")[difficulty_score]
    layout = "Random" if layout_score is None else ("Compact", "Balanced", "Open")[layout_score]
    first_move = dg_choice_phrase(
        context["firstMovePreference"],
        {
            "quick_start": "a quick start",
            "observe_then_decide": "a look at boxes, goals, and paths",
            "plan_ahead": "time to look over the map and plan",
        },
        "no set start style",
    )
    push_planning = dg_choice_phrase(
        context["pushPlanningPreference"],
        {
            "easy_to_adjust": "pushes that mostly work on their own",
            "consider_order": "some pushes linked by place or order",
            "connected_pushes": "several pushes that need earlier pushes",
        },
        "no set push links",
    )
    space = dg_choice_phrase(
        context["spacePreference"],
        {
            "focused_area": "key spots together in one area",
            "connected_areas": "key spots in a few linked areas",
            "wide_area": "key spots spread across a wider area",
        },
        "no set spot layout",
    )
    route = dg_choice_phrase(
        context["routeRhythmPreference"],
        {
            "short_routes": "short paths between nearby spots",
            "occasional_detours": "mostly direct paths with a few side trips",
            "long_routes": "long paths to explore or come back",
        },
        "no set path length",
    )
    if difficulty == "Random":
        difficulty_rationale = (
            "I do not see a directional planning preference in the two answers, so I would leave Difficulty Random for Confirm."
        )
    else:
        difficulty_rationale = (
            "I connect the first-move choice to " + first_move + " and the push decisions to "
            + push_planning + ", which supports " + difficulty + " planning complexity."
        )
    if layout == "Random":
        layout_rationale = (
            "I do not see a directional spatial preference in the two answers, so I would leave Layout Random for Confirm."
        )
    else:
        layout_rationale = (
            "I connect the space choice to " + space + " and the route choice to " + route
            + ", which supports a " + layout + " layout."
        )
    return {
        "summary": (
            "Got it, I'd try " + first_move + " with " + push_planning
            + " for now. How about " + space + " and " + route + "?"
        )[:480],
        "recommendedDifficulty": difficulty,
        "difficultyRationale": difficulty_rationale,
        "recommendedLayout": layout,
        "layoutRationale": layout_rationale,
        "source": "deterministic_fallback",
        "fallbackReason": reason,
    }


def create_dg_guide_summary(context, request_id=""):
    fallback = build_dg_fallback_summary(context)

    def validate(payload):
        if not isinstance(payload, dict):
            raise ValueError("DG guide response must be a JSON object")
        summary = normalize_dg_reflection(payload.get("summary"))
        difficulty_rationale = normalize_dg_rationale(
            payload.get("difficultyRationale"),
            "difficulty",
        )
        layout_rationale = normalize_dg_rationale(
            payload.get("layoutRationale"),
            "layout",
        )
        difficulty = str(payload.get("recommendedDifficulty") or "").strip()
        layout = str(payload.get("recommendedLayout") or "").strip()
        if not summary:
            raise ValueError("DG guide summary is required")
        if difficulty not in {"Easy", "Medium", "Hard", "Random"}:
            raise ValueError("DG guide difficulty is invalid")
        if layout not in {"Compact", "Balanced", "Open", "Random"}:
            raise ValueError("DG guide layout is invalid")
        difficulty_scores = dg_preference_allowed_scores(
            context["firstMovePreference"],
            context["pushPlanningPreference"],
            DG_DIFFICULTY_ANSWERS,
        )
        layout_scores = dg_preference_allowed_scores(
            context["spacePreference"],
            context["routeRhythmPreference"],
            DG_LAYOUT_ANSWERS,
        )
        allowed_difficulties = (
            {"Random"}
            if difficulty_scores is None
            else {("Easy", "Medium", "Hard")[score] for score in difficulty_scores}
        )
        allowed_layouts = (
            {"Random"}
            if layout_scores is None
            else {("Compact", "Balanced", "Open")[score] for score in layout_scores}
        )
        if difficulty not in allowed_difficulties or layout not in allowed_layouts:
            raise ValueError("DG guide recommendation does not match the neutral answer rules")
        return {
            "summary": summary,
            "recommendedDifficulty": difficulty,
            "difficultyRationale": difficulty_rationale,
            "recommendedLayout": layout,
            "layoutRationale": layout_rationale,
            "source": "llm",
        }

    try:
        execution = call_app_hook(
            "execute_json_request",
            task="dg_guide_summary",
            messages=build_dg_guide_summary_messages(context),
            validator=validate,
            temperature=0.2,
            timeout_seconds=SHORT_LLM_TIMEOUT_SECONDS,
            max_attempts=1,
            request_id=request_id,
            validation_stage="dg_guide_validation",
        )
        return execution.value
    except LLMServiceError as exception:
        fallback["fallbackReason"] = exception.safe_message
        return fallback
