"""Dependency injection settings, online sync gateway, and service container for 8010."""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import Request

from cocreation.clock import Clock, SYSTEM_CLOCK, parse_time
from cocreation.domain.demo_level_generator import generate_demo_level
from cocreation.llm.config_and_runtime import LLM_INTERNAL_DEADLINE_SECONDS, LLMConfig
from cocreation.llm.service import CoCreationLLMService
from cocreation.persistence.database import (
    BACKEND_DIR,
    DEFAULT_DATABASE_PATH,
    SQLiteRepository,
    load_json,
    record_event as default_record_event,
)

from .config_models import (
    ApiError,
    COCREATION_DEADLINE_SECONDS,
    DEFAULT_ALLOWED_ORIGINS,
    DEFAULT_PUBLIC_BASE_URL,
    DEFAULT_TOKEN_SECRET,
    DEFAULT_WEBGL_BASE_URL,
    DRAFT_REGENERATION_CLAIMED_TIMEOUT_SECONDS,
    DRAFT_REGENERATION_PENDING_TIMEOUT_SECONDS,
    DRAFT_REGENERATION_SWEEP_INTERVAL_SECONDS,
    HOST,
    INTERRUPTED_AFTER,
    ONLINE_MATCH_SYNC_TIMEOUT_SECONDS,
    PLAY_TICKET_LIFETIME,
    PORT,
    _online_sync_api_error,
    build_launch_url,
    build_play_url,
    build_return_url,
    derive_token,
    session_deadline_expired,
)


def _env_bool(source: Mapping[str, str], name: str, default: bool) -> bool:
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip().casefold()
    if value in {'1', 'true', 'yes', 'on'}:
        return True
    if value in {'0', 'false', 'no', 'off'}:
        return False
    return default


def _env_int(source: Mapping[str, str], name: str, default: int, minimum: int = 1) -> int:
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(minimum, int(str(raw).strip()))
    except ValueError:
        return default


def _env_float(source: Mapping[str, str], name: str, default: float, minimum: float = 0.1) -> float:
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(minimum, float(str(raw).strip()))
    except ValueError:
        return default


