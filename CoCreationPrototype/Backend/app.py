"""Uvicorn and ASGI entrypoint for the 8010 co-creation service."""
from __future__ import annotations

from pathlib import Path
import sys

_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from cocreation.api.app_factory import create_app  # noqa: E402
from cocreation.api.config_models import HOST, PORT  # noqa: E402
from cocreation.api.services import (  # noqa: E402
    CoCreationServices,
    CoCreationSettings,
    HttpOnlineSyncGateway,
    OnlineSyncGateway,
    build_default_services,
    get_services,
)

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
