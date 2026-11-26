"""Precompiled module-level regexes and lookup tables for map grounding."""

from __future__ import annotations

import re

from server.core.grid import (
    TILE_BOX,
    TILE_FLOOR,
    TILE_PLAYER,
    TILE_TARGET,
    TILE_WALL,
    TILE_WATER,
)

CODE_FENCE_RE = re.compile(r"```(?:json|text|plaintext)?\s*", re.IGNORECASE)
INLINE_CODE_RE = re.compile(r"`([^`\r\n]+)`")
MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
LINE_INDENT_RE = re.compile(r"\n[ \t]+")
CLAUSE_SPLIT_RE = re.compile(r"[,，;；.!?。！？\r\n]+")
ROUTE_CLAUSE_SPLIT_RE = re.compile(
    r"\s*(?:[;；]+|\s+(?:and then|while|whereas)\s+|,(?=\s*(?:while|whereas|before|after|or)\b)"
    r"|，(?=\s*(?:同时|然后|随后|再|而|或者|之前|之后)))\s*",
    re.IGNORECASE,
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s*|[\r\n]+")
PROTECT_COORD_COMMA_RE = re.compile(r"(?<=\d)[,，](?=\s*\d)")
PROTECT_ROW_COL_COMMA_RE = re.compile(
    r"((?:row|line)\s*\d{1,2})\s*,\s*((?:column|col)\b)", re.IGNORECASE
)

HISTORICAL_RE = re.compile(
    r"(?:\b(?:was|were|changed|turned|became|had\s+been|used\s+to|previously|formerly|"
    r"earlier|originally|last\s+version|previous\s+(?:version|draft|stage|map)|"
    r"prior\s+(?:version|draft|stage|map))\b|之前|原本|原先|曾经|过去|旧版|上一版|上个版本|"
    r"最初|变成了|改成了)",
    re.IGNORECASE,
)
NON_CURRENT_RE = re.compile(
    r"(?:\b(?:if|when|would|will|move(?:s|d|ing)?|push(?:es|ed|ing)?|toward(?:s)?|from)\b"
    r"|如果|若|假设|将|会|移动|推到|推向|改到|变成)",
    re.IGNORECASE,
)
NEGATION_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:not|no|never|isn't|aren't)(?![A-Za-z0-9_])"
    r"|(?:并没有|不在|没有|不是|并非|未)",
    re.IGNORECASE,
)
NEIGHBOR_PREFIX_RE = re.compile(
    r"(?:\badjacent\s+to|\bnext\s+to|\bclose\s+to|\bbeside|\bnear|靠近|相邻|紧挨|旁边)\s*$",
    re.IGNORECASE,
)
LEADING_ARTICLE_RE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)

_TILE_TOKEN = (
    r"(?:墙体?|墙|地板|空地|水域?|水|箱子?|目标点?|玩家|起点|"
    r"(?:(?:a|an|the)\s+)?(?:wall|floor|ground|water|box|crate|target|goal|player|start)"
    r"(?![A-Za-z0-9_])|#|\.|@|(?<![A-Za-z0-9_])(?:p|s|t)(?![A-Za-z0-9_]))"
)
COORD_TILE_CLAIM_RE = re.compile(
    rf"[（(]\s*(\d{{1,2}})\s*[,，]\s*(\d{{1,2}})\s*[)）]\s*"
    rf"(?:是|为|属于|is|contains(?:\s+the)?|has)\s*({_TILE_TOKEN})",
    re.IGNORECASE,
)
COORD_TRANSITION_RE = re.compile(
    rf"[（(]\s*(\d{{1,2}})\s*[,，]\s*(\d{{1,2}})\s*[)）]\s*.{{0,18}}?"
    rf"(?:从|由|from)\s*({_TILE_TOKEN})\s*(?:变成|改成|换成|变为|to|into|becomes?)\s*({_TILE_TOKEN})",
    re.IGNORECASE,
)

