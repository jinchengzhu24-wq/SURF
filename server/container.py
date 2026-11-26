"""Explicit constructor DI container for the unified Sokoban server."""

from __future__ import annotations

from dataclasses import dataclass

from server.application.analytics import DashboardService
from server.application.dialogue.handlers import (
    BinaryDiscoveryHandler,
    ChallengeHandler,
    OrdinaryChatHandler,
    RevisionCardHandler,
)
from server.application.dialogue.intent_service import IntentService
from server.application.dialogue.orchestrator import MessageOrchestrator
from server.application.dialogue.proposal_service import ProposalService
from server.application.dialogue.recovery_service import RecoveryService
from server.application.dialogue.translation_service import TranslationService
from server.application.rooms import OnlineRoomService
from server.application.sessions import (
    SessionLifecycleService,
    SessionReadModelService,
    StageService,
)
from server.application.study import DGGuideService, LevelPlanService, StudyRecordsService
from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.llm_client import AsyncOpenAITransport, LLMTransport
from server.llm.pipelines.binary import BinaryDiscoveryPipeline
from server.llm.pipelines.chat import ChatPipeline
from server.llm.pipelines.dg_guide import DGGuidePipeline
from server.llm.pipelines.level_plan import LevelPlanPipeline
from server.llm.pipelines.proposal import ProposalPipeline
from server.llm.pipelines.translation import TranslationPipeline
from server.llm.pipelines.understanding import UnderstandingPipeline
from server.persistence.database import SQLiteDatabase
from server.persistence.repositories import (
    DialogueRepository,
    OperationalRepository,
    RoomsRepository,
    SessionsRepository,
    StudyRepository,
)
from server.settings import SokobanSettings, load_settings


@dataclass(slots=True)
class AppContainer:
    """Explicit DI graph with isolated DeepSeek and Moonshot/Kimi LLM transports."""

    settings: SokobanSettings
    clock: Clock
    db: SQLiteDatabase
    deepseek_transport: LLMTransport
    cocreation_transport: LLMTransport
    rooms_repo: RoomsRepository
    study_repo: StudyRepository
    sessions_repo: SessionsRepository
    dialogue_repo: DialogueRepository
    operational_repo: OperationalRepository
    dg_guide_pipeline: DGGuidePipeline | None
    level_plan_pipeline: LevelPlanPipeline | None
    chat_pipeline: ChatPipeline | None
    understanding_pipeline: UnderstandingPipeline | None
    binary_pipeline: BinaryDiscoveryPipeline | None
    proposal_pipeline: ProposalPipeline | None
    translation_pipeline: TranslationPipeline | None
    online_room_service: OnlineRoomService
    dg_guide_service: DGGuideService
    level_plan_service: LevelPlanService
    study_records_service: StudyRecordsService
    dashboard_service: DashboardService
    session_read_model_service: SessionReadModelService
    session_lifecycle_service: SessionLifecycleService
    stage_service: StageService
    message_orchestrator: MessageOrchestrator
    proposal_service: ProposalService
    intent_service: IntentService
    translation_service: TranslationService
    recovery_service: RecoveryService

    async def close(self) -> None:
        """Close both async LLM transports and the SQLite database cleanly."""
        for transport in (self.deepseek_transport, self.cocreation_transport):
            closer = getattr(transport, "close", None)
            if callable(closer):
                await closer()
        self.db.close()


