"""
WCAG Analyzer — Data Models
All findings, fact sheets, and remediation results flow through these types.
Evidence source determines confidence tier automatically — no guessing.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
from enum import Enum


def _conformance_verdict(confirmed_findings: List["Finding"]) -> Dict[str, Any]:
    """Roll up confirmed findings into a single WCAG conformance verdict.

    A document fails WCAG 2.1 AA if it has any confirmed Level A or AA
    finding, regardless of severity. Severity informs how urgently to fix.
    """
    if not confirmed_findings:
        return {
            "level_reached": "AA",
            "verdict": "pass",
            "headline": "No confirmed WCAG 2.1 A/AA failures detected. Manual review still recommended.",
        }
    has_a = any(f.wcag_level == "A" for f in confirmed_findings)
    has_aa = any(f.wcag_level == "AA" for f in confirmed_findings)
    has_critical = any(f.severity == Severity.CRITICAL for f in confirmed_findings)
    if has_a:
        level_reached = "fails A"
    elif has_aa:
        level_reached = "A (fails AA)"
    else:
        level_reached = "AA"
    n = len(confirmed_findings)
    if has_critical:
        urgency = "Contains critical issues that block screen-reader users \u2014 fix before sharing."
    elif has_a:
        urgency = "Document fails WCAG 2.1 Level A. Address before publication."
    else:
        urgency = "Document fails WCAG 2.1 Level AA. Address for full AA compliance."
    return {
        "level_reached": level_reached,
        "verdict": "fail",
        "headline": f"{n} confirmed WCAG failure{'s' if n != 1 else ''}. {urgency}",
    }


class Severity(str, Enum):
    CRITICAL = "critical"
    SERIOUS = "serious"
    MODERATE = "moderate"
    MINOR = "minor"


class ConfidenceTier(str, Enum):
    CONFIRMED = "confirmed"   # Directly readable from XML — auditable
    POSSIBLE = "possible"     # Structurally plausible but requires manual verification


class EvidenceSource(str, Enum):
    XML_DIRECT = "xml_direct"           # Read directly from XML attribute/element value
    XML_INFERRED = "xml_inferred"       # Derived logically from XML structure (e.g. z-order)
    TEXT_CONTENT = "text_content"       # Observed in extracted text content
    THEME_RESOLVED = "theme_resolved"   # Color hex resolved through full theme chain
    DOM_DIRECT = "dom_direct"           # Read directly from HTML/DOM attribute or element value
    BROWSER_RENDERED = "browser_rendered"  # Read from a rendered browser page or computed styles
    RASTER_RENDERED = "raster_rendered"    # Read from a rendered raster/PDF page image


# Maps evidence source to display confidence label
CONFIDENCE_LABEL: Dict[EvidenceSource, str] = {
    EvidenceSource.XML_DIRECT: "high",
    EvidenceSource.XML_INFERRED: "medium",
    EvidenceSource.TEXT_CONTENT: "medium",
    EvidenceSource.THEME_RESOLVED: "high",
    EvidenceSource.DOM_DIRECT: "high",
    EvidenceSource.BROWSER_RENDERED: "high",
    EvidenceSource.RASTER_RENDERED: "medium",
}


@dataclass
class Finding:
    criterion_id: str           # e.g. "1.1.1"
    criterion_name: str         # e.g. "Non-text Content"
    wcag_level: str             # "A", "AA", "AAA"
    issue: str                  # Plain language description of what is wrong
    evidence: Optional[str]     # Specific quote or observation; None for possible findings
    severity: Severity
    why_it_matters: str
    remediation_steps: List[str]
    confidence_tier: ConfidenceTier
    confidence_label: str       # "high" / "medium" / "low" for display
    confidence_rationale: str   # One sentence explaining certainty level
    evidence_source: EvidenceSource
    location: Optional[str] = None          # e.g. "Slide 2 — Shape 'Picture 3'"
    remediation_id: Optional[str] = None    # Machine-readable ID for apply_remediations
    remediation_data: Optional[Dict] = None # Structured data needed to apply the fix
    advisory_payload: Optional[Dict[str, Any]] = None
    """Phase J — LLM advisory hook. Present only when the deterministic engine
    detects WHAT is wrong but cannot draft an ideal fix without natural-language
    judgment (e.g. authoring alt text, rewriting generic link text, choosing a
    descriptive heading). Downstream consumers (Copilot Studio agent, Power
    Automate flows) can route findings with `advisory_payload` to an LLM step.

    Schema:
      advisory_kind: str  — taxonomy: "alt_text" | "link_text" | "heading_text"
                                    | "title_text"
      target:        str  — stable in-document reference (image src, link index,
                            heading id, etc.) for deterministic round-tripping
      surface_text:  str  — the current bad value (empty when missing)
      context:       str  — surrounding text that grounds the LLM (<= 1000 chars)
      format_hint:   str  — "html" | "docx" | "pptx" | "xlsx" | "pdf"
    """

    @property
    def effort(self) -> str:
        """Estimate fix effort: 'trivial' | 'minutes' | 'review-needed'.
        
        Helps users batch quick wins:
        - trivial: < 1 min (fill a title, mark as decorative)
        - minutes: 1-5 min (reorder headings, add alt text to a few images)
        - review-needed: > 5 min or requires manual review (contrast ratios, layout restructuring)
        """
        # Map criteria + keywords to effort estimates
        criterion = self.criterion_id.split(';')[0].strip()  # Handle merged criteria
        issue_lower = (self.issue or "").lower()
        
        # Trivial fixes: structural placeholders, generic titles, empty titles
        if criterion in ("2.4.2", "4.1.2"):  # Page Title, Name/Role/Value
            if "empty" in issue_lower or "absent" in issue_lower or "generic" in issue_lower:
                return "trivial"
        if criterion == "3.1.1" and "not set" in issue_lower:  # Document language
            return "trivial"
        if criterion == "1.1.1":
            if "decorative" in issue_lower:
                return "trivial"
            # Single missing alt text = minutes; multiple = review-needed
            if "has no alt" in issue_lower or "empty alt" in issue_lower:
                if issue_lower.count("image") > 1 or issue_lower.count("chart") > 1:
                    return "review-needed"
                return "minutes"
        
        # Minutes: a few items, straightforward structural fix
        if criterion in ("1.3.1", "1.3.2"):  # Info and Relationships, Meaningful Sequence
            if "heading" in issue_lower:
                return "minutes" if "1" in issue_lower else "review-needed"
            if "list" in issue_lower:
                return "minutes"
            if "freeform" in issue_lower:
                return "minutes"
            if "title" in issue_lower and "order" in issue_lower:
                return "trivial"
        if criterion == "2.4.4" and "link" in issue_lower:  # Link Purpose
            if "1" in issue_lower.split()[0]:  # "1 link" vs "5 links"
                return "minutes"
            return "review-needed"
        if criterion == "1.4.1" and "color" in issue_lower:  # Use of Color
            return "review-needed"  # Requires design review
        
        # Review-needed: contrast, complex restructuring, manual validation
        if criterion == "1.4.3":  # Contrast
            return "review-needed"
        if "duplicate" in issue_lower:  # Duplicate titles
            return "minutes"
        
        # Default: review-needed for anything ambiguous
        return "review-needed"

    @property
    def finding_id(self) -> str:
        """Stable hash of (criterion + location + truncated evidence).

        Lets users reference a specific finding across re-runs. The hash is
        deterministic for the same observation but changes when evidence
        materially changes — which is the desired signal.
        """
        import hashlib
        key = f"{self.criterion_id}|{self.location or ''}|{(self.evidence or '')[:200]}"
        return hashlib.md5(key.encode('utf-8')).hexdigest()[:10]

    def to_dict(self) -> Dict:
        return {
            "finding_id": self.finding_id,
            "criterion_id": self.criterion_id,
            "criterion_name": self.criterion_name,
            "wcag_level": self.wcag_level,
            "issue": self.issue,
            "evidence": self.evidence,
            "severity": self.severity.value,
            "effort": self.effort,
            "why_it_matters": self.why_it_matters,
            "remediation_steps": self.remediation_steps,
            "confidence_tier": self.confidence_tier.value,
            "confidence_label": self.confidence_label,
            "confidence_rationale": self.confidence_rationale,
            "evidence_source": self.evidence_source.value,
            "location": self.location,
            "remediation_id": self.remediation_id,
            "remediation_data": self.remediation_data,
            "advisory_payload": self.advisory_payload,
        }


@dataclass
class ShapeInfo:
    """Extracted metadata for a single shape in a PPTX slide."""
    shape_id: int
    shape_name: str
    shape_type: str             # "image" | "text" | "title" | "body" | "chart" | "table" | "freeform_text" | "group"
    placeholder_type: Optional[str]   # "title" | "body" | "ctrTitle" | "subTitle" | None (= freeform)
    alt_text: Optional[str]           # None = descr attribute absent; "" = explicitly empty
    is_decorative: bool
    z_order: int                      # Index in spTree (0 = first = furthest back)
    slide_number: int
    text_content: Optional[str]
    list_levels: Optional[List[int]]  # Paragraph list-level values (0 = outermost)
    has_content: bool = True          # False for empty placeholders


@dataclass
class ParagraphInfo:
    """Extracted metadata for a single paragraph in a DOCX."""
    index: int
    style_name: str             # "Normal" | "Heading 1" | "Heading 2" | "List Paragraph" | etc.
    text: str
    list_level: Optional[int]
    is_empty: bool
    run_language: Optional[str] # Language tag on first run (e.g. "en-US")
    is_bold: bool = False
    font_size_pt: Optional[float] = None


@dataclass
class HyperlinkInfo:
    paragraph_index: int
    display_text: str
    url: Optional[str]


@dataclass
class ImageInfo:
    """An embedded image or drawing in a DOCX."""
    index: int
    alt_text: Optional[str]    # None = descr attribute absent
    alt_title: Optional[str]
    is_decorative: bool
    location_hint: str         # "Inline in paragraph N" | "Anchored near paragraph N"


@dataclass
class TableInfo:
    index: int
    has_header_row: bool        # True if <w:tblHeader> present on first row
    row_count: int
    col_count: int
    location_hint: str


@dataclass
class FactSheet:
    filename: str
    file_type: str              # "pptx" | "docx" | "html"

    # PPTX-specific
    slide_count: Optional[int] = None
    slides: Optional[List[List[ShapeInfo]]] = None

    # DOCX-specific
    paragraph_count: Optional[int] = None
    paragraphs: Optional[List[ParagraphInfo]] = None
    images: Optional[List[ImageInfo]] = None
    tables: Optional[List[TableInfo]] = None
    hyperlinks: Optional[List[HyperlinkInfo]] = None

    # Common
    document_title: Optional[str] = None
    document_language: Optional[str] = None
    has_unicode_checkboxes: bool = False

    # Findings (populated by rules engine)
    confirmed_findings: List[Finding] = field(default_factory=list)
    possible_findings: List[Finding] = field(default_factory=list)

    def to_dict(self) -> Dict:
        # Deterministic ordering: severity (worst first), then criterion, then
        # stable finding_id. Same input -> byte-identical output across runs,
        # which makes diffing and gold-set comparison reliable.
        _SEV_RANK = {
            Severity.CRITICAL: 0,
            Severity.SERIOUS: 1,
            Severity.MODERATE: 2,
            Severity.MINOR: 3,
        }
        _sort_key = lambda f: (_SEV_RANK.get(f.severity, 9), f.criterion_id, f.finding_id)
        confirmed = sorted(self.confirmed_findings, key=_sort_key)
        possible = sorted(self.possible_findings, key=_sort_key)
        
        # Deduplicate confirmed findings by location: group findings at the
        # same location and merge them into a single location-focused finding.
        confirmed_dedup = self._deduplicate_by_location(confirmed)
        
        return {
            "filename": self.filename,
            "file_type": self.file_type,
            "slide_count": self.slide_count,
            "paragraph_count": self.paragraph_count,
            "document_title": self.document_title,
            "document_language": self.document_language,
            "has_unicode_checkboxes": self.has_unicode_checkboxes,
            "structure": self._structure_overview(),
            "summary": {
                "confirmed_count": len(confirmed_dedup),
                "possible_count": len(possible),
                "by_severity": {
                    "critical": sum(1 for f in confirmed_dedup if f.severity == Severity.CRITICAL),
                    "serious": sum(1 for f in confirmed_dedup if f.severity == Severity.SERIOUS),
                    "moderate": sum(1 for f in confirmed_dedup if f.severity == Severity.MODERATE),
                    "minor": sum(1 for f in confirmed_dedup if f.severity == Severity.MINOR),
                },
                "conformance": _conformance_verdict(confirmed),  # Verdict based on pre-dedup original findings
            },
            "confirmed_findings": [f.to_dict() for f in confirmed_dedup],
            "possible_findings": [f.to_dict() for f in possible],
        }

    def _deduplicate_by_location(self, findings: List[Finding]) -> List[Finding]:
        """Group findings by location and merge those at the same location.
        
        When multiple findings target the same location (paragraph, slide, etc),
        merge them into a single grouped finding with:
        - Combined criterion IDs (e.g., "1.1.1, 1.3.1")
        - Most severe severity
        - Merged and deduplicated remediation steps
        - Reference to all original finding IDs in evidence
        - Preserved in `related_findings` field for transparency
        """
        from collections import defaultdict
        
        if not findings:
            return []
        
        # Group by location
        by_location = defaultdict(list)
        for f in findings:
            key = f.location or "(no location)"
            by_location[key].append(f)
        
        result = []
        for location, group in sorted(by_location.items()):
            if len(group) == 1:
                # No deduplication needed
                result.append(group[0])
                continue
            
            # Multiple findings at the same location: merge them
            primary = group[0]  # Use the first (highest severity due to pre-sorting)
            criteria = sorted(list(set(f.criterion_id for f in group)))
            criterion_names = sorted(list(set(f.criterion_name for f in group)))
            finding_ids = [f.finding_id for f in group]
            remediation_ids = sorted(list(set(f.remediation_id for f in group if f.remediation_id)))
            
            # Combine issues into a summary
            issues = [f.issue for f in group]
            combined_issue = (
                f"{len(group)} issues at {location}: "
                + "; ".join(f"{f.criterion_id}: {f.issue.split(':')[0]}" for f in group[:3])
                + (f" (and {len(group) - 3} more)" if len(group) > 3 else "")
            )
            
            # Merge remediation steps, removing duplicates while preserving order
            seen_steps = set()
            merged_steps = []
            for f in group:
                for step in f.remediation_steps:
                    step_normalized = step.strip().lower()
                    if step_normalized not in seen_steps:
                        seen_steps.add(step_normalized)
                        merged_steps.append(step)
            
            # Create merged finding
            merged = Finding(
                criterion_id="; ".join(criteria),
                criterion_name="; ".join(criterion_names),
                wcag_level=primary.wcag_level,  # Take from highest-severity finding
                issue=combined_issue,
                evidence=(
                    f"Multiple findings at this location: {', '.join(finding_ids)}. "
                    f"Issues: {'; '.join(issues)}"
                ),
                severity=group[0].severity,  # Already sorted by severity desc
                why_it_matters=(
                    "Multiple WCAG issues are present at this single location. "
                    "Fixing this location will address all listed criteria."
                ),
                remediation_steps=merged_steps,
                confidence_tier=primary.confidence_tier,
                confidence_label=primary.confidence_label,
                confidence_rationale=(
                    f"{len(group)} findings merged at this location. "
                    f"Most stringent: {primary.confidence_rationale}"
                ),
                evidence_source=primary.evidence_source,
                location=location,
                remediation_id=(
                    f"merged_{finding_ids[0]}"
                    if finding_ids else "merged"
                ),
                remediation_data={
                    "action": "fix_location_multiple_criteria",
                    "location": location,
                    "criteria": criteria,
                    "original_remediation_ids": remediation_ids,
                    "group_size": len(group),
                },
            )
            result.append(merged)
        
        # Re-sort by severity/criterion/id
        _SEV_RANK = {
            Severity.CRITICAL: 0,
            Severity.SERIOUS: 1,
            Severity.MODERATE: 2,
            Severity.MINOR: 3,
        }
        _sort_key = lambda f: (_SEV_RANK.get(f.severity, 9), f.criterion_id, f.finding_id)
        return sorted(result, key=_sort_key)

    def _structure_overview(self) -> Dict[str, Any]:
        """Compact, document-shape summary for triage.

        Lets a reviewer see at a glance: how big is the document, what's in
        it, and what its heading skeleton looks like \u2014 useful context
        alongside the findings list.
        """
        overview: Dict[str, Any] = {}
        if self.file_type == "docx":
            paragraphs = self.paragraphs or []
            images = self.images or []
            tables = self.tables or []
            hyperlinks = self.hyperlinks or []
            # Heading outline: list of (level, text) up to first 20 headings.
            # Style names appear in two common forms: "Heading 1" and "Heading1".
            # Title styles are treated as level 0 (document title).
            import re as _re
            outline = []
            for p in paragraphs:
                style = (p.style_name or "").strip()
                if not (style.startswith("Heading") or style == "Title"):
                    continue
                if style == "Title":
                    level = 0
                else:
                    m = _re.search(r"(\d+)", style)
                    level = int(m.group(1)) if m else 1
                text = (p.text or "").strip()
                if text:
                    outline.append({"level": level, "text": text[:80]})
                if len(outline) >= 20:
                    break
            overview = {
                "paragraph_count": len(paragraphs),
                "image_count": len(images),
                "table_count": len(tables),
                "hyperlink_count": len(hyperlinks),
                "heading_count": sum(
                    1 for p in paragraphs
                    if (p.style_name or "").startswith("Heading")
                    or (p.style_name or "") == "Title"
                ),
                "heading_outline": outline,
            }
        elif self.file_type == "pptx":
            slides = self.slides or []
            shape_total = sum(len(s) for s in slides)
            image_total = sum(1 for s in slides for sh in s if sh.shape_type == "image")
            table_total = sum(1 for s in slides for sh in s if sh.shape_type == "table")
            chart_total = sum(1 for s in slides for sh in s if sh.shape_type == "chart")
            # Slide titles (first 20).
            titles = []
            for i, s in enumerate(slides[:20], 1):
                title = next(
                    (sh.text_content for sh in s if sh.placeholder_type in ("title", "ctrTitle") and sh.text_content),
                    None,
                )
                titles.append({"slide": i, "title": (title or "").strip()[:80] or None})
            overview = {
                "slide_count": len(slides),
                "shape_total": shape_total,
                "image_count": image_total,
                "table_count": table_total,
                "chart_count": chart_total,
                "slide_titles": titles,
            }
        elif self.file_type == "html":
            paragraphs = self.paragraphs or []
            images = self.images or []
            tables = self.tables or []
            hyperlinks = self.hyperlinks or []
            outline = []
            for p in paragraphs:
                style = (p.style_name or "").strip().lower()
                if style not in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    continue
                level = int(style[1])
                text = (p.text or "").strip()
                if text:
                    outline.append({"level": level, "text": text[:80]})
                if len(outline) >= 20:
                    break
            overview = {
                "paragraph_count": len(paragraphs),
                "image_count": len(images),
                "table_count": len(tables),
                "hyperlink_count": len(hyperlinks),
                "heading_count": sum(
                    1 for p in paragraphs
                    if (p.style_name or "").strip().lower() in {"h1", "h2", "h3", "h4", "h5", "h6"}
                ),
                "heading_outline": outline,
            }
        return overview


@dataclass
class RemediationResult:
    success: bool
    applied_remediations: List[str] = field(default_factory=list)
    skipped_remediations: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    file_bytes: Optional[bytes] = None

    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "applied_remediations": self.applied_remediations,
            "skipped_remediations": self.skipped_remediations,
            "errors": self.errors,
            "file_size_bytes": len(self.file_bytes) if self.file_bytes else 0,
        }