_ENTITY_TOKEN = r"(?<![A-Za-z0-9])(?:P|B\d+|T\d+|玩家|起点)(?![A-Za-z0-9])"
_COORD_TOKEN = (
    r"(?:[\(（]\s*(?P<row_p>\d{1,2})\s*[,，]\s*(?P<col_p>\d{1,2})\s*[\)）]"
    r"|(?:row|line)\s*(?P<row_e>\d{1,2})\s*,?\s*(?:column|col)\s*(?P<col_e>\d{1,2})"
    r"|第\s*(?P<row_z>\d{1,2})\s*行\s*第\s*(?P<col_z>\d{1,2})\s*列)"
)
ENTITY_FORWARD_RE = re.compile(
    rf"(?P<entity>{_ENTITY_TOKEN})\s*(?:当前|目前|currently\s+)?"
    rf"(?:(?:的)?坐标\s*(?:是|为|位于|:|：)?|(?:'s\s+)?coordinates?\s*(?:is|are|:)?|"
    rf"位于|在|处于|坐落于|起点为|is\s+(?:currently\s+)?(?:at|in|on)|"
    rf"(?:is\s+)?located\s+(?:at|in|on)|sits?\s+(?:at|in|on)|occup(?:y|ies|ied)|"
    rf"positioned\s+(?:at|in|on)|placed\s+(?:at|in|on))\s*{_COORD_TOKEN}",
    re.IGNORECASE,
)
ENTITY_REVERSE_RE = re.compile(
    rf"{_COORD_TOKEN}\s*(?:的位置|这个位置|该位置|the\s+(?:cell|position)\s+)?"
    rf"(?:是|为|属于|is|contains?)\s*(?P<entity>{_ENTITY_TOKEN})",
    re.IGNORECASE,
)
ENTITY_PAIR_PROX_RE = re.compile(
    r"(?P<first>(?<![A-Za-z0-9])(?:P|B\d+|T\d+)(?![A-Za-z0-9]))\s*(?:与|和|跟)?\s*"
    r"(?:(?:is\s+|are\s+)?(?:near|close\s+to|adjacent\s+to|beside|next\s+to)|"
    r"靠近|相邻|在.{0,4}旁边|旁边|紧挨)\s*"
    r"(?P<second>(?<![A-Za-z0-9])(?:P|B\d+|T\d+)(?![A-Za-z0-9]))",
    re.IGNORECASE,
)
BOX_WATER_FWD_RE = re.compile(
    r"(?P<box>(?<![A-Za-z0-9_])B\d+(?![A-Za-z0-9_]))\s*(?:is\s+|在)?"
    r"(?:adjacent\s+to|beside|next\s+to|相邻|紧挨|紧贴|贴着|靠着|在.{0,4}旁边)\s*"
    r"(?:the\s+)?(?:water|水域|水边|水)",
    re.IGNORECASE,
)
BOX_WATER_REV_RE = re.compile(
    r"(?:the\s+)?(?:water|水域|水边|水)\s*(?:is\s+|在)?"
    r"(?:adjacent\s+to|beside|next\s+to|相邻|紧挨|紧贴|贴着|靠着|在.{0,4}旁边)\s*"
    r"(?P<box>(?<![A-Za-z0-9_])B\d+(?![A-Za-z0-9_]))",
    re.IGNORECASE,
)

PAREN_COORD_RE = re.compile(r"[\(（]\s*(\d{1,2})\s*[,，]\s*(\d{1,2})\s*[\)）]")
ENTITY_ANCHOR_RE = re.compile(_ENTITY_TOKEN, re.IGNORECASE)
ENTITY_FROM_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:P|B\d+|T\d+)(?![A-Za-z0-9])[^()\r\n]{0,32}(?:\bfrom\b|从)\s*$",
    re.IGNORECASE,
)
MOVEMENT_RE = re.compile(
    r"(?:→|->|\b(?:to|toward|towards|through|via|along|walk(?:s|ed|ing)?|move(?:s|d|ing)?|"
    r"go(?:es|ing)?|lead(?:s|ing)?|reach(?:es|ed|ing)?)\b|"
    r"到|向|往|朝向|通往|经过|沿着|走到|移动到|推向|推到|绕向|穿过)",
    re.IGNORECASE,
)
STATIC_BETWEEN_RE = re.compile(
    r"\b(?:is|are|lies?|sits?|located|positioned|placed)\b[^.!?]{0,40}"
    r"\b(?:near|close\s+to|beside|adjacent|next\s+to|to\s+the\s+left|to\s+the\s+right|above|below)\b"
    r"|(?:在|位于|靠近|旁边|相邻|左侧|右侧|上方|下方)",
    re.IGNORECASE,
)
TRAILING_CONJ_RE = re.compile(
    r"(?:\b(?:and|or|while|then|because|so|but)\b|(?:以及|并且|同时|然后|因此|但是|而且))$",
    re.IGNORECASE,
)
SENTENCE_PUNCT_RE = re.compile(r"[.!?。！？]")
INTERNAL_IDENT_RE = re.compile(r"\b(?:[a-z]+[A-Z][A-Za-z0-9]*|[a-z]+_[a-z][a-z0-9_]*)\b")

