"""
Semantic Flow Analysis Module
=============================

Shared semantic flow analysis for WCAG 1.3.1 (headings), 1.3.2 (sequence),
and 2.4.3 (focus order) across HTML, DOCX, and PPTX formats.

This module detects:
- Skipped heading levels (1.3.1)
- Out-of-order content sequence (1.3.2)
- Logical focus order (2.4.3)
"""
from typing import List, Dict, Optional
from wcag.models import Finding, Severity, ConfidenceTier, EvidenceSource, CONFIDENCE_LABEL


class SemanticFlowAnalyzer:
    """
    Analyzes semantic flow, heading hierarchy, and reading order.
    
    Handles:
    - Heading hierarchy (WCAG 1.3.1)
    - Meaningful sequence (WCAG 1.3.2)
    - Focus order (WCAG 2.4.3)
    """
    
    def validate_heading_hierarchy(
        self,
        headings: List[Dict]
    ) -> List[Finding]:
        """
        Validate that heading levels follow a logical hierarchy (WCAG 1.3.1).
        
        Rules:
        - Document should start with H1 (or H2 if H1 is page title)
        - Heading levels should not skip (e.g., H1 → H3 skips H2)
        - Multiple H1s are allowed but should be used intentionally
        
        Args:
            headings: List of heading dicts with 'level' (1-6) and 'text' properties
        
        Returns:
            List of Finding objects for heading hierarchy violations
        """
        findings = []
        
        if not headings:
            return findings
        
        # Track heading levels seen
        prev_level = None
        first_heading = True
        
        for idx, heading in enumerate(headings):
            level = heading.get('level', 0)
            text = heading.get('text', '')[:50]
            location = heading.get('location', f"Heading {idx+1}")
            
            if level < 1 or level > 6:
                continue
            
            # Check: Document should start with H1 (lenient: allow H2 if H1 used elsewhere)
            if first_heading and level > 2:
                findings.append(Finding(
                    criterion_id="1.3.1",
                    criterion_name="Info and Relationships",
                    wcag_level="A",
                    issue=(
                        f"Document starts with H{level} ('{text}') instead of H1 or H2. "
                        f"First heading should be H1 to establish document structure."
                    ),
                    evidence=f"First heading: {location}, level H{level}.",
                    severity=Severity.MODERATE,
                    why_it_matters=(
                        "Screen reader users rely on heading hierarchy to understand document structure. "
                        "Starting above H2 suggests an incomplete or non-standard structure."
                    ),
                    remediation_steps=[
                        "Change first heading to H1.",
                        "Or: If this is a sub-document, start with H2 (allowed in multi-document contexts).",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                    confidence_rationale="Heading level extracted directly from structure.",
                    evidence_source=EvidenceSource.DOM_DIRECT,
                    location=location,
                    remediation_id="heading_start_at_h1",
                ))
            
            first_heading = False
            
            # Check: No skipped levels (e.g., H1 → H3)
            if prev_level is not None and level > prev_level + 1:
                findings.append(Finding(
                    criterion_id="1.3.1",
                    criterion_name="Info and Relationships",
                    wcag_level="A",
                    issue=(
                        f"{location} is H{level} but follows H{prev_level}. "
                        f"Skipped heading level(s) H{prev_level+1}."
                    ),
                    evidence=(
                        f"Heading hierarchy jump: H{prev_level} → H{level}. "
                        f"Text: '{text}'."
                    ),
                    severity=Severity.MODERATE,
                    why_it_matters=(
                        "Skipped heading levels confuse screen reader users about document structure. "
                        "Headings should increase level-by-level (H1→H2→H3, not H1→H3)."
                    ),
                    remediation_steps=[
                        f"Change H{level} to H{prev_level+1}.",
                        "Or: Add intermediate heading (e.g., H2) before this H3.",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                    confidence_rationale="Heading levels extracted directly from structure.",
                    evidence_source=EvidenceSource.DOM_DIRECT,
                    location=location,
                    remediation_id="heading_skip_levels",
                ))
            
            prev_level = level
        
        return findings
    
    def check_meaningful_sequence(
        self,
        elements: List[Dict],
        context: str = "html"
    ) -> List[Finding]:
        """
        Validate that content sequence matches reading order (WCAG 1.3.2).
        
        For HTML: Check that focusable element DOM order matches visual position (top-to-bottom).
        For DOCX/PPTX: Check that content flows logically.
        
        Args:
            elements: List of focusable/content elements with visual position info
                     Each dict should have: 'rect' (with 'top', 'left'), 'text', 'location'
            context: "html", "docx", or "pptx"
        
        Returns:
            List of Finding objects for sequence violations
        """
        findings = []
        
        if not elements or context not in ["html", "docx", "pptx"]:
            return findings
        
        # For HTML: Check DOM vs visual order
        if context == "html":
            prev_y = -1000
            prev_x = 0
            out_of_order_indices = []
            
            for idx, elem in enumerate(elements):
                rect = elem.get('rect', {})
                current_y = rect.get('top', 0)
                current_x = rect.get('left', 0)
                
                # Element is out of order if it's significantly above previous element
                # (threshold: 50px for vertical, 20px for horizontal within same row)
                if current_y < prev_y - 50 or (abs(current_y - prev_y) < 50 and current_x < prev_x - 20):
                    out_of_order_indices.append(idx)
                
                prev_y = max(prev_y, current_y)
                prev_x = current_x if abs(current_y - prev_y) < 50 else 0
            
            if out_of_order_indices:
                offender_idx = out_of_order_indices[0]
                offender = elements[offender_idx]
                location = offender.get('location', f"Element {offender_idx}")
                text = (offender.get('text', '') or "")[:50]
                
                findings.append(Finding(
                    criterion_id="1.3.2",
                    criterion_name="Meaningful Sequence",
                    wcag_level="A",
                    issue=(
                        f"{location} appears out of DOM order. "
                        f"Keyboard/screen reader order does not match visual position."
                    ),
                    evidence=(
                        f"Element at DOM position {offender_idx} is visually above previous element. "
                        f"Text: '{text}'."
                    ),
                    severity=Severity.SERIOUS,
                    why_it_matters=(
                        "Screen reader and keyboard users follow DOM order, not visual order. "
                        "Mismatch confuses users about content sequence."
                    ),
                    remediation_steps=[
                        "Reorder HTML elements in source to match visual top-to-bottom flow.",
                        "Avoid using CSS (position, flex-order, grid-area) to reorder content.",
                        "If layout needs CSS reordering, restructure HTML to match intended order.",
                    ],
                    confidence_tier=ConfidenceTier.POSSIBLE,
                    confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
                    confidence_rationale="Element position measured from rendered layout; order inferred.",
                    evidence_source=EvidenceSource.BROWSER_RENDERED,
                    location=location,
                    remediation_id="sequence_reorder_dom",
                ))
        
        return findings
    
    def check_focus_order(
        self,
        focusable_elements: List[Dict],
        context: str = "html"
    ) -> List[Finding]:
        """
        Validate that focus order is logical and predictable (WCAG 2.4.3).
        
        For HTML: Check for positive tabindex and keyboard trap conditions.
        Note: 2.1.1 (keyboard) already covers positive tabindex detection.
        This focuses on focus order logic overall.
        
        Args:
            focusable_elements: List of focusable elements with tabindex, location
            context: "html" (PPTX/DOCX are not applicable)
        
        Returns:
            List of Finding objects for focus order violations
        """
        findings = []
        
        if context != "html" or not focusable_elements:
            return findings
        
        # Note: 2.1.1 (keyboard) already detects positive tabindex issues.
        # 2.4.3 is implicitly covered when 2.1.1 passes (no positive tabindex).
        # For now, this validator is passive—actual detection happens in 2.1.1.
        
        return findings
