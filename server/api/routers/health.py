"""Health and readiness probe routes."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from server.api.deps import ContainerDep
from server.container import AppContainer

router = APIRouter()


def _check_database_readable(container: AppContainer) -> bool:
    try:
        with container.db.read_conn() as conn:
            row = conn.execute("SELECT 1").fetchone()
            return row is not None and int(row[0]) == 1
    except Exception:
        return False


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(container: ContainerDep) -> JSONResponse:
    db_ok = await asyncio.to_thread(_check_database_readable, container)
    payload = {
        "status": "ready" if db_ok else "not_ready",
        "database": "ready" if db_ok else "not_ready",
        "tokenSecretConfigured": not container.settings.is_default_token_secret,
        "deepseekConfigured": container.settings.has_deepseek_config,
        "cocreationLlmConfigured": container.settings.has_cocreation_llm_config,
    }
    return JSONResponse(status_code=200 if db_ok else 503, content=payload)