@dataclass(frozen=True)
class CoCreationSettings:
    """Immutable runtime settings for the 8010 FastAPI service."""

    database_path: Path = DEFAULT_DATABASE_PATH
    public_base_url: str = DEFAULT_PUBLIC_BASE_URL
    webgl_base_url: str = DEFAULT_WEBGL_BASE_URL
    online_backend_base_url: str = ''
    intention_sync_secret: str = ''
    online_match_sync_timeout_seconds: float = ONLINE_MATCH_SYNC_TIMEOUT_SECONDS
    token_secret: str | bytes = DEFAULT_TOKEN_SECRET
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    llm_internal_deadline_seconds: float = LLM_INTERNAL_DEADLINE_SECONDS
    cocreation_deadline_seconds: int = COCREATION_DEADLINE_SECONDS
    interrupted_after_seconds: int = int(INTERRUPTED_AFTER.total_seconds())
    play_ticket_lifetime_seconds: int = int(PLAY_TICKET_LIFETIME.total_seconds())
    draft_regeneration_pending_timeout_seconds: int = DRAFT_REGENERATION_PENDING_TIMEOUT_SECONDS
    draft_regeneration_claimed_timeout_seconds: int = DRAFT_REGENERATION_CLAIMED_TIMEOUT_SECONDS
    draft_regeneration_sweep_interval_seconds: float = float(DRAFT_REGENERATION_SWEEP_INTERVAL_SECONDS)
    binary_discovery_enabled: bool = True
    sweeper_enabled: bool = True
    host: str = HOST
    port: int = PORT

    @property
    def token_secret_bytes(self) -> bytes:
        if isinstance(self.token_secret, bytes):
            return self.token_secret
        return str(self.token_secret).encode('utf-8')

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CoCreationSettings:
        if env is None:
            load_dotenv(BACKEND_DIR / '.env')
            source: Mapping[str, str] = os.environ
        else:
            source = env

        raw_db = str(source.get('COCREATION_DATABASE_PATH') or '').strip()
        if raw_db:
            db_path = Path(raw_db)
            if str(db_path) != ':memory:' and not db_path.is_absolute():
                db_path = (BACKEND_DIR / db_path).resolve()
        else:
            db_path = DEFAULT_DATABASE_PATH

        public_base_url = (
            str(source.get('COCREATION_PUBLIC_BASE_URL') or DEFAULT_PUBLIC_BASE_URL).strip()
            or DEFAULT_PUBLIC_BASE_URL
        ).rstrip('/')
        webgl_base_url = (
            str(source.get('COCREATION_WEBGL_BASE_URL') or DEFAULT_WEBGL_BASE_URL).strip()
            or DEFAULT_WEBGL_BASE_URL
        )
        online_backend_base_url = (
            str(source.get('COCREATION_ONLINE_MATCH_SYNC_URL') or '').strip().rstrip('/')
            or str(source.get('ONLINE_BACKEND_BASE_URL') or '').strip().rstrip('/')
        )
        intention_sync_secret = str(source.get('COCREATION_INTENTION_SYNC_SECRET') or '').strip()
        token_secret = str(source.get('COCREATION_TOKEN_SECRET') or DEFAULT_TOKEN_SECRET)
        raw_origins = source.get('COCREATION_ALLOWED_ORIGINS')
        if raw_origins is not None and str(raw_origins).strip():
            allowed_origins = tuple(item.strip() for item in str(raw_origins).split(',') if item.strip())
        else:
            allowed_origins = DEFAULT_ALLOWED_ORIGINS

        return cls(
            database_path=db_path,
            public_base_url=public_base_url,
            webgl_base_url=webgl_base_url,
            online_backend_base_url=online_backend_base_url,
            intention_sync_secret=intention_sync_secret,
            online_match_sync_timeout_seconds=_env_float(
                source,
                'COCREATION_ONLINE_MATCH_SYNC_TIMEOUT_SECONDS',
                ONLINE_MATCH_SYNC_TIMEOUT_SECONDS,
            ),
            token_secret=token_secret,
            allowed_origins=allowed_origins,
            llm_internal_deadline_seconds=_env_float(
                source,
                'COCREATION_LLM_INTERNAL_DEADLINE_SECONDS',
                LLM_INTERNAL_DEADLINE_SECONDS,
            ),
            cocreation_deadline_seconds=_env_int(
                source,
                'COCREATION_DEADLINE_SECONDS',
                COCREATION_DEADLINE_SECONDS,
            ),
            interrupted_after_seconds=_env_int(
                source,
                'COCREATION_INTERRUPTED_AFTER_SECONDS',
                int(INTERRUPTED_AFTER.total_seconds()),
            ),
            play_ticket_lifetime_seconds=_env_int(
                source,
                'COCREATION_PLAY_TICKET_LIFETIME_SECONDS',
                int(PLAY_TICKET_LIFETIME.total_seconds()),
            ),
            draft_regeneration_pending_timeout_seconds=_env_int(
                source,
                'COCREATION_DRAFT_REGENERATION_PENDING_TIMEOUT_SECONDS',
                DRAFT_REGENERATION_PENDING_TIMEOUT_SECONDS,
            ),
            draft_regeneration_claimed_timeout_seconds=_env_int(
                source,
                'COCREATION_DRAFT_REGENERATION_CLAIMED_TIMEOUT_SECONDS',
                DRAFT_REGENERATION_CLAIMED_TIMEOUT_SECONDS,
            ),
            draft_regeneration_sweep_interval_seconds=_env_float(
                source,
                'COCREATION_DRAFT_REGENERATION_SWEEP_INTERVAL_SECONDS',
                float(DRAFT_REGENERATION_SWEEP_INTERVAL_SECONDS),
            ),
            binary_discovery_enabled=_env_bool(source, 'COCREATION_BINARY_DISCOVERY_ENABLED', True),
            sweeper_enabled=_env_bool(source, 'COCREATION_SWEEPER_ENABLED', True),
        )


