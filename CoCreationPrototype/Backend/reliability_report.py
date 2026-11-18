from pathlib import Path
import sys

_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import cocreation  # noqa: F401
from cocreation.reliability import reliability_audit as _audit  # noqa: E402

for _k, _v in vars(_audit).items():
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = _v
del _k, _v, _audit

if __name__ == "__main__":
    main()
