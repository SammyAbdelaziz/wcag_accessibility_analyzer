"""
Form Analysis Module
====================

Shared form analysis for WCAG 4.1.2 (Name, Role, Value - specifically labels)
across HTML, DOCX, and PPTX formats.

This module detects:
- Missing or incorrect form field labels (4.1.2)
- Unlabeled input fields
- Form fields without descriptive names
"""
from typing import List, Dict, Optional
from wcag.models import Finding, Severity, ConfidenceTier, EvidenceSource, CONFIDENCE_LABEL


class FormAnalyzer:
    """
    Analyzes form structure and labels for accessibility.
    
    Handles:
    - Form field labels (WCAG 4.1.2)
    - Input name/role/value (WCAG 4.1.2)
    - Form control detection
    """
    
    def validate_labels(
        self,
        inputs: List[Dict],
        context: str = "html"
    ) -> List[Finding]:
        """
        Validate that form inputs have associated labels (WCAG 4.1.2).
        
        For HTML: <label> element with for="input_id" or wrapping input.
        For DOCX: Content control with title/tag set.
        For PPTX: Text box with descriptive label nearby.
        
        Args:
            inputs: List of input dicts with 'id', 'type', 'label', 'location'
            context: "html", "docx", or "pptx"
        
        Returns:
            List of Finding objects for missing/incorrect labels
        """
        findings = []
        
        if not inputs:
            return findings
        
        for input_elem in inputs:
            input_id = input_elem.get('id', '')
            input_type = input_elem.get('type', 'text')
            label_text = input_elem.get('label', '').strip()
            location = input_elem.get('location', 'Form field')
            
            # Skip hidden inputs and submit buttons (they don't need labels)
            if input_type in ['hidden', 'submit', 'reset', 'button', 'file']:
                continue
            
            # Check for missing or empty label
            if not label_text:
                findings.append(Finding(
                    criterion_id="4.1.2",
                    criterion_name="Name, Role, Value",
                    wcag_level="A",
                    issue=(
                        f"{location} ({input_type} field) has no associated label."
                    ),
                    evidence=(
                        f"Input type: {input_type}. "
                        f"No label found via <label> (HTML) or title/tag (DOCX/PPTX)."
                    ),
                    severity=Severity.SERIOUS,
                    why_it_matters=(
                        "Screen reader users need labels to understand what information a form field expects. "
                        "Without a label, the field's purpose is unclear."
                    ),
                    remediation_steps=[
                        f"Add a <label> element with for=\"{input_id}\" (HTML).",
                        "Or: Wrap the <input> inside the <label> element.",
                        "Or: Add title/tag to content control (DOCX) or descriptive text (PPTX).",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                    confidence_rationale="Label presence checked directly in DOM.",
                    evidence_source=EvidenceSource.DOM_DIRECT,
                    location=location,
                    remediation_id="form_add_label",
                ))
            
            # Check for generic label text (e.g., "Enter text here")
            elif self._is_generic_label(label_text):
                findings.append(Finding(
                    criterion_id="4.1.2",
                    criterion_name="Name, Role, Value",
                    wcag_level="A",
                    issue=(
                        f"{location} has a generic label: '{label_text}'."
                    ),
                    evidence=(
                        f"Input type: {input_type}. "
                        f"Label text is not descriptive of field purpose."
                    ),
                    severity=Severity.MODERATE,
                    why_it_matters=(
                        "Generic labels ('Click here', 'Enter text') don't help users understand "
                        "what information is requested. Labels should describe the field purpose."
                    ),
                    remediation_steps=[
                        f"Change label from '{label_text}' to something descriptive.",
                        "Example: 'Email Address', 'Phone Number', 'Shipping Address'.",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                    confidence_rationale="Label text extracted directly from DOM.",
                    evidence_source=EvidenceSource.DOM_DIRECT,
                    location=location,
                    remediation_id="form_improve_label",
                ))
        
        return findings
    
    def _is_generic_label(self, text: str) -> bool:
        """Check if label text is generic or placeholder-like."""
        t = text.strip().lower()
        generic_patterns = [
            'enter', 'type here', 'input', 'text',
            'name', 'value', 'field', 'box',
            'click here', 'submit', 'send',
        ]
        return any(pattern in t for pattern in generic_patterns) and len(t) < 20
    
    def detect_form_controls(
        self,
        elements: List[Dict],
        context: str = "html"
    ) -> List[Dict]:
        """
        Extract form controls in a format-agnostic way.
        
        For HTML: <input>, <select>, <textarea>
        For DOCX: Content controls (w:sdt elements)
        For PPTX: Text boxes with form-like characteristics
        
        Args:
            elements: List of raw elements from the document
            context: "html", "docx", or "pptx"
        
        Returns:
            List of form control dicts with standardized properties
        """
        form_controls = []
        
        if context == "html":
            # Extract from HTML elements list
            for elem in elements:
                elem_type = elem.get('type', '').lower()
                if elem_type in ['input', 'select', 'textarea', 'button']:
                    form_controls.append({
                        'id': elem.get('id'),
                        'type': elem_type,
                        'label': elem.get('label', ''),
                        'required': elem.get('required', False),
                        'location': elem.get('location', ''),
                    })
        
        elif context == "docx":
            # Extract from DOCX content controls
            for elem in elements:
                if elem.get('is_content_control'):
                    form_controls.append({
                        'id': elem.get('id'),
                        'type': elem.get('control_type', 'text'),
                        'label': elem.get('title', ''),
                        'required': elem.get('required', False),
                        'location': elem.get('location', ''),
                    })
        
        elif context == "pptx":
            # Extract from PPTX shape text boxes
            for elem in elements:
                if elem.get('is_text_box'):
                    form_controls.append({
                        'id': elem.get('shape_id'),
                        'type': 'text_box',
                        'label': elem.get('text', ''),
                        'required': False,
                        'location': elem.get('location', ''),
                    })
        
        return form_controls
