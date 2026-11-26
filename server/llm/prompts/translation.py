"""Prompt builders for turn translation and independent proposal-translation verification."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from server.i18n.messages import normalize_language

_TRANSLATION_SYSTEM = (
    "Translate the supplied Sokoban co-design UI items into the requested targetLanguage. "
    "Return ONLY JSON matching TranslationPayload.\n"
    "Rules: Preserve meaning, uncertainty, direct first/second-person voice, paragraph breaks, "
    "tone, and relative appeal of options. Never add advice, claims, or explanations. "
    "Keep every turnId unchanged and keep every null field null. Preserve array lengths and "
    "ordering for uiCueTexts, coordinateLinkTexts, and proposalVerifiedActions.\n"
    "When proposalQuestion is present, translate ONLY question, note, and choices[].label while "
    "preserving all IDs, letters, number, selectedChoiceId, and status as one indivisible group.\n"
    "When disagreement is present, preserve status, subject, resolution, phase, and displayCard, "
    "translating ONLY userPosition, aiPosition, coreDisagreement, and nextQuestion.\n"
    "When coordinateLinks is present, return coordinateLinkTexts in the exact same order; EVERY "
    "string MUST be a verbatim character-for-character substring of your translated body and keep "
    "the same map endpoints. Keep visible map labels (P, B1, B2, T1, T2, Stage) intact."
)

_VERIFICATION_SYSTEM = (
    "Independently verify a translation of Sokoban co-design proposal turns. Return ONLY JSON "
    "matching TranslationVerificationPayload ({'accepted': bool, 'reason': str}).\n"
    "Verify that every option label, choice tradeoff, question, uncertainty, and speaker "
    "attribution is preserved faithfully. Reject added claims/advice, omitted choices, or any "
    "translation implying edits to protected entities (boxes, targets, player start, outer shell). "
    "Only water and internal walls are editable; normal gameplay pushes are distinct."
)


def build_translation_messages(
    *,
    items: Sequence[Mapping[str, Any]],
    target_language: str,
) -> list[dict[str, str]]:
    """Build turn-translation messages stripping internal map-validation metadata."""
    locale = normalize_language(target_language)
    prompt_items = [
        {k: v for k, v in item.items() if k not in {"stageRows", "entityBindings", "guidance"}}
        for item in items
    ]
    payload = {
        "targetLanguage": "Simplified Chinese (zh-CN)" if locale == "zh-CN" else "English (en)",
        "items": prompt_items,
    }
    return [
        {"role": "system", "content": _TRANSLATION_SYSTEM},
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
        "proposalSummary",
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
