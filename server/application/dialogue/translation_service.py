"""On-demand turn translation service (per-turn persistence + active session enforcement)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from server.application.sessions import CoCreationAppSettings, SessionReadModelService
from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.llm_client import LLMError
from server.domain.errors import ConflictError, NotFoundError
from server.i18n.messages import normalize_language
from server.llm.pipelines.translation import TranslationPipeline
from server.persistence.models import ConversationTurnRecord, LevelVersionRecord
from server.persistence.repositories.dialogue_repo import DialogueRepository
from server.persistence.repositories.sessions_repo import SessionsRepository

from ._shared import require_active_session


def _build_translation_item(
    turn: ConversationTurnRecord,
    version: LevelVersionRecord | None,
) -> dict[str, Any]:
    guidance = dict(turn.guidance or {})
    raw_cues = [
        c
        for c in (guidance.get("uiCues") or ())
        if isinstance(c, Mapping) and str(c.get("text") or "").strip()
    ]
    cue_texts = [str(c["text"]).strip() for c in raw_cues]

    body = str(turn.content or "").rstrip()
    follow_up = str(guidance.get("followUpQuestion") or "").strip() or None
    for suffix in reversed([*cue_texts, *( [follow_up] if follow_up else [] )]):
        if body.endswith(suffix):
            body = body[: -len(suffix)].rstrip()
    if not body:
        body = str(turn.content or "")

    item: dict[str, Any] = {
        "turnId": turn.turn_id,
        "role": turn.role,
        "body": body,
        "language": turn.language,
        "uiCueTexts": cue_texts,
    }
    if follow_up is not None:
        item["followUpQuestion"] = follow_up
    if guidance.get("intentHypothesis"):
        item["intentHypothesis"] = str(guidance["intentHypothesis"])

    offer = guidance.get("proposalOffer")
    if isinstance(offer, Mapping):
        if offer.get("summary"):
            item["proposalSummary"] = str(offer["summary"])
            item["proposalOfferSummary"] = str(offer["summary"])
        if offer.get("rationale"):
            item["proposalOfferRationale"] = str(offer["rationale"])
        pres = offer.get("proposalPresentation")
        if isinstance(pres, Mapping):
            if pres.get("schemaVersion") == 2 and pres.get("reason"):
                item["proposalPresentationReason"] = str(pres["reason"])
            if pres.get("binaryCompact") and pres.get("verifiedActions"):
                item["proposalVerifiedActions"] = [
                    str(a) for a in (pres.get("verifiedActions") or ()) if str(a).strip()
                ]

    if isinstance(guidance.get("proposalQuestion"), Mapping):
        pq = guidance["proposalQuestion"]
        item["proposalQuestion"] = {
            "questionId": pq.get("questionId"),
            "topicId": pq.get("topicId", ""),
            "baseVersionId": pq.get("baseVersionId", ""),
            "question": pq.get("question", ""),
            "note": pq.get("note", ""),
            "number": pq.get("number", 1),
            "choices": [
                {
                    "choiceId": c.get("choiceId"),
                    "letter": c.get("letter"),
                    "label": c.get("label"),
                }
                for c in (pq.get("choices") or ())
            ],
        }
    if isinstance(guidance.get("disagreement"), Mapping):
        item["disagreement"] = dict(guidance["disagreement"])
    if isinstance(guidance.get("coordinateLinks"), list) and guidance["coordinateLinks"]:
        item["coordinateLinks"] = list(guidance["coordinateLinks"])
    if version is not None:
        item["stageRows"] = list(version.rows)
        if version.entity_bindings is not None:
            item["entityBindings"] = dict(version.entity_bindings)
    return item


def _apply_translated_guidance(
    src: ConversationTurnRecord,
    tr: Mapping[str, Any],
) -> dict[str, Any]:
    src_guidance = dict(src.guidance or {})
    tr_guidance = dict(src_guidance)

    src_cues = [dict(c) for c in (src_guidance.get("uiCues") or ()) if isinstance(c, Mapping)]
    tr_cue_texts = list(tr.get("uiCueTexts") or ())
    if src_cues and len(tr_cue_texts) == len(src_cues):
        tr_guidance["uiCues"] = [
            {**cue, "text": str(tr_cue_texts[idx])} for idx, cue in enumerate(src_cues)
        ]

    if tr.get("followUpQuestion") is not None:
        tr_guidance["followUpQuestion"] = tr["followUpQuestion"]
    if tr.get("intentHypothesis"):
        tr_guidance["intentHypothesis"] = tr["intentHypothesis"]

    src_offer = src_guidance.get("proposalOffer")
    if isinstance(src_offer, Mapping):
        tr_offer = dict(src_offer)
        if tr.get("proposalOfferSummary"):
            tr_offer["summary"] = tr["proposalOfferSummary"]
        elif tr.get("proposalSummary"):
            tr_offer["summary"] = tr["proposalSummary"]
        if tr.get("proposalOfferRationale"):
            tr_offer["rationale"] = tr["proposalOfferRationale"]
        pres = tr_offer.get("proposalPresentation")
        if isinstance(pres, Mapping):
            tr_pres = dict(pres)
            if tr.get("proposalPresentationReason"):
                tr_pres["reason"] = tr["proposalPresentationReason"]
            if tr_pres.get("binaryCompact") and "proposalVerifiedActions" in tr:
                tr_pres["verifiedActions"] = list(tr["proposalVerifiedActions"] or ())
            if tr_pres.get("schemaVersion") != 2:
                if tr.get("proposalOfferSummary"):
                    tr_pres["summary"] = tr["proposalOfferSummary"]
                if tr.get("proposalOfferRationale"):
                    tr_pres["rationale"] = tr["proposalOfferRationale"]
            tr_offer["proposalPresentation"] = tr_pres
        tr_guidance["proposalOffer"] = tr_offer

    if isinstance(tr.get("proposalQuestion"), Mapping):
        tr_guidance["proposalQuestion"] = dict(tr["proposalQuestion"])
    if isinstance(tr.get("disagreement"), Mapping):
        src_dis = src_guidance.get("disagreement")
        if isinstance(src_dis, Mapping):
            tr_guidance["disagreement"] = {**src_dis, **tr["disagreement"]}
        else:
            tr_guidance["disagreement"] = dict(tr["disagreement"])

    src_links = list(src_guidance.get("coordinateLinks") or ())
    tr_link_texts = list(tr.get("coordinateLinkTexts") or ())
    tr_body = str(tr.get("body") or "")
    if src_links and len(tr_link_texts) == len(src_links):
        tr_guidance["coordinateLinks"] = [
            {**dict(link), "text": txt}
            for link, txt in zip(src_links, tr_link_texts, strict=True)
            if isinstance(txt, str) and txt.strip() and txt in tr_body
        ]
    elif src_links:
        tr_guidance["coordinateLinks"] = []

    return tr_guidance


class TranslationService:
    """Translates untranslated turns via TranslationPipeline with per-turn persistence."""

    def __init__(
        self,
        sessions_repo: SessionsRepository,
        dialogue_repo: DialogueRepository,
        read_model: SessionReadModelService,
        translation_pipeline: TranslationPipeline | None,
        *,
        clock: Clock = SYSTEM_CLOCK,
        settings: CoCreationAppSettings | None = None,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._dialogue_repo = dialogue_repo
        self._read_model = read_model
        self._translation_pipeline = translation_pipeline
        self._clock = clock
        self._settings = settings or CoCreationAppSettings()

    async def translate_turns(
        self,
        session_id: str,
        access_token: str | None,
        *,
        target_language: str,
        turn_ids: Sequence[str] | None = None,
        request_id: str = "",
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Translate missing turns per-turn outside write_tx so partial success persists."""
        require_active_session(
            self._sessions_repo,
            session_id,
            access_token,
            settings=self._settings,
            clock=self._clock,
        )
        locale = normalize_language(target_language)
        read_model = self._sessions_repo.load_session_read_model(session_id)
        if read_model is None:
            raise NotFoundError("SESSION_NOT_FOUND", "error.session_not_found")

        allowed_ids = set(turn_ids) if turn_ids is not None else None
        versions_by_id = {v.version_id: v for v in read_model.versions}
        pending = [
            t
            for t in read_model.turns
            if t.language != locale
            and locale not in (t.translations or {})
            and (allowed_ids is None or t.turn_id in allowed_ids)
        ]
        if pending:
            if self._translation_pipeline is None:
                raise ConflictError("LLM_UNAVAILABLE", "error.configuration")
            for src in pending:
                item = _build_translation_item(src, versions_by_id.get(src.version_id))
                try:
                    res = await self._translation_pipeline.translate_turns(
                        [item],
                        target_language=locale,
                        deadline=deadline,
                        request_id=f"{request_id}:{src.turn_id}" if request_id else src.turn_id,
                    )
                except LLMError as exc:
                    if not exc.retryable:
                        raise
                    # Preserve original text on failed turns while keeping already-verified turns.
                    continue
                except TimeoutError:
                    # Preserve original text on failed turns while keeping already-verified turns.
                    continue
                now_iso = self._clock.utc_now()
                for tr in res.translations:
                    if str(tr.get("turnId") or "") != src.turn_id:
                        continue
                    payload = {
                        "body": str(tr.get("body") or ""),
                        "guidance": _apply_translated_guidance(src, tr),
                        "proposalSummary": tr.get("proposalSummary"),
                        "createdAt": now_iso,
                    }
                    self._dialogue_repo.upsert_turn_translation(
                        session_id, src.turn_id, locale, payload
                    )
        return await self._read_model.get_session_payload(session_id)


__all__ = ["TranslationService"]
