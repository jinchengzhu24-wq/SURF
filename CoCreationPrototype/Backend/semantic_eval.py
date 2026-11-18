from pathlib import Path
import sys

_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import cocreation  # noqa: F401
from cocreation.tools import semantic_eval_runner as _runner  # noqa: E402

for _k, _v in vars(_runner).items():
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = _v
del _k, _v, _runner

if __name__ == "__main__":
    main()
