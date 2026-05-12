"""
WCAG Common Modules
===================

Shared analysis modules used across HTML, DOCX, and PPTX analyzers.

Modules:
- utils.py          — Shared patterns, constants, and utility functions
- color_analysis.py — Color accessibility (1.4.1, 1.4.3, 1.4.11)
- semantic_flow.py  — Semantic structure and reading order (1.3.1, 1.3.2, 2.4.3)
- form_analysis.py  — Form fields and labels (4.1.2)
"""

from wcag.common.utils import (
    hex_luminance,
    contrast_ratio,
    validate_contrast,
    GENERIC_LINK_TEXT,
    URL_PATTERN,
    HEADING_PATTERNS,
    HEADING_STYLES,
    GENERIC_TITLE_PATTERNS,
    UNICODE_CHECKBOXES,
    is_generic_link_text,
    is_generic_title,
    matches_heading_pattern,
    contains_checkbox_character,
)

from wcag.common.color_analysis import ColorAnalyzer

from wcag.common.semantic_flow import SemanticFlowAnalyzer

from wcag.common.form_analysis import FormAnalyzer

__all__ = [
    # Utils
    'hex_luminance',
    'contrast_ratio',
    'validate_contrast',
    'GENERIC_LINK_TEXT',
    'URL_PATTERN',
    'HEADING_PATTERNS',
    'HEADING_STYLES',
    'GENERIC_TITLE_PATTERNS',
    'UNICODE_CHECKBOXES',
    'is_generic_link_text',
    'is_generic_title',
    'matches_heading_pattern',
    'contains_checkbox_character',
    # Analyzers
    'ColorAnalyzer',
    'SemanticFlowAnalyzer',
    'FormAnalyzer',
]
