"""On-demand turn translation service (zero write locks during reads or LLM translation)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.application.sessions import CoCreationAppSettings, SessionReadModelService
from server.core.clock import SYSTEM_CLOCK, Clock
from server.domain.errors import ConflictError, NotFoundError
from server.i18n.messages import normalize_language
from server.llm.pipelines.translation import TranslationPipeline
from server.persistence.models import ConversationTurnRecord, LevelVersionRecord
from server.persistence.repositories.dialogue_repo import DialogueRepository
from server.persistence.repositories.sessions_repo import SessionsRepository

from ._shared import require_browser_session


def _build_translation_item(
    turn: ConversationTurnRecord,
    version: LevelVersionRecord | None,
) -> dict[str, Any]:
    guidance = dict(turn.guidance or {})
    item: dict[str, Any] = {
        "turnId": turn.turn_id,
        "role": turn.role,
        "body": turn.content,
        "language": turn.language,
        "uiCueTexts": [str(x) for x in (guidance.get("uiCueTexts") or ()) if str(x).strip()],
    }
    if guidance.get("intentHypothesis"):
        item["intentHypothesis"] = str(guidance["intentHypothesis"])
    offer = guidance.get("proposalOffer")
    if isinstance(offer, dict) and offer.get("summary"):
        item["proposalSummary"] = str(offer["summary"])
    if isinstance(guidance.get("proposalQuestion"), dict):
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
    if isinstance(guidance.get("disagreement"), dict):
        item["disagreement"] = dict(guidance["disagreement"])
    if version is not None:
        item["stageRows"] = list(version.rows)
        if version.entity_bindings is not None:
            item["entityBindings"] = dict(version.entity_bindings)
    return item


class TranslationService:
    """Translates untranslated turns via TranslationPipeline with zero read-path write locks."""

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
        """Translate missing turns outside any write transaction, then persist idempotently."""
        require_browser_session(
            self._sessions_repo, session_id, access_token, settings=self._settings
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
            items = [_build_translation_item(t, versions_by_id.get(t.version_id)) for t in pending]
            res = await self._translation_pipeline.translate_turns(
                items,
                target_language=locale,
                deadline=deadline,
                request_id=request_id,
            )
            now_iso = self._clock.utc_now()
            by_turn = {t.turn_id: t for t in pending}
            for tr in res.translations:
                tid = str(tr.get("turnId") or "")
                src = by_turn.get(tid)
                if src is None:
                    continue
                src_guidance = dict(src.guidance or {})
                tr_guidance = dict(src_guidance)
                if "uiCueTexts" in tr:
                    tr_guidance["uiCueTexts"] = list(tr["uiCueTexts"])
                if "proposalQuestion" in tr and isinstance(tr["proposalQuestion"], dict):
                    tr_guidance["proposalQuestion"] = tr["proposalQuestion"]
                if "disagreement" in tr and isinstance(tr["disagreement"], dict):
                    tr_guidance["disagreement"] = tr["disagreement"]
                if tr.get("intentHypothesis"):
                    tr_guidance["intentHypothesis"] = tr["intentHypothesis"]
                payload = {
                    "body": str(tr.get("body") or ""),
                    "guidance": tr_guidance,
                    "proposalSummary": tr.get("proposalSummary"),
                    "createdAt": now_iso,
                }
                self._dialogue_repo.upsert_turn_translation(session_id, tid, locale, payload)
        return await self._read_model.get_session_payload(session_id)


__all__ = ["TranslationService"]
