"""Explicitly-loaded application settings with strict DeepSeek / Moonshot isolation."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, PrivateAttr, SecretStr, field_validator
from pydantic_settings import NoDecode

from server.core.settings import BaseSokobanSettings, parse_allowed_origins

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_DATABASE_PATH = DEFAULT_DATA_DIR / "sokoban.sqlite3"
DEFAULT_STATIC_DIR = PROJECT_ROOT / "web" / "dist"
DEFAULT_WEBGL_DIR = PROJECT_ROOT / "WebGLBuild"

DEFAULT_TOKEN_SECRET = "development-only-change-before-deployment"
PLACEHOLDER_TOKEN_SECRET = "replace-with-a-long-random-production-secret"
PLACEHOLDER_DELETE_PASSWORD = "replace-with-dashboard-delete-password"
PLACEHOLDER_API_KEYS: frozenset[str] = frozenset(
    {
        "your_deepseek_api_key_here",
        "your_kimi_api_key_here",
        "your_llm_api_key_here",
    }
)
_INSECURE_TOKEN_SECRETS = frozenset({DEFAULT_TOKEN_SECRET, PLACEHOLDER_TOKEN_SECRET})
_INSECURE_DELETE_PASSWORDS = frozenset({PLACEHOLDER_DELETE_PASSWORD})
_SECRET_ENV_ALIASES: tuple[str, ...] = (
    "DEEPSEEK_API_KEY",
    "COCREATION_LLM_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "TOKEN_SECRET",
    "COCREATION_TOKEN_SECRET",
    "DASHBOARD_DELETE_PASSWORD",
)
DEFAULT_PUBLIC_BASE_URL = "https://sokobanaidemo.top/cocreation"
DEFAULT_WEBGL_BASE_URL = "https://sokobanaidemo.top/game/"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_COCREATION_BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_COCREATION_MODEL = "kimi-k2.6"
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "https://sokobanaidemo.top",
    "https://www.sokobanaidemo.top",
    "http://127.0.0.1:8000",
    "http://localhost:8000",
)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_int(value: Any, default: int, *, minimum: int = 1) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        return max(minimum, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float, *, minimum: float = 0.1) -> float:
    if value is None or str(value).strip() == "":
        return default
    try:
        return max(minimum, float(str(value).strip()))
    except (TypeError, ValueError):
        return default


class SokobanSettings(BaseSokobanSettings):
    """Unified platform settings keeping DeepSeek and Moonshot/Kimi strictly isolated."""

    # --- DeepSeek (DG summary & level-plan blueprints ONLY) ---
    deepseek_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "deepseek_api_key"),
    )
    deepseek_base_url: str = Field(
        default=DEFAULT_DEEPSEEK_BASE_URL,
        validation_alias=AliasChoices("DEEPSEEK_BASE_URL", "deepseek_base_url"),
    )
    deepseek_model: str = Field(
        default=DEFAULT_DEEPSEEK_MODEL,
        validation_alias=AliasChoices("DEEPSEEK_MODEL", "deepseek_model"),
    )
    deepseek_timeout_seconds: float = Field(
        default=45.0,
        validation_alias=AliasChoices("PLAN_LLM_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"),
    )
    dg_timeout_seconds: float = Field(
        default=20.0,
        validation_alias=AliasChoices("DG_LLM_TIMEOUT_SECONDS", "dg_timeout_seconds"),
    )
    level_plan_timeout_seconds: float = Field(
        default=30.0,
        validation_alias=AliasChoices("LEVEL_PLAN_TIMEOUT_SECONDS", "level_plan_timeout_seconds"),
    )

    # --- Moonshot / Kimi (co-creation chat, binary, proposal, translation ONLY) ---
    cocreation_llm_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "COCREATION_LLM_API_KEY",
            "KIMI_API_KEY",
            "MOONSHOT_API_KEY",
            "cocreation_llm_api_key",
        ),
    )
    cocreation_llm_base_url: str = Field(
        default=DEFAULT_COCREATION_BASE_URL,
        validation_alias=AliasChoices(
            "COCREATION_LLM_BASE_URL",
            "KIMI_BASE_URL",
            "MOONSHOT_BASE_URL",
            "cocreation_llm_base_url",
        ),
    )
    cocreation_llm_model: str = Field(
        default=DEFAULT_COCREATION_MODEL,
        validation_alias=AliasChoices(
            "COCREATION_LLM_MODEL",
            "KIMI_MODEL",
            "MOONSHOT_MODEL",
            "cocreation_llm_model",
        ),
    )
    cocreation_llm_timeout_seconds: float = Field(
        default=120.0,
        validation_alias=AliasChoices(
            "COCREATION_LLM_TIMEOUT_SECONDS", "cocreation_llm_timeout_seconds"
        ),
    )
    cocreation_llm_internal_deadline_seconds: float = Field(
        default=116.0,
        validation_alias=AliasChoices(
            "COCREATION_LLM_INTERNAL_DEADLINE_SECONDS",
            "cocreation_llm_internal_deadline_seconds",
        ),
    )

    # --- Security, storage, and static asset paths ---
    token_secret: SecretStr = Field(
        default=SecretStr(DEFAULT_TOKEN_SECRET),
        validation_alias=AliasChoices("TOKEN_SECRET", "COCREATION_TOKEN_SECRET", "token_secret"),
    )
    dashboard_delete_password: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("DASHBOARD_DELETE_PASSWORD", "dashboard_delete_password"),
    )
    data_dir: Path = Field(
        default=DEFAULT_DATA_DIR,
        validation_alias=AliasChoices("SOKOBAN_DATA_DIR", "data_dir"),
    )
    db_path: Path = Field(
        default=DEFAULT_DATABASE_PATH,
        validation_alias=AliasChoices(
            "SOKOBAN_DB_PATH", "COCREATION_DATABASE_PATH", "db_path"
        ),
    )
    allowed_origins: Annotated[tuple[str, ...], NoDecode] = Field(
        default=DEFAULT_ALLOWED_ORIGINS,
        validation_alias=AliasChoices(
            "ALLOWED_ORIGINS",
            "BACKEND_ALLOWED_ORIGINS",
            "COCREATION_ALLOWED_ORIGINS",
            "allowed_origins",
        ),
    )
    static_dir: Path = Field(
        default=DEFAULT_STATIC_DIR,
        validation_alias=AliasChoices("SOKOBAN_STATIC_DIR", "static_dir"),
    )
    webgl_build_dir: Path = Field(
        default=DEFAULT_WEBGL_DIR,
        validation_alias=AliasChoices("SOKOBAN_WEBGL_BUILD_DIR", "webgl_build_dir"),
    )

    # --- URLs & operational timers ---
    public_base_url: str = Field(
        default=DEFAULT_PUBLIC_BASE_URL,
        validation_alias=AliasChoices("COCREATION_PUBLIC_BASE_URL", "public_base_url"),
    )
    webgl_base_url: str = Field(
        default=DEFAULT_WEBGL_BASE_URL,
        validation_alias=AliasChoices("COCREATION_WEBGL_BASE_URL", "webgl_base_url"),
    )
    online_room_ttl_seconds: float = Field(
        default=1800.0,
        validation_alias=AliasChoices("ONLINE_ROOM_TTL_SECONDS", "online_room_ttl_seconds"),
    )
    room_reaper_interval_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices(
            "ROOM_REAPER_INTERVAL_SECONDS", "room_reaper_interval_seconds"
        ),
    )
    reaper_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("SOKOBAN_REAPER_ENABLED", "reaper_enabled"),
    )
    cocreation_deadline_seconds: int = Field(
        default=1200,
        validation_alias=AliasChoices("COCREATION_DEADLINE_SECONDS", "cocreation_deadline_seconds"),
    )
    demo_ttl_seconds: int = Field(
        default=86400,
        validation_alias=AliasChoices("COCREATION_DEMO_TTL_SECONDS", "demo_ttl_seconds"),
    )
    play_ticket_lifetime_seconds: int = Field(
        default=300,
        validation_alias=AliasChoices(
            "COCREATION_PLAY_TICKET_LIFETIME_SECONDS", "play_ticket_lifetime_seconds"
        ),
    )
    interrupted_after_seconds: int = Field(
        default=1800,
        validation_alias=AliasChoices(
            "COCREATION_INTERRUPTED_AFTER_SECONDS", "interrupted_after_seconds"
        ),
    )
    draft_regeneration_pending_timeout_seconds: int = Field(
        default=60,
        validation_alias=AliasChoices(
            "COCREATION_DRAFT_REGENERATION_PENDING_TIMEOUT_SECONDS",
            "draft_regeneration_pending_timeout_seconds",
        ),
    )
    draft_regeneration_claimed_timeout_seconds: int = Field(
        default=120,
        validation_alias=AliasChoices(
            "COCREATION_DRAFT_REGENERATION_CLAIMED_TIMEOUT_SECONDS",
            "draft_regeneration_claimed_timeout_seconds",
        ),
    )
    binary_discovery_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "COCREATION_BINARY_DISCOVERY_ENABLED", "binary_discovery_enabled"
        ),
    )
    _extra_env_secrets: tuple[str, ...] = PrivateAttr(default=())
    _isolated_env: bool = PrivateAttr(default=False)

    @field_validator("deepseek_base_url", mode="before")
    @classmethod
    def _clean_deepseek_base_url(cls, value: Any) -> str:
        return str(value or "").strip() or DEFAULT_DEEPSEEK_BASE_URL

    @field_validator("deepseek_model", mode="before")
    @classmethod
    def _clean_deepseek_model(cls, value: Any) -> str:
        return str(value or "").strip() or DEFAULT_DEEPSEEK_MODEL

    @field_validator("cocreation_llm_base_url", mode="before")
    @classmethod
    def _clean_cocreation_base_url(cls, value: Any) -> str:
        return str(value or "").strip() or DEFAULT_COCREATION_BASE_URL

    @field_validator("cocreation_llm_model", mode="before")
    @classmethod
    def _clean_cocreation_model(cls, value: Any) -> str:
        return str(value or "").strip() or DEFAULT_COCREATION_MODEL

    @field_validator("public_base_url", mode="before")
    @classmethod
    def _clean_public_base_url(cls, value: Any) -> str:
        return (str(value or "").strip() or DEFAULT_PUBLIC_BASE_URL).rstrip("/")

    @field_validator("webgl_base_url", mode="before")
    @classmethod
    def _clean_webgl_base_url(cls, value: Any) -> str:
        return str(value or "").strip() or DEFAULT_WEBGL_BASE_URL

    @field_validator("token_secret", mode="before")
    @classmethod
    def _clean_token_secret(cls, value: Any) -> str:
        if isinstance(value, SecretStr):
            raw = value.get_secret_value()
        else:
            raw = str(value or "")
        return raw.strip() or DEFAULT_TOKEN_SECRET

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _clean_allowed_origins(cls, value: Any) -> tuple[str, ...]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return DEFAULT_ALLOWED_ORIGINS
        return parse_allowed_origins(value)

    @property
    def dashboard_dist_dir(self) -> Path:
        return self.static_dir / "dashboard"

    @property
    def cocreation_dist_dir(self) -> Path:
        return self.static_dir / "cocreation"

    @property
    def is_default_token_secret(self) -> bool:
        value = self.token_secret.get_secret_value().strip()
        return not value or value in _INSECURE_TOKEN_SECRETS

    @property
    def has_deepseek_config(self) -> bool:
        value = self.deepseek_api_key.get_secret_value().strip()
        return bool(value) and value not in PLACEHOLDER_API_KEYS

    @property
    def has_cocreation_llm_config(self) -> bool:
        value = self.cocreation_llm_api_key.get_secret_value().strip()
        return bool(value) and value not in PLACEHOLDER_API_KEYS

    @property
    def is_placeholder_delete_password(self) -> bool:
        value = self.dashboard_delete_password.get_secret_value().strip()
        return value in _INSECURE_DELETE_PASSWORDS

    def secret_values(self) -> tuple[str, ...]:
        """Collect all configured secret values across fields and known env aliases."""
        collected = list(super().secret_values())
        collected.extend(self._extra_env_secrets)
        if not self._isolated_env:
            for key in _SECRET_ENV_ALIASES:
                raw = str(os.environ.get(key) or "").strip()
                if raw:
                    collected.append(raw)
        return tuple(dict.fromkeys(item for item in collected if item))

    def warn_if_default_token_secret(self) -> None:
        """Emit prominent startup warnings for default/placeholder security secrets."""
        if self.is_default_token_secret:
            logger.warning(
                "SECURITY WARNING: TOKEN_SECRET is set to the default/placeholder value "
                "'%s'. Configure a strong random TOKEN_SECRET before deployment.",
                self.token_secret.get_secret_value(),
            )
        if self.is_placeholder_delete_password:
            logger.warning(
                "SECURITY WARNING: DASHBOARD_DELETE_PASSWORD is set to the example placeholder "
                "value and will be treated as unconfigured (HTTP 503 on delete)."
            )

    def to_cocreation_app_settings(self) -> Any:
        """Build CoCreationAppSettings from unified SokobanSettings."""
        from server.application.sessions import CoCreationAppSettings

        return CoCreationAppSettings(
            public_base_url=self.public_base_url,
            webgl_base_url=self.webgl_base_url,
            token_secret=self.token_secret.get_secret_value(),
            cocreation_deadline_seconds=self.cocreation_deadline_seconds,
            play_ticket_lifetime_seconds=self.play_ticket_lifetime_seconds,
            interrupted_after_seconds=self.interrupted_after_seconds,
            demo_ttl_seconds=self.demo_ttl_seconds,
            draft_regeneration_pending_timeout_seconds=(
                self.draft_regeneration_pending_timeout_seconds
            ),
            draft_regeneration_claimed_timeout_seconds=(
                self.draft_regeneration_claimed_timeout_seconds
            ),
            binary_discovery_enabled=self.binary_discovery_enabled,
        )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        env_file: Path | str | None = None,
        load_dotenv_file: bool = False,
    ) -> SokobanSettings:
        """Explicitly construct settings from a mapping or process environment."""
        if load_dotenv_file and env is None:
            target = Path(env_file) if env_file is not None else DEFAULT_ENV_FILE
            if target.exists():
                load_dotenv(target, override=False)
        source = os.environ if env is None else env
        data_dir_raw = str(source.get("SOKOBAN_DATA_DIR") or "").strip()
        data_dir = Path(data_dir_raw) if data_dir_raw else DEFAULT_DATA_DIR
        db_raw = str(
            source.get("SOKOBAN_DB_PATH") or source.get("COCREATION_DATABASE_PATH") or ""
        ).strip()
        if db_raw == ":memory:":
            db_path = Path(":memory:")
        elif db_raw:
            db_path = Path(db_raw)
        else:
            db_path = data_dir / "sokoban.sqlite3"

        provider = str(source.get("COCREATION_LLM_PROVIDER") or "").strip().casefold()
        if provider not in {"", "kimi", "moonshot"}:
            cocreation_key = ""
        else:
            cocreation_key = (
                str(source.get("COCREATION_LLM_API_KEY") or "").strip()
                or str(source.get("KIMI_API_KEY") or "").strip()
                or str(source.get("MOONSHOT_API_KEY") or "").strip()
            )
        cocreation_base_url = (
            str(source.get("COCREATION_LLM_BASE_URL") or "").strip()
            or str(source.get("KIMI_BASE_URL") or "").strip()
            or str(source.get("MOONSHOT_BASE_URL") or "").strip()
            or DEFAULT_COCREATION_BASE_URL
        )
        cocreation_model = (
            str(source.get("COCREATION_LLM_MODEL") or "").strip()
            or str(source.get("KIMI_MODEL") or "").strip()
            or str(source.get("MOONSHOT_MODEL") or "").strip()
            or DEFAULT_COCREATION_MODEL
        )
        raw_origins = (
            source.get("ALLOWED_ORIGINS")
            or source.get("BACKEND_ALLOWED_ORIGINS")
            or source.get("COCREATION_ALLOWED_ORIGINS")
        )
        static_raw = str(source.get("SOKOBAN_STATIC_DIR") or "").strip()
        webgl_raw = str(source.get("SOKOBAN_WEBGL_BUILD_DIR") or "").strip()

        extra_env_secrets = tuple(
            dict.fromkeys(
                str(source.get(key) or "").strip()
                for key in _SECRET_ENV_ALIASES
                if str(source.get(key) or "").strip()
            )
        )

        instance = cls(
            log_level=str(
                source.get("LOG_LEVEL") or source.get("BACKEND_LOG_LEVEL") or "INFO"
            ).strip(),
            deepseek_api_key=SecretStr(str(source.get("DEEPSEEK_API_KEY") or "").strip()),
            deepseek_base_url=str(
                source.get("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL
            ).strip(),
            deepseek_model=str(source.get("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL).strip(),
            deepseek_timeout_seconds=_coerce_float(
                source.get("PLAN_LLM_TIMEOUT_SECONDS") or source.get("LLM_TIMEOUT_SECONDS"),
                45.0,
            ),
            dg_timeout_seconds=_coerce_float(source.get("DG_LLM_TIMEOUT_SECONDS"), 20.0),
            level_plan_timeout_seconds=_coerce_float(
                source.get("LEVEL_PLAN_TIMEOUT_SECONDS"), 30.0
            ),
            cocreation_llm_api_key=SecretStr(cocreation_key),
            cocreation_llm_base_url=cocreation_base_url,
            cocreation_llm_model=cocreation_model,
            cocreation_llm_timeout_seconds=_coerce_float(
                source.get("COCREATION_LLM_TIMEOUT_SECONDS"), 120.0
            ),
            cocreation_llm_internal_deadline_seconds=_coerce_float(
                source.get("COCREATION_LLM_INTERNAL_DEADLINE_SECONDS"), 116.0
            ),
            token_secret=SecretStr(
                str(
                    source.get("TOKEN_SECRET")
                    or source.get("COCREATION_TOKEN_SECRET")
                    or DEFAULT_TOKEN_SECRET
                ).strip()
                or DEFAULT_TOKEN_SECRET
            ),
            dashboard_delete_password=SecretStr(
                str(source.get("DASHBOARD_DELETE_PASSWORD") or "").strip()
            ),
            data_dir=data_dir,
            db_path=db_path,
            allowed_origins=(
                parse_allowed_origins(raw_origins)
                if raw_origins is not None and str(raw_origins).strip()
                else DEFAULT_ALLOWED_ORIGINS
            ),
            static_dir=Path(static_raw) if static_raw else DEFAULT_STATIC_DIR,
            webgl_build_dir=Path(webgl_raw) if webgl_raw else DEFAULT_WEBGL_DIR,
            public_base_url=str(
                source.get("COCREATION_PUBLIC_BASE_URL") or DEFAULT_PUBLIC_BASE_URL
            ).strip(),
            webgl_base_url=str(
                source.get("COCREATION_WEBGL_BASE_URL") or DEFAULT_WEBGL_BASE_URL
            ).strip(),
            online_room_ttl_seconds=_coerce_float(
                source.get("ONLINE_ROOM_TTL_SECONDS"), 1800.0, minimum=1.0
            ),
            room_reaper_interval_seconds=_coerce_float(
                source.get("ROOM_REAPER_INTERVAL_SECONDS"), 60.0, minimum=1.0
            ),
            reaper_enabled=_coerce_bool(source.get("SOKOBAN_REAPER_ENABLED"), True),
            cocreation_deadline_seconds=_coerce_int(
                source.get("COCREATION_DEADLINE_SECONDS"), 1200
            ),
            demo_ttl_seconds=_coerce_int(source.get("COCREATION_DEMO_TTL_SECONDS"), 86400),
            play_ticket_lifetime_seconds=_coerce_int(
                source.get("COCREATION_PLAY_TICKET_LIFETIME_SECONDS"), 300
            ),
            interrupted_after_seconds=_coerce_int(
                source.get("COCREATION_INTERRUPTED_AFTER_SECONDS"), 1800
            ),
            draft_regeneration_pending_timeout_seconds=_coerce_int(
                source.get("COCREATION_DRAFT_REGENERATION_PENDING_TIMEOUT_SECONDS"), 60
            ),
            draft_regeneration_claimed_timeout_seconds=_coerce_int(
                source.get("COCREATION_DRAFT_REGENERATION_CLAIMED_TIMEOUT_SECONDS"), 120
            ),
            binary_discovery_enabled=_coerce_bool(
                source.get("COCREATION_BINARY_DISCOVERY_ENABLED"), True
            ),
        )
        instance._extra_env_secrets = extra_env_secrets
        instance._isolated_env = env is not None
        return instance


def load_settings(
    *,
    env_file: Path | str | None = None,
    load_dotenv_file: bool = True,
) -> SokobanSettings:
    """Explicit settings factory used by create_app() (never invoked at import time)."""
    return SokobanSettings.from_env(env_file=env_file, load_dotenv_file=load_dotenv_file)


__all__ = [
    "DEFAULT_ALLOWED_ORIGINS",
    "DEFAULT_COCREATION_BASE_URL",
    "DEFAULT_COCREATION_MODEL",
    "DEFAULT_DATABASE_PATH",
    "DEFAULT_DATA_DIR",
    "DEFAULT_DEEPSEEK_BASE_URL",
    "DEFAULT_DEEPSEEK_MODEL",
    "DEFAULT_ENV_FILE",
    "DEFAULT_PUBLIC_BASE_URL",
    "DEFAULT_STATIC_DIR",
    "DEFAULT_TOKEN_SECRET",
    "DEFAULT_WEBGL_BASE_URL",
    "DEFAULT_WEBGL_DIR",
    "PLACEHOLDER_API_KEYS",
    "PLACEHOLDER_DELETE_PASSWORD",
    "PLACEHOLDER_TOKEN_SECRET",
    "PROJECT_ROOT",
    "SokobanSettings",
    "load_settings",
]
