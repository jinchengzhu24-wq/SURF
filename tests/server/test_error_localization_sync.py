"""Cross-stack error localization sync and CoreLLMError details regression tests."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient
from server.core.llm_client import LLMValidationError
from server.main import create_app
from server.settings import SokobanSettings

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "server"
I18N_DIR = REPO_ROOT / "web" / "apps" / "cocreation" / "src" / "i18n"

_TARGET_ERROR_CLASSES = frozenset({"LLMValidationError", "CoreLLMValidationError", "BinaryError"})
_HANDLED_PREFIXES = ("BINARY_", "TRANSLATION_")
_HANDLED_SUFFIXES = ("_IN_PROGRESS",)


def _extract_server_error_codes() -> dict[str, set[str]]:
    """Statically parse all LLMValidationError(...) and BinaryError(...) string-literal codes."""
    codes_by_origin: dict[str, set[str]] = {}
    for py_path in sorted(SERVER_DIR.rglob("*.py")):
        tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
        rel_path = py_path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr
            else:
                continue
            if func_name not in _TARGET_ERROR_CLASSES:
                continue

            code_node: ast.AST | None = None
            if node.args:
                code_node = node.args[0]
            else:
                for kw in node.keywords:
                    if kw.arg == "code":
                        code_node = kw.value
                        break

            if isinstance(code_node, ast.Constant) and isinstance(code_node.value, str):
                code = code_node.value.strip()
                if code:
                    codes_by_origin.setdefault(code, set()).add(f"{rel_path}:{node.lineno}")
    return codes_by_origin


def _extract_grouped_error_codes(index_ts_text: str) -> dict[str, set[str]]:
    """Extract GROUPED_ERROR_KEYS mapping from web/apps/cocreation/src/i18n/index.ts."""
    match = re.search(
        r"const\s+GROUPED_ERROR_KEYS\b.*?=\s*\[(?P<body>.*?)\n\];",
        index_ts_text,
        re.DOTALL,
    )
    assert match is not None, "Could not locate GROUPED_ERROR_KEYS in i18n/index.ts"
    body = match.group("body")

    groups: dict[str, set[str]] = {}
    for entry in re.finditer(
        r'\[\s*"(?P<group_key>errors\.[A-Z0-9_]+)"\s*,\s*\[(?P<codes>.*?)\]\s*,?\s*\]',
        body,
        re.DOTALL,
    ):
        group_key = entry.group("group_key")
        codes = set(re.findall(r'"([A-Z0-9_]+)"', entry.group("codes")))
        groups.setdefault(group_key, set()).update(codes)
    return groups


def test_all_server_llm_and_binary_error_codes_resolve_in_cocreation_i18n() -> None:
    en_bundle: dict[str, str] = json.loads((I18N_DIR / "en.json").read_text(encoding="utf-8"))
    zh_bundle: dict[str, str] = json.loads((I18N_DIR / "zh-CN.json").read_text(encoding="utf-8"))
    index_ts = (I18N_DIR / "index.ts").read_text(encoding="utf-8")

    # 1. Exact key parity between en.json and zh-CN.json
    assert set(en_bundle.keys()) == set(zh_bundle.keys())

    # 2. Verify pattern fallbacks exist in index.ts before trusting them
    for prefix in _HANDLED_PREFIXES:
        assert f'startsWith("{prefix}")' in index_ts, (
            f"Expected prefix fallback {prefix!r} in i18n/index.ts"
        )
    for suffix in _HANDLED_SUFFIXES:
        assert f'endsWith("{suffix}")' in index_ts, (
            f"Expected suffix fallback {suffix!r} in i18n/index.ts"
        )

    # 3. Extract GROUPED_ERROR_KEYS and ensure every group key exists in both bundles
    grouped = _extract_grouped_error_codes(index_ts)
    assert grouped, "GROUPED_ERROR_KEYS must not be empty"
    grouped_codes: set[str] = set()
    for group_key, codes in grouped.items():
        assert group_key in en_bundle, f"Missing group key {group_key} in en.json"
        assert group_key in zh_bundle, f"Missing group key {group_key} in zh-CN.json"
        grouped_codes.update(codes)

    # 4. Extract all server LLMValidationError / BinaryError literal codes
    server_codes = _extract_server_error_codes()
    assert len(server_codes) >= 30, (
        f"Unexpectedly few server error codes found: {len(server_codes)}"
    )

    unresolved: list[str] = []
    for code, locations in sorted(server_codes.items()):
        exact_key = f"errors.{code}"
        has_exact = exact_key in en_bundle and exact_key in zh_bundle
        has_group = code in grouped_codes
        has_pattern = code.startswith(_HANDLED_PREFIXES) or code.endswith(_HANDLED_SUFFIXES)
        if not (has_exact or has_group or has_pattern):
            unresolved.append(f"{code} ({', '.join(sorted(locations))})")

    assert not unresolved, (
        "Server LLMValidationError/BinaryError codes missing cocreation localization "
        "(would fall back to errors.GENERIC):\n  - " + "\n  - ".join(unresolved)
    )


def test_handle_core_llm_error_includes_attempts_used_in_details(tmp_path: Path) -> None:
    settings = SokobanSettings(
        db_path=tmp_path / "test.sqlite3",
        reaper_enabled=False,
    )
    app = create_app(settings=settings)

    @app.get("/_test/core-llm-error")
    async def _raise_core_llm_error() -> None:
        raise LLMValidationError(
            "BINARY_PROTECTED_STRATEGY",
            "branches[0].strategy uses protected operator",
            attempts_used=3,
        )

    @app.get("/_test/core-llm-error-zero-attempts")
    async def _raise_core_llm_error_zero_attempts() -> None:
        raise LLMValidationError(
            "BINARY_PROTECTED_STRATEGY",
            "no attempts recorded",
            attempts_used=0,
        )

    with TestClient(app) as client:
        resp = client.get("/_test/core-llm-error")
        assert resp.status_code == 502
        payload = resp.json()
        assert payload["code"] == "BINARY_PROTECTED_STRATEGY"
        assert payload["details"]["attemptsUsed"] == 3
        assert payload["error"]["details"]["attemptsUsed"] == 3
        assert resp.headers.get("X-LLM-Attempts-Used") == "3"

        resp_zero = client.get("/_test/core-llm-error-zero-attempts")
        assert resp_zero.status_code == 502
        payload_zero = resp_zero.json()
        assert "attemptsUsed" not in payload_zero["details"]
        assert "X-LLM-Attempts-Used" not in resp_zero.headers