def build_container(
    settings: SokobanSettings | None = None,
    *,
    clock: Clock | None = None,
    db: SQLiteDatabase | None = None,
    deepseek_transport: LLMTransport | None = None,
    cocreation_transport: LLMTransport | None = None,
) -> AppContainer:
    """Construct the full service graph with strict DeepSeek vs Moonshot isolation."""
    resolved_settings = settings if settings is not None else load_settings()
    resolved_clock = clock if clock is not None else SYSTEM_CLOCK
    resolved_db = db if db is not None else SQLiteDatabase(resolved_settings.db_path)

    # Strict credential & transport isolation: never share or fall back between providers.
    resolved_deepseek_transport: LLMTransport = (
        deepseek_transport
        if deepseek_transport is not None
        else AsyncOpenAITransport(
            api_key=resolved_settings.deepseek_api_key.get_secret_value(),
            base_url=resolved_settings.deepseek_base_url,
            default_timeout_seconds=resolved_settings.deepseek_timeout_seconds,
        )
    )
    resolved_cocreation_transport: LLMTransport = (
        cocreation_transport
        if cocreation_transport is not None
        else AsyncOpenAITransport(
            api_key=resolved_settings.cocreation_llm_api_key.get_secret_value(),
            base_url=resolved_settings.cocreation_llm_base_url,
            default_timeout_seconds=resolved_settings.cocreation_llm_timeout_seconds,
        )
    )

    rooms_repo = RoomsRepository(resolved_db)
    study_repo = StudyRepository(resolved_db)
    sessions_repo = SessionsRepository(resolved_db)
    dialogue_repo = DialogueRepository(resolved_db)
    operational_repo = OperationalRepository(resolved_db)

    deepseek_enabled = deepseek_transport is not None or resolved_settings.has_deepseek_config
    dg_guide_pipeline = (
        DGGuidePipeline(
            resolved_deepseek_transport,
            model=resolved_settings.deepseek_model,
            clock=resolved_clock,
            timeout_seconds=resolved_settings.dg_timeout_seconds,
        )
        if deepseek_enabled
        else None
    )
    level_plan_pipeline = (
        LevelPlanPipeline(
            resolved_deepseek_transport,
            model=resolved_settings.deepseek_model,
            clock=resolved_clock,
            timeout_seconds=resolved_settings.level_plan_timeout_seconds,
        )
        if deepseek_enabled
        else None
    )

    cocreation_enabled = (
        cocreation_transport is not None or resolved_settings.has_cocreation_llm_config
    )
    chat_pipeline = (
        ChatPipeline(
            resolved_cocreation_transport,
            model=resolved_settings.cocreation_llm_model,
            clock=resolved_clock,
        )
        if cocreation_enabled
        else None
    )
    understanding_pipeline = (
        UnderstandingPipeline(
            resolved_cocreation_transport,
            model=resolved_settings.cocreation_llm_model,
            clock=resolved_clock,
        )
        if cocreation_enabled
        else None
    )
    binary_pipeline = (
        BinaryDiscoveryPipeline(
            resolved_cocreation_transport,
            model=resolved_settings.cocreation_llm_model,
            clock=resolved_clock,
        )
        if cocreation_enabled
        else None
    )
    proposal_pipeline = (
        ProposalPipeline(
            resolved_cocreation_transport,
            model=resolved_settings.cocreation_llm_model,
            clock=resolved_clock,
        )
        if cocreation_enabled
        else None
    )
    translation_pipeline = (
        TranslationPipeline(
            resolved_cocreation_transport,
            model=resolved_settings.cocreation_llm_model,
            clock=resolved_clock,
        )
        if cocreation_enabled
        else None
    )

    cocreation_settings = resolved_settings.to_cocreation_app_settings()
    online_room_service = OnlineRoomService(
        rooms_repo,
        clock=resolved_clock,
        room_ttl_seconds=resolved_settings.online_room_ttl_seconds,
    )
    dg_guide_service = DGGuideService(
        dg_guide_pipeline,
        extra_secrets=resolved_settings.secret_values(),
    )
    level_plan_service = LevelPlanService(
        level_plan_pipeline,
        clock=resolved_clock,
    )
    study_records_service = StudyRecordsService(
        study_repo,
        clock=resolved_clock,
    )
    dashboard_service = DashboardService(
        rooms_repo,
        delete_password=resolved_settings.dashboard_delete_password.get_secret_value(),
        clock=resolved_clock,
    )

    session_read_model_service = SessionReadModelService(
        sessions_repo,
        dialogue_repo,
        operational_repo,
        clock=resolved_clock,
        settings=cocreation_settings,
    )
    session_lifecycle_service = SessionLifecycleService(
        resolved_db,
        sessions_repo,
        operational_repo,
        session_read_model_service,
        room_sync=online_room_service,
        clock=resolved_clock,
        settings=cocreation_settings,
    )
    stage_service = StageService(
        resolved_db,
        sessions_repo,
        operational_repo,
        session_read_model_service,
        chat_pipeline=chat_pipeline,
        understanding_pipeline=understanding_pipeline,
        room_sync=online_room_service,
        clock=resolved_clock,
        settings=cocreation_settings,
    )

    ordinary_handler = OrdinaryChatHandler(
        resolved_db,
        chat_pipeline,
        room_sync=online_room_service,
        clock=resolved_clock,
    )
    binary_handler = BinaryDiscoveryHandler(
        resolved_db,
        operational_repo,
        binary_pipeline,
        proposal_pipeline,
        room_sync=online_room_service,
        clock=resolved_clock,
    )
    revision_handler = RevisionCardHandler(
        resolved_db,
        proposal_pipeline,
        room_sync=online_room_service,
        clock=resolved_clock,
    )
    challenge_handler = ChallengeHandler(
        resolved_db,
        understanding_pipeline,
        room_sync=online_room_service,
        clock=resolved_clock,
    )

    message_orchestrator = MessageOrchestrator(
        resolved_db,
        sessions_repo,
        operational_repo,
        session_read_model_service,
        ordinary_handler=ordinary_handler,
        binary_handler=binary_handler,
        revision_handler=revision_handler,
        challenge_handler=challenge_handler,
        understanding_pipeline=understanding_pipeline,
        room_sync=online_room_service,
        clock=resolved_clock,
        settings=cocreation_settings,
    )
    proposal_service = ProposalService(
        resolved_db,
        sessions_repo,
        operational_repo,
        session_read_model_service,
        binary_handler,
        room_sync=online_room_service,
        clock=resolved_clock,
        settings=cocreation_settings,
    )
    intent_service = IntentService(
        resolved_db,
        sessions_repo,
        session_read_model_service,
        clock=resolved_clock,
        settings=cocreation_settings,
    )
    translation_service = TranslationService(
        sessions_repo,
        dialogue_repo,
        session_read_model_service,
        translation_pipeline,
        clock=resolved_clock,
        settings=cocreation_settings,
    )
    recovery_service = RecoveryService(
        resolved_db,
        sessions_repo,
        operational_repo,
        session_read_model_service,
        chat_pipeline=chat_pipeline,
        room_sync=online_room_service,
        clock=resolved_clock,
        settings=cocreation_settings,
    )

    return AppContainer(
        settings=resolved_settings,
        clock=resolved_clock,
        db=resolved_db,
        deepseek_transport=resolved_deepseek_transport,
        cocreation_transport=resolved_cocreation_transport,
        rooms_repo=rooms_repo,
        study_repo=study_repo,
        sessions_repo=sessions_repo,
        dialogue_repo=dialogue_repo,
        operational_repo=operational_repo,
        dg_guide_pipeline=dg_guide_pipeline,
        level_plan_pipeline=level_plan_pipeline,
        chat_pipeline=chat_pipeline,
        understanding_pipeline=understanding_pipeline,
        binary_pipeline=binary_pipeline,
        proposal_pipeline=proposal_pipeline,
        translation_pipeline=translation_pipeline,
        online_room_service=online_room_service,
        dg_guide_service=dg_guide_service,
        level_plan_service=level_plan_service,
        study_records_service=study_records_service,
        dashboard_service=dashboard_service,
        session_read_model_service=session_read_model_service,
        session_lifecycle_service=session_lifecycle_service,
        stage_service=stage_service,
        message_orchestrator=message_orchestrator,
        proposal_service=proposal_service,
        intent_service=intent_service,
        translation_service=translation_service,
        recovery_service=recovery_service,
    )


__all__ = ["AppContainer", "build_container"]
