"""Prompt builders for turn translation and independent proposal-translation verification."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from server.i18n.messages import normalize_language

_STRUCTURED_KEYS: tuple[str, ...] = (
    "followUpQuestion",
    "intentHypothesis",
    "proposalOfferSummary",
    "proposalOfferRationale",
    "proposalPresentationReason",
    "proposalSummary",
    "coordinateLinks",
    "disagreement",
    "proposalQuestion",
    "proposalVerifiedActions",
)

_DISAGREEMENT_TEXT_KEYS: tuple[str, ...] = (
    "userPosition",
    "aiPosition",
    "coreDisagreement",
    "nextQuestion",
)

_PLAIN_TRANSLATION_SYSTEM = (
    "Translate the supplied Sokoban co-design messages into the requested targetLanguage. "
    "Return ONLY JSON matching PlainTranslationPayload ({'translations': [{'turnId': str, "
    "'body': str}]}).\n"
    "Rules: Preserve meaning, uncertainty, direct first/second-person voice, paragraph breaks, "
    "and tone. Never add advice, claims, or explanations. Keep every turnId unchanged and keep "
    "visible map labels (P, B1, B2, T1, T2, Stage) intact."
)

_TRANSLATION_SYSTEM = (
    "Translate the supplied Sokoban co-design UI items into the requested targetLanguage. "
    "Return ONLY JSON matching TranslationPayload.\n"
    "Rules: Preserve meaning, uncertainty, direct first/second-person voice, paragraph breaks, "
    "tone, and relative appeal of options. Never add advice, claims, or explanations. "
    "Keep every turnId unchanged and keep every null field null. Preserve array lengths and "
    "ordering for uiCueTexts, coordinateLinkTexts, and proposalVerifiedActions.\n"
    "When proposalQuestion is present, return ONLY question, note, and choices ([{'choiceId': str, "
    "'label': str}]) keeping each choiceId unchanged and preserving the choices as one indivisible "
    "group (do not add metadata IDs, letters, or status fields).\n"
    "When disagreement is present, return ONLY userPosition, aiPosition, coreDisagreement, and "
    "nextQuestion (keeping null fields null; do not add status, subject, or ID metadata).\n"
    "When coordinateLinks is present, return coordinateLinkTexts in the exact same order; EVERY "
    "string MUST be a verbatim character-for-character substring of your translated body and keep "
    "the same map endpoints. Keep visible map labels (P, B1, B2, T1, T2, Stage) intact."
)

_VERIFICATION_SYSTEM = (
    "Independently verify a translation of Sokoban co-design proposal turns. Return ONLY JSON "
    "matching TranslationVerificationPayload ({'accepted': bool, 'reason': str}). "
    "If accepted is false, reason MUST be a concise non-empty explanation.\n"
    "Verify that every option label, choice tradeoff, question, uncertainty, and speaker "
    "attribution is preserved faithfully. Reject added claims/advice, omitted choices, or any "
    "translation implying edits to protected entities (boxes, targets, player start, outer shell). "
    "Only water and internal walls are editable; normal gameplay pushes are distinct."
)


def is_plain_translation_batch(items: Sequence[Mapping[str, Any]]) -> bool:
    """Return True when every turn item has only body text (no cards, cues, or links)."""
    for item in items:
        if list(item.get("uiCueTexts") or ()):
            return False
        for key in _STRUCTURED_KEYS:
            if key in item and item.get(key) not in (None, [], {}):
                return False
    return True


def strip_translation_prompt_metadata(
    item: Mapping[str, Any], *, plain_only: bool = False
) -> dict[str, Any]:
    """Strip non-translatable server IDs and validation metadata from a prompt item."""
    if plain_only:
        return {
            "turnId": str(item.get("turnId") or ""),
            "body": str(item.get("body") or ""),
        }
    out: dict[str, Any] = {
        "turnId": str(item.get("turnId") or ""),
        "body": str(item.get("body") or ""),
    }
    if item.get("role"):
        out["role"] = str(item["role"])
    if item.get("language"):
        out["language"] = str(item["language"])
    if "uiCueTexts" in item:
        out["uiCueTexts"] = list(item.get("uiCueTexts") or ())
    for key in (
        "followUpQuestion",
        "intentHypothesis",
        "proposalOfferSummary",
        "proposalOfferRationale",
        "proposalPresentationReason",
    ):
        if key in item:
            out[key] = item[key]
    if "proposalOfferSummary" not in out and "proposalSummary" in item:
        out["proposalOfferSummary"] = item["proposalSummary"]
    if "proposalVerifiedActions" in item:
        out["proposalVerifiedActions"] = item["proposalVerifiedActions"]
    if item.get("coordinateLinks"):
        out["coordinateLinks"] = [
            {
                "text": str(link.get("text") or ""),
                **({"from": link["from"]} if "from" in link else {}),
                **({"to": link["to"]} if "to" in link else {}),
            }
            for link in item["coordinateLinks"]
            if isinstance(link, Mapping)
        ]
    pq = item.get("proposalQuestion")
    if isinstance(pq, Mapping):
        out["proposalQuestion"] = {
            "question": str(pq.get("question") or ""),
            "note": str(pq.get("note") or ""),
            "choices": [
                {
                    "choiceId": str(choice.get("choiceId") or ""),
                    "label": str(choice.get("label") or ""),
                }
                for choice in (pq.get("choices") or ())
                if isinstance(choice, Mapping)
            ],
        }
    dis = item.get("disagreement")
    if isinstance(dis, Mapping):
        out["disagreement"] = {
            key: dis.get(key) for key in _DISAGREEMENT_TEXT_KEYS if key in dis
        }
    elif "disagreement" in item:
        out["disagreement"] = None
    return out


def build_translation_messages(
    *,
    items: Sequence[Mapping[str, Any]],
    target_language: str,
    plain_only: bool | None = None,
) -> list[dict[str, str]]:
    """Build turn-translation messages stripping non-translatable server metadata."""
    locale = normalize_language(target_language)
    use_plain = is_plain_translation_batch(items) if plain_only is None else bool(plain_only)
    prompt_items = [
        strip_translation_prompt_metadata(item, plain_only=use_plain) for item in items
    ]
    payload = {
        "targetLanguage": "Simplified Chinese (zh-CN)" if locale == "zh-CN" else "English (en)",
        "items": prompt_items,
    }
    return [
        {
            "role": "system",
            "content": _PLAIN_TRANSLATION_SYSTEM if use_plain else _TRANSLATION_SYSTEM,
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def build_translation_verification_messages(
    *,
    source_items: Sequence[Mapping[str, Any]],
    translated_items: Sequence[Mapping[str, Any]],
    target_language: str,
) -> list[dict[str, str]]:
    """Build independent verification messages for proposal/option translations."""
    source_keys = (
        "turnId",
        "body",
        "followUpQuestion",
        "intentHypothesis",
        "proposalOfferSummary",
        "proposalOfferRationale",
        "proposalPresentationReason",
        "uiCueTexts",
        "disagreement",
        "proposalQuestion",
        "proposalVerifiedActions",
    )
    compact_sources = [{k: item[k] for k in source_keys if k in item} for item in source_items]
    payload = {
        "targetLanguage": normalize_language(target_language),
        "sources": compact_sources,
        "translations": [dict(t) for t in translated_items],
    }
    return [
        {"role": "system", "content": _VERIFICATION_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]
