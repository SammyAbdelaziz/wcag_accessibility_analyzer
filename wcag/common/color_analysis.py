"""
Color Analysis Module
=====================

Shared color analysis for WCAG 1.4.1 (Color Used Alone), 1.4.3 (Contrast),
and 1.4.11 (Non-text Contrast) across HTML, DOCX, and PPTX formats.

This module detects:
- Color-only meaning (1.4.1)
- Insufficient contrast (1.4.3)
- Non-text color contrast (1.4.11)
"""
from typing import List, Dict, Optional, Tuple
from wcag.models import Finding, Severity, ConfidenceTier, EvidenceSource, CONFIDENCE_LABEL
from wcag.common.utils import hex_luminance, contrast_ratio, validate_contrast


class ColorAnalyzer:
    """
    Analyzes colors across document formats to detect accessibility issues.
    
    Handles:
    - Color-only meaning (WCAG 1.4.1)
    - Contrast issues (WCAG 1.4.3)
    - Non-text color issues (WCAG 1.4.11)
    """
    
    def detect_color_only_meaning(
        self,
        elements: List[Dict],
        context: str = "html"
    ) -> List[Finding]:
        """
        Detect reliance on color alone to convey meaning (WCAG 1.4.1).
        
        Color is used alone to convey meaning if:
        - Color change indicates status without text (e.g., "Required" field)
        - Color change indicates emphasis without pattern/underline/bold
        - No text label or symbol distinguishes the meaning
        
        Args:
            elements: List of elements with color/text info
                     Each dict should have: 'color', 'text', 'location', 'context_type'
            context: "html", "docx", or "pptx"
        
        Returns:
            List of Finding objects for color-only violations
        """
        findings = []
        
        # Group elements by color to find color-only indicators
        color_groups: Dict[str, List[Dict]] = {}
        for elem in elements:
            color = elem.get('color', '').upper()
            if color and color.startswith('#'):
                if color not in color_groups:
                    color_groups[color] = []
                color_groups[color].append(elem)
        
        # Check each color group for meaning
        for color, group in color_groups.items():
            if len(group) < 2:
                continue
            
            # Check if all elements with this color have similar meaning
            texts = [e.get('text', '').strip().lower() for e in group if e.get('text')]
            
            # Pattern: Color changes but no distinguishing text
            # Example: Red asterisk "*" (required) vs no asterisk (optional)
            if not texts or all(t == texts[0] for t in texts):
                # All same text or no text = color is the only differentiator
                location = group[0].get('location', 'Element')
                text_preview = group[0].get('text', '')[:30] or '(no text)'
                
                findings.append(Finding(
                    criterion_id="1.4.1",
                    criterion_name="Use of Color",
                    wcag_level="A",
                    issue=(
                        f"{location} relies on color ({color}) alone to convey meaning."
                    ),
                    evidence=(
                        f"Color {color} used on {len(group)} elements without additional text/icon."
                    ),
                    severity=Severity.SERIOUS,
                    why_it_matters=(
                        "Color-blind users cannot distinguish elements by color alone. "
                        "Meaning must be conveyed by text, patterns, or icons as well."
                    ),
                    remediation_steps=[
                        "Add text label to clarify meaning (e.g., '(Required)' for red fields).",
                        "Use symbols or icons in addition to color (e.g., asterisk for required).",
                        "Add pattern or texture to color-only indicators.",
                    ],
                    confidence_tier=ConfidenceTier.POSSIBLE,
                    confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                    confidence_rationale="Color and text extracted directly; meaning inferred.",
                    evidence_source=EvidenceSource.DOM_DIRECT,
                    location=location,
                    remediation_id="color_add_text_label",
                ))
        
        return findings
    
    def check_contrast(
        self,
        fg_hex: str,
        bg_hex: str,
        level: str = "AA",
        element_location: str = "",
        element_text: str = ""
    ) -> Optional[Finding]:
        """
        Check if foreground and background colors meet WCAG contrast requirements.
        
        Args:
            fg_hex: Foreground color in hex (e.g., "FF0000")
            bg_hex: Background color in hex (e.g., "FFFFFF")
            level: "A" (4.5:1) or "AA" (7:1) for normal text
            element_location: Where element is located (for reporting)
            element_text: Text content (for reporting)
        
        Returns:
            Finding object if contrast fails, None if passes
        """
        ratio, passes = validate_contrast(fg_hex, bg_hex, level)
        
        if passes:
            return None
        
        threshold = 7.0 if level == "AA" else 4.5
        location = element_location or "Text element"
        text_preview = (element_text or "")[:50]
        
        return Finding(
            criterion_id="1.4.3",
            criterion_name="Contrast (Minimum)",
            wcag_level="AA" if level == "AA" else "A",
            issue=(
                f"{location} has insufficient contrast. "
                f"Foreground ({fg_hex}) vs Background ({bg_hex}) = {ratio:.2f}:1 "
                f"(requires {threshold}:1)."
            ),
            evidence=(
                f"Measured contrast: {ratio:.2f}:1. Text: '{text_preview}'."
            ),
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Low contrast makes text difficult to read for people with low vision. "
                "WCAG Level AA requires 7:1 for normal text, 4.5:1 for large text."
            ),
            remediation_steps=[
                f"Increase contrast to at least {threshold}:1.",
                f"Lighten foreground color (make text lighter) or darken background.",
                f"Or: Use a different color combination entirely.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="Colors extracted directly from element; ratio calculated per WCAG formula.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=location,
            remediation_id="color_increase_contrast",
        )
    
    def extract_colors(
        self,
        element: Dict,
        context: str = "html"
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract foreground and background colors from an element.
        
        Format-specific logic:
        - HTML: CSS color, background-color properties
        - DOCX: w:color, shd properties, theme colors
        - PPTX: solidFill, schemeClr attributes, theme resolution
        
        Args:
            element: Element dict with color properties (format-dependent)
            context: "html", "docx", or "pptx"
        
        Returns:
            Tuple of (foreground_hex, background_hex), or (None, None) if not extractable
        """
        # Format-specific extraction logic handled by calling analyzer
        # This is a placeholder; actual implementation in each analyzer
        fg_color = element.get('fg_color') or element.get('color')
        bg_color = element.get('bg_color') or element.get('background_color')
        
        return (fg_color, bg_color)
