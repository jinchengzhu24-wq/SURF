"""Text sanitization, question classification, and deterministic identity helpers."""

from __future__ import annotations

import hashlib
import re
from typing import Any

MAX_TEXT = 1200

_QUESTION_MARKERS = re.compile(
    r"(?:\?|\uFF1F|\b(?:what|which|how|whether|can|should|would|could|do|does|is|are|"
    r"did|will)\b|\u4ec0\u4e48|\u54ea|\u5982\u4f55|\u662f\u5426|\u8981\u4e0d\u8981|\u5e94\u8be5|"
    r"\u66f4\u503e\u5411|\u5e0c\u671b|\u54ea\u4e2a)",
    flags=re.IGNORECASE,
)
_COORDINATE_MARKERS = re.compile(r"[\(\uFF08]\s*\d{1,2}\s*[,\uFF0C]\s*\d{1,2}\s*[\)\uFF09]")
_ROUTE_TRACE_MARKERS = re.compile(
    r"(?:\b(?:route\s+from|path|next|then|move\s+to|go\s+to|reachability|reachable|"
    r"bfs|solver|step)\b|\u5148(?:\u5230|\u8d70|\u63a8|\u79fb\u52a8)|\u518d(?:\u5230|\u8d70|\u63a8|\u79fb\u52a8)|"
    r"\u7ecf\u8fc7|\u8def\u5f84\u600e\u4e48\u8d70|\u54ea\u4e00\u6b65|\u4e0b\u4e00\u6b65|\u56de\u5230|"
    r"\u7ed5\u884c|\u53ef\u8fbe|\u901a\u4e0d\u901a|\u79fb\u52a8\u5230|\u63a8\u5230|\u8d70\u5230|"
    r"\u2192|->)",
    flags=re.IGNORECASE,
)
_SPATIAL_ONLY_MARKERS = re.compile(
    r"(?:\b(?:where|which\s+cell|what\s+coordinate|adjacent|near|beside|left|right|above|"
    r"below)\b|\u54ea\u91cc|\u5728\u54ea|\u54ea\u4e2a\u683c|\u5750\u6807|\u76f8\u90bb|\u9760\u8fd1|"
    r"\u5de6\u4fa7|\u53f3\u4fa7|\u4e0a\u65b9|\u4e0b\u65b9)",
    flags=re.IGNORECASE,
)
_DESIGN_MARKERS = re.compile(
    r"(?:\b(?:design|experience|intent|trade[- ]?off|choice|emphasis|rhythm|difficulty|"
    r"readability|guidance|layout|purpose|highlight|notice|stand\s+out|understand|read|"
    r"clarity|expectation|feel|match|direction|matters|first\s+push|push\s+order|detour|"
    r"route|corridor|water|box|target|player)\b|"
    r"\u8bbe\u8ba1|\u4f53\u9a8c|\u610f\u56fe|\u53d6\u820d|\u503e\u5411|\u91cd\u70b9|\u5f3a\u8c03|\u7a81\u51fa|"
    r"\u8282\u594f|\u96be\u5ea6|\u53ef\u8bfb\u6027|\u5f15\u5bfc|\u5e03\u5c40|\u4f5c\u7528|\u5206\u5de5|"
    r"\u5148\u540e|\u987a\u5e8f|\u8def\u7ebf|\u8def\u5f84|\u901a\u9053|\u7bb1\u5b50|\u76ee\u6807|"
    r"\u6c34\u57df|\u74f6\u9888|\u7ed5\u8def|\u8bfb\u61c2|\u6ce8\u610f|\u5e0c\u671b|\u503c\u5f97|"
    r"\u611f\u89c9|\u9884\u671f|\u6539\u52a8|\u4fdd\u7559|\u5e94\u8be5)",
    flags=re.IGNORECASE,
)
_STRONG_DESIGN_MARKERS = re.compile(
    r"(?:\b(?:design|experience|intent|trade[- ]?off|choice|emphasis|rhythm|difficulty|"
    r"readability|guidance|layout|purpose|highlight|stand\s+out|first\s+push|push\s+order|"
    r"feel|read|understand|notice|expectation|stay\s+open|keep|preserve|winding|longer|shorter)\b|"
    r"\u8bbe\u8ba1|\u4f53\u9a8c|\u610f\u56fe|\u53d6\u820d|\u91cd\u70b9|\u5f3a\u8c03|\u7a81\u51fa|"
    r"\u8282\u594f|\u96be\u5ea6|\u53ef\u8bfb\u6027|\u5f15\u5bfc|\u5e03\u5c40|\u4f5c\u7528|\u5206\u5de5|"
    r"\u5148\u540e|\u987a\u5e8f|\u7ed5\u8def|\u74f6\u9888)",
    flags=re.IGNORECASE,
)
_CURRENT_FACT_RE = re.compile(
    r"(?:\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_])\s*(?:\u5728|\u4f4d\u4e8e|\u5750\u843d\u4e8e|occupies|is\s+at|is\s+located\s+at|sits\s+on)\s*"
    r"(?:\u7b2c\s*)?\d{1,2}\s*(?:\u884c\s*[,\uFF0C]?\s*\u7b2c\s*|[,\uFF0C])\s*\d{1,2}\s*(?:\u5217)?"
    r"|\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_])\s*(?:\u5728|\u4f4d\u4e8e|\u5750\u843d\u4e8e|occupies|is\s+at|is\s+located\s+at|sits\s+on)\s*[\uFF08(]\s*\d{1,2}\s*[,\uFF0C]\s*\d{1,2}\s*[\uFF09)]"
    r"|[\uFF08(]\s*\d{1,2}\s*[,\uFF0C]\s*\d{1,2}\s*[\uFF09)]\s*(?:\u662f|\u4e3a|\u5c5e\u4e8e|is|contains)\s*"
    r"(?:\u6c34\u57df|\u6c34|\u5899|\u5899\u4f53|\u5730\u9762|\u7a7a\u5730|\u901a\u9053|water|wall|floor|ground|corridor)"
    r"|(?:\u7b2c\s*)?\d{1,2}\s*\u884c\s*(?:\u7b2c\s*)?\d{1,2}\s*\u5217\s*(?:\u662f|\u4e3a)\s*(?:P|B\d+|T\d+)(?![A-Za-z0-9_])"
    r"|\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_])\s*(?:occupies|is\s+at|is\s+located\s+(?:at|in)|sits\s+on)\s+row\s*\d{1,2}\s*,\s*column\s*\d{1,2}"
    r"|\brow\s*\d{1,2}\s*[,\uFF0C]\s*column\s*\d{1,2}\s*(?:is|contains)\s*(?:P|B\d+|T\d+)(?![A-Za-z0-9_]))",
    flags=re.IGNORECASE,
)
_REVERSE_FACT_RE = re.compile(
    r"\brow\s*\d{1,2}\s*[,\uFF0C]\s*column\s*\d{1,2}\s*(?:is|contains)\s*(?:P|B\d+|T\d+)(?![A-Za-z0-9_])",
    flags=re.IGNORECASE,
)
_FUTURE_PREFIX_RE = re.compile(
    r"(?:\u5982\u679c|\u82e5|\u5047\u8bbe|\u5c06|\u4f1a|\u5e0c\u671b|\u60f3\u8981|\u60f3\u628a|\u60f3\u8ba9|"
    r"\b(?:if|when|would|will|want|wish)\b)",
    flags=re.IGNORECASE,
)