def compute_cocreation_duration_seconds(
    session: Any,
    *,
    cocreation_deadline_seconds: int = COCREATION_DEADLINE_SECONDS,
) -> int | None:
    """Return the consumed portion of the confirmed-language workbench deadline."""
    deadline_at = session['deadline_at']
    finalized_at = session['finalized_at']
    if not deadline_at or not finalized_at:
        return None
    remaining_seconds = max(0, int((parse_time(deadline_at) - parse_time(finalized_at)).total_seconds()))
    return min(cocreation_deadline_seconds, max(0, cocreation_deadline_seconds - remaining_seconds))


def build_version_sync_event(
    database: Any,
    session: Any,
    version_row: Any,
    event_type: str = 'stage',
    *,
    duration_seconds: int | None = None,
    cocreation_deadline_seconds: int = COCREATION_DEADLINE_SECONDS,
) -> dict[str, Any] | None:
    """Build the canonical 8000 flow event payload for a saved Stage version."""
    source = version_row['source']
    if event_type == 'stage' and source not in {'human_edit', 'llm_accepted', 'restored'}:
        return None
    if event_type == 'first_stage' and (source != 'initial' or version_row['stage_number'] != 1):
        return None
    event: dict[str, Any] = {
        'eventId': f"{event_type}:{version_row['id']}",
        'eventType': event_type,
        'versionId': version_row['id'],
        'stageNumber': version_row['stage_number'],
        'rows': load_json(version_row['rows_json']),
        'diff': load_json(version_row['diff_json']),
    }
    if event_type == 'stage':
        event['source'] = {'human_edit': 'manual', 'llm_accepted': 'ai', 'restored': 'restored'}[source]
        if source == 'restored':
            restoration = database.execute(
                "SELECT payload_json FROM audit_events WHERE session_id = ?\n"
                " AND event_type = 'stage_restored'\n"
                " AND json_extract(payload_json, '$.versionId') = ?\n"
                " ORDER BY id DESC LIMIT 1",
                (session['id'], version_row['id']),
            ).fetchone()
            if restoration is None:
                return None
            metadata = load_json(restoration['payload_json'])
            event.update({
                key: metadata[key]
                for key in (
                    'restoredFromVersionId',
                    'restoredFromStageNumber',
                    'replacedVersionId',
                    'replacedStageNumber',
                )
            })
    elif event_type == 'first_stage':
        event['initialDraftMethod'] = session['initial_draft_method']
    elif event_type == 'final':
        resolved_duration = (
            duration_seconds
            if duration_seconds is not None
            else compute_cocreation_duration_seconds(
                session,
                cocreation_deadline_seconds=cocreation_deadline_seconds,
            )
        )
        if resolved_duration is not None:
            event['coCreationDurationSeconds'] = resolved_duration
    return event