SINGLE_LEVEL_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"第[零〇一二三四五六七八九十百两\d]+关(?:卡)?该有的"), "这个版本现在呈现出的"),
    (re.compile(r"(?:后面|之后|后续|未来)(?:的)?关卡"), "后续版本"),
    (re.compile(r"(?:下一个|下一)关(?:卡)?"), "下一个版本"),
    (re.compile(r"(?:上一个|上一)关(?:卡)?"), "上一个版本"),
    (re.compile(r"前面(?:的)?关卡"), "之前的版本"),
    (re.compile(r"第[零〇一二三四五六七八九十百两\d]+关(?:卡)?"), "这个 Stage"),
    (
        re.compile(
            r"\b(?:the\s+)?(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
            r"\d+(?:st|nd|rd|th))\s+levels?\b",
            re.IGNORECASE,
        ),
        "this Stage",
    ),
    (re.compile(r"\b(?:the\s+)?(?:next|following)\s+level\b", re.IGNORECASE), "the next version"),
    (
        re.compile(r"\b(?:later|future|subsequent|following)\s+levels\b", re.IGNORECASE),
        "later versions",
    ),
    (re.compile(r"\b(?:the\s+)?previous\s+level\b", re.IGNORECASE), "the previous version"),
)

INTERNAL_TERM_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
    (re.compile(rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])"), zh, en)
    for tok, (zh, en) in {
        "gridDistance": ("曼哈顿距离", "Manhattan distance"),
        "_solver": ("确定性求解器", "deterministic solver"),
        "solutionSteps": ("求解步数", "solution steps"),
        "solutionPushes": ("推动次数", "pushes"),
        "minimumPushes": ("最少推动数", "minimum pushes"),
        "boxAlternations": ("箱子交替次数", "box alternations"),
        "searchedStates": ("搜索状态数", "searched states"),
        "tileAt": ("当前格子", "current tile"),
        "mapFacts": ("当前地图事实", "current map facts"),
        "mapFingerprint": ("当前地图", "current map"),
        "designContextPatch": ("设计进度", "design progress"),
        "coordinateLinks": ("路线端点", "route endpoints"),
        "stageSnapshot": ("当前地图快照", "current Stage snapshot"),
    }.items()
)

LEAKED_INTERNAL_TOKENS: frozenset[str] = frozenset(
    {
        "gridDistance",
        "_solver",
        "solutionSteps",
        "solutionPushes",
        "minimumPushes",
        "boxAlternations",
        "searchedStates",
        "tileAt",
        "mapFacts",
        "mapFingerprint",
        "designContextPatch",
        "coordinateLinks",
        "stageSnapshot",
        "entityBindings",
        "proposedRows",
        "revisionPlan",
        "executionBrief",
        "assistantMessage",
        "intentDecision",
    }
)

TILE_CLAIM_MAP: dict[str, str] = {
    "#": TILE_WALL,
    "墙": TILE_WALL,
    "墙体": TILE_WALL,
    "wall": TILE_WALL,
    ".": TILE_FLOOR,
    "地板": TILE_FLOOR,
    "空地": TILE_FLOOR,
    "floor": TILE_FLOOR,
    "ground": TILE_FLOOR,
    "@": TILE_WATER,
    "水": TILE_WATER,
    "水域": TILE_WATER,
    "water": TILE_WATER,
    "p": TILE_PLAYER,
    "玩家": TILE_PLAYER,
    "起点": TILE_PLAYER,
    "player": TILE_PLAYER,
    "start": TILE_PLAYER,
    "s": TILE_BOX,
    "箱": TILE_BOX,
    "箱子": TILE_BOX,
    "box": TILE_BOX,
    "crate": TILE_BOX,
    "t": TILE_TARGET,
    "目标": TILE_TARGET,
    "目标点": TILE_TARGET,
    "target": TILE_TARGET,
    "goal": TILE_TARGET,
}
