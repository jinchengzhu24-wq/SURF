import sys
from pathlib import Path


def _module(name):
    return sys.modules.get(name)


def app_module():
    module = _module("app") or _module("cocreation.api.app_factory")
    if module is None:
        raise RuntimeError("app module is not loaded")
    return module


def app_attr(name, default=None):
    module = _module("app")
    if module is not None and hasattr(module, name):
        return getattr(module, name)
    return default


def call_app(name, *args, **kwargs):
    fn = getattr(app_module(), name)
    return fn(*args, **kwargs)


def llm_module():
    module = _module("llm_client") or _module("cocreation.llm.facade")
    if module is None:
        raise RuntimeError("llm_client module is not loaded")
    return module


def llm_attr(name, default=None):
    module = _module("llm_client")
    if module is not None and hasattr(module, name):
        return getattr(module, name)
    return default


def call_llm(name, *args, **kwargs):
    fn = getattr(llm_module(), name)
    return fn(*args, **kwargs)


def repo_module():
    module = _module("repository") or _module("cocreation.persistence.repository")
    if module is None:
        raise RuntimeError("repository module is not loaded")
    return module


def database_path():
    repo = repo_module()
    path = getattr(repo, "DATABASE_PATH", None)
    if path is not None:
        return Path(path)
    return Path(__file__).resolve().parents[1] / "cocreation.db"
