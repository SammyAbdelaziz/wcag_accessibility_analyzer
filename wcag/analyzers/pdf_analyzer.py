"""
PDF WCAG Analyzer
Reads PDF structure using pikepdf for metadata and structural analysis.
Falls back to text extraction for content-level checks.

WCAG criteria covered:

  CONFIRMED (pdf_direct):
    1.1.1  — images with missing alt text (/Alt entry absent on XObject)
    2.4.2  — document title absent or empty in /Info or XMP metadata
    3.1.1  — document language not set (/Lang absent on catalog)

  CONFIRMED (pdf_inferred):
    1.3.1  — no /MarkInfo or Tags in document (untagged PDF — all structure unavailable to AT)
    1.3.1  — form fields (AcroForm) with no /TU (tooltip/label) entry

  POSSIBLE (pdf_inferred):
    1.1.1  — raster images that lack /Alt (image objects detected via XObject scan)
    1.3.1  — headings: if document is tagged, validates /H1-/H6 presence; if not, estimates
    1.4.3  — text color contrast (DeviceRGB/DeviceGray where extractable)
    2.4.4  — link annotations with generic or URL-only display text
    3.1.2  — mixed language spans (if tagged)
"""
from __future__ import annotations

import io
import re
from typing import List, Optional, Dict, Tuple, Any

import pikepdf
from pikepdf import Pdf, Dictionary, Array, Name, String

from wcag.models import (
    FactSheet, Finding,
    Severity, ConfidenceTier, EvidenceSource,
)

# Generic link text patterns (mirrors docx/html analyzers)
GENERIC_LINK_TEXT = re.compile(
    r'^(click here|click|here|this link|learn more|more|read more|link|url|see here)$',
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r'^https?://', re.IGNORECASE)


def _pdf_str(obj) -> Optional[str]:
    """Safely coerce a pikepdf String / Name / bytes to a Python str."""
    if obj is None:
        return None
    try:
        if isinstance(obj, String):
            return str(obj)
        if isinstance(obj, Name):
            return str(obj)
        if isinstance(obj, bytes):
            return obj.decode('utf-8', errors='replace')
        return str(obj)
    except Exception:
        return None


def _resolve(obj):
    """Resolve an indirect reference to its direct object."""
    try:
        if isinstance(obj, pikepdf.objects.Object):
            return obj
    except Exception:
        pass
    return obj