def _clean_text(value: Any, maximum: int = MAX_TEXT) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]


def _opt_source(value: Any, maximum: int = 128) -> str | None:
    if value is None:
        return None
    cleaned = _clean_text(value, maximum)
    return cleaned or None


def _clamp_confidence(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _stable_id(kind: str, *values: Any) -> str:
    seed = "|".join(_clean_text(v, 500) for v in values)
    return f"dc_{kind}_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def sanitize_user_design_text(value: Any) -> str:
    """Remove present-tense map facts before semantic memory is persisted."""
    text = _clean_text(value)
    if not text:
        return text

    def _strip_current(match: re.Match[str]) -> str:
        prefix = text[max(0, match.start() - 32) : match.start()]
        return match.group(0) if _FUTURE_PREFIX_RE.search(prefix) else " "

    cleaned = _CURRENT_FACT_RE.sub(_strip_current, text)
    cleaned = _REVERSE_FACT_RE.sub(
        lambda m: m.group(0) if _FUTURE_PREFIX_RE.search(cleaned[: m.start()]) else " ",
        cleaned,
    )
    return re.sub(r"\s+([\uFF0C\u3002\uFF1B;,.!?\uFF01\uFF1F])", r"\1", cleaned).strip(
        " \t\r\n,;\uFF0C\uFF1B"
    )


def is_design_level_question(question: Any, evidence_text: Any = None) -> bool:
    """Return whether text is a designer-facing open question rather than route mechanics."""
    _ = evidence_text
    clean = _clean_text(question)
    if not clean or not _QUESTION_MARKERS.search(clean):
        return False

    lowered = clean.casefold()
    generic = (
        "what do you think",
        "does this work",
        "is this okay",
        "would you like to continue",
        "你怎么看",
        "可以吗",
        "是否满意",
        "还满意吗",
    )
    if any(value in lowered or value in clean for value in generic):
        return False
    if re.search(r"\b(?:bfs|solver|reachability|reachable)\b", lowered):
        return False

    coordinate_count = len(_COORDINATE_MARKERS.findall(clean))
    has_route_trace = bool(_ROUTE_TRACE_MARKERS.search(clean))
    has_design = bool(_DESIGN_MARKERS.search(clean))
    has_strong_design = bool(_STRONG_DESIGN_MARKERS.search(clean))
    has_spatial_only = bool(_SPATIAL_ONLY_MARKERS.search(clean))

    if coordinate_count >= 2 and not has_strong_design:
        return False
    if has_route_trace and coordinate_count >= 1 and not has_strong_design:
        return False
    if has_spatial_only and not has_design:
        return False
    return has_design


def question_dedup_key(value: Any) -> str:
    """Return a conservative identity for visibly equivalent questions."""
    clean = _clean_text(value).casefold()
    clean = re.sub(r"[\u201c\u201d\u2018\u2019\"'\u300c\u300d\u300e\u300f]", "", clean)
    clean = re.sub(r"^(?:\u8bf7)?(?:\u53ea|\u4ec5)?\u786e\u8ba4\s*[:\uFF1A]\s*", "", clean)
    clean = re.sub(
        r"(?:"
        r"(?:[?\uFF1F,\uFF0C;\uFF1B:\uFF1A.\u3002!\uFF01]\s*)?"
        r"(?:(?:please\s+)?(?:answer|reply)\s+(?:with\s+)?)?"
        r"yes\s*(?:or|/)\s*no\s*(?:only)?"
        r"|(?:[?\uFF1F,\uFF0C;\uFF1B:\uFF1A.\u3002!\uFF01]\s*)?"
        r"(?:\u8bf7)?(?:\u76f4\u63a5)?(?:\u56de\u7b54|\u56de\u590d)?\s*"
        r"\u662f\s*(?:\u6216|\u8fd8\u662f|/)\s*\u5426\s*"
        r"(?:\u5373\u53ef|\u5c31\u53ef\u4ee5)?"
        r")[.\u3002!\uFF01?\uFF1F]*$",
        "",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip(" ?\uFF1F,\uFF0C;\uFF1B:\uFF1A.\u3002!\uFF01").casefold()