@runtime_checkable
class OnlineSyncGateway(Protocol):
    """Port for synchronizing 8010 co-creation state to the 8000 match server."""

    def sync_event(
        self,
        session: Any,
        event_type: str,
        stage_number: int | None = None,
        version_id: str | None = None,
        source: str | None = None,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    def sync_version(
        self,
        database: Any,
        session: Any,
        version_row: Any,
        *,
        event_type: str = 'stage',
        duration_seconds: int | None = None,
    ) -> None: ...

    def sync_turn(
        self,
        session: Any,
        turn_id: str,
        role: str,
        content: str,
        language: str,
        stage_number: int | None,
        created_at: str,
        guidance: dict[str, Any] | None = None,
    ) -> None: ...

    def sync_final_intention(
        self,
        session: Any,
        content: str,
        language: str | None = None,
        created_at: str | None = None,
        *,
        event_id: str = '',
    ) -> None: ...


class HttpOnlineSyncGateway:
    """HTTP client implementation posting co-creation sync events to 8000."""

    def __init__(
        self,
        settings: CoCreationSettings,
        http_client_factory: Callable[..., Any] = httpx.Client,
    ) -> None:
        self.settings = settings
        self.http_client_factory = http_client_factory

    @contextmanager
    def _open_client(self):
        client = self.http_client_factory(timeout=self.settings.online_match_sync_timeout_seconds)
        if hasattr(client, '__enter__') and hasattr(client, '__exit__'):
            with client as entered:
                yield entered
            return
        try:
            yield client
        finally:
            close_fn = getattr(client, 'close', None)
            if callable(close_fn):
                close_fn()

    def _post_json(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        rejected_code: str,
        unavailable_code: str,
        saved_item: str,
    ) -> None:
        try:
            with self._open_client() as client:
                response = client.post(
                    endpoint,
                    headers={'X-CoCreation-Sync-Secret': self.settings.intention_sync_secret},
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise _online_sync_api_error(
                error,
                rejected_code=rejected_code,
                unavailable_code=unavailable_code,
                saved_item=saved_item,
            ) from error

    def _validate_match_target(self, session: Any) -> tuple[str, int] | None:
        try:
            if bool(session['demo_mode']):
                return None
        except (KeyError, IndexError, TypeError):
            pass
        match_id = str(session['match_id'] or '').strip()
        player_number = session['player_number']
        if not match_id:
            return None
        if player_number not in (1, 2):
            raise ApiError(409, 'INVALID_MATCH_PLAYER', 'The linked online match player is invalid.')
        if not self.settings.online_backend_base_url or not self.settings.intention_sync_secret:
            return None
        return (match_id, player_number)

    def sync_event(
        self,
        session: Any,
        event_type: str,
        stage_number: int | None = None,
        version_id: str | None = None,
        source: str | None = None,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        target = self._validate_match_target(session)
        if target is None:
            return
        match_id, player_number = target
        if isinstance(metadata, dict) and ('eventType' in metadata or 'eventId' in metadata):
            event_payload = dict(metadata)
            event_payload.setdefault('eventType', event_type)
            if stage_number is not None:
                event_payload.setdefault('stageNumber', stage_number)
            if version_id is not None:
                event_payload.setdefault('versionId', version_id)
            if source is not None:
                event_payload.setdefault('source', source)
        else:
            event_payload = {'eventType': event_type}
            if stage_number is not None:
                event_payload['stageNumber'] = stage_number
            if version_id is not None:
                event_payload['versionId'] = version_id
            if source is not None:
                event_payload['source'] = source
            if summary is not None:
                event_payload['summary'] = summary
            if isinstance(metadata, dict):
                for key, value in metadata.items():
                    event_payload.setdefault(key, value)
        endpoint = f"{self.settings.online_backend_base_url}/online/rooms/{quote(match_id, safe='')}/cocreation-events"
        payload = {'playerNumber': player_number, 'sessionId': session['id'], **event_payload}
        self._post_json(
            endpoint,
            payload,
            rejected_code='ONLINE_FLOW_SYNC_REJECTED',
            unavailable_code='ONLINE_FLOW_SYNC_UNAVAILABLE',
            saved_item='co-creation change',
        )

    def sync_version(
        self,
        database: Any,
        session: Any,
        version_row: Any,
        *,
        event_type: str = 'stage',
        duration_seconds: int | None = None,
    ) -> None:
        if bool(session['demo_mode']):
            return
        event = build_version_sync_event(
            database,
            session,
            version_row,
            event_type=event_type,
            duration_seconds=duration_seconds,
            cocreation_deadline_seconds=self.settings.cocreation_deadline_seconds,
        )
        if event is None:
            return
        self.sync_event(
            session,
            event_type,
            stage_number=version_row['stage_number'],
            version_id=version_row['id'],
            source=event.get('source'),
            metadata=event,
        )

    def sync_turn(
        self,
        session: Any,
        turn_id: str,
        role: str,
        content: str,
        language: str,
        stage_number: int | None,
        created_at: str,
        guidance: dict[str, Any] | None = None,
    ) -> None:
        metadata: dict[str, Any] = {
            'eventId': f'turn:{turn_id}',
            'eventType': 'turn',
            'turnId': turn_id,
            'role': role,
            'content': content,
            'language': language,
            'stageNumber': stage_number,
            'createdAt': created_at,
        }
        if guidance is not None:
            metadata['guidance'] = guidance
        self.sync_event(session, 'turn', stage_number=stage_number, metadata=metadata)

    def sync_final_intention(
        self,
        session: Any,
        content: str,
        language: str | None = None,
        created_at: str | None = None,
        *,
        event_id: str = '',
    ) -> None:
        target = self._validate_match_target(session)
        if target is None:
            return
        match_id, player_number = target
        endpoint = f"{self.settings.online_backend_base_url}/online/rooms/{quote(match_id, safe='')}/designer-intention"
        payload = {
            'playerNumber': player_number,
            'designerIntention': content,
            'eventId': event_id,
            'sessionId': session['id'],
        }
        self._post_json(
            endpoint,
            payload,
            rejected_code='ONLINE_INTENTION_SYNC_REJECTED',
            unavailable_code='ONLINE_INTENTION_SYNC_UNAVAILABLE',
            saved_item='final design intention',
        )


@dataclass
class CoCreationServices:
    """Explicit dependency container for 8010 FastAPI routes and domain workflows."""

    settings: CoCreationSettings
    repository: SQLiteRepository
    llm: CoCreationLLMService
    online_sync: OnlineSyncGateway
    clock: Clock = SYSTEM_CLOCK
    demo_level_generator: Callable[..., Any] = generate_demo_level
    record_event_fn: Callable[..., Any] | None = None
    insert_turn_fn: Callable[..., str] | None = None
    retry_exhausted_execution_fn: Callable[..., Any] | None = None
    should_start_binary_fn: Callable[..., bool] | None = None
    _message_locks: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict, repr=False)
    _message_locks_guard: Lock = field(default_factory=Lock, repr=False)

    def connect(self, immediate: bool = False):
        return self.repository.connect(immediate=immediate)

    def utc_datetime(self) -> datetime:
        return self.clock.utc_datetime()

    def utc_now(self) -> str:
        return self.clock.utc_now()

    def monotonic(self) -> float:
        return self.clock.monotonic()

    def cocreation_deadline_timedelta(self) -> timedelta:
        return timedelta(seconds=self.settings.cocreation_deadline_seconds)

    def play_ticket_lifetime_timedelta(self) -> timedelta:
        return timedelta(seconds=self.settings.play_ticket_lifetime_seconds)

    def interrupted_after_timedelta(self) -> timedelta:
        return timedelta(seconds=self.settings.interrupted_after_seconds)

    def record_event(
        self,
        database: Any,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: str | None = None,
    ) -> None:
        timestamp = created_at if created_at is not None else self.utc_now()
        if self.record_event_fn is not None:
            self.record_event_fn(database, session_id, event_type, payload, timestamp)
            return
        default_record_event(database, session_id, event_type, payload, timestamp)

    def default_insert_turn(
        self,
        database: Any,
        session: Any,
        role: str,
        content: str,
        version_id: str,
        request_id: str | None,
        execution: Any,
    ) -> str:
        from .session_persistence_helpers import insert_turn

        return insert_turn(
            database,
            session,
            role,
            content,
            version_id,
            request_id,
            execution,
            services=self,
        )

    def insert_turn(
        self,
        database: Any,
        session: Any,
        role: str,
        content: str,
        version_id: str,
        request_id: str | None,
        execution: Any,
    ) -> str:
        if self.insert_turn_fn is not None:
            return self.insert_turn_fn(database, session, role, content, version_id, request_id, execution)
        return self.default_insert_turn(database, session, role, content, version_id, request_id, execution)

    def retry_exhausted_execution(self, language: str, exception: Any):
        if self.retry_exhausted_execution_fn is not None:
            return self.retry_exhausted_execution_fn(language, exception)
        from .proposal_and_challenge import _retry_exhausted_execution

        return _retry_exhausted_execution(language, exception)

    def should_start_binary(self, payload: Any, understanding: Any, context: dict[str, Any]) -> bool:
        if self.should_start_binary_fn is not None:
            return bool(self.should_start_binary_fn(payload, understanding, context))
        if not self.settings.binary_discovery_enabled:
            return False
        from .binary_routes import should_start

        return bool(should_start(payload, understanding, context))

    def serialize_session(self, database: Any, session_id: str, *, now: datetime | None = None):
        return self.repository.serialize_session(database, session_id, now=now)

    def derive_token(self, kind: str, identifier: str) -> str:
        return derive_token(kind, identifier, secret=self.settings.token_secret_bytes)

    def build_launch_url(self, session_id: str, bootstrap_token: str, mode: str = 'unity') -> str:
        return build_launch_url(session_id, bootstrap_token, mode=mode, public_base_url=self.settings.public_base_url)

    def build_return_url(self, session_id: str, version_id: str) -> str:
        return build_return_url(session_id, version_id, public_base_url=self.settings.public_base_url)

    def build_play_url(self, attempt_id: str, ticket: str) -> str:
        return build_play_url(attempt_id, ticket, webgl_base_url=self.settings.webgl_base_url)

    def session_deadline_expired(self, session: Any) -> bool:
        return session_deadline_expired(session, now=self.utc_datetime())


def build_default_services(
    *,
    settings: CoCreationSettings | None = None,
    llm_config: LLMConfig | None = None,
    repository: SQLiteRepository | None = None,
    llm: CoCreationLLMService | None = None,
    online_sync: OnlineSyncGateway | None = None,
    clock: Clock | None = None,
    client_factory: Callable[..., Any] | None = None,
    http_client_factory: Callable[..., Any] = httpx.Client,
    demo_level_generator: Callable[..., Any] | None = None,
    record_event_fn: Callable[..., Any] | None = None,
    insert_turn_fn: Callable[..., str] | None = None,
    retry_exhausted_execution_fn: Callable[..., Any] | None = None,
    should_start_binary_fn: Callable[..., bool] | None = None,
) -> CoCreationServices:
    """Assemble a lazy, zero-I/O service container from settings and defaults."""
    if settings is None or llm_config is None:
        load_dotenv(BACKEND_DIR / '.env')
    resolved_settings = settings or CoCreationSettings.from_env()
    resolved_clock = clock or SYSTEM_CLOCK
    resolved_llm_config = llm_config or LLMConfig.from_env()
    resolved_repo = repository or SQLiteRepository(resolved_settings.database_path, clock=resolved_clock)
    if llm is None:
        llm_kwargs: dict[str, Any] = {'clock': resolved_clock}
        if client_factory is not None:
            llm_kwargs['client_factory'] = client_factory
        resolved_llm = CoCreationLLMService(resolved_llm_config, **llm_kwargs)
    else:
        resolved_llm = llm
    resolved_sync = online_sync or HttpOnlineSyncGateway(
        resolved_settings,
        http_client_factory=http_client_factory,
    )
    return CoCreationServices(
        settings=resolved_settings,
        repository=resolved_repo,
        llm=resolved_llm,
        online_sync=resolved_sync,
        clock=resolved_clock,
        demo_level_generator=demo_level_generator or generate_demo_level,
        record_event_fn=record_event_fn,
        insert_turn_fn=insert_turn_fn,
        retry_exhausted_execution_fn=retry_exhausted_execution_fn,
        should_start_binary_fn=should_start_binary_fn,
    )


def get_services(request: Request) -> CoCreationServices:
    services = getattr(getattr(request, 'app', None), 'state', None)
    services = getattr(services, 'services', None)
    if services is None:
        raise RuntimeError('CoCreationServices is not initialized on request.app.state')
    return services
