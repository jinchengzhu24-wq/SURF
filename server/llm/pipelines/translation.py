"""Native-async turn translation pipeline with independent generation & verification budgets."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.llm_client import LLMTransport, LLMValidationError, call_structured
from server.i18n.messages import normalize_language
from server.llm.grounding import (
    extract_coordinate_links,
    sanitize_visible_text,
    validate_visible_language,
    verify_grounding,
)
from server.llm.prompts.translation import (
    build_translation_messages,
    build_translation_verification_messages,
)
from server.llm.schemas import (
    TranslatedTurnItemPayload,
    TranslationPayload,
    TranslationVerificationPayload,
)

_SCALAR_FIELDS: tuple[str, ...] = (
    "followUpQuestion",
    "intentHypothesis",
    "proposalOfferSummary",
    "proposalOfferRationale",
    "proposalPresentationReason",
    "proposalSummary",
)
_QUESTION_LOCKED_KEYS = (
    "questionId",
    "topicId",
    "baseVersionId",
    "number",
    "selectedChoiceId",
    "status",
)


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """Validated translated turn items with separate generation and verification counts."""

    translations: list[dict[str, Any]]
    generation_attempts: int
    verification_attempts: int
    verified_independently: bool
    model: str
    request_id: str
    latency_ms: int
    usage: dict[str, int | None] = field(default_factory=dict)


def _needs_independent_verification(items: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        bool(item.get("proposalQuestion")) or "proposalVerifiedActions" in item for item in items
    )


def _validate_text(
    value: str | None,
    source_value: Any,
    field_name: str,
    locale: str,
    *,
    allow_empty: bool = False,
) -> str | None:
    if source_value is None:
        if value is not None:
            raise LLMValidationError("TRANSLATION_NULL_VIOLATION", f"{field_name} must be null.")
        return None
    if not isinstance(value, str):
        raise LLMValidationError("TRANSLATION_TYPE_ERROR", f"{field_name} must be a string.")
    cleaned = sanitize_visible_text(value, locale)
    if not cleaned and not allow_empty and str(source_value).strip():
        raise LLMValidationError("TRANSLATION_EMPTY_FIELD", f"{field_name} must not be empty.")
    if len(cleaned) > max(400, len(str(source_value)) * 4 + 200):
        raise LLMValidationError("TRANSLATION_TOO_LONG", f"{field_name} is unexpectedly long.")
    leaks = validate_visible_language(cleaned, locale)
    if leaks:
        raise LLMValidationError("TRANSLATION_INTERNAL_LEAK", leaks[0])
    return cleaned


def _validate_proposal_question(
    tr_q: Any, src_q: Mapping[str, Any], turn_id: str, locale: str
) -> dict[str, Any]:
    if not isinstance(tr_q, Mapping):
        raise LLMValidationError(
            "TRANSLATION_QUESTION_MISSING", f"{turn_id}.proposalQuestion is required."
        )
    for key in _QUESTION_LOCKED_KEYS:
        if key in src_q and tr_q.get(key) != src_q.get(key):
            raise LLMValidationError(
                "TRANSLATION_QUESTION_ID_MUTATED", f"{turn_id}.proposalQuestion.{key} changed."
            )
    src_choices = list(src_q.get("choices") or [])
    tr_choices = list(tr_q.get("choices") or [])
    if len(tr_choices) != len(src_choices):
        raise LLMValidationError("TRANSLATION_CHOICE_COUNT", f"{turn_id} choices count mismatch.")
    norm_choices: list[dict[str, Any]] = []
    for idx, (sc, tc) in enumerate(zip(src_choices, tr_choices, strict=True)):
        if tc.get("choiceId") != sc.get("choiceId") or tc.get("letter") != sc.get("letter"):
            raise LLMValidationError("TRANSLATION_CHOICE_ID_MUTATED", f"choice[{idx}] ID changed.")
        label = _validate_text(tc.get("label"), sc.get("label"), f"choice[{idx}].label", locale)
        norm_choices.append({**sc, "label": label or ""})
    return {
        **src_q,
        "question": _validate_text(tr_q.get("question"), src_q.get("question"), "question", locale)
        or "",
        "note": _validate_text(
            tr_q.get("note"), src_q.get("note", ""), "note", locale, allow_empty=True
        )
        or "",
        "choices": norm_choices,
    }


def _validate_disagreement(
    tr_dis: Any, src_dis: Any, turn_id: str, locale: str
) -> dict[str, Any] | None:
    if src_dis is None:
        if tr_dis is not None:
            raise LLMValidationError(
                "TRANSLATION_DISAGREEMENT_NULL", f"{turn_id}.disagreement must remain null."
            )
        return None
    if not isinstance(tr_dis, Mapping):
        raise LLMValidationError(
            "TRANSLATION_DISAGREEMENT_MISSING", f"{turn_id}.disagreement is required."
        )
    for key in ("status", "subject", "resolution", "phase", "displayCard"):
        if key in src_dis and tr_dis.get(key) != src_dis.get(key):
            raise LLMValidationError(
                "TRANSLATION_DISAGREEMENT_MUTATED", f"{turn_id}.disagreement.{key} changed."
            )
    norm_dis = dict(src_dis)
    for key in ("userPosition", "aiPosition", "coreDisagreement", "nextQuestion"):
        if key in src_dis:
            norm_dis[key] = _validate_text(tr_dis.get(key), src_dis.get(key), key, locale)
    return norm_dis


def _validate_item(
    item: TranslatedTurnItemPayload, source: Mapping[str, Any], locale: str
) -> dict[str, Any]:
    raw = item.model_dump(by_alias=True)
    tid = item.turn_id
    out: dict[str, Any] = {"turnId": tid}

    body = _validate_text(
        raw.get("body"), source.get("body", ""), f"{tid}.body", locale, allow_empty=True
    )
    if source.get("body") and not body:
        raise LLMValidationError("TRANSLATION_EMPTY_BODY", f"{tid}.body cannot be empty.")
    out["body"] = body or ""

    for name in _SCALAR_FIELDS:
        if name in source:
            out[name] = _validate_text(raw.get(name), source[name], f"{tid}.{name}", locale)

    src_cues = list(source.get("uiCueTexts") or [])
    tr_cues = list(raw.get("uiCueTexts") or [])
    if len(tr_cues) != len(src_cues):
        raise LLMValidationError("TRANSLATION_CUES_COUNT", f"{tid}.uiCueTexts count mismatch.")
    out["uiCueTexts"] = [
        _validate_text(t, s, f"{tid}.uiCueTexts[{i}]", locale) or ""
        for i, (t, s) in enumerate(zip(tr_cues, src_cues, strict=True))
    ]

    if "proposalVerifiedActions" in source:
        src_act = list(source["proposalVerifiedActions"] or [])
        tr_act = raw.get("proposalVerifiedActions")
        if not isinstance(tr_act, list) or len(tr_act) != len(src_act):
            raise LLMValidationError("TRANSLATION_ACTIONS_COUNT", f"{tid} actions count mismatch.")
        out["proposalVerifiedActions"] = [
            _validate_text(t, s, f"{tid}.proposalVerifiedActions[{i}]", locale) or ""
            for i, (t, s) in enumerate(zip(tr_act, src_act, strict=True))
        ]

    if source.get("proposalQuestion"):
        out["proposalQuestion"] = _validate_proposal_question(
            raw.get("proposalQuestion"), source["proposalQuestion"], tid, locale
        )
    if "disagreement" in source:
        out["disagreement"] = _validate_disagreement(
            raw.get("disagreement"), source.get("disagreement"), tid, locale
        )

    src_links = list(source.get("coordinateLinks") or [])
    stage_rows, bindings = source.get("stageRows"), source.get("entityBindings")
    if src_links:
        tr_links = raw.get("coordinateLinkTexts")
        if not isinstance(tr_links, list) or len(tr_links) != len(src_links):
            raise LLMValidationError("TRANSLATION_LINKS_COUNT", f"{tid} link count mismatch.")
        norm_links: list[str] = []
        for idx, (lt, sl) in enumerate(zip(tr_links, src_links, strict=True)):
            clean = _validate_text(lt, sl.get("text"), f"{tid}.coordinateLinkTexts[{idx}]", locale)
            if not clean or clean not in out["body"]:
                raise LLMValidationError(
                    "TRANSLATION_LINK_NOT_SUBSTRING", f"{tid} link[{idx}] not in translated body."
                )
            if stage_rows and "from" in sl and "to" in sl:
                grounded = extract_coordinate_links(
                    out["body"],
                    stage_rows,
                    candidate_links=[{"text": clean, "from": sl["from"], "to": sl["to"]}],
                    entity_bindings=bindings,
                )
                if not any(g["text"] == clean for g in grounded):
                    raise LLMValidationError(
                        "TRANSLATION_LINK_UNGROUNDED", f"{tid} link[{idx}] fails endpoint check."
                    )
            norm_links.append(clean)
        out["coordinateLinkTexts"] = norm_links

    if stage_rows:
        check_texts = [out["body"], *out["uiCueTexts"]]
        check_texts.extend(out[k] for k in _SCALAR_FIELDS if out.get(k))
        if isinstance(out.get("proposalQuestion"), Mapping):
            pq = out["proposalQuestion"]
            check_texts.extend([pq["question"], pq["note"], *(c["label"] for c in pq["choices"])])
        for text in check_texts:
            issues = verify_grounding(text, stage_rows, bindings)
            if issues:
                raise LLMValidationError("TRANSLATION_GROUNDING_MISMATCH", issues[0].message)
    return out


class TranslationPipeline:
    """Native-async translation pipeline with independent generation & verification budgets.

    Fixes the legacy attempt-budget deadlock where generation and option verification
    shared one attempt counter and failed with 'no remaining verification slot' whenever
    generation needed a retry.
    """

    def __init__(
        self,
        transport: LLMTransport,
        *,
        model: str,
        clock: Clock | None = None,
        extra_body: Mapping[str, Any] | None = None,
        generation_max_attempts: int = 2,
        verification_max_attempts: int = 2,
    ) -> None:
        self.transport = transport
        self.model = model
        self.clock = clock if clock is not None else SYSTEM_CLOCK
        self.extra_body = dict(extra_body) if extra_body is not None else None
        self.generation_max_attempts = max(1, int(generation_max_attempts))
        self.verification_max_attempts = max(1, int(verification_max_attempts))

    async def translate_turns(
        self,
        items: Sequence[Mapping[str, Any]],
        *,
        target_language: str,
        deadline: float | None = None,
        request_id: str = "",
    ) -> TranslationResult:
        """Translate turns and run independent verification with its own attempt budget."""
        if not items:
            return TranslationResult([], 0, 0, False, self.model, request_id, 0)

        started = self.clock.perf_counter()
        locale = normalize_language(target_language)
        source_by_id = {str(item["turnId"]): item for item in items}
        messages = build_translation_messages(items=items, target_language=locale)
        holder: dict[str, list[dict[str, Any]]] = {}

        def _validate_gen(parsed: TranslationPayload) -> TranslationPayload:
            if len(parsed.translations) != len(items):
                raise LLMValidationError(
                    "TRANSLATION_COUNT_MISMATCH", "Expected one translated item per source turn."
                )
            by_id: dict[str, dict[str, Any]] = {}
            for tr_item in parsed.translations:
                if tr_item.turn_id not in source_by_id or tr_item.turn_id in by_id:
                    raise LLMValidationError(
                        "TRANSLATION_TURN_ID_MISMATCH", "turnIds must match requested turns."
                    )
                by_id[tr_item.turn_id] = _validate_item(
                    tr_item, source_by_id[tr_item.turn_id], locale
                )
            holder["items"] = [by_id[str(src["turnId"])] for src in items]
            return parsed

        gen_res = await call_structured(
            self.transport,
            task="translation",
            model=self.model,
            messages=messages,
            response_model=TranslationPayload,
            validator=_validate_gen,
            deadline=deadline,
            max_attempts=self.generation_max_attempts,
            temperature=0.1,
            max_tokens=3200,
            attempt_timeout_seconds=55.0,
            extra_body=self.extra_body,
            clock=self.clock,
            request_id=request_id,
        )
        translated = holder["items"]
        verify_attempts = 0
        verified_independently = False

        if _needs_independent_verification(items):
            v_messages = build_translation_verification_messages(
                source_items=items, translated_items=translated, target_language=locale
            )
            v_res = await call_structured(
                self.transport,
                task="translation_verification",
                model=self.model,
                messages=v_messages,
                response_model=TranslationVerificationPayload,
                deadline=deadline,
                max_attempts=self.verification_max_attempts,
                temperature=0.0,
                max_tokens=200,
                attempt_timeout_seconds=15.0,
                extra_body=self.extra_body,
                clock=self.clock,
                request_id=request_id,
            )
            verify_attempts = v_res.attempts_used
            if not v_res.parsed.accepted:
                raise LLMValidationError(
                    "TRANSLATION_VERIFICATION_REJECTED",
                    v_res.parsed.reason or "Proposal translation rejected by verifier.",
                    request_id=v_res.request_id,
                    attempts_used=gen_res.attempts_used + verify_attempts,
                )
            verified_independently = True

        return TranslationResult(
            translations=translated,
            generation_attempts=gen_res.attempts_used,
            verification_attempts=verify_attempts,
            verified_independently=verified_independently,
            model=gen_res.model,
            request_id=gen_res.request_id,
            latency_ms=round((self.clock.perf_counter() - started) * 1000),
            usage=gen_res.usage,
        )
