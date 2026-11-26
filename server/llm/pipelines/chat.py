"""Native-async Stage opening and ordinary co-creation chat pipeline."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.grid import Grid, parse_structural_grid
from server.core.llm_client import LLMError, LLMTransport, LLMValidationError, call_structured
from server.i18n.messages import get_message, normalize_language
from server.llm.grounding import (
    build_stage_facts,
    extract_coordinate_links,
    sanitize_visible_text,
    validate_visible_language,
    verify_grounding,
)
from server.llm.prompts.chat import build_chat_messages, build_stage_opening_messages
from server.llm.schemas import ChatReplyPayload


@dataclass(frozen=True, slots=True)
class ChatPipelineResult:
    """Validated chat or Stage-opening output ready for persistence and projection."""

    assistant_message: str
    move: str
    guidance: dict[str, Any]
    assessment: dict[str, Any] | None = None
    design_context_patch: dict[str, Any] | None = None
    model: str = ""
    attempts_used: int = 0
    request_id: str = ""
    latency_ms: int = 0
    source: str = "llm"
    usage: dict[str, int | None] = field(default_factory=dict)


def _latest_user_message(conversation: Sequence[Mapping[str, Any]]) -> str:
    for item in reversed(conversation):
        if str(item.get("role") or "") == "user":
            return str(item.get("content") or "")
    return ""


def _empty_opening_guidance(links: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "move": "observe_stage", "intentHypothesis": None, "intentConfidence": None,
        "followUpQuestion": None, "proposalOffer": None, "disagreement": None,
        "uiCues": [], "coordinateLinks": links,
    }


def _build_snapshot_fallback_opening(
    grid: Grid,
    *,
    stage_number: int,
    language: str,
    solver_metrics: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    locale = normalize_language(language)
    facts = build_stage_facts(grid)
    sm = solver_metrics or {}
    steps, pushes = sm.get("solutionSteps"), sm.get("solutionPushes")
    wc, ic = len(facts.water_cells), len(facts.editable_internal_walls)
    has_metrics = isinstance(steps, int) and isinstance(pushes, int)

    if locale == "zh-CN":
        metric = (
            f"当前版本已通过确定性可解检查（最短约 {steps} 步、{pushes} 次推动）。"
            if has_metrics
            else "当前保存的 Stage 已通过确定性结构与可解性检查。"
        )
        body = (
            f"这是当前 Stage {stage_number} 的已保存布局，地图内有 {wc} 格水域与 {ic} 格可调整"
            f"的内部墙体。{metric}\n\n从我的角度看，你可以先留意起手第一步和箱子之间的通行顺序，"
            "再决定是否调整通道或障碍。"
        )
        assessment = {
            "solutionSummary": metric,
            "difficultyOpinion": "具体体感难度仍需结合你的试玩反馈一起判断。",
            "features": [f"水域 {wc} 格，可编辑内部墙 {ic} 格"],
            "suggestions": ["先试玩或分享你对开局路线与空间节奏的第一印象。"],
            "satisfactionQuestion": None,
        }
    else:
        metric = (
            f"This saved Stage passed deterministic solvability checks "
            f"(shortest solution around {steps} steps and {pushes} pushes)."
            if has_metrics
            else "This saved Stage passed deterministic structure and solvability checks."
        )
        body = (
            f"Here is the saved layout for Stage {stage_number}, with {wc} water tiles and {ic} "
            f"editable internal wall tiles. {metric}\n\nIn my view, a good starting focus is how "
            "the opening move sets up push order before deciding whether to reshape any passages."
        )
        assessment = {
            "solutionSummary": metric,
            "difficultyOpinion": "Perceived difficulty is best judged alongside playtesting.",
            "features": [f"{wc} water cells and {ic} editable internal walls"],
            "suggestions": ["Playtest or share your first impression of the opening route."],
            "satisfactionQuestion": None,
        }
    if stage_number == 1:
        body = f"{body}\n\n{get_message('fallback.stage1_orientation', locale)}"
    return body, assessment


def _is_grounded(text: str, grid: Grid, bindings: Mapping[str, Any] | None) -> bool:
    return not verify_grounding(text, grid, bindings)


def _sanitize_and_project_guidance(
    payload: ChatReplyPayload,
    *,
    body: str,
    grid: Grid,
    language: str,
    latest_user: str,
    entity_bindings: Mapping[str, Any] | None,
    stage_opening: bool,
) -> dict[str, Any]:
    raw_links = [link.model_dump(by_alias=True) for link in payload.coordinate_links]
    links = extract_coordinate_links(
        body, grid, candidate_links=raw_links, entity_bindings=entity_bindings
    )
    if stage_opening:
        return _empty_opening_guidance(links)

    # Isolate optional card failures so a verified body is never discarded for a bad card.
    intent_hypothesis: str | None = None
    intent_confidence = payload.intent_confidence
    intent_decision_dict: dict[str, Any] | None = None
    if payload.intent_decision is not None:
        decision = payload.intent_decision
        valid_claims = [
            c.model_dump(by_alias=True)
            for c in decision.claims
            if c.evidence_span and c.evidence_span in latest_user
        ]
        card = sanitize_visible_text(decision.card_text, language)
        if (
            decision.classification == "candidate"
            and valid_claims
            and card
            and _is_grounded(card, grid, entity_bindings)
        ):
            intent_hypothesis = card
            intent_decision_dict = {
                "classification": "candidate",
                "claims": valid_claims,
                "cardText": card,
            }
        else:
            intent_confidence = None
            cls = decision.classification if decision.classification != "candidate" else "none"
            intent_decision_dict = {"classification": cls, "claims": [], "cardText": ""}

    follow_up: str | None = None
    if payload.follow_up_question:
        q = sanitize_visible_text(payload.follow_up_question, language)
        if q and _is_grounded(q, grid, entity_bindings):
            follow_up = q

    offer_dict: dict[str, Any] | None = None
    if payload.revision_offer is not None:
        raw_offer = payload.revision_offer.model_dump(by_alias=True)
        raw_offer["summary"] = sanitize_visible_text(raw_offer["summary"], language)
        raw_offer["rationale"] = sanitize_visible_text(raw_offer["rationale"], language)
        if _is_grounded(raw_offer["summary"], grid, entity_bindings) and _is_grounded(
            raw_offer["rationale"], grid, entity_bindings
        ):
            offer_dict = raw_offer

    disagreement_dict: dict[str, Any] | None = None
    if payload.disagreement is not None:
        raw_dis = payload.disagreement.model_dump(by_alias=True)
        dis_fields = ("userPosition", "aiPosition", "coreDisagreement", "nextQuestion")
        for key in dis_fields:
            raw_dis[key] = sanitize_visible_text(raw_dis.get(key), language)
        if all(_is_grounded(raw_dis.get(k) or "", grid, entity_bindings) for k in dis_fields):
            disagreement_dict = raw_dis

    cues = [
        {"type": cue.type, "text": text}
        for cue in payload.ui_cues[:2]
        if (text := sanitize_visible_text(cue.text, language))
        and _is_grounded(text, grid, entity_bindings)
    ]
    guidance: dict[str, Any] = {
        "move": payload.move,
        "intentHypothesis": intent_hypothesis,
        "intentConfidence": intent_confidence,
        "followUpQuestion": follow_up,
        "proposalOffer": offer_dict,
        "disagreement": disagreement_dict,
        "uiCues": cues,
        "coordinateLinks": links,
    }
    if intent_decision_dict is not None:
        guidance["_intentDecision"] = intent_decision_dict
    return guidance


def _validate_chat_body(
    parsed: ChatReplyPayload,
    grid: Grid,
    locale: str,
    entity_bindings: Mapping[str, Any] | None,
) -> ChatReplyPayload:
    body = sanitize_visible_text(parsed.assistant_message, locale)
    if not body:
        raise LLMValidationError("EMPTY_BODY", "Assistant message cannot be empty.")
    grounding_issues = verify_grounding(body, grid, entity_bindings)
    if grounding_issues:
        raise LLMValidationError("GROUNDING_MISMATCH", grounding_issues[0].message)
    lang_issues = validate_visible_language(body, locale)
    if lang_issues:
        raise LLMValidationError("INTERNAL_TOKEN_LEAK", lang_issues[0])
    return parsed


class ChatPipeline:
    """Native-async structured pipeline for Stage openings and ordinary chat."""

    def __init__(
        self,
        transport: LLMTransport,
        *,
        model: str,
        clock: Clock | None = None,
        extra_body: Mapping[str, Any] | None = None,
        temperature: float = 0.4,
        max_tokens: int = 2400,
    ) -> None:
        self.transport = transport
        self.model = model
        self.clock = clock if clock is not None else SYSTEM_CLOCK
        self.extra_body = dict(extra_body) if extra_body is not None else None
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)

    def _result_from_opening(
        self, res: Any, body: str, grid: Grid, locale: str, bindings: Mapping[str, Any] | None
    ) -> ChatPipelineResult:
        guidance = _sanitize_and_project_guidance(
            res.parsed,
            body=body,
            grid=grid,
            language=locale,
            latest_user="",
            entity_bindings=bindings,
            stage_opening=True,
        )
        assessment = (
            res.parsed.assessment.model_dump(by_alias=True)
            if res.parsed.assessment is not None
            else None
        )
        return ChatPipelineResult(
            body, "observe_stage", guidance, assessment, None,
            res.model, res.attempts_used, res.request_id, res.latency_ms, "llm", res.usage,
        )

    async def generate_stage_opening(
        self,
        grid_or_rows: Grid | Sequence[str],
        *,
        stage_number: int = 1,
        language: str = "en",
        solver_metrics: Mapping[str, Any] | None = None,
        design_context: Mapping[str, Any] | None = None,
        change_summary: Mapping[str, Any] | None = None,
        entity_bindings: Mapping[str, Any] | None = None,
        deadline: float | None = None,
        request_id: str = "",
        max_attempts: int = 2,
    ) -> ChatPipelineResult:
        """Generate a Stage opening with deterministic snapshot fallback on retryable failure."""
        locale = normalize_language(language)
        grid = parse_structural_grid(grid_or_rows)
        messages = build_stage_opening_messages(
            stage_snapshot=build_stage_facts(grid, entity_bindings).to_prompt_dict(),
            language=locale,
            stage_number=stage_number,
            solver_metrics=solver_metrics,
            design_context=design_context,
            change_summary=change_summary,
        )
        started = self.clock.perf_counter()
        try:
            result = await call_structured(
                self.transport,
                task="stage_opening",
                model=self.model,
                messages=messages,
                response_model=ChatReplyPayload,
                validator=lambda p: _validate_chat_body(p, grid, locale, entity_bindings),
                deadline=deadline,
                max_attempts=max_attempts,
                temperature=min(self.temperature, 0.35),
                max_tokens=self.max_tokens,
                attempt_timeout_seconds=55.0,
                extra_body=self.extra_body,
                clock=self.clock,
                request_id=request_id,
            )
        except LLMError as exc:
            if not exc.retryable and exc.code != "DEADLINE_EXCEEDED":
                raise
            body, assessment = _build_snapshot_fallback_opening(
                grid, stage_number=stage_number, language=locale, solver_metrics=solver_metrics
            )
            return ChatPipelineResult(
                body, "observe_stage", _empty_opening_guidance([]), assessment, None,
                self.model, exc.attempts_used, exc.request_id or request_id,
                round((self.clock.perf_counter() - started) * 1000), "snapshot_fallback",
            )

        body = sanitize_visible_text(result.parsed.assistant_message, locale)
        if stage_number == 1:
            orientation = get_message("fallback.stage1_orientation", locale)
            if orientation not in body:
                body = f"{body}\n\n{orientation}"
        return self._result_from_opening(result, body, grid, locale, entity_bindings)

    async def generate_chat_reply(
        self,
        *,
        conversation: Sequence[Mapping[str, Any]],
        grid_or_rows: Grid | Sequence[str],
        language: str = "en",
        solver_metrics: Mapping[str, Any] | None = None,
        play_summary: Mapping[str, Any] | None = None,
        design_context: Mapping[str, Any] | None = None,
        routing_hint: Mapping[str, Any] | None = None,
        entity_bindings: Mapping[str, Any] | None = None,
        deadline: float | None = None,
        request_id: str = "",
        max_attempts: int = 3,
    ) -> ChatPipelineResult:
        """Generate a grounded, structured ordinary chat reply (no synthetic fallback)."""
        locale = normalize_language(language)
        grid = parse_structural_grid(grid_or_rows)
        facts = build_stage_facts(grid, entity_bindings)
        latest_user = _latest_user_message(conversation)
        messages = build_chat_messages(
            conversation=conversation,
            stage_snapshot=facts.to_prompt_dict(),
            language=locale,
            solver_metrics=solver_metrics,
            play_summary=play_summary,
            design_context=design_context,
            routing_hint=routing_hint,
        )
        result = await call_structured(
            self.transport,
            task="ordinary_chat",
            model=self.model,
            messages=messages,
            response_model=ChatReplyPayload,
            validator=lambda p: _validate_chat_body(p, grid, locale, entity_bindings),
            deadline=deadline,
            max_attempts=max_attempts,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            attempt_timeout_seconds=55.0,
            extra_body=self.extra_body,
            clock=self.clock,
            request_id=request_id,
        )
        body = sanitize_visible_text(result.parsed.assistant_message, locale)
        guidance = _sanitize_and_project_guidance(
            result.parsed,
            body=body,
            grid=grid,
            language=locale,
            latest_user=latest_user,
            entity_bindings=entity_bindings,
            stage_opening=False,
        )
        patch = (
            result.parsed.design_context_patch.model_dump(by_alias=True)
            if result.parsed.design_context_patch is not None
            else None
        )
        return ChatPipelineResult(
            body, result.parsed.move, guidance, None, patch,
            result.model, result.attempts_used, result.request_id,
            result.latency_ms, "llm", result.usage,
        )
