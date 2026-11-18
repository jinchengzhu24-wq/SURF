import sys
import time as _time


def get_app_module():
    """Return the active Backend.app module so monkeypatches on it remain authoritative."""
    module = sys.modules.get("Backend.app") or sys.modules.get("app")
    if module is None:
        raise RuntimeError("Backend.app module is not loaded")
    return module


def app_attr(name, default=None):
    module = sys.modules.get("Backend.app") or sys.modules.get("app")
    if module is None:
        return default
    return getattr(module, name, default)


def call_app_hook(name, *args, **kwargs):
    fn = getattr(get_app_module(), name)
    return fn(*args, **kwargs)


def time_module():
    module = sys.modules.get("Backend.app") or sys.modules.get("app")
    if module is not None and hasattr(module, "time"):
        return module.time
    return _time


def current_time():
    return time_module().time()


def current_perf_counter():
    return time_module().perf_counter()


def utc_timestamp():
    tm = time_module()
    return tm.strftime("%Y-%m-%dT%H:%M:%SZ", tm.gmtime())
