"""Modular package for the 8010 Sokoban co-creation service."""
import importlib
from pathlib import Path
import sys

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Import and bind legacy top-level module aliases strictly in dependency order
# so internal bare imports resolve cleanly to the packaged modules.
_LEGACY_MODULE_MAP = (
    ("proposal_diagnostics", "cocreation.domain.proposal_diagnostics"),
    ("conversation_evidence", "cocreation.domain.conversation_evidence"),
    ("component_recovery", "cocreation.reliability.component_recovery"),
    ("component_patches", "cocreation.reliability.component_patches"),
    ("level_validation", "cocreation.domain.level_validation"),
    ("demo_level_generator", "cocreation.domain.demo_level_generator"),
    ("reply_reliability", "cocreation.reliability.reply_reliability"),
    ("proposal_options", "cocreation.domain.proposal_options"),
    ("design_requirements", "cocreation.domain.design_requirements"),
    ("design_context", "cocreation.domain.design_context"),
    ("proposal_search", "cocreation.domain.proposal_search"),
    ("revision_workflow", "cocreation.domain.revision_workflow"),
    ("proposal_understanding", "cocreation.domain.proposal_understanding"),
    ("understanding_review", "cocreation.domain.understanding_review"),
    ("binary_discovery", "cocreation.domain.binary_discovery"),
    ("repository", "cocreation.persistence.repository"),
    ("llm_client", "cocreation.llm.facade"),
    ("binary_model", "cocreation.llm.binary_model"),
    ("binary_routes", "cocreation.api.binary_routes"),
    ("understanding_review_probe", "cocreation.tools.understanding_review_probe"),
)

for _legacy_name, _target_path in _LEGACY_MODULE_MAP:
    _loaded = importlib.import_module(_target_path)
    sys.modules.setdefault(_legacy_name, _loaded)

del _legacy_name, _target_path, _loaded, _LEGACY_MODULE_MAP
