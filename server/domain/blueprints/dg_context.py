"""DG (Design Guidance) neutral answer choices, context models, and validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.i18n.messages import format_dg_fallback_summary, get_message, normalize_language

from .models import DifficultyLevel, LayoutStyle

DG_ANSWER_CHOICES: dict[str, frozenset[str]] = {
    "q1Answer": frozenset({"quick_start", "observe_then_decide", "plan_ahead", "no_preference"}),
    "q2Answer": frozenset(
        {"easy_to_adjust", "consider_order", "connected_pushes", "no_preference"}
    ),
    "q3Answer": frozenset({"focused_area", "connected_areas", "wide_area", "no_preference"}),
    "q4Answer": frozenset({"short_routes", "occasional_detours", "long_routes", "no_preference"}),
}

DG_DIFFICULTY_ANSWERS: dict[str, int | None] = {
    "quick_start": 0,
    "observe_then_decide": 1,
    "plan_ahead": 2,
    "easy_to_adjust": 0,
    "consider_order": 1,
    "connected_pushes": 2,
    "no_preference": None,
}

DG_LAYOUT_ANSWERS: dict[str, int | None] = {
    "focused_area": 0,
    "connected_areas": 1,
    "wide_area": 2,
    "short_routes": 0,
    "occasional_detours": 1,
    "long_routes": 2,
    "no_preference": None,
}

ONLINE_DRAFT_METADATA_KEYS: tuple[str, ...] = (
    "q1Answer",
    "q2Answer",
    "q3Answer",
    "q4Answer",
    "aiReflection",
    "aiDifficultyRationale",
    "aiLayoutRationale",
    "aiRecommendedDifficulty",
    "aiRecommendedLayout",
    "aiRecommendationSource",
)

ONLINE_DRAFT_RECOMMENDATION_SOURCES: frozenset[str] = frozenset(
    {"llm", "deterministic_fallback"}
)

DG_FIRST_PERSON_PREFIXES: tuple[str, ...] = (
    "got it",
    "nice!",
    "sure!",
    "okay!",
    "alright!",
    "sounds good!",
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


class DGContext(BaseModel):
    """Four neutral design guidance choices."""

    model_config = ConfigDict(populate_by_name=True)

    first_move_preference: str = Field(alias="firstMovePreference")
    push_planning_preference: str = Field(alias="pushPlanningPreference")
    space_preference: str = Field(alias="spacePreference")
    route_rhythm_preference: str = Field(alias="routeRhythmPreference")
    language: str = "en"

    @model_validator(mode="after")
    def _validate_answers(self) -> DGContext:
        checks = (
            ("firstMovePreference", self.first_move_preference, DG_ANSWER_CHOICES["q1Answer"]),
            (
                "pushPlanningPreference",
                self.push_planning_preference,
                DG_ANSWER_CHOICES["q2Answer"],
            ),
            ("spacePreference", self.space_preference, DG_ANSWER_CHOICES["q3Answer"]),
            ("routeRhythmPreference", self.route_rhythm_preference, DG_ANSWER_CHOICES["q4Answer"]),
        )
        for field_name, val, allowed in checks:
            if val not in allowed:
                raise ValueError(f"Invalid DG answer for {field_name}: {val!r}")
        return self


class DGGuideSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    summary: str
    recommended_difficulty: DifficultyLevel = Field(alias="recommendedDifficulty")
    difficulty_rationale: str = Field(alias="difficultyRationale")
    recommended_layout: LayoutStyle = Field(alias="recommendedLayout")
    layout_rationale: str = Field(alias="layoutRationale")
    source: str
    fallback_reason: str | None = Field(default=None, alias="fallbackReason")


def dg_preference_score(first: str, second: str, choices: Mapping[str, int | None]) -> int | None:
    explicit = [val for val in (choices[first], choices[second]) if val is not None]
    if not explicit:
        return None
    if len(explicit) == 1 or explicit[0] == explicit[-1]:
        return explicit[0]
    low, high = sorted(explicit)
    if high - low == 1:
        return low if low == 0 else high
    return 1


def dg_preference_allowed_scores(
    first: str,
    second: str,
    choices: Mapping[str, int | None],
) -> set[int] | None:
    explicit = [val for val in (choices[first], choices[second]) if val is not None]
    if not explicit:
        return None
    if len(explicit) == 1 or explicit[0] == explicit[-1]:
        return {explicit[0]}
    baseline = dg_preference_score(first, second, choices)
    assert baseline is not None
    return {baseline, max(0, baseline - 1), min(2, baseline + 1)}


def _fit_within_limit(text: str, maximum_length: int, preserve_terminal_punct: bool = False) -> str:
    clean = str(text or "").strip()
    if len(clean) <= maximum_length:
        return clean
    if preserve_terminal_punct:
        terminal = clean[-1] if clean[-1] in ".!?" else "."
        cutoff = max(1, maximum_length - 1)
        trimmed = clean[:cutoff].rstrip(" ,;:-")
        if " " in trimmed:
            candidate = trimmed.rsplit(" ", 1)[0].rstrip(" ,;:-")
            if len(candidate) >= 24:
                trimmed = candidate
        return trimmed.rstrip(".!?") + terminal

    sentences = _SENTENCE_SPLIT_RE.split(clean)
    if len(sentences) >= 3:
        kept: list[str] = []
        for sentence in sentences:
            candidate = " ".join([*kept, sentence]).strip()
            if len(candidate) <= maximum_length:
                kept.append(sentence)
            else:
                break
        if len(kept) >= 2:
            return " ".join(kept)

    cutoff = max(1, maximum_length - 1)
    trimmed = clean[:cutoff].rstrip(" ,;:-")
    if " " in trimmed:
        candidate = trimmed.rsplit(" ", 1)[0].rstrip(" ,;:-")
        if len(candidate) >= 24:
            trimmed = candidate
    return trimmed.rstrip(".!?") + "."


def normalize_dg_reflection(summary: Any, language: str = "en") -> str:
    clean = str(summary or "").strip()
    if not clean:
        return ""
    if clean.lower().startswith(DG_FIRST_PERSON_PREFIXES):
        normalized = clean
    else:
        normalized = get_message("dg.reflection_prefix", language) + clean
    normalized = _fit_within_limit(normalized, 480, preserve_terminal_punct=False)
    sentence_count = len(_TERMINATOR_RE.findall(normalized))
    starts_with_interjection = bool(
        re.match(r"^(?:Got it|Nice|Sure|Okay|Alright|Sounds good)!\s+", normalized, re.IGNORECASE)
    )
    max_sentences = 4 if starts_with_interjection else 3
    if sentence_count < 2 or sentence_count > max_sentences:
        raise ValueError("DG guide summary must be 2 or 3 sentences")
    return normalized


def normalize_dg_rationale(rationale: Any, label: str, language: str = "en") -> str:
    normalized = str(rationale or "").strip()
    sentence_count = len(_TERMINATOR_RE.findall(normalized))
    if len(normalized) < 30 or sentence_count != 1:
        raise ValueError(
            f"DG guide {label} rationale must explain its recommendation in one sentence"
        )
    if not re.search(r"\bI(?:['’](?:d|m|ve|ll))?\b", normalized, re.IGNORECASE):
        normalized = get_message("dg.rationale_prefix", language) + normalized
    if len(normalized) > 320:
        normalized = _fit_within_limit(normalized, 320, preserve_terminal_punct=True)
    return normalized


def build_dg_fallback_summary(
    context: DGContext | Mapping[str, Any],
    reason: str = "",
) -> dict[str, Any]:
    ctx = context.model_dump(by_alias=True) if isinstance(context, DGContext) else dict(context)
    difficulty_score = dg_preference_score(
        ctx["firstMovePreference"], ctx["pushPlanningPreference"], DG_DIFFICULTY_ANSWERS
    )
    layout_score = dg_preference_score(
        ctx["spacePreference"], ctx["routeRhythmPreference"], DG_LAYOUT_ANSWERS
    )
    if difficulty_score is None:
        difficulty = DifficultyLevel.RANDOM.value
    else:
        difficulty = ("Easy", "Medium", "Hard")[difficulty_score]
    if layout_score is None:
        layout = LayoutStyle.RANDOM.value
    else:
        layout = ("Compact", "Balanced", "Open")[layout_score]
    return format_dg_fallback_summary(
        first_move_key=(
            ctx["firstMovePreference"]
            if ctx["firstMovePreference"] != "no_preference"
            else "no_set_start"
        ),
        push_planning_key=(
            ctx["pushPlanningPreference"]
            if ctx["pushPlanningPreference"] != "no_preference"
            else "no_set_push"
        ),
        space_key=(
            ctx["spacePreference"]
            if ctx["spacePreference"] != "no_preference"
            else "no_set_space"
        ),
        route_key=(
            ctx["routeRhythmPreference"]
            if ctx["routeRhythmPreference"] != "no_preference"
            else "no_set_route"
        ),
        difficulty=difficulty,
        layout=layout,
        reason=reason,
        language=normalize_language(ctx.get("language")),
    )


def validate_dg_guide_payload(
    payload: Any,
    context: DGContext | Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("DG guide response must be a JSON object")
    ctx = context.model_dump(by_alias=True) if isinstance(context, DGContext) else dict(context)
    language = normalize_language(ctx.get("language"))
    summary = normalize_dg_reflection(payload.get("summary"), language=language)
    difficulty_rationale = normalize_dg_rationale(
        payload.get("difficultyRationale"), "difficulty", language=language
    )
    layout_rationale = normalize_dg_rationale(
        payload.get("layoutRationale"), "layout", language=language
    )
    difficulty = str(payload.get("recommendedDifficulty") or "").strip()
    layout = str(payload.get("recommendedLayout") or "").strip()
    if not summary:
        raise ValueError("DG guide summary is required")
    if difficulty not in {d.value for d in DifficultyLevel}:
        raise ValueError("DG guide difficulty is invalid")
    if layout not in {s.value for s in LayoutStyle}:
        raise ValueError("DG guide layout is invalid")

    difficulty_scores = dg_preference_allowed_scores(
        ctx["firstMovePreference"], ctx["pushPlanningPreference"], DG_DIFFICULTY_ANSWERS
    )
    layout_scores = dg_preference_allowed_scores(
        ctx["spacePreference"], ctx["routeRhythmPreference"], DG_LAYOUT_ANSWERS
    )
    allowed_difficulties = (
        {DifficultyLevel.RANDOM.value}
        if difficulty_scores is None
        else {("Easy", "Medium", "Hard")[score] for score in difficulty_scores}
    )
    allowed_layouts = (
        {LayoutStyle.RANDOM.value}
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
