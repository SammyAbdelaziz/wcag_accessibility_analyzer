"""
Shared Utilities for WCAG Analyzers
====================================

Provides common patterns, constants, and utility functions used across
HTML, DOCX, and PPTX analyzers to avoid duplication.
"""
import re
from typing import Optional, Tuple


# ============================================================================
# Color Analysis Utilities
# ============================================================================

def hex_luminance(hex_str: str) -> float:
    """
    Compute relative luminance from a 6-character hexadecimal color string.
    
    Uses the WCAG 2.0 relative luminance formula:
    L = 0.2126 * R + 0.7152 * G + 0.0722 * B
    where R, G, B are linearized from sRGB.
    
    Args:
        hex_str: Color in hex format (e.g., "FFFFFF", "#FFFFFF")
    
    Returns:
        Relative luminance value (0.0 to 1.0).
        Returns 0.5 as fallback for invalid colors.
    """
    def linearize(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    
    try:
        h = hex_str.lstrip('#').upper()
        if len(h) != 6:
            return 0.5
        r = int(h[0:2], 16) / 255
        g = int(h[2:4], 16) / 255
        b = int(h[4:6], 16) / 255
        return 0.2126 * linearize(r) + 0.7152 * linearize(g) + 0.0722 * linearize(b)
    except Exception:
        return 0.5


def contrast_ratio(lum_light: float, lum_dark: float) -> float:
    """
    Calculate WCAG contrast ratio between two luminance values.
    
    Formula: (L1 + 0.05) / (L2 + 0.05), where L1 >= L2
    
    Args:
        lum_light: Luminance of lighter color (0.0 to 1.0)
        lum_dark: Luminance of darker color (0.0 to 1.0)
    
    Returns:
        Contrast ratio (1:1 to 21:1)
    """
    if lum_light < lum_dark:
        lum_light, lum_dark = lum_dark, lum_light
    return (lum_light + 0.05) / (lum_dark + 0.05)


def validate_contrast(
    fg_hex: str,
    bg_hex: str,
    level: str = "AA"
) -> Tuple[float, bool]:
    """
    Check if color contrast meets WCAG standard.
    
    Args:
        fg_hex: Foreground color in hex
        bg_hex: Background color in hex
        level: "A" (4.5:1 normal, 3:1 large) or "AA" (7:1 normal, 4.5:1 large)
    
    Returns:
        Tuple of (ratio, passes_level)
    """
    fg_lum = hex_luminance(fg_hex)
    bg_lum = hex_luminance(bg_hex)
    ratio = contrast_ratio(fg_lum, bg_lum)
    
    threshold = 7.0 if level == "AA" else 4.5
    return (ratio, ratio >= threshold)


# ============================================================================
# Link Text Patterns
# ============================================================================

GENERIC_LINK_TEXT = re.compile(
    r'^(click here|click|here|this link|learn more|more|read more|'
    r'link|url|see here)$',
    re.IGNORECASE
)
"""Regex pattern for generic link text (WCAG 2.4.4)"""

URL_PATTERN = re.compile(r'^https?://', re.IGNORECASE)
"""Regex pattern for bare URLs as link text"""


def is_generic_link_text(text: str) -> bool:
    """
    Check if link text is generic (WCAG 2.4.4 violation).
    
    Args:
        text: Link display text
    
    Returns:
        True if text is generic or a bare URL
    """
    return bool(GENERIC_LINK_TEXT.match(text) or URL_PATTERN.match(text))


# ============================================================================
# Heading & Title Patterns
# ============================================================================

HEADING_PATTERNS = [
    re.compile(r'^[A-Z][^.!?]{3,50}$'),  # Short title-case sentence
    re.compile(r'^\d+\.\s+[A-Z]'),  # Numbered section (e.g., "1. Introduction")
    re.compile(r'^[A-Z]+\s*:$'),  # All-caps label with colon
]
"""Patterns suggesting heading intent in Normal-styled text (WCAG 1.3.1)"""

HEADING_STYLES = {
    'Heading 1', 'Heading 2', 'Heading 3', 'Heading 4', 'Heading 5', 'Heading 6',
    'heading 1', 'heading 2', 'heading 3', 'heading 4', 'heading 5', 'heading 6',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
}
"""Known heading style names across Office formats"""

GENERIC_TITLE_PATTERNS = [
    r"^slide\s*\d*$",
    r"^title\s*\d*$",
    r"^click to (add|edit) title$",
    r"^sample content(\s+to\s+test)?$",
    r"^lorem ipsum",
    r"^untitled\s*$",
    r"^\s*$",
]
"""Patterns for generic/placeholder slide titles (WCAG 2.4.2)"""


def is_generic_title(text: str) -> bool:
    """
    Check if text is a generic or placeholder title.
    
    Args:
        text: Title or heading text
    
    Returns:
        True if text matches generic patterns
    """
    t = text.strip().lower()
    return any(re.match(p, t) for p in GENERIC_TITLE_PATTERNS)


def matches_heading_pattern(text: str) -> bool:
    """
    Check if text suggests heading intent via pattern matching.
    
    Args:
        text: Paragraph text
    
    Returns:
        True if text matches heading-like patterns
    """
    return any(pattern.match(text) for pattern in HEADING_PATTERNS)


# ============================================================================
# Form & Interactive Element Patterns
# ============================================================================

UNICODE_CHECKBOXES = {'☐', '☑', '☒', '✓', '✗', '✘', '□', '■', '○', '●'}
"""Unicode checkbox characters indicating non-semantic list usage"""


def contains_checkbox_character(text: str) -> bool:
    """
    Check if text contains unicode checkbox symbols.
    
    Args:
        text: Text to check
    
    Returns:
        True if any checkbox character is present
    """
    return any(char in text for char in UNICODE_CHECKBOXES)


# ============================================================================
# Language & Mixed Language Detection
# ============================================================================

COMMON_LANG_CODES = {
    'en', 'es', 'fr', 'de', 'it', 'pt', 'ru', 'ja', 'zh', 'ko',
    'ar', 'hi', 'th', 'pl', 'nl', 'sv', 'no', 'da', 'fi',
}
"""Common ISO 639-1 language codes"""


def is_valid_lang_code(code: str) -> bool:
    """
    Check if a language code looks valid (basic check).
    
    Args:
        code: Language code (e.g., "en", "en-US")
    
    Returns:
        True if code format is valid
    """
    if not code:
        return False
    base = code.split('-')[0].lower()
    return len(base) == 2 and base.isalpha()


# ============================================================================
# Severity & Confidence Utilities
# ============================================================================

SEVERITY_ORDER = ['CRITICAL', 'SERIOUS', 'MODERATE', 'MINOR']
"""Order of severity levels from highest to lowest impact"""

CONFIDENCE_ORDER = ['CONFIRMED', 'POSSIBLE']
"""Order of confidence tiers from highest to lowest certainty"""


def severity_rank(severity_label: str) -> int:
    """Get numeric rank of severity (higher = more serious)."""
    return len(SEVERITY_ORDER) - SEVERITY_ORDER.index(severity_label.upper())


def confidence_rank(confidence_label: str) -> int:
    """Get numeric rank of confidence (higher = more certain)."""
    return len(CONFIDENCE_ORDER) - CONFIDENCE_ORDER.index(confidence_label.upper())
