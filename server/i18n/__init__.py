"""Unified bilingual backend message catalog."""

from .messages import (
    DEFAULT_LANGUAGE,
    MESSAGES,
    SUPPORTED_LANGUAGES,
    format_dg_fallback_summary,
    format_map_claim_correction,
    format_protected_refusal,
    format_tradeoff_notice,
    format_verified_diff_summary,
    get_message,
    get_tile_label,
    normalize_language,
)

__all__ = [
    "DEFAULT_LANGUAGE",
    "MESSAGES",
    "SUPPORTED_LANGUAGES",
    "format_dg_fallback_summary",
    "format_map_claim_correction",
    "format_protected_refusal",
    "format_tradeoff_notice",
    "format_verified_diff_summary",
    "get_message",
    "get_tile_label",
    "normalize_language",
]
