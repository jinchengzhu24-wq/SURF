import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from .llm import prompts, runtime

# Register legacy module aliases so bare imports and existing test patches
# target the exact extracted modules inside sokoban_backend.
sys.modules.setdefault("llm_runtime", runtime)
sys.modules.setdefault("Backend.llm_runtime", runtime)
sys.modules.setdefault("prompt", prompts)
sys.modules.setdefault("Backend.prompt", prompts)

__all__ = ["prompts", "runtime"]
