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
    is_plain_translation_batch,
)
from server.llm.schemas import (
    PlainTranslationPayload,
    TranslatedPlainTurnItemPayload,
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
)
_DISAGREEMENT_TEXT_KEYS: tuple[str, ...] = (
    "userPosition",
    "aiPosition",
    "coreDisagreement",
    "nextQuestion",
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
            raise LLMValidationError(
                "TRANSLATION_NULL_VIOLATION", f"{field_name} must remain null."
            )
        return None
    if not isinstance(value, str):
        raise LLMValidationError(
            "TRANSLATION_TYPE_ERROR",
            f"{field_name} must be a string (got {type(value).__name__}).",
        )
    cleaned = sanitize_visible_text(value, locale)
    if not cleaned and not allow_empty and str(source_value).strip():
        raise LLMValidationError("TRANSLATION_EMPTY_FIELD", f"{field_name} must not be empty.")
    limit = max(400, len(str(source_value)) * 4 + 200)
    if len(cleaned) > limit:
        raise LLMValidationError(
            "TRANSLATION_TOO_LONG",
            f"{field_name} length {len(cleaned)} exceeds max limit {limit}.",
        )
    leaks = validate_visible_language(cleaned, locale)
    if leaks:
        joined = "; ".join(dict.fromkeys(leaks[:4]))
        raise LLMValidationError("TRANSLATION_INTERNAL_LEAK", f"{field_name}: {joined}")
    return cleaned


def _validate_proposal_question(
    tr_q: Any, src_q: Mapping[str, Any], turn_id: str, locale: str
) -> dict[str, Any]:
    if not isinstance(tr_q, Mapping):
        raise LLMValidationError(
            "TRANSLATION_QUESTION_MISSING", f"{turn_id}.proposalQuestion is required."
        )
    src_choices = list(src_q.get("choices") or [])
    tr_choices = list(tr_q.get("choices") or [])
    if len(tr_choices) != len(src_choices):
        raise LLMValidationError(
            "TRANSLATION_CHOICE_COUNT",
            f"{turn_id}.proposalQuestion.choices count mismatch: expected {len(src_choices)}, "
            f"got {len(tr_choices)}.",
        )
    norm_choices: list[dict[str, Any]] = []
    for idx, (sc, tc) in enumerate(zip(src_choices, tr_choices, strict=True)):
        expected_cid = sc.get("choiceId")
        actual_cid = tc.get("choiceId")
        if actual_cid != expected_cid:
            raise LLMValidationError(
                "TRANSLATION_CHOICE_ID_MUTATED",
                f"{turn_id}.proposalQuestion.choices[{idx}].choiceId expected "
                f"{expected_cid!r}, got {actual_cid!r}.",
            )
        label = _validate_text(
            tc.get("label"),
            sc.get("label"),
            f"{turn_id}.proposalQuestion.choices[{idx}].label",
            locale,
        )
        norm_choices.append({**sc, "label": label or ""})
    return {
        **src_q,
        "question": _validate_text(
            tr_q.get("question"),
            src_q.get("question"),
            f"{turn_id}.proposalQuestion.question",
            locale,
        )
        or "",
        "note": _validate_text(
            tr_q.get("note"),
            src_q.get("note", ""),
            f"{turn_id}.proposalQuestion.note",
            locale,
            allow_empty=True,
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
    # Merge translated visible text back onto authoritative server metadata
    # (preserving disagreementId, challengeId, status, subject, resolution, phase, etc.).
    norm_dis = dict(src_dis)
    for key in _DISAGREEMENT_TEXT_KEYS:
        if key in src_dis:
            norm_dis[key] = _validate_text(
                tr_dis.get(key),
                src_dis.get(key),
                f"{turn_id}.disagreement.{key}",
                locale,
            )
    return norm_dis


def _validate_item(
    item: TranslatedTurnItemPayload | TranslatedPlainTurnItemPayload,
    source: Mapping[str, Any],
    locale: str,
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
    if "proposalOfferSummary" not in source and "proposalSummary" in source:
        out["proposalOfferSummary"] = _validate_text(
            raw.get("proposalOfferSummary"),
            source["proposalSummary"],
            f"{tid}.proposalOfferSummary",
            locale,
        )

    src_cues = list(source.get("uiCueTexts") or [])
    tr_cues = list(raw.get("uiCueTexts") or [])
    if len(tr_cues) != len(src_cues):
        raise LLMValidationError(
            "TRANSLATION_CUES_COUNT",
            f"{tid}.uiCueTexts count mismatch: expected {len(src_cues)}, got {len(tr_cues)}.",
        )
    out["uiCueTexts"] = [
        _validate_text(t, s, f"{tid}.uiCueTexts[{i}]", locale) or ""
        for i, (t, s) in enumerate(zip(tr_cues, src_cues, strict=True))
    ]

    if "proposalVerifiedActions" in source:
        src_act = list(source["proposalVerifiedActions"] or [])
        tr_act = raw.get("proposalVerifiedActions")
        if not isinstance(tr_act, list) or len(tr_act) != len(src_act):
            actual = len(tr_act) if isinstance(tr_act, list) else type(tr_act).__name__
            raise LLMValidationError(
                "TRANSLATION_ACTIONS_COUNT",
                f"{tid}.proposalVerifiedActions count mismatch: expected {len(src_act)}, "
                f"got {actual}.",
            )
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
            actual = len(tr_links) if isinstance(tr_links, list) else type(tr_links).__name__
            raise LLMValidationError(
                "TRANSLATION_LINKS_COUNT",
                f"{tid}.coordinateLinkTexts count mismatch: expected {len(src_links)}, "
                f"got {actual}.",
            )
        norm_links: list[str] = []
        for idx, (lt, sl) in enumerate(zip(tr_links, src_links, strict=True)):
            clean = _validate_text(lt, sl.get("text"), f"{tid}.coordinateLinkTexts[{idx}]", locale)
            if not clean or clean not in out["body"]:
                raise LLMValidationError(
                    "TRANSLATION_LINK_NOT_SUBSTRING",
                    f"{tid}.coordinateLinkTexts[{idx}] {clean!r} must appear verbatim in "
                    f"translated {tid}.body.",
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
                        "TRANSLATION_LINK_UNGROUNDED",
                        f"{tid}.coordinateLinkTexts[{idx}] {clean!r} failed endpoint grounding: "
                        f"must connect from={sl['from']} to={sl['to']} with a motion connector.",
                    )
            norm_links.append(clean)
        out["coordinateLinkTexts"] = norm_links

    if stage_rows:
        labeled_texts: list[tuple[str, str]] = [(f"{tid}.body", out["body"])]
        for idx, cue_text in enumerate(out["uiCueTexts"]):
            labeled_texts.append((f"{tid}.uiCueTexts[{idx}]", cue_text))
        for key in _SCALAR_FIELDS:
            if out.get(key):
                labeled_texts.append((f"{tid}.{key}", str(out[key])))
        for idx, act_text in enumerate(out.get("proposalVerifiedActions") or ()):
            labeled_texts.append((f"{tid}.proposalVerifiedActions[{idx}]", str(act_text)))
        if isinstance(out.get("proposalQuestion"), Mapping):
            pq = out["proposalQuestion"]
            labeled_texts.append((f"{tid}.proposalQuestion.question", str(pq["question"])))
            labeled_texts.append((f"{tid}.proposalQuestion.note", str(pq["note"])))
            for idx, choice in enumerate(pq["choices"]):
                labeled_texts.append(
                    (f"{tid}.proposalQuestion.choices[{idx}].label", str(choice["label"]))
                )
        if isinstance(out.get("disagreement"), Mapping):
            dis = out["disagreement"]
            for key in _DISAGREEMENT_TEXT_KEYS:
                if dis.get(key):
                    labeled_texts.append((f"{tid}.disagreement.{key}", str(dis[key])))

        grounding_msgs: list[str] = []
        for field_path, text in labeled_texts:
            for issue in verify_grounding(text, stage_rows, bindings):
                msg = f"{field_path}: {issue.message}"
                if msg not in grounding_msgs:
                    grounding_msgs.append(msg)
        if grounding_msgs:
            raise LLMValidationError(
                "TRANSLATION_GROUNDING_MISMATCH", "; ".join(grounding_msgs[:4])
            )
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
        expected_ids = [str(item["turnId"]) for item in items]
        source_by_id = {str(item["turnId"]): item for item in items}
        plain_only = is_plain_translation_batch(items)
        response_model: type[PlainTranslationPayload | TranslationPayload] = (
            PlainTranslationPayload if plain_only else TranslationPayload
        )
        messages = build_translation_messages(
            items=items, target_language=locale, plain_only=plain_only
        )
        holder: dict[str, list[dict[str, Any]]] = {}

        def _validate_gen(
            parsed: PlainTranslationPayload | TranslationPayload,
        ) -> PlainTranslationPayload | TranslationPayload:
            if len(parsed.translations) != len(items):
                actual_ids = [tr.turn_id for tr in parsed.translations]
                raise LLMValidationError(
                    "TRANSLATION_COUNT_MISMATCH",
                    f"Expected {len(items)} translated items (turnIds={expected_ids}), "
                    f"got {len(parsed.translations)} (turnIds={actual_ids}).",
                )
            by_id: dict[str, dict[str, Any]] = {}
            for tr_item in parsed.translations:
                if tr_item.turn_id not in source_by_id:
                    raise LLMValidationError(
                        "TRANSLATION_TURN_ID_MISMATCH",
                        f"Unexpected turnId {tr_item.turn_id!r}; expected turnIds={expected_ids}.",
                    )
                if tr_item.turn_id in by_id:
                    raise LLMValidationError(
                        "TRANSLATION_TURN_ID_MISMATCH",
                        f"Duplicate turnId {tr_item.turn_id!r}; expected each of "
                        f"{expected_ids} exactly once.",
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
            response_model=response_model,
            validator=_validate_gen,
            deadline=deadline,
            max_attempts=self.generation_max_attempts,
            temperature=0.1,
            max_tokens=min(8192, max(3200, 1400 * len(items))),
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
                max_tokens=600,
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