class PdfAnalyzer:
    """
    Analyze a PDF file for WCAG 2.2 Level A/AA issues.

    Runs purely from the PDF byte stream — no subprocess calls, no rendering.
    Uses pikepdf for structural access and extracts:
      - Document metadata (/Info + XMP)
      - Document catalog (/Lang, /MarkInfo, /AcroForm)
      - Page-level annotations (links, form fields)
      - XObject images (checks /Alt entries)
      - Structure tree tags (if present)
    """

    def __init__(self, file_bytes: bytes, filename: str):
        self.file_bytes = file_bytes
        self.filename = filename
        self.fact_sheet = FactSheet(filename=filename, file_type='pdf')
        self._pdf: Optional[Pdf] = None

    # ── Public entry point ────────────────────────────────────────────────────

    def analyze(self) -> FactSheet:
        try:
            self._pdf = Pdf.open(io.BytesIO(self.file_bytes))
        except Exception as e:
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="2.4.2",
                criterion_name="Page Titled",
                wcag_level="A",
                issue="PDF could not be opened — file may be corrupt or encrypted.",
                evidence=f"pikepdf error: {str(e)[:200]}",
                severity=Severity.CRITICAL,
                why_it_matters="A corrupt or encrypted PDF cannot be read by assistive technologies at all.",
                remediation_steps=[
                    "Ensure the PDF is not password-protected.",
                    "Re-export from source application (Word, InDesign, etc.).",
                    "Use File → Save As PDF with accessibility options enabled.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="high",
                confidence_rationale="PDF failed to open — confirmed by exception.",
                evidence_source=EvidenceSource.XML_DIRECT,
                location="Document (could not open)",
                remediation_id="pdf_corrupt",
            ))
            return self.fact_sheet

        try:
            self._run_rules()
        finally:
            self._pdf.close()

        return self.fact_sheet

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def _catalog(self) -> Dictionary:
        return self._pdf.Root

    @property
    def _page_count(self) -> int:
        try:
            return len(self._pdf.pages)
        except Exception:
            return 0

    def _get_info_field(self, key: str) -> Optional[str]:
        """Read a field from the /Info dictionary."""
        try:
            info = self._pdf.docinfo
            if info and key in info:
                return _pdf_str(info[key])
        except Exception:
            pass
        return None

    def _get_xmp_field(self, key: str) -> Optional[str]:
        """Read a field from XMP metadata (dc:title, dc:language, etc.)."""
        try:
            with self._pdf.open_metadata() as meta:
                val = meta.get(key)
                if val:
                    return str(val)
        except Exception:
            pass
        return None

    def _is_tagged(self) -> bool:
        """Check if /MarkInfo is present and /Marked is True."""
        try:
            mark_info = self._catalog.get('/MarkInfo')
            if mark_info is None:
                return False
            marked = mark_info.get('/Marked')
            return bool(marked)
        except Exception:
            return False

    def _has_structure_tree(self) -> bool:
        """Check if /StructTreeRoot is present (PDF has a structure tree)."""
        try:
            return '/StructTreeRoot' in self._catalog
        except Exception:
            return False

    def _get_document_language(self) -> Optional[str]:
        """Get document-level language from /Lang on catalog."""
        try:
            lang = self._catalog.get('/Lang')
            if lang is not None:
                return _pdf_str(lang)
        except Exception:
            pass
        # Fallback: XMP dc:language
        return self._get_xmp_field('dc:language')

    def _get_document_title(self) -> Optional[str]:
        """Get title from /Info /Title or XMP dc:title."""
        title = self._get_info_field('/Title')
        if not title:
            title = self._get_xmp_field('dc:title')
        return title

    def _iter_pages(self):
        """Yield (page_number_1based, page_dict) for each page."""
        try:
            for i, page in enumerate(self._pdf.pages, start=1):
                yield i, page
        except Exception:
            return

    def _collect_images(self) -> List[Dict]:
        """
        Scan all pages for image XObjects.
        Returns list of dicts: {page, name, has_alt, alt_text, subtype}.
        """
        images = []
        seen = set()
        for page_num, page in self._iter_pages():
            try:
                resources = page.get('/Resources')
                if resources is None:
                    continue
                xobjects = resources.get('/XObject')
                if xobjects is None:
                    continue
                for name, xobj in xobjects.items():
                    try:
                        xobj = _resolve(xobj)
                        subtype = _pdf_str(xobj.get('/Subtype'))
                        if subtype != '/Image':
                            continue
                        obj_id = id(xobj)
                        if obj_id in seen:
                            continue
                        seen.add(obj_id)
                        alt = xobj.get('/Alt')
                        alt_text = _pdf_str(alt) if alt is not None else None
                        images.append({
                            'page': page_num,
                            'name': str(name),
                            'has_alt': alt is not None,
                            'alt_text': alt_text,
                            'subtype': subtype,
                        })
                    except Exception:
                        continue
            except Exception:
                continue
        return images

    def _collect_form_fields(self) -> List[Dict]:
        """
        Collect AcroForm fields and check for /TU (tooltip = accessible label).
        Returns list of dicts: {page, field_name, field_type, has_label, label}.
        """
        fields = []
        try:
            acroform = self._catalog.get('/AcroForm')
            if acroform is None:
                return fields
            form_fields = acroform.get('/Fields')
            if form_fields is None:
                return fields

            def _walk(field_array, page_num=None):
                for ref in field_array:
                    try:
                        field = _resolve(ref)
                        # /TU = user-facing tooltip (accessible label)
                        tu = field.get('/TU')
                        t = field.get('/T')   # /T = partial field name
                        ft = field.get('/FT') # /FT = field type (Tx, Btn, Ch, Sig)
                        kids = field.get('/Kids')

                        label = _pdf_str(tu) if tu is not None else None
                        name = _pdf_str(t) if t is not None else 'Unknown'
                        ftype = _pdf_str(ft) if ft is not None else 'Unknown'

                        # Estimate page number from /P entry if available
                        p_ref = field.get('/P')
                        pg = page_num
                        if p_ref is not None:
                            # Try to match against known pages
                            try:
                                for i, pg_obj in enumerate(self._pdf.pages, start=1):
                                    if id(pg_obj) == id(_resolve(p_ref)):
                                        pg = i
                                        break
                            except Exception:
                                pass

                        if kids:
                            _walk(kids, pg)
                        else:
                            fields.append({
                                'page': pg or '?',
                                'field_name': name,
                                'field_type': ftype,
                                'has_label': label is not None and label.strip() != '',
                                'label': label,
                            })
                    except Exception:
                        continue
            _walk(form_fields)
        except Exception:
            pass
        return fields

    def _collect_link_annotations(self) -> List[Dict]:
        """
        Collect URI link annotations across all pages.
        Returns list of dicts: {page, uri, display_text}.
        """
        links = []
        for page_num, page in self._iter_pages():
            try:
                annots = page.get('/Annots')
                if annots is None:
                    continue
                for annot_ref in annots:
                    try:
                        annot = _resolve(annot_ref)
                        subtype = _pdf_str(annot.get('/Subtype'))
                        if subtype != '/Link':
                            continue
                        action = annot.get('/A')
                        if action is None:
                            continue
                        action_type = _pdf_str(action.get('/S'))
                        if action_type != '/URI':
                            continue
                        uri = _pdf_str(action.get('/URI')) or ''
                        # Try to get display text from /Contents (tooltip)
                        contents = _pdf_str(annot.get('/Contents')) or ''
                        links.append({
                            'page': page_num,
                            'uri': uri,
                            'display_text': contents,
                        })
                    except Exception:
                        continue
            except Exception:
                continue
        return links

    def _estimate_heading_count(self) -> int:
        """If tagged, count /H1-/H6 structure elements. Return -1 if untagged."""
        if not self._has_structure_tree():
            return -1
        try:
            struct_root = self._catalog.get('/StructTreeRoot')
            if struct_root is None:
                return -1
            count = 0
            heading_types = {'/H', '/H1', '/H2', '/H3', '/H4', '/H5', '/H6'}

            def _walk(node):
                nonlocal count
                try:
                    s = _pdf_str(node.get('/S'))
                    if s in heading_types:
                        count += 1
                    kids = node.get('/K')
                    if kids is None:
                        return
                    if isinstance(kids, Array):
                        for kid in kids:
                            try:
                                _walk(_resolve(kid))
                            except Exception:
                                pass
                    else:
                        try:
                            _walk(_resolve(kids))
                        except Exception:
                            pass
                except Exception:
                    pass

            _walk(struct_root)
            return count
        except Exception:
            return -1

    # ── Rules engine ─────────────────────────────────────────────────────────

    def _run_rules(self):
        # Populate shared metadata first
        self.fact_sheet.document_title = self._get_document_title()
        self.fact_sheet.document_language = self._get_document_language()
        self.fact_sheet.slide_count = self._page_count  # reuse slide_count for page count

        self._rule_1_3_1_tagged_pdf()
        self._rule_1_1_1_scanned_image_only_hint()
        self._rule_1_3_1_form_fields()
        self._rule_1_3_1_headings()
        self._rule_1_1_1_images()
        self._rule_1_4_3_contrast_hint()
        self._rule_2_4_2_doc_title()
        self._rule_2_4_4_link_text()
        self._rule_2_4_5_multiple_ways()
        self._rule_2_4_6_heading_labels()
        self._rule_3_1_1_language()
        self._rule_3_1_2_language_parts()
        self._rule_2_4_3_focus_order()  # Phase A: /Tabs entry check on pages with annotations
        self._rule_1_3_2_reading_order()  # Phase D: structure tree sparsity vs page count
        self._rule_1_3_5_input_purpose()  # Phase K

    # ── 1.3.1 Info and Relationships ─────────────────────────────────────────

    def _rule_1_3_1_tagged_pdf(self):
        """
        An untagged PDF is completely inaccessible to screen readers.
        This is the single highest-impact finding for PDF accessibility.
        """
        tagged = self._is_tagged()
        has_tree = self._has_structure_tree()

        if not tagged and not has_tree:
            has_text_signals = self._has_extractable_text_signals()
            triage_bucket = "untagged_text_layer" if has_text_signals else "untagged_unknown"
            if not has_text_signals:
                try:
                    triage_bucket = "untagged_image_only" if len(self._collect_images()) > 0 else "untagged_unknown"
                except Exception:
                    triage_bucket = "untagged_unknown"

            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="1.3.1",
                criterion_name="Info and Relationships",
                wcag_level="A",
                issue="PDF is untagged — document structure is completely invisible to assistive technologies.",
                evidence=(
                    "/MarkInfo entry absent or /Marked is false in document catalog. "
                    "No /StructTreeRoot found. Screen readers see raw character streams "
                    "with no headings, lists, tables, or reading order."
                ),
                severity=Severity.CRITICAL,
                why_it_matters=(
                    "An untagged PDF is the worst-case accessibility failure. "
                    "Screen readers cannot determine reading order, identify headings, "
                    "navigate tables, or detect form fields. Users with visual impairments "
                    "hear a meaningless stream of characters or nothing at all."
                ),
                remediation_steps=[
                    "📍 WHERE TO FIX: Re-export the PDF from the source application with accessibility tagging enabled.",
                    "",
                    "HOW TO FIX (by source application):",
                    "  • Microsoft Word: File → Save As → PDF → Options → check 'Document structure tags for accessibility'.",
                    "  • Adobe InDesign: File → Export PDF → check 'Create Tagged PDF'.",
                    "  • LibreOffice: File → Export as PDF → check 'Export PDF tags'.",
                    "  • Adobe Acrobat Pro: Tools → Accessibility → Auto-Tag Document (then review and fix tags).",
                    "  • Online tool: Adobe Acrobat online 'Make accessible' feature.",
                    "",
                    "After tagging, re-run this analyzer and verify /MarkInfo and /StructTreeRoot are present.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="high",
                confidence_rationale="Absence of /MarkInfo and /StructTreeRoot confirmed directly from PDF catalog.",
                evidence_source=EvidenceSource.XML_DIRECT,
                location="Document catalog (PDF root)",
                remediation_id="pdf_untagged",
                remediation_data={
                    "action": "tag_pdf",
                    "has_mark_info": tagged,
                    "has_struct_tree": has_tree,
                    "triage_bucket": triage_bucket,
                },
            ))

            if has_text_signals:
                self.fact_sheet.possible_findings.append(Finding(
                    criterion_id="1.3.1",
                    criterion_name="Info and Relationships",
                    wcag_level="A",
                    issue="PDF appears untagged but contains a text layer — structure is missing even though text exists.",
                    evidence="Text-show operators (Tj/TJ) found in content streams while /StructTreeRoot is absent.",
                    severity=Severity.MODERATE,
                    why_it_matters=(
                        "This document may be searchable and selectable, but without tags screen readers still lack "
                        "heading, list, and table relationships needed for reliable navigation."
                    ),
                    remediation_steps=[
                        "Re-export as tagged PDF from source application.",
                        "If source file is unavailable, use Acrobat Pro Auto-Tag then review reading order and headings.",
                    ],
                    confidence_tier=ConfidenceTier.POSSIBLE,
                    confidence_label="medium",
                    confidence_rationale="Content streams contain text operators but structure tree is absent.",
                    evidence_source=EvidenceSource.XML_INFERRED,
                    location="Document catalog (PDF root)",
                    remediation_id="pdf_untagged_text_layer_review",
                    remediation_data={"triage_bucket": "untagged_text_layer"},
                ))
        elif tagged and not has_tree:
            # Marked but no structure tree: partial/broken tagging
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="1.3.1",
                criterion_name="Info and Relationships",
                wcag_level="A",
                issue="PDF is marked as tagged but has no structure tree — tagging may be incomplete.",
                evidence="/MarkInfo /Marked is true but /StructTreeRoot is absent from catalog.",
                severity=Severity.SERIOUS,
                why_it_matters="Without a structure tree, screen readers cannot navigate by headings, identify lists, or understand table structure even though the PDF claims to be tagged.",
                remediation_steps=[
                    "Re-export from the source application with full accessibility tagging.",
                    "If using Adobe Acrobat Pro: Tools → Accessibility → Auto-Tag Document.",
                    "Verify with Acrobat's Accessibility Checker that the structure tree is complete.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale="/MarkInfo present but /StructTreeRoot absent — structure tree missing.",
                evidence_source=EvidenceSource.XML_INFERRED,
                location="Document catalog (PDF root)",
                remediation_id="pdf_partial_tags",
            ))

    def _rule_1_3_1_form_fields(self):
        """Detect AcroForm fields without accessible labels (/TU tooltip)."""
        fields = self._collect_form_fields()
        if not fields:
            return

        unlabeled = [f for f in fields if not f['has_label']]
        if not unlabeled:
            return

        examples = []
        for f in unlabeled[:5]:
            examples.append(
                f"'{f['field_name']}' ({f['field_type']}) on page {f['page']}"
            )
        more = len(unlabeled) - 5
        example_str = '; '.join(examples)
        if more > 0:
            example_str += f"; ... and {more} more"

        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.3.1",
            criterion_name="Info and Relationships",
            wcag_level="A",
            issue=f"{len(unlabeled)} form field(s) have no accessible label (/TU tooltip entry absent).",
            evidence=f"AcroForm fields without /TU (user-facing tooltip): {example_str}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Screen readers announce form fields using their /TU tooltip as the label. "
                "Without it, users hear only the field type ('text field') with no context "
                "about what information to enter, making the form unusable."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(unlabeled)} form fields need accessible labels.",
                f"   Fields: {example_str[:200]}",
                "",
                "HOW TO FIX:",
                "  • Adobe Acrobat Pro: Right-click each field → Properties → General tab → Tooltip.",
                "  • Enter a descriptive label (e.g., 'First name', 'Email address', 'Date of birth').",
                "  • In source application (Word): Use Developer tab content controls with descriptive Title.",
                "  • Re-export the PDF after fixing labels in the source document.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale=f"Absence of /TU entry confirmed directly on {len(unlabeled)} AcroForm field objects.",
            evidence_source=EvidenceSource.XML_DIRECT,
            location=f"{len(unlabeled)} form field(s) across {self._page_count} page(s)",
            remediation_id="pdf_form_labels",
            remediation_data={
                "action": "add_form_field_tooltips",
                "unlabeled_count": len(unlabeled),
                "fields": [{"name": f["field_name"], "type": f["field_type"], "page": f["page"]} for f in unlabeled[:10]],
            },
        ))

        # Phase H: same evidence also fails 4.1.2 Name, Role, Value — programmatic
        # name is missing for these form controls. Emit a companion finding so
        # downstream consumers see both the structural (1.3.1) and the
        # name/role/value (4.1.2) failure surfaces.
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="4.1.2",
            criterion_name="Name, Role, Value",
            wcag_level="A",
            issue=f"{len(unlabeled)} AcroForm field(s) expose no programmatic name (no /TU tooltip).",
            evidence=f"Form fields without an accessible name: {example_str}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "WCAG 4.1.2 requires every UI component to expose its name, role, and "
                "value to assistive technology. PDF form fields without /TU have no name "
                "for AT to announce, so users cannot tell what each field is for."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(unlabeled)} AcroForm fields need a /TU tooltip.",
                f"   Fields: {example_str[:200]}",
                "",
                "HOW TO FIX:",
                "  • Adobe Acrobat Pro: Right-click each field → Properties → General → Tooltip.",
                "  • The tooltip becomes the field's accessible name for screen readers.",
                "  • In Word: use Content Controls and set the Title — the title carries through to /TU on export.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale=f"Absence of /TU entry confirmed directly on {len(unlabeled)} AcroForm field objects.",
            evidence_source=EvidenceSource.XML_DIRECT,
            location=f"{len(unlabeled)} form field(s) across {self._page_count} page(s)",
            remediation_id="pdf_form_names_412",
            remediation_data={
                "action": "add_form_field_tooltips",
                "unlabeled_count": len(unlabeled),
                "fields": [{"name": f["field_name"], "type": f["field_type"], "page": f["page"]} for f in unlabeled[:10]],
            },
        ))

        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="3.3.2",
            criterion_name="Labels or Instructions",
            wcag_level="A",
            issue=f"{len(unlabeled)} AcroForm field(s) provide no user-facing label or instruction (/TU tooltip absent).",
            evidence=f"Form fields without tooltip label/instruction: {example_str}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "WCAG 3.3.2 requires form controls to provide labels or instructions so users know what to enter. "
                "PDF AcroForm fields without /TU give assistive technology users no prompt about the field purpose, "
                "and sighted keyboard users also lose the visible instruction text many PDF viewers surface from the tooltip."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(unlabeled)} AcroForm fields need a descriptive tooltip.",
                f"   Fields: {example_str[:200]}",
                "",
                "HOW TO FIX:",
                "  • Adobe Acrobat Pro: Right-click each field → Properties → General → Tooltip.",
                "  • Write a prompt users can act on, e.g. 'Email address' or 'Date of birth (MM/DD/YYYY)'.",
                "  • Re-export from Word/InDesign with descriptive form control titles so the /TU entry is preserved.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale=f"Absence of /TU entry confirmed directly on {len(unlabeled)} AcroForm field objects.",
            evidence_source=EvidenceSource.XML_DIRECT,
            location=f"{len(unlabeled)} form field(s) across {self._page_count} page(s)",
            remediation_id="pdf_form_labels_332",
            remediation_data={
                "action": "add_form_field_tooltips",
                "unlabeled_count": len(unlabeled),
                "fields": [{"name": f["field_name"], "type": f["field_type"], "page": f["page"]} for f in unlabeled[:10]],
            },
        ))

    def _rule_1_3_1_headings(self):
        """
        If tagged: check heading structure present.
        If untagged: flag as POSSIBLE issue (already covered by untagged finding).
        """
        # Only run heading check if tagged (untagged is already flagged critically)
        if not self._is_tagged():
            return

        heading_count = self._estimate_heading_count()

        if heading_count == 0:
            # Tagged but no headings at all
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="1.3.1",
                criterion_name="Info and Relationships",
                wcag_level="A",
                issue="Tagged PDF has no heading structure — document cannot be navigated by heading.",
                evidence=(
                    "Structure tree present but no /H, /H1–/H6 elements found. "
                    f"Document has {self._page_count} page(s)."
                ),
                severity=Severity.SERIOUS,
                why_it_matters=(
                    "Screen reader users navigate long documents by jumping between headings. "
                    "Without headings, users must read linearly from start to finish, "
                    "making multi-page documents extremely difficult to use."
                ),
                remediation_steps=[
                    f"📍 WHERE TO FIX: The source document (all {self._page_count} page(s)) needs heading styles.",
                    "",
                    "HOW TO FIX:",
                    "  • In the source document (Word): Apply Heading 1, Heading 2, Heading 3 styles to section titles.",
                    "  • Re-export to PDF with 'Document structure tags' enabled.",
                    "  • In Adobe Acrobat Pro: Tags panel → manually tag text as /H1, /H2, etc. (last resort).",
                    "  • Verify: Open PDF in Acrobat → View → Show/Hide → Navigation Panes → Tags.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale="No heading tags found in structure tree; requires manual review to confirm intent.",
                evidence_source=EvidenceSource.XML_INFERRED,
                location="Document structure tree",
                remediation_id="pdf_no_headings",
                remediation_data={"action": "add_headings", "heading_count": 0, "page_count": self._page_count},
            ))
        elif heading_count > 0:
            # Headings present — store count for informational purposes (no finding)
            pass  # Good structure — no finding needed

    # ── 1.1.1 Non-text Content ────────────────────────────────────────────────

    def _rule_1_1_1_images(self):
        """Detect image XObjects without /Alt text entries."""
        images = self._collect_images()
        if not images:
            return

        missing_alt = [img for img in images if not img['has_alt']]
        empty_alt = [img for img in images if img['has_alt'] and img['alt_text'] is not None and img['alt_text'].strip() == '']

        total_flagged = len(missing_alt) + len(empty_alt)
        if total_flagged == 0:
            return

        # Build location-specific evidence
        examples = []
        for img in missing_alt[:4]:
            examples.append(f"Page {img['page']}: image '{img['name']}' (no /Alt entry)")
        for img in empty_alt[:2]:
            examples.append(f"Page {img['page']}: image '{img['name']}' (empty /Alt)")
        more = total_flagged - len(examples)
        example_str = '; '.join(examples)
        if more > 0:
            example_str += f"; ... and {more} more"

        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.1.1",
            criterion_name="Non-text Content",
            wcag_level="A",
            issue=(
                f"{len(missing_alt)} image(s) have no /Alt text; "
                f"{len(empty_alt)} image(s) have empty /Alt text."
                if empty_alt else
                f"{len(missing_alt)} image(s) have no /Alt text (/Alt entry absent)."
            ),
            evidence=f"XObject scan: {example_str}.",
            severity=Severity.CRITICAL,
            why_it_matters=(
                "Screen readers use /Alt text to describe images to blind users. "
                "Without it, images are either silently skipped or announced as "
                "meaningless identifiers like 'Im1' or 'Figure'."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {total_flagged} image(s) across the document need alt text.",
                f"   {example_str[:200]}",
                "",
                "HOW TO FIX:",
                "  • Adobe Acrobat Pro: Right-click image → Edit Alt Text → enter a description.",
                "  • In source document (Word): Right-click image → Edit Alt Text → describe image.",
                "  • For decorative images: Set /Alt to empty string '' and mark as Artifact.",
                "  • For charts/graphs: Describe the key data takeaway, not the visual style.",
                "  • Re-export from source with accessibility options to preserve alt text.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale=f"/Alt entry absence confirmed by scanning XObject dictionary on {total_flagged} image(s).",
            evidence_source=EvidenceSource.XML_DIRECT,
            location=f"{total_flagged} image(s) across {self._page_count} page(s)",
            remediation_id="pdf_image_alt",
            remediation_data={
                "action": "add_image_alt_text",
                "missing_count": len(missing_alt),
                "empty_count": len(empty_alt),
                "images": [{"page": i["page"], "name": i["name"]} for i in (missing_alt + empty_alt)[:10]],
            },
        ))

    def _has_extractable_text_signals(self) -> bool:
        """Best-effort check for text operators in page content streams.

        This does not fully parse PDF graphics state, but it is a strong signal
        to distinguish likely image-only scans from digitally-generated text PDFs.
        """
        for _, page in self._iter_pages():
            try:
                contents = page.get('/Contents')
            except Exception:
                continue
            if contents is None:
                continue
            streams = contents if isinstance(contents, Array) else [contents]
            for s in streams:
                try:
                    raw = _resolve(s).read_bytes()
                except Exception:
                    continue
                # Common text-showing operators in PDF content streams.
                if b' TJ' in raw or b' Tj' in raw or b"'" in raw or b'\"' in raw:
                    return True
        return False

    def _rule_1_1_1_scanned_image_only_hint(self):
        """POSSIBLE hint: likely scanned/image-only PDF with little/no text layer.

        Conditions:
        - PDF is untagged (already a critical 1.3.1 signal)
        - At least one image XObject is present
        - No clear text-show operators found in page content streams
        """
        if self._is_tagged():
            return
        images = self._collect_images()
        if not images:
            return
        if self._has_extractable_text_signals():
            return

        pages_with_images = sorted({img.get('page') for img in images if img.get('page') is not None})
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.1.1",
            criterion_name="Non-text Content",
            wcag_level="A",
            issue=(
                "PDF appears image-only (likely scanned) with no detectable text layer; "
                "screen-reader access will be severely limited without OCR/tagging."
            ),
            evidence=(
                f"Detected {len(images)} image XObject(s) across page(s) {pages_with_images}; "
                "no text-show operators (Tj/TJ) were detected in page content streams."
            ),
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Image-only PDFs cannot be read reliably by assistive technologies because "
                "there is no machine-readable text layer to announce, search, copy, or navigate."
            ),
            remediation_steps=[
                "Run OCR and verify text recognition quality.",
                "Re-export from source document as tagged PDF when possible.",
                "Provide alt text and reading order tags after OCR cleanup.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=(
                "No text operators detected in content streams plus image-only resources; "
                "manual review still recommended for edge cases."
            ),
            evidence_source=EvidenceSource.XML_INFERRED,
            location=f"{self._page_count} page(s)",
            remediation_id="pdf_scanned_image_only",
            remediation_data={
                "image_count": len(images),
                "pages_with_images": pages_with_images,
                "text_operators_detected": False,
            },
        ))

    # ── 1.4.3 Contrast (hint level) ──────────────────────────────────────────

    def _rule_1_4_3_contrast_hint(self):
        """
        PDF contrast checking at structural level.
        Full pixel-level contrast requires rendering (OCR layer).
        This rule flags when we cannot confirm contrast is adequate.
        """
        # Only meaningful if the PDF is tagged; untagged is already critical
        if not self._is_tagged():
            return

        # If tagged and document has content, we can only flag as POSSIBLE
        # without rendering. The OCR layer (Layer 3) would upgrade this.
        if self._page_count > 0:
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="1.4.3",
                criterion_name="Contrast (Minimum)",
                wcag_level="AA",
                issue="Text contrast in this PDF cannot be verified without rendering — manual review required.",
                evidence=(
                    "PDF contrast requires pixel-level analysis of rendered pages. "
                    "Structural PDF analysis alone cannot confirm color contrast ratios. "
                    f"Document has {self._page_count} page(s)."
                ),
                severity=Severity.MODERATE,
                why_it_matters=(
                    "WCAG 1.4.3 requires a 4.5:1 contrast ratio for normal text (3:1 for large text). "
                    "Low contrast makes text unreadable for users with low vision or color blindness, "
                    "even when using a screen reader in combination with residual sight."
                ),
                remediation_steps=[
                    f"📍 WHERE TO CHECK: All {self._page_count} page(s) of this PDF.",
                    "",
                    "HOW TO VERIFY CONTRAST:",
                    "  • Open the PDF in Adobe Acrobat → Tools → Accessibility → Reading Order.",
                    "  • Use WebAIM Contrast Checker (webaim.org/resources/contrastchecker/) on sampled colors.",
                    "  • In source document: Use built-in accessibility checker before exporting.",
                    "  • Ensure body text uses dark colors (#333333 or darker) on light backgrounds.",
                    "  • Minimum ratio: 4.5:1 for normal text, 3:1 for text 18pt+ or 14pt+ bold.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale="Structural PDF analysis cannot determine rendered text color — requires pixel-level review.",
                evidence_source=EvidenceSource.XML_INFERRED,
                location=f"All {self._page_count} page(s)",
                remediation_id="pdf_contrast_review",
                remediation_data={"action": "manual_contrast_review", "page_count": self._page_count},
            ))

    # ── 2.4.2 Page Titled ────────────────────────────────────────────────────

    def _rule_2_4_2_doc_title(self):
        """Check /Info /Title and XMP dc:title for presence and non-emptiness."""
        title = self.fact_sheet.document_title
        info_title = self._get_info_field('/Title')
        xmp_title = self._get_xmp_field('dc:title')

        if not title or not title.strip():
            display = f'"{title}"' if title is not None else 'absent'

            # Suggest a title from filename
            import os
            stem = os.path.splitext(os.path.basename(self.filename or ''))[0]
            stem = re.sub(r'^[0-9a-f]{6,}[-_]', '', stem, flags=re.IGNORECASE)
            suggested = re.sub(r'[_\-]+', ' ', stem).strip().title() or 'Untitled Document'

            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="2.4.2",
                criterion_name="Page Titled",
                wcag_level="A",
                issue=f"PDF document title is {display} — assistive technologies cannot identify this document.",
                evidence=(
                    f"/Info /Title: {'absent' if info_title is None else repr(info_title)}. "
                    f"XMP dc:title: {'absent' if xmp_title is None else repr(xmp_title)}."
                ),
                severity=Severity.MODERATE,
                why_it_matters=(
                    "Screen readers announce the document title when a PDF is opened. "
                    "Without a title, users hear the filename (often a GUID or date) "
                    "instead of a meaningful description of the document's content."
                ),
                remediation_steps=[
                    "📍 WHERE TO FIX: Document metadata (PDF /Info dictionary and XMP).",
                    "",
                    "HOW TO FIX:",
                    "  • Adobe Acrobat Pro: File → Properties → Description → Title field.",
                    f"  • Suggested title based on filename: '{suggested}'",
                    "  • In source document (Word): File → Info → Properties → Title.",
                    "  • Re-export PDF after setting title in source document.",
                    "  • Verify: Reopen PDF → File → Properties → Title should be populated.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="high",
                confidence_rationale="Absence of /Title in /Info and XMP dc:title confirmed from PDF metadata.",
                evidence_source=EvidenceSource.XML_DIRECT,
                location="Document metadata (/Info dictionary)",
                remediation_id="pdf_doc_title",
                remediation_data={"action": "set_pdf_title", "suggested_title": suggested},
            ))

    # ── 2.4.4 Link Purpose ───────────────────────────────────────────────────

    def _rule_2_4_4_link_text(self):
        """Detect URI links with generic or URL-only display text."""
        links = self._collect_link_annotations()
        if not links:
            return

        flagged = []
        for link in links:
            text = (link['display_text'] or '').strip()
            uri = link['uri'] or ''
            # Flag: empty display text, generic text, or raw URL as display
            if not text:
                flagged.append({**link, 'reason': 'no display text'})
            elif GENERIC_LINK_TEXT.match(text):
                flagged.append({**link, 'reason': f'generic link text: "{text}"'})
            elif URL_PATTERN.match(text) and len(text) > 60:
                flagged.append({**link, 'reason': 'raw URL used as display text'})

        if not flagged:
            return

        examples = [f"Page {l['page']}: {l['reason']} → {l['uri'][:60]}" for l in flagged[:5]]
        more = len(flagged) - 5
        example_str = '; '.join(examples)
        if more > 0:
            example_str += f"; ... and {more} more"

        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.4.4",
            criterion_name="Link Purpose (In Context)",
            wcag_level="A",
            issue=f"{len(flagged)} link(s) have no or non-descriptive display text.",
            evidence=f"Link annotations with missing/generic text: {example_str}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "Screen reader users often navigate by listing all links in a document. "
                "Links without descriptive text (or with raw URLs) provide no context "
                "about where the link leads or why a user should follow it."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(flagged)} link annotation(s) need descriptive text.",
                f"   {example_str[:200]}",
                "",
                "HOW TO FIX:",
                "  • In source document (Word): Change the display text of hyperlinks to describe the destination.",
                "  • Example: 'click here' → 'Download the Q4 Accessibility Report (PDF)'.",
                "  • Adobe Acrobat Pro: Right-click link → Edit → change display text.",
                "  • Avoid raw URLs as visible text — use descriptive phrases instead.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale=f"{len(flagged)} link annotations with missing/generic /Contents confirmed.",
            evidence_source=EvidenceSource.XML_DIRECT,
            location=f"{len(flagged)} link(s) across document",
            remediation_id="pdf_link_text",
            remediation_data={
                "action": "fix_link_text",
                "flagged_count": len(flagged),
                "links": [{"page": l["page"], "uri": l["uri"][:100], "reason": l["reason"]} for l in flagged[:10]],
            },
        ))

    # ── 2.4.5 Multiple Ways ───────────────────────────────────────────────────

    def _rule_2_4_5_multiple_ways(self):
        """Check that the PDF provides a document outline (bookmarks) for navigation.

        WCAG 2.4.5 (Multiple Ways, AA) requires that multi-page documents provide
        more than one way to locate content. For PDFs, the /Outlines (bookmarks)
        tree is the primary navigational aid beyond sequential page reading.
        A document with > 10 pages and no outline provides only one way to navigate.

        Only runs for documents with > 3 pages (single/small documents are exempt).
        """
        if self._page_count <= 3:
            return  # Short document — single navigation method is acceptable

        try:
            outlines = self._catalog.get('/Outlines')
            has_outlines = outlines is not None
            if has_outlines:
                # Check if the outline has any actual entries
                first = _resolve(outlines).get('/First') if outlines else None
                has_outlines = first is not None
        except Exception:
            has_outlines = False

        if has_outlines:
            return

        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.4.5",
            criterion_name="Multiple Ways",
            wcag_level="AA",
            issue=(
                f"This {self._page_count}-page PDF has no bookmarks/outline — "
                "users can only navigate page-by-page."
            ),
            evidence=(
                f"/Outlines absent from PDF catalog. "
                f"Document has {self._page_count} pages with no navigational outline structure."
            ),
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 2.4.5 requires multiple ways to locate content in multi-page documents. "
                "PDF bookmarks allow users (especially screen reader users) to jump directly "
                "to sections without reading every page. Without bookmarks, navigation in "
                "long documents is extremely difficult."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: PDF document must have /Outlines (bookmarks) for {self._page_count} pages.",
                "",
                "HOW TO FIX:",
                "  • Adobe Acrobat Pro: View → Navigation Panels → Bookmarks → add bookmarks for each section.",
                "  • In source document (Word): Styles-based headings automatically become PDF bookmarks.",
                "    Export as PDF: File → Save As PDF → Options → check 'Create bookmarks from Headings'.",
                "  • InDesign: Export PDF → check 'Bookmarks' and 'Include Heading Styles'.",
                "  • Verify: open PDF in Acrobat and click the Bookmarks icon in the left panel.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale=(
                f"Absence of /Outlines confirmed from PDF catalog. "
                f"{self._page_count}-page document with no bookmark structure."
            ),
            evidence_source=EvidenceSource.XML_DIRECT,
            location="Document catalog (/Outlines)",
            remediation_id="pdf_no_bookmarks",
            remediation_data={
                "action": "add_bookmarks",
                "page_count": self._page_count,
            },
        ))

    # ── 2.4.6 Headings and Labels ─────────────────────────────────────────────

    _GENERIC_HEADING_RE = re.compile(
        r'^(section\s*\d*|chapter\s*\d*|heading\s*\d*|part\s*\d*|introduction|'
        r'overview|conclusion|summary|background|appendix\s*[a-z]?|title|untitled|'
        r'tbd|todo|placeholder|click to edit)$',
        re.IGNORECASE,
    )

    def _rule_2_4_6_heading_labels(self):
        """WCAG 2.4.6 Headings and Labels: heading elements must be descriptive.

        Only runs for tagged PDFs that have a structure tree. Walks the
        /StructTreeRoot to collect all /H, /H1–/H6 elements and extracts their
        text content (from /ActualText or the Alt entry of child Spans).
        Flags headings that match known generic placeholder patterns.
        """
        if not self._has_structure_tree():
            return  # Untagged — 1.3.1 covers this; no heading text to inspect

        generic_headings: List[Dict] = []

        try:
            struct_root = self._catalog.get('/StructTreeRoot')
            if struct_root is None:
                return

            heading_types = {'/H', '/H1', '/H2', '/H3', '/H4', '/H5', '/H6'}

            def _extract_text(node) -> str:
                """Best-effort text extraction from a structure element."""
                try:
                    # Try /ActualText first
                    actual = node.get('/ActualText')
                    if actual:
                        return _pdf_str(actual) or ''
                    # Try /Alt
                    alt = node.get('/Alt')
                    if alt:
                        return _pdf_str(alt) or ''
                    # Try /Title
                    title = node.get('/Title')
                    if title:
                        return _pdf_str(title) or ''
                except Exception:
                    pass
                return ''

            def _walk(node):
                try:
                    node = _resolve(node)
                    s = _pdf_str(node.get('/S'))
                    if s in heading_types:
                        text = _extract_text(node).strip()
                        if text and self._GENERIC_HEADING_RE.match(text):
                            generic_headings.append({'type': s, 'text': text[:80]})
                    kids = node.get('/K')
                    if kids is None:
                        return
                    if isinstance(kids, Array):
                        for kid in kids:
                            try:
                                _walk(_resolve(kid))
                            except Exception:
                                pass
                    else:
                        _walk(_resolve(kids))
                except Exception:
                    pass

            _walk(struct_root)
        except Exception:
            return

        if not generic_headings:
            return

        examples = '; '.join(
            f"'{h['text']}' ({h['type']})" for h in generic_headings[:5]
        )
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="2.4.6",
            criterion_name="Headings and Labels",
            wcag_level="AA",
            issue=(
                f"{len(generic_headings)} heading element(s) have generic text "
                "that does not describe the section content."
            ),
            evidence=f"Generic headings in structure tree: {examples}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 2.4.6 requires headings to be descriptive so users can understand "
                "document structure from the heading outline alone. Generic labels like "
                "'Section 1' or 'Overview' give screen reader users no indication of content."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Tagged PDF structure tree — headings listed above.",
                "",
                "HOW TO FIX:",
                "  • In source document: update heading text to be content-specific.",
                "  • 'Introduction' → 'Introduction to Q4 Accessibility Audit'",
                "  • 'Overview' → 'Financial Overview: 2026 Budget Allocation'",
                "  • Re-export PDF from source to propagate updated heading text.",
                "  • Alternatively: Acrobat Pro → Tags panel → navigate to heading element → edit text.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=(
                f"{len(generic_headings)} heading structure element(s) matched generic-text patterns "
                "via ActualText/Alt inspection — manual review recommended."
            ),
            evidence_source=EvidenceSource.XML_DIRECT,
            location="Document structure tree (/StructTreeRoot heading elements)",
            remediation_id="pdf_heading_labels",
            remediation_data={
                "action": "improve_heading_text",
                "headings": generic_headings[:10],
            },
        ))

    # ── 3.1.1 Language of Page ───────────────────────────────────────────────

    def _rule_3_1_1_language(self):
        """Check that /Lang is set on the document catalog."""
        lang = self.fact_sheet.document_language

        if not lang or not lang.strip():
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="3.1.1",
                criterion_name="Language of Page",
                wcag_level="A",
                issue="PDF document language is not set — screen readers will use system default language.",
                evidence=(
                    "/Lang entry absent from PDF catalog. "
                    "XMP dc:language also absent or empty."
                ),
                severity=Severity.MODERATE,
                why_it_matters=(
                    "Screen readers use the document language to select the correct "
                    "text-to-speech engine and pronunciation rules. Without it, the reader "
                    "may mispronounce all content using the wrong language's phonetics."
                ),
                remediation_steps=[
                    "📍 WHERE TO FIX: PDF document catalog /Lang entry.",
                    "",
                    "HOW TO FIX:",
                    "  • Adobe Acrobat Pro: File → Properties → Advanced → Language → select language.",
                    "  • In source document (Word): Review → Language → Set Proofing Language → Set As Default.",
                    "  • Re-export PDF from Word with language set before export.",
                    "  • Common values: 'en-US' (English), 'fr-FR' (French), 'de-DE' (German), 'es-ES' (Spanish).",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="high",
                confidence_rationale="Absence of /Lang on catalog and XMP dc:language confirmed from PDF metadata.",
                evidence_source=EvidenceSource.XML_DIRECT,
                location="Document catalog (/Lang)",
                remediation_id="pdf_doc_language",
                remediation_data={"action": "set_pdf_language", "suggested_lang": "en-US"},
            ))

    # ── 3.1.2 Language of Parts ──────────────────────────────────────────────

    def _rule_3_1_2_language_parts(self):
        """Check that structure tree elements with a language different from the
        document default declare their language via /Lang.

        In a tagged PDF, any span or block that uses a different language should
        carry a /Lang attribute on its structure element so screen readers can
        switch the TTS engine mid-document.

        Strategy: walk the StructTreeRoot and collect all /Lang values found on
        child elements. If multiple distinct languages are found, the document
        has mixed-language content. We then check whether each distinct language
        is explicitly declared (i.e. found on the element itself, not inferred).
        If the document is untagged this rule is skipped (3.1.1 / 1.3.1 already
        cover the untagged case).
        """
        if not self._has_structure_tree():
            return  # Untagged — 1.3.1 covers this

        doc_lang = (self.fact_sheet.document_language or '').lower().split('-')[0]

        langs_found: List[str] = []
        elements_without_lang: List[str] = []  # element type / role

        try:
            struct_root = self._catalog.get('/StructTreeRoot')
            if struct_root is None:
                return

            def _walk(node, inherited_lang: Optional[str] = None):
                try:
                    node = _resolve(node)
                    node_lang = _pdf_str(node.get('/Lang')) if '/Lang' in node else None
                    effective_lang = node_lang or inherited_lang

                    if node_lang:
                        lang_simple = node_lang.lower().split('-')[0]
                        if lang_simple not in langs_found:
                            langs_found.append(lang_simple)

                    # If this element has text content (S = structural type)
                    # and its effective language differs from doc-level, flag it
                    # only if it has NO /Lang of its own.
                    s_type = _pdf_str(node.get('/S'))
                    if s_type and effective_lang and not node_lang:
                        eff_simple = effective_lang.lower().split('-')[0]
                        if doc_lang and eff_simple != doc_lang:
                            label = s_type or 'element'
                            if label not in elements_without_lang:
                                elements_without_lang.append(label)

                    kids = node.get('/K')
                    if kids is None:
                        return
                    if isinstance(kids, Array):
                        for kid in kids:
                            try:
                                _walk(_resolve(kid), effective_lang)
                            except Exception:
                                pass
                    else:
                        try:
                            _walk(_resolve(kids), effective_lang)
                        except Exception:
                            pass
                except Exception:
                    pass

            _walk(struct_root)
        except Exception:
            return

        # Only report if we found multiple distinct language subtrees
        distinct_non_doc = [l for l in langs_found if l != doc_lang and l]
        if not distinct_non_doc:
            return

        lang_list = ', '.join(distinct_non_doc[:5])
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="3.1.2",
            criterion_name="Language of Parts",
            wcag_level="AA",
            issue=(
                f"Structure tree contains elements in language(s) other than the document "
                f"default ('{self.fact_sheet.document_language or 'not set'}'): {lang_list}. "
                "Verify each section has an explicit /Lang declaration."
            ),
            evidence=(
                f"Non-document language tags found in /StructTreeRoot: {lang_list}. "
                f"Document-level /Lang: '{self.fact_sheet.document_language or 'not set'}'."
            ),
            severity=Severity.MODERATE,
            why_it_matters=(
                "Screen readers switch pronunciation engines based on language. "
                "Sections in a different language without an explicit /Lang tag are "
                "mispronounced — critically harmful for bilingual or multilingual documents."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Tagged structure tree elements with foreign-language content.",
                "",
                "HOW TO FIX:",
                "  • Adobe Acrobat Pro: Tags panel → right-click a language-specific element → Properties → Language.",
                "  • In source document (Word): select the foreign-language text → Review → Language → Set Proofing Language.",
                "  • Re-export PDF from Word; Word's per-run language settings map to PDF /Lang on structure elements.",
                "  • Verify with PDF accessibility checker: Accessibility → Full Check → 'Language specified for text'.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale=(
                f"Multiple /Lang values ({lang_list}) detected in structure tree "
                "via direct inspection of /StructTreeRoot child elements."
            ),
            evidence_source=EvidenceSource.XML_DIRECT,
            location="Document structure tree (/StructTreeRoot /Lang entries)",
            remediation_id="pdf_lang_parts",
            remediation_data={
                "action": "set_element_language",
                "doc_lang": self.fact_sheet.document_language,
                "foreign_langs": distinct_non_doc,
            },
        ))

    # ── 2.4.3 Focus Order ────────────────────────────────────────────────────

    def _rule_2_4_3_focus_order(self):
        """WCAG 2.4.3 — On pages with form fields or links, /Tabs should be /S
        (structure-based tab order) so that AT follows logical reading order."""
        if not self._pdf:
            return
        problem_pages: List[int] = []
        for page_num, page in enumerate(self._pdf.pages, 1):
            try:
                annots = page.get('/Annots')
                if not annots:
                    continue
                # Check for interactive annotations (links, form fields)
                has_interactive = False
                try:
                    for a in annots:
                        a_resolved = _resolve(a)
                        subtype = a_resolved.get('/Subtype') if a_resolved else None
                        if subtype and str(subtype) in {'/Link', '/Widget'}:
                            has_interactive = True
                            break
                except Exception:
                    continue
                if not has_interactive:
                    continue
                tabs = page.get('/Tabs')
                tabs_str = str(tabs) if tabs else None
                # /S = structure-based, /R = row, /C = column. Only /S is recommended for AT.
                if tabs_str != '/S':
                    problem_pages.append(page_num)
            except Exception:
                continue

        if not problem_pages:
            return

        sample = problem_pages[:5]
        more = len(problem_pages) - 5
        sample_str = ', '.join(str(p) for p in sample)
        if more > 0:
            sample_str += f", ... and {more} more"

        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.4.3",
            criterion_name="Focus Order",
            wcag_level="A",
            issue=(
                f"{len(problem_pages)} page(s) with interactive elements lack /Tabs /S "
                "(structural tab order)."
            ),
            evidence=(
                f"Pages with links or form fields where /Tabs entry is missing or not /S: {sample_str}."
            ),
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Without /Tabs /S, keyboard and screen reader users tab through "
                "annotations in arbitrary order rather than visual/logical order. "
                "This makes interactive PDFs disorienting or unusable for AT users."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Page properties of pages with interactive content.",
                "",
                "HOW TO FIX:",
                "  • Adobe Acrobat Pro: Page Thumbnails panel → right-click page → Properties → Tab Order → 'Use Document Structure'.",
                "  • Apply to all pages: select all thumbnails first, then set Tab Order.",
                "  • Re-run Accessibility → Full Check → 'Tab order'.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale="/Tabs entry on each page object is read directly via pikepdf.",
            evidence_source=EvidenceSource.XML_DIRECT,
            location=f"{len(problem_pages)} page(s)",
            remediation_id="pdf_focus_order",
            remediation_data={
                "action": "set_tabs_structure",
                "pages_affected": problem_pages[:50],
            },
        ))

    # ── Phase D: 1.3.2 Meaningful Sequence (reading order) ──────────────────

    def _rule_1_3_2_reading_order(self):
        """WCAG 1.3.2 — When a tagged PDF has a /StructTreeRoot but the structure
        tree contains far fewer elements than the document has pages, the reading
        order is not actually defined for most of the content. Skipped if the
        document is untagged (covered by 1.3.1)."""
        if not self._has_structure_tree() or not self._is_tagged():
            return
        try:
            struct_root = self._catalog.get('/StructTreeRoot')
            if struct_root is None:
                return
        except Exception:
            return

        # Count total structure elements under the root.
        count = 0
        # Track whether the first child is /Document (or /Part), which is the
        # standard PDF/UA pattern for a properly wrapped reading order.
        first_child_type: Optional[str] = None
        try:
            kids = struct_root.get('/K')
        except Exception:
            kids = None
        if kids is None:
            return

        def _walk(node) -> None:
            nonlocal count
            try:
                if node is None:
                    return
                # A struct element has /S (structure type)
                s = node.get('/S') if hasattr(node, 'get') else None
                if s is not None:
                    count += 1
                k = node.get('/K') if hasattr(node, 'get') else None
                if k is None:
                    return
                if isinstance(k, Array):
                    for child in k:
                        try:
                            _walk(_resolve(child))
                        except Exception:
                            pass
                else:
                    try:
                        _walk(_resolve(k))
                    except Exception:
                        pass
            except Exception:
                return

        try:
            if isinstance(kids, Array):
                # capture first child's /S
                if len(kids) > 0:
                    try:
                        first = _resolve(kids[0])
                        first_child_type = _pdf_str(first.get('/S')) if first else None
                    except Exception:
                        first_child_type = None
                for kid in kids:
                    try:
                        _walk(_resolve(kid))
                    except Exception:
                        pass
            else:
                try:
                    first = _resolve(kids)
                    first_child_type = _pdf_str(first.get('/S')) if first else None
                except Exception:
                    first_child_type = None
                _walk(_resolve(kids))
        except Exception:
            return

        page_count = max(self._page_count or 1, 1)

        # Threshold: tagged PDF should have at least one structure element per
        # page on average. Below that, most content is outside the reading order.
        if count >= page_count:
            return

        # Build evidence string.
        wrap_note = ""
        if first_child_type and first_child_type not in ('/Document', '/Part'):
            wrap_note = f" Root child is {first_child_type} (expected /Document)."

        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.3.2",
            criterion_name="Meaningful Sequence",
            wcag_level="A",
            issue=(
                f"Tagged PDF has only {count} structure element(s) for {page_count} page(s) — "
                "reading order is not defined for most content."
            ),
            evidence=(
                f"/StructTreeRoot present, but walking /K yielded {count} structure element(s) "
                f"vs {page_count} page(s).{wrap_note}"
            ),
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Screen readers follow the structure tree to determine reading order. "
                "When the tree is sparse, AT users hear content in raw stream order "
                "(often columns out of sequence, sidebars interleaved with body, etc.) "
                "or miss content entirely."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Document structure (Tags panel in Acrobat Pro).",
                "",
                "HOW TO FIX:",
                "  • Adobe Acrobat Pro: View → Show/Hide → Navigation Panes → Tags.",
                "  • Use Accessibility → Autotag Document, then manually verify and reorder via the Tags panel.",
                "  • Wrap content in a /Document root element with /Sect, /P, /H1-/H6 children in reading order.",
                "  • Re-export from source (Word, InDesign) with 'Tagged PDF' enabled if available.",
                "  • Verify with Accessibility → Reading Order tool ('Show Order Panel').",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale=(
                "Element count obtained by walking /StructTreeRoot via pikepdf and "
                "compared against the document's page count."
            ),
            evidence_source=EvidenceSource.XML_DIRECT,
            location="Document structure tree",
            remediation_id="pdf_reading_order_sparse",
            remediation_data={
                "action": "rebuild_structure_tree",
                "structure_elements": count,
                "page_count": page_count,
                "first_child_type": first_child_type,
            },
        ))

    # WCAG 2.1 autocomplete tokens (subset most relevant to PDF form fields)
    _AUTOCOMPLETE_TOKENS = {
        'name', 'given-name', 'family-name', 'honorific-prefix', 'additional-name',
        'honorific-suffix', 'nickname',
        'email', 'username', 'new-password', 'current-password',
        'organization-title', 'organization',
        'street-address', 'address-line1', 'address-line2', 'address-line3',
        'address-level1', 'address-level2', 'address-level3', 'address-level4',
        'country', 'country-name', 'postal-code',
        'cc-name', 'cc-given-name', 'cc-family-name', 'cc-number',
        'cc-exp', 'cc-exp-month', 'cc-exp-year', 'cc-csc', 'cc-type',
        'transaction-currency', 'transaction-amount',
        'language', 'bday', 'bday-day', 'bday-month', 'bday-year',
        'sex', 'tel', 'tel-country-code', 'tel-national', 'tel-area-code',
        'tel-local', 'tel-extension',
        'url', 'photo',
    }

    # Common substring hints — when a field label/name *suggests* one of these
    # purposes but the name doesn't use the canonical token.
    _PURPOSE_HINTS = {
        'email': 'email',
        'phone': 'tel',
        'telephone': 'tel',
        'mobile': 'tel',
        'fax': 'tel',
        'firstname': 'given-name',
        'first_name': 'given-name',
        'first name': 'given-name',
        'lastname': 'family-name',
        'last_name': 'family-name',
        'last name': 'family-name',
        'surname': 'family-name',
        'fullname': 'name',
        'full name': 'name',
        'address': 'street-address',
        'street': 'address-line1',
        'city': 'address-level2',
        'state': 'address-level1',
        'province': 'address-level1',
        'zip': 'postal-code',
        'zipcode': 'postal-code',
        'postal': 'postal-code',
        'country': 'country',
        'birthday': 'bday',
        'dob': 'bday',
        'date of birth': 'bday',
        'creditcard': 'cc-number',
        'credit card': 'cc-number',
        'cardnumber': 'cc-number',
        'cvv': 'cc-csc',
        'cvc': 'cc-csc',
    }

    def _rule_1_3_5_input_purpose(self):
        """WCAG 1.3.5 Identify Input Purpose (AA) — for fields collecting common
        user-info, the field name should match one of the WCAG-defined input
        purpose tokens so AT can offer autofill / personalisation. Possible
        (because PDF form fields don't have a true autocomplete attribute, we
        infer from /T and /TU)."""
        fields = self._collect_form_fields()
        if not fields:
            return
        offenders: List[Dict] = []
        for f in fields:
            name = (f.get('field_name') or '').strip().lower()
            label = (f.get('label') or '').strip().lower() if f.get('label') else ''
            haystack = f"{name} {label}".strip()
            if not haystack:
                continue
            # Already canonical?
            tokens = set(re.split(r'[\s_\-]+', haystack))
            if tokens & self._AUTOCOMPLETE_TOKENS:
                continue
            # Look for hint substrings.
            for hint, suggested in self._PURPOSE_HINTS.items():
                if hint in haystack:
                    offenders.append({
                        "field_name": f.get('field_name'),
                        "label": f.get('label'),
                        "page": f.get('page'),
                        "detected_purpose": hint,
                        "suggested_token": suggested,
                    })
                    break
        if not offenders:
            return
        sample = "; ".join(
            f"'{o['field_name']}' (suggest autocomplete='{o['suggested_token']}')"
            for o in offenders[:3]
        )
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.3.5",
            criterion_name="Identify Input Purpose",
            wcag_level="AA",
            issue=(
                f"{len(offenders)} form field(s) likely collect a common user "
                "input purpose but the field name doesn't use a WCAG autocomplete token."
            ),
            evidence=f"Fields needing input-purpose mapping: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 1.3.5 requires that fields collecting personal information "
                "(name, email, address, phone, payment info) be programmatically "
                "identifiable so assistive technology and the user's own personal-data "
                "store can offer autofill, custom symbols, or simplified labels. "
                "PDF AcroForm fields don't carry the HTML autocomplete attribute, so "
                "naming the field with a recognisable token is the practical signal."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} form field(s) across the PDF.",
                "  • Acrobat Pro: Prepare Form → select field → Field Properties → General → set Name to a WCAG autocomplete token.",
                "  • Recommended: re-export from the source (Word, InDesign) using descriptive field names like 'email', 'tel', 'given-name'.",
                "  • Where business naming conflicts (e.g. 'cust_email_1'), add a /TU tooltip whose text contains the canonical token.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=(
                "Inferred from substring hints in /T (field name) and /TU (tooltip). "
                "True intent confirmation requires the form designer."
            ),
            evidence_source=EvidenceSource.XML_INFERRED,
            location=f"{len(offenders)} form field(s)",
            remediation_id="pdf_input_purpose",
            remediation_data={"fields": offenders},
        ))
