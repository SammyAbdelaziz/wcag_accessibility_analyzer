"""WCAG 1.4.11 Non-text Contrast — Shared Evaluator.

Used across HTML / DOCX / PPTX / XLSX analyzers to evaluate whether UI
component or graphical object color pairs meet the 3:1 minimum contrast
threshold required for WCAG 1.4.11 (Level AA).

This module centralizes:
  * The 3:1 threshold constant
  * Pair-evaluation helpers that return (ratio, passes)
  * Hex normalization + parsing helpers tolerant of "RRGGBB", "#RRGGBB",
    "AARRGGBB", and 3-char shorthand "#RGB".

Format-specific analyzers extract candidate (foreground, background) pairs
and call ``evaluate_pair`` to decide whether to emit a 1.4.11 finding.
"""
from __future__ import annotations

from typing import Optional, Tuple

from wcag.common.utils import contrast_ratio, hex_luminance

# WCAG 2.1 SC 1.4.11 threshold for non-text components and graphical objects
MIN_NON_TEXT_CONTRAST: float = 3.0


def normalize_hex(value: Optional[str]) -> Optional[str]:
    """Return a canonical 6-char uppercase hex string, or None if invalid.

    Accepts: "FFFFFF", "#FFFFFF", "fff", "#FFF", "FFFFFFFF" (ARGB), "00FFFFFF".
    For ARGB the leading alpha byte is dropped.
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip().lstrip('#').upper()
    if len(raw) == 3:
        raw = ''.join(c * 2 for c in raw)
    elif len(raw) == 8:
        raw = raw[2:]  # strip alpha
    if len(raw) != 6:
        return None
    if any(ch not in '0123456789ABCDEF' for ch in raw):
        return None
    return raw


def evaluate_pair(fg: str, bg: str) -> Optional[Tuple[float, bool]]:
    """Evaluate a (foreground, background) hex pair against 1.4.11 threshold.

    Returns ``(ratio, passes)`` where ``passes`` is True when ratio >= 3.0.
    Returns ``None`` when either color cannot be parsed.
    """
    fg_n = normalize_hex(fg)
    bg_n = normalize_hex(bg)
    if not fg_n or not bg_n:
        return None
    ratio = contrast_ratio(hex_luminance(fg_n), hex_luminance(bg_n))
    return (ratio, ratio >= MIN_NON_TEXT_CONTRAST)


def passes(fg: str, bg: str) -> bool:
    """Convenience: True when pair passes 3:1, False when it fails or is unparsable."""
    result = evaluate_pair(fg, bg)
    return bool(result and result[1])
