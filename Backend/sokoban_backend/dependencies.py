from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, Request

from .config import (
    BackendSettings,
    Clock,
    STUDY_LOG_DIR,
    StudyLogPaths,
    verify_delete_password_header,
)
from .llm.runtime import (
    EventLogger,
    LLMGateway,
    default_openai_client_factory,
)
from .services.blueprint_planner import BlueprintPlannerService
from .services.dg_guide import DGGuideService
from .services.online_rooms import OnlineRoomManager
from .services.pc_generator import PCLevelGenerationService
from .storage.jsonl_store import StudyLogStore


@dataclass
class AppContainer:
    settings: BackendSettings
    paths: StudyLogPaths
    clock: Clock
    event_logger: EventLogger
    store: StudyLogStore
    room_manager: OnlineRoomManager
    llm_gateway: LLMGateway
    dg_service: DGGuideService
    blueprint_service: BlueprintPlannerService
    pc_service: PCLevelGenerationService

    def close(self) -> None:
        self.llm_gateway.close()
        self.event_logger.close()


def build_container(
    *,
    settings: BackendSettings | None = None,
    paths: StudyLogPaths | None = None,
    study_log_dir: Path | str | None = None,
    clock: Clock | None = None,
    event_logger: EventLogger | None = None,
    store: StudyLogStore | None = None,
    room_manager: OnlineRoomManager | None = None,
    llm_gateway: LLMGateway | None = None,
    client_factory: Callable[[str, str, float], Any] | None = None,
    dg_service: DGGuideService | None = None,
    blueprint_service: BlueprintPlannerService | None = None,
    pc_service: PCLevelGenerationService | None = None,
) -> AppContainer:
    resolved_settings = settings or BackendSettings.from_env()
    resolved_paths = paths or StudyLogPaths(
        study_log_dir if study_log_dir is not None else STUDY_LOG_DIR
    )
    resolved_clock = clock or Clock()
    resolved_logger = event_logger or EventLogger(
        settings=resolved_settings,
        clock=resolved_clock,
    )
    resolved_store = store or StudyLogStore(
        paths=resolved_paths,
        clock=resolved_clock,
    )
    resolved_room_manager = room_manager or OnlineRoomManager(
        store=resolved_store,
        settings=resolved_settings,
        clock=resolved_clock,
    )
    resolved_gateway = llm_gateway or LLMGateway(
        settings=resolved_settings,
        client_factory=client_factory or default_openai_client_factory,
        clock=resolved_clock,
        event_logger=resolved_logger,
    )
    resolved_dg_service = dg_service or DGGuideService(
        resolved_gateway,
        settings=resolved_settings,
    )
    resolved_blueprint_service = blueprint_service or BlueprintPlannerService(
        resolved_gateway,
        settings=resolved_settings,
        clock=resolved_clock,
        event_logger=resolved_logger,
    )
    resolved_pc_service = pc_service or PCLevelGenerationService(
        resolved_blueprint_service,
        resolved_settings,
        llm_gateway=resolved_gateway,
        clock=resolved_clock,
        event_logger=resolved_logger,
    )
    return AppContainer(
        settings=resolved_settings,
        paths=resolved_paths,
        clock=resolved_clock,
        event_logger=resolved_logger,
        store=resolved_store,
        room_manager=resolved_room_manager,
        llm_gateway=resolved_gateway,
        dg_service=resolved_dg_service,
        blueprint_service=resolved_blueprint_service,
        pc_service=resolved_pc_service,
    )


def get_container(request: Request) -> AppContainer:
    container = getattr(getattr(request, "app", None), "state", None)
    container = getattr(container, "container", None)
    if container is None:
        raise RuntimeError("AppContainer is not initialized on request.app.state")
    return container


def require_delete_password(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> None:
    resolved_container = (
        container
        if isinstance(container, AppContainer)
        else get_container(request)
    )
    verify_delete_password_header(
        request.headers.get("X-Delete-Password", ""),
        resolved_container.settings.dashboard_delete_password,
    )


__all__ = [
    "AppContainer",
    "BackendSettings",
    "BlueprintPlannerService",
    "Clock",
    "DGGuideService",
    "EventLogger",
    "LLMGateway",
    "OnlineRoomManager",
    "PCLevelGenerationService",
    "StudyLogPaths",
    "StudyLogStore",
    "build_container",
    "get_container",
    "require_delete_password",
]
