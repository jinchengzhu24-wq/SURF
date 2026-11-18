from pathlib import Path
import sys

_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import cocreation  # noqa: F401
from cocreation.api import app_factory as _app_factory  # noqa: E402

if __name__ == "__main__":
    sys.modules.setdefault("app", sys.modules[__name__])

for _k, _v in vars(_app_factory).items():
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = _v
del _k, _v, _app_factory

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
