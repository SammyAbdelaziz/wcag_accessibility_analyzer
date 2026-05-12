"""
DOCX WCAG Analyzer
Reads OOXML directly from a .docx ZIP to extract structural accessibility facts.
Every finding is tied to a specific XML location — no guessing.

WCAG criteria covered:
  CONFIRMED (xml_direct):
    1.1.1  — images with missing/empty wp:docPr/@descr (not decorative)
    1.3.1  — table rows without w:tblHeader on first row
    2.4.2  — empty or absent dc:title in docProps/core.xml
    2.4.4  — link display text is generic ("click here", "here", bare URL)
    3.1.1  — default language not set in document settings

  CONFIRMED (text_content):
    1.3.1  — Unicode checkbox characters used instead of semantic list structure
    2.4.4  — link text matches generic pattern

  CONFIRMED (xml_inferred):
    1.3.1  — paragraph text that looks like a heading but uses Normal style
    4.1.2  — form content controls without descriptive title/tag

  POSSIBLE:
    1.3.1  — content sequence anomalies (hard to determine without rendering)
    3.1.2  — mixed language runs detected
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import Any, Dict, List, Optional
from lxml import etree

from wcag.models import (
    FactSheet, ParagraphInfo, ImageInfo, TableInfo, HyperlinkInfo, Finding,
    Severity, ConfidenceTier, EvidenceSource, CONFIDENCE_LABEL,
)
from wcag.common import (
    ColorAnalyzer,
    SemanticFlowAnalyzer,
    FormAnalyzer,
    hex_luminance,
    is_generic_link_text,
)

# XML namespaces
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DC = "http://purl.org/dc/elements/1.1/"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
WORD_REL_HYPERLINK = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"

# Light hex colors — known near-white that fail contrast on white backgrounds
# Threshold: luminance > 0.4 → fails 4.5:1 on white (#FFFFFF, L=1.0)
LIGHT_HEX_THRESHOLD = 0.4   # relative luminance

# Theme color alias map for DOCX themes (word/theme/theme1.xml)
DOCX_SCHEME_ALIAS: dict = {
    'bg1': 'lt1', 'bg2': 'lt2', 'tx1': 'dk1', 'tx2': 'dk2',
}

# Default Office theme colors (DOCX fallback)
DOCX_DEFAULT_THEME: dict = {
    'dk1': '000000', 'lt1': 'FFFFFF', 'dk2': '44546A', 'lt2': 'E7E6E6',
    'accent1': '4472C4', 'accent2': 'ED7D31', 'accent3': 'A9D18E',
    'accent4': 'FFC000', 'accent5': '5B9BD5', 'accent6': '70AD47',
}


def _hex_luminance(hex_str: str) -> float:
    """Compute relative luminance from a 6-char hex color."""
    def linearize(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    try:
        h = hex_str.lstrip('#').upper()
        if len(h) != 6:
            return 0.5
        r, g, b = int(h[0:2], 16)/255, int(h[2:4], 16)/255, int(h[4:6], 16)/255
        return 0.2126*linearize(r) + 0.7152*linearize(g) + 0.0722*linearize(b)
    except Exception:
        return 0.5




# Unicode checkbox characters that indicate non-semantic list usage
UNICODE_CHECKBOXES = {'☐', '☑', '☒', '✓', '✗', '✘', '□', '■', '○', '●'}

# Patterns for generic link text
GENERIC_LINK_TEXT = re.compile(
    r'^(click here|click|here|this link|learn more|more|read more|link|url|see here)$',
    re.IGNORECASE
)
URL_PATTERN = re.compile(r'^https?://', re.IGNORECASE)

# Patterns suggesting heading intent in Normal-styled text
HEADING_PATTERNS = [
    re.compile(r'^[A-Z][^.!?]{3,50}$'),    # Short title-case sentence, no punctuation
    re.compile(r'^\d+\.\s+[A-Z]'),          # Numbered section e.g. "1. Introduction"
    re.compile(r'^[A-Z]+\s*:$'),            # All-caps label with colon e.g. "INTRODUCTION:"
]

HEADING_STYLES = {'Heading 1', 'Heading 2', 'Heading 3', 'Heading 4', 'Heading 5', 'Heading 6',
                  'heading 1', 'heading 2', 'heading 3',
                  # Word style IDs are sometimes serialized without a space.
                  'Heading1', 'Heading2', 'Heading3', 'Heading4', 'Heading5', 'Heading6',
                  'heading1', 'heading2', 'heading3'}


def _looks_like_heading(para: ParagraphInfo) -> bool:
    if para.style_name not in ('Normal', 'Default Paragraph Style', ''):
        return False
    text = para.text.strip()
    if not text:
        return False
    # Long paragraphs are almost certainly running text, not headings.
    # Empirical cap: 120 chars catches block quotes, callouts, and
    # one-liner intros without losing real heading candidates.
    if len(text) > 120:
        return False
    # Headings typically don't end in sentence-final punctuation.
    if text.endswith(('.', '!', '?')) and not text.endswith('...'):
        return False
    if para.is_bold and len(text.split()) <= 10:
        for pattern in HEADING_PATTERNS:
            if pattern.match(text):
                return True
    return False


class DocxAnalyzer:
    def __init__(self, file_bytes: bytes, filename: str):
        self.file_bytes = file_bytes
        self.filename = filename
        self.zip = zipfile.ZipFile(io.BytesIO(file_bytes))
        self.fact_sheet = FactSheet(filename=filename, file_type='docx')
        self._hyperlink_rels: dict = {}
        self._theme_colors: Optional[dict] = None
        
        # Initialize shared analyzers
        self.color_analyzer = ColorAnalyzer()
        self.flow_analyzer = SemanticFlowAnalyzer()
        self.form_analyzer = FormAnalyzer()

    def analyze(self) -> FactSheet:
        self._load_hyperlink_rels()
        self._extract_core_metadata()
        self._extract_document_language()
        paragraphs, images, tables, hyperlinks = self._extract_document_body()

        self.fact_sheet.paragraphs = paragraphs
        self.fact_sheet.paragraph_count = len(paragraphs)
        self.fact_sheet.images = images
        self.fact_sheet.tables = tables
        self.fact_sheet.hyperlinks = hyperlinks

        # Check for unicode checkboxes
        all_text = ' '.join(p.text for p in paragraphs)
        self.fact_sheet.has_unicode_checkboxes = any(c in all_text for c in UNICODE_CHECKBOXES)

        self._run_rules()
        return self.fact_sheet

    # ── Extraction ───────────────────────────────────────────────────────────

    def _load_hyperlink_rels(self):
        """Load hyperlink relationship targets from document.xml.rels."""
        try:
            content = self.zip.read('word/_rels/document.xml.rels')
            root = etree.fromstring(content)
            for rel in root:
                if rel.get('Type', '') == WORD_REL_HYPERLINK:
                    self._hyperlink_rels[rel.get('Id', '')] = rel.get('Target', '')
        except Exception:
            pass

    def _extract_core_metadata(self):
        try:
            content = self.zip.read('docProps/core.xml')
            root = etree.fromstring(content)
            title_el = root.find(f'{{{DC}}}title')
            self.fact_sheet.document_title = (title_el.text or '').strip() if title_el is not None else None
        except Exception:
            self.fact_sheet.document_title = None

    def _extract_document_language(self):
        """Read default document language from settings, styles defaults, or document defaults."""
        try:
            content = self.zip.read('word/settings.xml')
            root = etree.fromstring(content)
            lang = root.find(f'.//{{{W}}}lang')
            if lang is not None:
                self.fact_sheet.document_language = lang.get(f'{{{W}}}val') or lang.get(f'{{{W}}}bidi')
                return
        except Exception:
            pass

        try:
            content = self.zip.read('word/styles.xml')
            root = etree.fromstring(content)
            lang = root.find(f'.//{{{W}}}docDefaults//{{{W}}}rPrDefault//{{{W}}}rPr//{{{W}}}lang')
            if lang is not None:
                self.fact_sheet.document_language = lang.get(f'{{{W}}}val') or lang.get(f'{{{W}}}eastAsia') or lang.get(f'{{{W}}}bidi')
                return
        except Exception:
            pass

        # Fallback: check document defaults embedded in document.xml
        try:
            content = self.zip.read('word/document.xml')
            root = etree.fromstring(content)
            rPrDefault = root.find(f'.//{{{W}}}rPrDefault')
            if rPrDefault is not None:
                lang = rPrDefault.find(f'{{{W}}}lang')
                if lang is not None:
                    self.fact_sheet.document_language = lang.get(f'{{{W}}}val') or lang.get(f'{{{W}}}eastAsia') or lang.get(f'{{{W}}}bidi')
        except Exception:
            pass

    def _extract_document_body(self):
        content = self.zip.read('word/document.xml')
        root = etree.fromstring(content)
        body = root.find(f'{{{W}}}body')
        if body is None:
            return [], [], [], []

        paragraphs: List[ParagraphInfo] = []
        images: List[ImageInfo] = []
        tables: List[TableInfo] = []
        hyperlinks: List[HyperlinkInfo] = []
        para_index = 0
        table_index = 0
        image_index = 0

        for child in body:
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if tag == 'p':
                p_info = self._parse_paragraph(child, para_index)
                paragraphs.append(p_info)
                # Check for inline images/drawings in this paragraph
                for drawing in child.findall(f'.//{{{W}}}drawing'):
                    img = self._parse_drawing(drawing, image_index, para_index)
                    if img:
                        images.append(img)
                        image_index += 1
                # Check for hyperlinks in this paragraph
                for hl in child.findall(f'.//{{{W}}}hyperlink'):
                    link = self._parse_hyperlink(hl, para_index)
                    if link:
                        hyperlinks.append(link)
                para_index += 1
            elif tag == 'tbl':
                tbl = self._parse_table(child, table_index, para_index)
                tables.append(tbl)
                table_index += 1

        return paragraphs, images, tables, hyperlinks

    def _parse_paragraph(self, p: etree._Element, index: int) -> ParagraphInfo:
        pPr = p.find(f'{{{W}}}pPr')
        style_name = ''
        list_level = None
        if pPr is not None:
            pStyle = pPr.find(f'{{{W}}}pStyle')
            if pStyle is not None:
                style_name = pStyle.get(f'{{{W}}}val', '')
            numPr = pPr.find(f'{{{W}}}numPr')
            if numPr is not None:
                ilvl = numPr.find(f'{{{W}}}ilvl')
                list_level = int(ilvl.get(f'{{{W}}}val', 0)) if ilvl is not None else 0

        # Extract text and run properties
        text_parts = []
        is_bold = False
        font_size = None
        run_lang = None

        for r in p.findall(f'.//{{{W}}}r'):
            rPr = r.find(f'{{{W}}}rPr')
            if rPr is not None:
                b = rPr.find(f'{{{W}}}b')
                if b is not None and b.get(f'{{{W}}}val', '1') != '0':
                    is_bold = True
                sz = rPr.find(f'{{{W}}}sz')
                if sz is not None:
                    val = sz.get(f'{{{W}}}val')
                    if val:
                        try:
                            font_size = int(val) / 2  # half-points to points
                        except ValueError:
                            pass
                lang = rPr.find(f'{{{W}}}lang')
                if lang is not None and run_lang is None:
                    run_lang = lang.get(f'{{{W}}}val')
            for t in r.findall(f'{{{W}}}t'):
                if t.text:
                    text_parts.append(t.text)

        text = ''.join(text_parts)
        return ParagraphInfo(
            index=index,
            style_name=style_name,
            text=text,
            list_level=list_level,
            is_empty=not bool(text.strip()),
            run_language=run_lang,
            is_bold=is_bold,
            font_size_pt=font_size,
        )

    def _parse_drawing(self, drawing: etree._Element, idx: int, para_idx: int) -> Optional[ImageInfo]:
        # Look for docPr element which carries alt text
        docPr = drawing.find(f'.//{{{WP}}}docPr')
        if docPr is None:
            # Also try inline vs anchor
            for ns_tag in [f'{{{WP}}}inline', f'{{{WP}}}anchor']:
                el = drawing.find(ns_tag)
                if el is not None:
                    docPr = el.find(f'{{{WP}}}docPr')
                    if docPr is not None:
                        break
        if docPr is None:
            return ImageInfo(
                index=idx,
                alt_text=None,
                alt_title=None,
                is_decorative=False,
                location_hint=f"Inline in paragraph {para_idx}",
            )

        alt_text = docPr.get('descr')    # None = absent
        alt_title = docPr.get('title')

        # Check for decorative marker (Office 2017+)
        is_decorative = False
        DECORATIVE_URI = "{C183D7F6-B498-43B3-948B-1728B52AA6E4}"
        for child in docPr.iter():
            if child.get('uri') == DECORATIVE_URI:
                for desc_child in child:
                    if desc_child.get('val') == '1':
                        is_decorative = True
                        break

        is_inline = drawing.find(f'{{{WP}}}inline') is not None
        location = f"Inline in paragraph {para_idx}" if is_inline else f"Anchored near paragraph {para_idx}"

        return ImageInfo(
            index=idx,
            alt_text=alt_text,
            alt_title=alt_title,
            is_decorative=is_decorative,
            location_hint=location,
        )

    def _parse_table(self, tbl: etree._Element, idx: int, para_idx: int) -> TableInfo:
        rows = tbl.findall(f'{{{W}}}tr')
        row_count = len(rows)
        col_count = 0
        has_header = False

        if rows:
            first_row = rows[0]
            cells = first_row.findall(f'{{{W}}}tc')
            col_count = len(cells)
            trPr = first_row.find(f'{{{W}}}trPr')
            if trPr is not None:
                tblHeader = trPr.find(f'{{{W}}}tblHeader')
                if tblHeader is not None and tblHeader.get(f'{{{W}}}val', '1') != '0':
                    has_header = True

        return TableInfo(
            index=idx,
            has_header_row=has_header,
            row_count=row_count,
            col_count=col_count,
            location_hint=f"Table {idx + 1} near paragraph {para_idx}",
        )

    def _parse_hyperlink(self, hl: etree._Element, para_idx: int) -> Optional[HyperlinkInfo]:
        rel_id = hl.get(f'{{{R}}}id')
        url = self._hyperlink_rels.get(rel_id) if rel_id else None
        # Internal anchor links have no r:id but have w:anchor
        if not url:
            anchor = hl.get(f'{{{W}}}anchor')
            url = f"#{anchor}" if anchor else None

        text_parts = []
        for t in hl.findall(f'.//{{{W}}}t'):
            if t.text:
                text_parts.append(t.text)
        display_text = ''.join(text_parts).strip()
        if not display_text:
            return None
        return HyperlinkInfo(paragraph_index=para_idx, display_text=display_text, url=url)

    # ── Rules engine ─────────────────────────────────────────────────────────

    def _run_rules(self):
        self._rule_1_1_1_images()
        self._rule_1_3_1_tables()
        self._rule_1_3_1_table_header_associations()  # M5: Complex table header association checks
        self._rule_1_3_1_form_control_labels()  # M4: Form control labeling checks
        self._rule_1_3_1_checkboxes()
        self._rule_1_3_1_false_headings()
        self._rule_1_3_1_heading_hierarchy()  # NEW: Heading hierarchy validation
        self._rule_1_3_1_list_coherence()  # NEW: List structure validation via object model
        self._rule_1_3_1_list_styles()  # NEW: List formatting validation (bullets/numbering)
        self._rule_2_4_6_heading_labels()   # 2.4.6: Descriptive headings
        self._rule_1_3_2_empty_paragraphs()  # M1: Empty paragraph detection
        self._rule_1_3_2_spacing_paragraphs()  # M3: Spacing paragraph detection
        self._rule_1_3_2_text_spacing()  # Phase 5: Text spacing attributes validation
        self._rule_1_3_2_floating_boxes()
        self._rule_1_4_3_contrast()
        self._rule_1_4_1_color_only()  # NEW: Color used as the only visual means of conveying info
        self._rule_1_4_4_text_size()  # M2: Very small text detection
        self._rule_1_4_5_images_of_text()  # NEW: Detect images that appear to contain text
        self._rule_2_4_2_doc_title()
        self._rule_2_4_4_link_text()
        self._rule_3_1_1_language()
        self._rule_3_1_2_mixed_language()
        self._rule_2_4_5_multiple_ways()  # Phase A: TOC / bookmarks for navigation
        self._rule_1_4_11_non_text_contrast()  # Phase B
        self._rule_1_3_3_sensory_characteristics()  # Phase I
        self._rule_1_4_10_fixed_width_tables()  # Phase K
        self._rule_1_3_1_table_header_marking()  # Phase M-refinements R3

    def _rule_1_3_3_sensory_characteristics(self):
        """WCAG 1.3.3 — flag paragraphs that reference UI elements only by
        color/shape/position (e.g. 'click the red button'). Strict regex."""
        from wcag.common.sensory_characteristics import find_sensory_phrases
        paragraphs = self.fact_sheet.paragraphs or []
        if not paragraphs:
            return
        offenders = find_sensory_phrases(
            (p.index, (p.text or "").strip()) for p in paragraphs
        )
        if not offenders:
            return
        sample = "; ".join(
            f"{o['kind']}-only @ paragraph {o['index']}: \"{o['snippet']}\""
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.3.3",
            criterion_name="Sensory Characteristics",
            wcag_level="A",
            issue=(
                f"{len(offenders)} paragraph(s) reference UI elements only by "
                "color, shape, or position."
            ),
            evidence=f"Sensory-only references: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "Users who are blind, color-blind, or using screen readers cannot identify "
                "a control by its visual appearance or position. Instructions must include "
                "the control's name or label."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} paragraph(s) with sensory-only language.",
                "  • Add the control's name: 'Click the red Submit button' instead of 'click the red button'.",
                "  • Combine color/position with text: 'Use the menu labeled Help (top-right)'.",
                "  • Reword shape references to use the visible label.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale="Body paragraph text scanned for sensory-only phrase patterns.",
            evidence_source=EvidenceSource.TEXT_CONTENT,
            location=f"{len(offenders)} paragraph(s)",
            remediation_id="docx_sensory_characteristics",
            remediation_data={"references": offenders},
        ))

    def _rule_1_4_10_fixed_width_tables(self):
        """WCAG 1.4.10 Reflow — DOCX tables with fixed absolute width
        (`w:tblW type='dxa'`) wider than the page body force horizontal
        scrolling at narrow viewports / on print, breaking reflow. Strict.

        DXA = twentieths of a point. 1 inch = 1440 dxa. Default Letter body
        width is ~6.5" usable = 9360 dxa. We use a conservative 9000 dxa
        threshold; tables wider than that on Letter break body text reflow.
        """
        try:
            content = self.zip.read('word/document.xml')
        except KeyError:
            return
        try:
            root = etree.fromstring(content)
        except etree.XMLSyntaxError:
            return

        # Page body width: pgSz - margins (left+right). Fall back to 9000 dxa
        # if we cannot resolve sectPr.
        body_width = 9000
        try:
            sectPr = root.find(f'.//{{{W}}}sectPr')
            if sectPr is not None:
                pgSz = sectPr.find(f'{{{W}}}pgSz')
                pgMar = sectPr.find(f'{{{W}}}pgMar')
                if pgSz is not None and pgMar is not None:
                    page_w = int(pgSz.get(f'{{{W}}}w') or 12240)
                    left = int(pgMar.get(f'{{{W}}}left') or 1440)
                    right = int(pgMar.get(f'{{{W}}}right') or 1440)
                    body_width = max(1, page_w - left - right)
        except (ValueError, TypeError):
            pass

        offenders = []
        for tbl in root.findall(f'.//{{{W}}}tbl'):
            tblW = tbl.find(f'{{{W}}}tblPr/{{{W}}}tblW')
            if tblW is None:
                continue
            w_type = tblW.get(f'{{{W}}}type')
            w_val = tblW.get(f'{{{W}}}w')
            if w_type != 'dxa' or not w_val:
                continue
            try:
                width = int(w_val)
            except ValueError:
                continue
            if width > body_width:
                offenders.append({
                    "width_dxa": width,
                    "width_inches": round(width / 1440.0, 2),
                    "body_width_dxa": body_width,
                    "body_width_inches": round(body_width / 1440.0, 2),
                })
        if not offenders:
            return
        worst = max(o["width_inches"] for o in offenders)
        sample = "; ".join(
            f"table {o['width_inches']}\" wide vs body {o['body_width_inches']}\""
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.4.10",
            criterion_name="Reflow",
            wcag_level="AA",
            issue=(
                f"{len(offenders)} table(s) have fixed widths exceeding the page "
                f"body (worst: {worst}\" wide), preventing reflow at narrow widths."
            ),
            evidence=f"Fixed-width tables wider than body: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 1.4.10 requires content to reflow at 320 CSS pixel widths without "
                "horizontal scrolling. DOCX tables sized in absolute dxa do not shrink — "
                "users on small screens, in narrow reading panes, or zoomed in to 400% "
                "must scroll horizontally to reach all data, losing context."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} table(s) with absolute (dxa) widths.",
                "  • Open Table Properties → Table tab → set Preferred width to 100% (or Auto).",
                "  • In the underlying XML this changes <w:tblW w:type='dxa'> to type='pct' (or removes it).",
                "  • For complex layout tables, split into two narrower tables stacked vertically.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale=f"Compared each table's <w:tblW> dxa value against computed body width ({body_width} dxa).",
            evidence_source=EvidenceSource.XML_DIRECT,
            location=f"{len(offenders)} table(s)",
            remediation_id="docx_fixed_width_tables",
            remediation_data={"tables": offenders, "body_width_dxa": body_width},
        ))

    def _rule_1_3_1_table_header_marking(self):
        """WCAG 1.3.1 (refinement) — DOCX tables whose first row is NOT marked
        as a header row (no <w:tblHeader/> in the row's <w:trPr>) lose
        programmatic header semantics. Strict.
        """
        try:
            content = self.zip.read('word/document.xml')
        except KeyError:
            return
        try:
            root = etree.fromstring(content)
        except etree.XMLSyntaxError:
            return

        offenders = []
        tbl_index = 0
        for tbl in root.findall(f'.//{{{W}}}tbl'):
            tbl_index += 1
            rows = tbl.findall(f'{{{W}}}tr')
            if len(rows) < 2:
                continue  # Single-row tables aren't really data tables
            first_row = rows[0]
            trPr = first_row.find(f'{{{W}}}trPr')
            has_header_marker = (
                trPr is not None
                and trPr.find(f'{{{W}}}tblHeader') is not None
            )
            if has_header_marker:
                continue
            # Estimate first-row text for evidence (cap to 80 chars)
            cells = first_row.findall(f'{{{W}}}tc')
            sample = ' | '.join(
                ''.join(t.text or '' for t in c.findall(f'.//{{{W}}}t'))[:20]
                for c in cells[:4]
            )[:80]
            offenders.append({
                "table_index": tbl_index,
                "row_count": len(rows),
                "first_row_sample": sample or '(empty)',
            })
        if not offenders:
            return
        sample_str = "; ".join(
            f"table {o['table_index']} ({o['row_count']} rows): \"{o['first_row_sample']}\""
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.3.1",
            criterion_name="Info and Relationships",
            wcag_level="A",
            issue=(
                f"{len(offenders)} table(s) have no header row marking — first row is "
                "treated as data by assistive tech, losing column context."
            ),
            evidence=f"Tables without <w:tblHeader/>: {sample_str}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "When a DOCX table's first row is not marked as a header (via the "
                "<w:tblHeader/> element on its <w:trPr>), screen readers cannot announce "
                "column headers as the user moves cell-to-cell. Sighted users also lose "
                "headers across page breaks. WCAG 1.3.1 requires header relationships "
                "to be programmatically determinable."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} table(s) in the document.",
                "  • Click in the header row → Table Layout → Properties → Row tab → check 'Repeat as header row at the top of each page'.",
                "  • This sets <w:tblHeader/> in the underlying XML and exposes the row to AT as a header.",
                "  • Verify by checking Print Preview — the header row should repeat on each page.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale="Iterated all <w:tbl> elements; checked first <w:tr>/<w:trPr>/<w:tblHeader> directly in document.xml.",
            evidence_source=EvidenceSource.XML_DIRECT,
            location=f"{len(offenders)} table(s)",
            remediation_id="docx_table_header_marking",
            remediation_data={"tables": offenders},
        ))

    def _rule_1_1_1_images(self):
        for img in (self.fact_sheet.images or []):
            if img.is_decorative:
                continue
            location = img.location_hint
            advisory = self._build_alt_text_advisory(img)
            if img.alt_text is None:
                self.fact_sheet.confirmed_findings.append(Finding(
                    criterion_id="1.1.1",
                    criterion_name="Non-text Content",
                    wcag_level="A",
                    issue=f"Image at '{location}' has no alt text attribute.",
                    evidence=f"<wp:docPr> element has no 'descr' attribute — alt text is completely absent.",
                    severity=Severity.CRITICAL,
                    why_it_matters="Screen readers cannot describe this image to users who cannot see it.",
                    remediation_steps=[
                        f"Right-click the image near '{location}' in Word.",
                        "Select 'Edit Alt Text...'",
                        "Write a meaningful description of the image content.",
                        "If decorative, check 'Mark as decorative'.",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label="high",
                    confidence_rationale="Absence of descr attribute directly confirmed in wp:docPr element.",
                    evidence_source=EvidenceSource.XML_DIRECT,
                    location=location,
                    remediation_id=f"img_alt_{img.index}",
                    remediation_data={"image_index": img.index, "action": "set_alt_text"},
                    advisory_payload=advisory,
                ))
            elif img.alt_text.strip() == '':
                self.fact_sheet.confirmed_findings.append(Finding(
                    criterion_id="1.1.1",
                    criterion_name="Non-text Content",
                    wcag_level="A",
                    issue=f"Image at '{location}' has an empty alt text string.",
                    evidence="<wp:docPr descr=\"\"> — explicitly empty, without decorative marking.",
                    severity=Severity.CRITICAL,
                    why_it_matters="Empty alt text without decorative marking tells screen readers to skip the image with no intent communicated.",
                    remediation_steps=[
                        f"Right-click the image at '{location}' → 'Edit Alt Text...'",
                        "Add a meaningful description, or check 'Mark as decorative'.",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label="high",
                    confidence_rationale="Empty descr attribute confirmed in wp:docPr — not marked decorative.",
                    evidence_source=EvidenceSource.XML_DIRECT,
                    location=location,
                    remediation_id=f"img_alt_{img.index}",
                    remediation_data={"image_index": img.index, "action": "set_alt_text"},
                    advisory_payload=advisory,
                ))

    def _build_alt_text_advisory(self, img) -> Dict[str, Any]:
        """Phase J — build advisory_payload for an image missing alt text.

        Pulls surrounding paragraph text (image's own paragraph + 2 before
        + 2 after) so an LLM can draft a context-aware description.
        """
        # Parse "Inline in paragraph 5" / "Anchored near paragraph 5"
        import re as _re
        m = _re.search(r"paragraph\s+(\d+)", img.location_hint or "")
        ctx_text = ""
        if m and self.fact_sheet.paragraphs:
            target_idx = int(m.group(1)) - 1  # location_hint is 1-based
            paragraphs = self.fact_sheet.paragraphs
            lo = max(0, target_idx - 2)
            hi = min(len(paragraphs), target_idx + 3)
            ctx_text = " ".join(
                p.text.strip() for p in paragraphs[lo:hi]
                if p.text and p.text.strip()
            )[:1000]
        return {
            "advisory_kind": "alt_text",
            "target": f"img[{img.index}]",
            "surface_text": "" if img.alt_text is None else img.alt_text,
            "context": ctx_text,
            "format_hint": "docx",
        }

    def _rule_1_3_1_tables(self):
        for tbl in (self.fact_sheet.tables or []):
            if not tbl.has_header_row and tbl.row_count > 1:
                self.fact_sheet.confirmed_findings.append(Finding(
                    criterion_id="1.3.1",
                    criterion_name="Info and Relationships",
                    wcag_level="A",
                    issue=f"Table {tbl.index + 1} ({tbl.row_count} rows × {tbl.col_count} cols) has no header row marked.",
                    evidence=f"First row of table {tbl.index + 1} has no <w:tblHeader> element — not programmatically identified as a header.",
                    severity=Severity.SERIOUS,
                    why_it_matters="Without header row markup, screen readers cannot associate data cells with their column headers, making tabular data confusing.",
                    remediation_steps=[
                        f"Select the first row of table {tbl.index + 1}.",
                        "Right-click → Table Properties → Row tab.",
                        "Check 'Repeat as header row at the top of each page'.",
                        "This sets <w:tblHeader> which marks it as a programmatic header.",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label="high",
                    confidence_rationale="Absence of w:tblHeader on first table row directly confirmed in XML.",
                    evidence_source=EvidenceSource.XML_DIRECT,
                    location=tbl.location_hint,
                    remediation_id=f"table_header_{tbl.index}",
                    remediation_data={"table_index": tbl.index, "action": "add_table_header"},
                ))

    def _rule_1_3_1_table_header_associations(self):
        """M5: Detect complex tables where header associations are likely incomplete.

        Word tables do not expose HTML-like scope/id mappings. For complex tables,
        we infer risk when only one header row is marked but the first header row
        contains merged cells (multi-level heading pattern).
        """
        try:
            content = self.zip.read('word/document.xml')
            root = etree.fromstring(content)
        except Exception:
            return

        tables = root.findall(f'.//{{{W}}}tbl')
        if not tables:
            return

        findings = []
        for idx, tbl in enumerate(tables):
            rows = tbl.findall(f'{{{W}}}tr')
            if len(rows) < 4:
                continue

            first_row = rows[0]
            first_cells = first_row.findall(f'{{{W}}}tc')
            effective_cols = 0
            for tc in first_cells:
                tcPr = tc.find(f'{{{W}}}tcPr')
                if tcPr is None:
                    effective_cols += 1
                    continue
                grid_span = tcPr.find(f'{{{W}}}gridSpan')
                try:
                    effective_cols += int(grid_span.get(f'{{{W}}}val', '1')) if grid_span is not None else 1
                except Exception:
                    effective_cols += 1

            if effective_cols < 3:
                continue

            header_rows = 0
            for tr in rows:
                trPr = tr.find(f'{{{W}}}trPr')
                if trPr is None:
                    break
                tblHeader = trPr.find(f'{{{W}}}tblHeader')
                if tblHeader is not None and tblHeader.get(f'{{{W}}}val', '1') != '0':
                    header_rows += 1
                else:
                    break

            has_merged_header_cells = False
            has_empty_header_cells = False
            for tc in first_cells:
                tcPr = tc.find(f'{{{W}}}tcPr')
                if tcPr is not None:
                    grid_span = tcPr.find(f'{{{W}}}gridSpan')
                    v_merge = tcPr.find(f'{{{W}}}vMerge')
                    if (grid_span is not None and int(grid_span.get(f'{{{W}}}val', '1')) > 1) or v_merge is not None:
                        has_merged_header_cells = True
                text = ''.join(t.text for t in tc.findall(f'.//{{{W}}}t') if t.text).strip()
                if not text:
                    has_empty_header_cells = True

            if header_rows == 1 and has_merged_header_cells:
                findings.append({
                    "table_index": idx,
                    "row_count": len(rows),
                    "col_count": effective_cols,
                    "risk": "single_header_row_on_complex_header",
                })
            elif header_rows >= 1 and has_empty_header_cells and effective_cols >= 3:
                findings.append({
                    "table_index": idx,
                    "row_count": len(rows),
                    "col_count": effective_cols,
                    "risk": "empty_cells_in_header_row",
                })

        if not findings:
            return

        examples = []
        for item in findings[:3]:
            if item["risk"] == "single_header_row_on_complex_header":
                examples.append(
                    f"Table {item['table_index'] + 1}: merged header cells with only one row marked as header"
                )
            else:
                examples.append(
                    f"Table {item['table_index'] + 1}: empty cell(s) in marked header row"
                )

        more = len(findings) - len(examples)
        evidence = '; '.join(examples)
        if more > 0:
            evidence += f"; and {more} more table(s)"

        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.3.1",
            criterion_name="Info and Relationships",
            wcag_level="A",
            issue=f"{len(findings)} complex table(s) may have incomplete header associations for assistive technologies.",
            evidence=evidence,
            severity=Severity.SERIOUS,
            why_it_matters="Complex tables need clear, programmatic header relationships. If only part of a multi-level header is marked, screen readers may announce ambiguous or incorrect context.",
            remediation_steps=[
                "For complex tables with merged header cells, mark all top header rows using Table Properties -> Row -> Repeat as header row.",
                "Ensure header cells are not empty; add concise header text for each logical column/section.",
                "If the table uses grouped headers, consider simplifying structure or splitting into multiple simpler tables.",
                "Validate with a screen reader table-navigation command (move by row/column) to confirm header context is announced correctly.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Complex-header risk inferred from row/cell XML structure; manual verification is needed to confirm assistive-technology impact.",
            evidence_source=EvidenceSource.XML_INFERRED,
            location="Multiple tables",
            remediation_id="table_header_associations",
            remediation_data={
                "action": "improve_table_header_associations",
                "tables": findings,
            },
        ))

    def _rule_1_3_1_form_control_labels(self):
        """M4: Detect unlabeled Word content controls used as form fields."""
        try:
            content = self.zip.read('word/document.xml')
            root = etree.fromstring(content)
        except Exception:
            return

        controls = root.findall(f'.//{{{W}}}sdt')
        if not controls:
            return

        missing_labels = []
        control_types = {
            f'{{{W}}}text': 'text',
            f'{{{W}}}comboBox': 'combo box',
            f'{{{W}}}dropDownList': 'drop-down list',
            f'{{{W}}}checkBox': 'checkbox',
            f'{{{W}}}date': 'date picker',
            f'{{{W}}}picture': 'picture',
        }

        for idx, sdt in enumerate(controls):
            sdtPr = sdt.find(f'{{{W}}}sdtPr')
            if sdtPr is None:
                continue

            alias = sdtPr.find(f'{{{W}}}alias')
            tag = sdtPr.find(f'{{{W}}}tag')
            alias_val = alias.get(f'{{{W}}}val', '').strip() if alias is not None else ''
            tag_val = tag.get(f'{{{W}}}val', '').strip() if tag is not None else ''

            control_type = 'content control'
            for node, label in control_types.items():
                if sdtPr.find(node) is not None:
                    control_type = label
                    break

            if not alias_val and not tag_val:
                missing_labels.append({
                    "index": idx,
                    "type": control_type,
                })

        if not missing_labels:
            return

        examples = ', '.join(
            f"#{c['index'] + 1} ({c['type']})" for c in missing_labels[:5]
        )
        if len(missing_labels) > 5:
            examples += f", and {len(missing_labels) - 5} more"

        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.3.1",
            criterion_name="Info and Relationships",
            wcag_level="A",
            issue=f"{len(missing_labels)} form content control(s) are missing programmatic labels (alias/tag).",
            evidence=f"Unlabeled controls detected in w:sdtPr with no w:alias/@w:val and no w:tag/@w:val. Examples: {examples}.",
            severity=Severity.SERIOUS,
            why_it_matters="Without programmatic labels, screen readers may announce generic control names, making form completion slow and error-prone.",
            remediation_steps=[
                "Select each content control in Word (Developer tab).",
                "Open Properties and set a descriptive Title (maps to w:alias).",
                "Optionally set Tag for stable machine-readable identification.",
                "Use labels that describe user intent (for example, 'Email address' instead of 'Field 1').",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale="Missing alias/tag values are directly confirmed in each w:sdtPr node.",
            evidence_source=EvidenceSource.XML_DIRECT,
            location="Document form controls",
            remediation_id="form_control_labels",
            remediation_data={
                "action": "label_form_controls",
                "controls": missing_labels,
            },
        ))

        # Phase E: also emit a 4.1.2 finding — same data, different SC.
        # Missing alias = missing accessible NAME for the control's role.
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="4.1.2",
            criterion_name="Name, Role, Value",
            wcag_level="A",
            issue=(
                f"{len(missing_labels)} form content control(s) lack an accessible name "
                "(no w:alias). Screen readers announce only the control's role."
            ),
            evidence=(
                f"Content controls without w:alias/@w:val: {examples}. "
                "Role is conveyed by the control element (text/checkBox/comboBox/etc.) "
                "but no programmatic name is exposed."
            ),
            severity=Severity.SERIOUS,
            why_it_matters=(
                "WCAG 4.1.2 requires every UI control to expose a name, role, and current "
                "value to assistive technology. A content control without w:alias announces "
                "as just its role (e.g. 'edit' or 'check box') with no indication of what "
                "the user is being asked to enter."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Word → Developer tab → click each content control → Properties.",
                "  • Set the Title field to a meaningful label (this becomes w:alias).",
                "  • Use action-oriented names: 'Patient first name', 'Date of admission', etc.",
                "  • Avoid generic names like 'Field 1' or 'Text 2'.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale="Absence of w:alias is read directly from each w:sdtPr node.",
            evidence_source=EvidenceSource.XML_DIRECT,
            location="Document form controls",
            remediation_id="form_control_names_412",
            remediation_data={
                "action": "set_content_control_titles",
                "controls": missing_labels,
            },
        ))

    def _rule_1_3_1_checkboxes(self):
        if not self.fact_sheet.has_unicode_checkboxes:
            return
        affected = []
        for p in (self.fact_sheet.paragraphs or []):
            if any(c in p.text for c in UNICODE_CHECKBOXES):
                affected.append(p.index)

        snippet = f"Paragraphs {affected[:5]}" if len(affected) > 5 else f"Paragraphs {affected}"
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.3.1",
            criterion_name="Info and Relationships",
            wcag_level="A",
            issue="Unicode checkbox characters used instead of semantic list or form elements.",
            evidence=f"Characters such as ☐, ☑, □, ■ found in text content at {snippet}. These are visual symbols, not programmatic checkboxes.",
            severity=Severity.SERIOUS,
            why_it_matters="Screen readers announce these as the Unicode character name (e.g. 'white square') rather than as interactive checkboxes or list items, breaking structure.",
            remediation_steps=[
                "Replace unicode checkbox characters with Word's Content Controls (Developer tab → Legacy Tools → Check Box).",
                "Or convert checklists to structured bullet lists (Home → Bullets) if they are not interactive.",
                "Ensure all list items use the same semantic approach consistently.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale="Unicode checkbox characters detected directly in paragraph text content.",
            evidence_source=EvidenceSource.TEXT_CONTENT,
            location="Multiple paragraphs",
            remediation_id="unicode_checkboxes",
            remediation_data={"action": "replace_checkboxes", "paragraph_indices": affected},
        ))

    def _rule_1_3_1_false_headings(self):
        """Aggregate all false-heading paragraphs into a single finding to reduce noise."""
        flagged = [
            p for p in (self.fact_sheet.paragraphs or [])
            if _looks_like_heading(p)
        ]
        if not flagged:
            return
        n = len(flagged)
        is_singular = n == 1
        # Show up to 8 short examples — concrete examples are far more actionable
        # than abstract counts, and these snippets are short.
        snippets = [p.text.strip()[:60] for p in flagged[:8]]
        more = n - 8
        snippet_list = '; '.join(f"'{s}'" for s in snippets)
        if more > 0:
            snippet_list += f" (and {more} more)"
        first_idx = flagged[0].index
        last_idx = flagged[-1].index
        location = (
            f"Paragraph {first_idx}"
            if is_singular
            else f"{n} paragraphs (first at index {first_idx}, last at index {last_idx})"
        )
        if is_singular:
            issue_text = (
                "1 paragraph appears to be a heading but uses Normal style "
                "instead of a Heading style."
            )
            evidence_text = (
                f"1 bold Normal-style paragraph with heading-like text detected. "
                f"Text: {snippet_list}."
            )
            rationale_text = (
                "1 bold Normal-style paragraph with heading-like text found; "
                "heading intent is likely but requires human confirmation."
            )
        else:
            issue_text = (
                f"{n} paragraphs appear to be headings but use Normal style "
                f"instead of a Heading style."
            )
            evidence_text = (
                f"{n} bold Normal-style paragraphs with heading-like text detected. "
                f"Text: {snippet_list}."
            )
            rationale_text = (
                f"{n} bold Normal-style paragraphs with heading-like text found; "
                f"heading intent is likely but requires human confirmation."
            )
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.3.1",
            criterion_name="Info and Relationships",
            wcag_level="A",
            issue=issue_text,
            evidence=evidence_text,
            severity=Severity.SERIOUS,
            why_it_matters="Screen reader users navigate documents by heading structure. Bold Normal text is invisible to heading navigation — screen readers skip it entirely.",
            remediation_steps=[
                f"In Word, locate each paragraph listed above (use Find or scroll to paragraph index {first_idx}+).",
                "Click in the paragraph, then apply an appropriate Heading style (Heading 1, Heading 2, etc.) from the Styles panel.",
                "Use heading levels in hierarchical order (H1 → H2 → H3) — do not skip levels.",
                "Verify with the Navigation Pane (View → Navigation Pane) that all intended headings appear.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=rationale_text,
            evidence_source=EvidenceSource.XML_INFERRED,
            location=location,
            remediation_id="false_headings",
            remediation_data={"action": "apply_heading_style", "paragraph_indices": [p.index for p in flagged]},
        ))

    def _rule_1_3_1_heading_hierarchy(self):
        """Validate heading hierarchy: no skipped levels, proper nesting.
        
        WCAG 1.3.1 requires that heading elements properly convey document structure.
        Heading hierarchy must not skip levels (H1 → H2 → H3, not H1 → H3).
        """
        # Extract all true headings (paragraphs with Heading styles)
        headings = []
        for p in (self.fact_sheet.paragraphs or []):
            if p.style_name in HEADING_STYLES:
                # Extract heading level from style name. Both "Heading 1" and
                # "Heading1" forms occur in real DOCX files.
                level_match = re.search(r'[Hh]eading\s*(\d)', p.style_name)
                if level_match:
                    level = int(level_match.group(1))
                    headings.append((p.index, level, p.text.strip()[:60]))
        
        if not headings:
            return  # No headings to validate

        # Check (1): document should start with H1.
        first_idx, first_level, first_text = headings[0]
        starts_below_h1 = first_level > 1

        # Check (2): no skipped levels relative to the immediately preceding heading.
        issues = []
        seen_levels = set()
        prev_level = None

        for para_idx, level, text in headings:
            seen_levels.add(level)
            if prev_level is not None and level > prev_level + 1:
                # Skipped a level relative to the current branch (e.g., H2 -> H5).
                expected = prev_level + 1
                issues.append({
                    'type': 'skip',
                    'para_index': para_idx,
                    'text': text,
                    'level': level,
                    'expected': expected,
                    'message': f"Heading level {level} at paragraph {para_idx} skips expected level {expected}.",
                })
            prev_level = level

        if not issues and not starts_below_h1:
            return

        # Build a short outline string so the user sees the exact pattern.
        outline_preview = ' \u2192 '.join(
            f"H{lv}" for _, lv, _ in headings[:8]
        )
        if len(headings) > 8:
            outline_preview += f" \u2192 \u2026 ({len(headings) - 8} more)"

        all_levels = ', '.join(str(l) for l in sorted(seen_levels))
        snippet_parts = []
        if starts_below_h1:
            snippet_parts.append(
                f"first heading is H{first_level} '{first_text[:40]}' "
                f"at paragraph {first_idx} (expected H1)"
            )
        snippet_parts.extend(
            f"H{i['level']} '{i['text'][:40]}' (para {i['para_index']}, expected H{i['expected']})"
            for i in issues[:3]
        )
        more = len(issues) + (1 if starts_below_h1 else 0) - len(snippet_parts)
        snippet = '; '.join(snippet_parts)
        if more > 0:
            snippet += f"; \u2026 and {more} more"

        total_issue_count = len(issues) + (1 if starts_below_h1 else 0)
        issue_text = (
            f"Heading hierarchy is invalid: outline reads {outline_preview}. "
            f"{total_issue_count} violation(s) detected (levels present: {all_levels})."
        )
        steps = [
            "Open View \u2192 Navigation Pane to see the heading outline.",
        ]
        if starts_below_h1:
            steps.append(
                f"Promote the first heading ('{first_text[:40]}') to "
                f"Heading 1 \u2014 every document should begin with H1."
            )
        if issues:
            steps.append(
                "For each skipped level, insert the missing intermediate "
                "heading or change the current heading to one level deeper "
                "(e.g. H1 \u2192 H3 should become H1 \u2192 H2 \u2192 H3)."
            )
        steps.append(
            "After fixing, verify in the Navigation Pane that levels ascend "
            "naturally without gaps."
        )

        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.3.1",
            criterion_name="Info and Relationships",
            wcag_level="A",
            issue=issue_text,
            evidence=f"Examples: {snippet}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Screen-reader users navigate by heading level. A document "
                "that starts below H1 has no top-level landmark, and skipped "
                "levels (H1 \u2192 H3) make users believe content is missing. "
                "Both break the document outline."
            ),
            remediation_steps=steps,
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale=(
                f"{total_issue_count} heading-hierarchy violation(s) detected "
                "directly from heading-style names in XML."
            ),
            evidence_source=EvidenceSource.XML_DIRECT,
            location=f"Headings: {outline_preview}",
            remediation_id="heading_hierarchy",
            remediation_data={
                "action": "fix_heading_hierarchy",
                "violations": [
                    {"paragraph_index": issue['para_index'], "current_level": issue['level'], "expected_level": issue['expected']}
                    for issue in issues
                ]
            },
        ))

    # WCAG 2.4.6 generic/vague heading patterns
    _GENERIC_HEADING_RE = re.compile(
        r'^(section\s*\d*|chapter\s*\d*|heading\s*\d*|part\s*\d*|introduction|overview|'
        r'conclusion|summary|background|appendix\s*[a-z]?|title|untitled|tbd|todo|'
        r'placeholder|click to edit|enter heading here|insert heading)$',
        re.IGNORECASE,
    )

    def _rule_2_4_6_heading_labels(self):
        """WCAG 2.4.6 Headings and Labels: headings must be descriptive.

        Detects headings that use generic placeholder text. A heading like
        'Section 1' or 'Introduction' with no further context tells a screen-reader
        user nothing about what that section contains. We flag headings whose full
        text matches known generic patterns.
        """
        headings = [
            p for p in (self.fact_sheet.paragraphs or [])
            if p.style_name in HEADING_STYLES and p.text.strip()
        ]
        if not headings:
            return

        vague = [
            p for p in headings
            if self._GENERIC_HEADING_RE.match(p.text.strip())
        ]
        if not vague:
            return

        examples = '; '.join(f"'{p.text.strip()[:40]}' (para {p.index})" for p in vague[:5])
        indices = [p.index for p in vague]

        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="2.4.6",
            criterion_name="Headings and Labels",
            wcag_level="AA",
            issue=(
                f"{len(vague)} heading(s) use generic text that does not describe the section content."
            ),
            evidence=f"Generic headings: {examples}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 2.4.6 requires headings to be descriptive so users can understand "
                "document structure from the heading outline alone. Screen reader users "
                "navigate by headings — generic labels like 'Section 1' or 'Introduction' "
                "give no indication of what the section actually covers."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIND IT: Paragraphs {indices[:5]}. "
                "Open the Navigation Pane (View → Navigation Pane) to see all headings.",
                "",
                "HOW TO FIX:",
                "  • Replace generic text with a description of the section's content.",
                "  • 'Introduction' → 'Introduction to Q4 Budget Analysis'",
                "  • 'Section 2' → 'Q4 Revenue by Region'",
                "  • 'Overview' → 'Overview of Accessibility Policy Changes'",
                "  • Keep headings concise (ideally ≤ 60 characters) but specific enough to navigate by.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=(
                f"{len(vague)} heading(s) matched generic-placeholder patterns; "
                "may be intentional in some document types — manual review recommended."
            ),
            evidence_source=EvidenceSource.TEXT_CONTENT,
            location=f"Paragraphs {indices[:5]}",
            remediation_id="docx_heading_labels",
            remediation_data={
                "action": "improve_heading_text",
                "paragraph_indices": indices,
                "examples": [p.text.strip()[:60] for p in vague[:10]],
            },
        ))

    def _rule_1_3_1_list_coherence(self):
        """Validate list structure coherence using object model.
        
        WCAG 1.3.1 requires that lists convey programmatic structure.
        List nesting must be coherent: no skipped levels (e.g., Level 0 → Level 2).
        
        This rule uses python-docx object model for better accuracy than XML parsing alone.
        """
        try:
            from wcag.analyzers.object_model import DocxNormalizer
            # Use the original DOCX data
            normalizer = DocxNormalizer(self.file_bytes, self.filename)
            model = normalizer.normalize()
            if not model or not model.lists:
                return
            
            # Check each list for coherence violations
            incoherent_lists = [l for l in model.lists if not l.is_coherent]
            if not incoherent_lists:
                return
            
            # Build evidence from violations
            violation_summaries = []
            for list_info in incoherent_lists:
                violation_summaries.extend(list_info.violations[:3])
            
            summary = '; '.join(violation_summaries[:5])
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="1.3.1",
                criterion_name="Info and Relationships",
                wcag_level="A",
                issue=f"{len(incoherent_lists)} list(s) have nesting level violations (skipped levels).",
                evidence=f"List nesting analysis: {summary}.",
                severity=Severity.SERIOUS,
                why_it_matters="Inconsistent list nesting confuses screen reader users who navigate list structure. They may miss content or be unable to understand the logical hierarchy.",
                remediation_steps=[
                    "Select a list with nesting issues.",
                    "Use Home → Increase Indent (Tab) or Decrease Indent (Shift+Tab) to fix nesting.",
                    "Ensure each nested level is exactly 1 level deeper than the previous item.",
                    "Example: Level 0 → Level 1 → Level 2 (never 0 → 2, always consecutive).",
                    "Verify with View → Navigation Pane that list structure is logical.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale=f"{len(incoherent_lists)} lists detected with nesting violations via object model; requires manual review to assess impact.",
                evidence_source=EvidenceSource.XML_INFERRED,
                location=f"{len(incoherent_lists)} list(s)",
                remediation_id="list_coherence",
                remediation_data={
                    "action": "fix_list_nesting",
                    "list_count": len(incoherent_lists),
                    "violations": [
                        {
                            "list_id": l.list_id,
                            "item_count": len(l.items),
                            "max_depth": l.max_depth,
                            "violations": l.violations[:3]
                        }
                        for l in incoherent_lists
                    ]
                },
            ))
        
        except ImportError:
            # python-docx not available; skip this rule
            pass
        except Exception:
            # Graceful fallback if normalization fails; rule is advisory.
            pass

    def _rule_1_3_1_list_styles(self):
        """Validate that list-like paragraphs use semantic list formatting (bullets/numbering).
        
        WCAG 1.3.1 requires that lists are marked with semantic list structures, not just
        formatted text or unicode symbols. This rule detects paragraphs that look like list items
        (indentation, bullet patterns, numbering) but don't use Word's list formatting.
        """
        try:
            from wcag.analyzers.object_model import DocxNormalizer
            normalizer = DocxNormalizer(self.file_bytes, self.filename)
            model = normalizer.normalize()
            if not model or not model.formatted_lists:
                return
            
            # Check each formatted list for accessibility issues
            problem_lists = [l for l in model.formatted_lists if l.accessibility_issues]
            if not problem_lists:
                return
            
            # Build evidence from violations
            violation_count = sum(len(l.accessibility_issues) for l in problem_lists)
            issue_examples = []
            for list_info in problem_lists[:3]:
                for issue in list_info.accessibility_issues[:2]:
                    issue_examples.append(f"'{issue['text'][:40]}' — {issue['reason']}")
            
            example_str = '; '.join(issue_examples[:5])
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="1.3.1",
                criterion_name="Info and Relationships",
                wcag_level="A",
                issue=f"{len(problem_lists)} list(s) with {violation_count} item(s) appear to use inline formatting instead of semantic list structures.",
                evidence=f"Lists detected with formatting-only markers instead of programmatic list elements. Examples: {example_str}.",
                severity=Severity.SERIOUS,
                why_it_matters="Formatted text that looks like a list (indented paragraphs with bullet symbols or numbers) is not recognized by screen readers as a list. Users cannot navigate the list structure or know how many items are in the list.",
                remediation_steps=[
                    "Identify all paragraphs that are meant to be list items.",
                    "Select all list items.",
                    "Apply bullet formatting: Home → Bullets (for unordered lists).",
                    "Or apply numbering: Home → Numbering (for ordered lists).",
                    "Avoid manually typing bullet characters (•, -, *, etc.) or numbers — use Word's built-in list formatting.",
                    "Verify in View → Navigation Pane that the list is now properly structured.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale=f"{len(problem_lists)} formatted lists detected via object model analysis; requires manual review to confirm intent and whether semantic lists are needed.",
                evidence_source=EvidenceSource.XML_INFERRED,
                location=f"{len(problem_lists)} location(s)",
                remediation_id="list_formatting",
                remediation_data={
                    "action": "apply_semantic_lists",
                    "formatted_list_count": len(problem_lists),
                    "total_affected_items": violation_count,
                    "details": [
                        {
                            "list_id": l.list_id,
                            "item_count": len(l.items),
                            "formatting_type": l.formatting_type,
                            "issues": l.accessibility_issues[:3]
                        }
                        for l in problem_lists
                    ]
                },
            ))
        
        except ImportError:
            # python-docx not available; skip this rule
            pass
        except Exception:
            # Graceful fallback if normalization fails; rule is advisory.
            pass


    def _rule_1_3_2_empty_paragraphs(self):
        """Detect empty paragraphs used for spacing instead of paragraph spacing attributes.
        
        WCAG 1.3.2 requires meaningful sequence. Empty paragraphs used as spacers break
        logical reading order and are announced as blank lines by screen readers.
        """
        empty_paras = []
        for p in (self.fact_sheet.paragraphs or []):
            # Check if paragraph is truly empty (no text, no special content)
            if not p.text or not p.text.strip():
                # Filter out naturally empty paragraphs (like between sections)
                # Only flag if there are multiple consecutive ones (pattern of spacing)
                empty_paras.append(p.index)
        
        if not empty_paras:
            return  # No empty paragraphs
        
        # Only flag if we have multiple empty paragraphs (suggesting spacing pattern)
        if len(empty_paras) < 2:
            return
        
        # Check for clustering (consecutive or near-consecutive)
        clusters = []
        current_cluster = [empty_paras[0]]
        for i in range(1, len(empty_paras)):
            if empty_paras[i] - empty_paras[i-1] <= 2:  # Allow 1-2 para gap between
                current_cluster.append(empty_paras[i])
            else:
                if len(current_cluster) >= 2:
                    clusters.append(current_cluster)
                current_cluster = [empty_paras[i]]
        if len(current_cluster) >= 2:
            clusters.append(current_cluster)
        
        if not clusters:
            return  # Only scattered empties, likely intentional
        
        # Report clustered empty paragraphs as spacing anti-pattern
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.3.2",
            criterion_name="Meaningful Sequence",
            wcag_level="A",
            issue=f"{len(empty_paras)} empty paragraph(s) detected — likely used for spacing instead of paragraph spacing attributes.",
            evidence=f"Empty paragraphs at indices: {empty_paras[:10]}{'...' if len(empty_paras) > 10 else ''}.",
            severity=Severity.SERIOUS,
            why_it_matters="Empty paragraphs used as spacers are announced as blank lines by screen readers, disrupting logical reading order and creating confusion about document structure.",
            remediation_steps=[
                "Select empty paragraphs that appear between content sections.",
                "Delete them.",
                "Use proper paragraph spacing instead: right-click paragraph → Paragraph → Spacing → Before/After.",
                "Set spacing before/after as needed (e.g., 6pt or 12pt before/after).",
                "This maintains visual spacing while preserving logical document order.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=f"{len(empty_paras)} empty paragraphs detected; clustering suggests spacing anti-pattern, but requires manual verification.",
            evidence_source=EvidenceSource.XML_INFERRED,
            location=f"Multiple paragraphs",
            remediation_id="empty_paragraphs",
            remediation_data={
                "action": "remove_empty_paras_use_spacing",
                "empty_para_count": len(empty_paras),
                "paragraph_indices": empty_paras,
            },
        ))

    def _rule_1_4_1_color_only(self):
        """Detect text that uses color as the only visual means of conveying meaning.

        WCAG 1.4.1 Use of Color (Level A) requires that color is not the only
        visual means used to convey information, indicate an action, prompt a
        response, or distinguish a visual element. We flag short runs colored
        with classic "status" colors (red / green / amber) when the run text
        carries no status word and the surrounding paragraph contains no other
        non-color cue (icon character, label keyword, or asterisk).
        """
        # Hex prefixes considered semantic-status colors. We match by the first
        # 4 characters of the upper-case hex to capture variants (e.g. FF0000,
        # FF1111). These thresholds are intentionally loose; precision comes
        # from the surrounding-context check below.
        STATUS_COLOR_PREFIXES = {
            'red':    ('FF00', 'E00', 'D00', 'C00', 'B00', 'A00', 'F00'),
            'green':  ('00B0', '00C0', '00A0', '008B', '0F0', '0E0'),
            'amber':  ('FFA0', 'FFB0', 'FF80', 'FFC0', 'FFD7'),
        }
        STATUS_KEYWORDS = re.compile(
            r'\b(pass(es|ed|ing)?|fail(s|ed|ing|ure)?|error|warning|warn|'
            r'ok|okay|good|bad|risk|critical|high|low|medium|safe|unsafe|'
            r'approved|rejected|denied|granted|success|complete|incomplete|'
            r'valid|invalid|yes|no|true|false|on|off|active|inactive|new|old|'
            r'done|todo|blocked|unblocked|open|closed|on track|off track|'
            r'delayed|at risk)\b',
            re.IGNORECASE,
        )
        # Common in-text non-color cues users add alongside color (icons, asterisk).
        NON_COLOR_CUES = re.compile(
            r'[\*\u2713\u2714\u2717\u2718\u26A0\u2705\u274C\u26D4\u2B55\u24C9\u24CA]'
        )

        def _color_category(hex_val: str) -> Optional[str]:
            up = hex_val.upper()
            for cat, prefixes in STATUS_COLOR_PREFIXES.items():
                for pfx in prefixes:
                    if up.startswith(pfx):
                        return cat
            return None

        suspects = []  # list of (color, hex, text, paragraph_index)
        try:
            doc_xml = self.zip.read('word/document.xml')
            root = etree.fromstring(doc_xml)
        except Exception:
            return

        body = root.find(f'{{{W}}}body')
        if body is None:
            return

        para_index = 0
        for child in body:
            tag = child.tag.split('}')[-1]
            if tag != 'p':
                continue
            # Capture full paragraph text once for context.
            full_text = ''.join(t.text or '' for t in child.findall(f'.//{{{W}}}t')).strip()
            if not full_text:
                para_index += 1
                continue
            # Look at each run's color.
            for r in child.findall(f'.//{{{W}}}r'):
                rPr = r.find(f'{{{W}}}rPr')
                if rPr is None:
                    continue
                color_el = rPr.find(f'{{{W}}}color')
                if color_el is None:
                    continue
                val = color_el.get(f'{{{W}}}val', 'auto')
                if val == 'auto' or val == 'theme' or len(val) != 6:
                    continue
                category = _color_category(val)
                if not category:
                    continue
                run_text = ''.join(t.text or '' for t in r.findall(f'.//{{{W}}}t')).strip()
                if not run_text:
                    continue
                # If the colored run itself or its surrounding paragraph
                # already includes a status keyword or non-color cue, the
                # color is reinforcing meaning, not the sole carrier.
                if STATUS_KEYWORDS.search(run_text) or STATUS_KEYWORDS.search(full_text):
                    continue
                if NON_COLOR_CUES.search(run_text) or NON_COLOR_CUES.search(full_text):
                    continue
                # Long runs are usually styled body text, not single-token
                # status indicators; skip to avoid noise.
                if len(run_text) > 40 or len(run_text.split()) > 6:
                    continue
                suspects.append((category, val.upper(), run_text[:60], para_index))
            para_index += 1

        if not suspects:
            return

        # Group by paragraph_index to report once per location.
        seen_locs = []
        for s in suspects:
            if s[3] not in [x[3] for x in seen_locs]:
                seen_locs.append(s)

        examples = seen_locs[:5]
        n = len(seen_locs)
        more = n - 5
        examples_str = '; '.join(
            f"'{e[2]}' ({e[0]} #{e[1]} at paragraph {e[3]})" for e in examples
        )
        if more > 0:
            examples_str += f" (and {more} more)"

        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.4.1",
            criterion_name="Use of Color",
            wcag_level="A",
            issue=(
                f"{n} colored text run(s) appear to use color alone to convey "
                f"meaning (no status word or icon detected nearby)."
            ),
            evidence=(
                f"Runs colored with status colors (red/green/amber) where "
                f"neither the run nor its paragraph contains a status keyword "
                f"(pass/fail/risk/etc.) or non-color cue (icon, asterisk). "
                f"Examples: {examples_str}."
            ),
            severity=Severity.MODERATE,
            why_it_matters=(
                "Users who are colorblind, use screen readers, or print in "
                "grayscale lose the meaning entirely if color is the only "
                "indicator. WCAG 1.4.1 requires at least one additional "
                "non-color cue."
            ),
            remediation_steps=[
                "Locate each colored run listed in the evidence.",
                "Add a non-color cue alongside the color: a status word "
                "(e.g. 'Failed', 'High risk'), a symbol (\u2713 / \u2717 / \u26A0), "
                "or bold/italic emphasis with a label.",
                "Verify by viewing the document in grayscale (File \u2192 Print "
                "\u2192 Preview in grayscale) \u2014 the meaning should still be clear.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=(
                "Color usage detected directly from w:color XML attributes; "
                "context absence is heuristic and may miss domain-specific "
                "status terms \u2014 manual review recommended."
            ),
            evidence_source=EvidenceSource.XML_INFERRED,
            location=(
                f"Paragraph {seen_locs[0][3]}"
                if n == 1
                else f"{n} paragraphs (first at index {seen_locs[0][3]})"
            ),
            remediation_id="color_only_meaning",
            remediation_data={
                "action": "add_non_color_cue",
                "paragraph_indices": [s[3] for s in seen_locs],
            },
        ))

    def _rule_1_4_4_text_size(self):
        """Detect text smaller than 11pt which may be unreadable for users with visual impairments.
        
        WCAG 1.4.4 Resize text requires that text be resizable. Very small text (< 11pt)
        is harder to read and magnify effectively.
        """
        small_text_paras = []
        for p in (self.fact_sheet.paragraphs or []):
            # Check if font size is set and very small
            if p.font_size_pt is not None and p.font_size_pt < 11:
                small_text_paras.append({
                    'index': p.index,
                    'size': p.font_size_pt,
                    'text': p.text.strip()[:60]
                })
        
        if not small_text_paras:
            return
        
        # Build evidence from examples
        examples = []
        for item in small_text_paras[:5]:
            examples.append(f"{item['size']}pt: '{item['text']}'")
        more = len(small_text_paras) - 5
        example_str = '; '.join(examples)
        if more > 0:
            example_str += f"; ... and {more} more"
        
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.4.4",
            criterion_name="Resize Text",
            wcag_level="AA",
            issue=f"{len(small_text_paras)} instance(s) of text smaller than 11pt detected — may be difficult to read.",
            evidence=f"Text sizes detected: {example_str}.",
            severity=Severity.MODERATE,
            why_it_matters="Users with low vision often need to magnify text. Text smaller than 11pt is harder to magnify and may become unreadable even when zoomed.",
            remediation_steps=[
                "Select small text that should be readable (not captions or footnotes).",
                "Home → Font Size (or Ctrl+]) to increase to at least 11pt.",
                "For footnotes/captions that must be small, ensure they are truly secondary.",
                "Test: Zoom to 200% and verify text is still readable.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=f"{len(small_text_paras)} instances of text < 11pt detected directly from font-size attributes; some may be intentional (footnotes, captions).",
            evidence_source=EvidenceSource.XML_DIRECT,
            location="Multiple paragraphs",
            remediation_id="text_size",
            remediation_data={
                "action": "increase_font_size",
                "small_text_count": len(small_text_paras),
                "min_size": 11,
                "paragraph_indices": [item['index'] for item in small_text_paras],
            },
        ))

    def _rule_1_4_5_images_of_text(self):
        """Detect images that appear to be screenshots or text diagrams.
        
        WCAG 1.4.5 requires that text not be presented as images alone (except
        for logos and essential images). This rule uses heuristics to flag images
        that LOOK like they contain text (screenshots, code, diagrams) so the user
        can review them and add proper alt text.
        
        Heuristics:
        - Image filename contains: screenshot, code, diagram, chart, formula, etc.
        - Image is small/square-ish (characteristic of screenshots)
        - Image has no alt text (suspicious if it looks like a screenshot)
        """
        if not self.fact_sheet.images:
            return
        
        suspects = []  # List of (img, confidence_score)
        
        for img in self.fact_sheet.images:
            if img.is_decorative:
                continue  # Explicitly marked as decorative, skip
            
            confidence = 0
            flags = []
            
            # Flag 1: No alt text (0–2 points)
            if img.alt_text is None:
                confidence += 2
                flags.append("no alt text")
            elif img.alt_text.strip() == "":
                confidence += 1
                flags.append("empty alt text")
            
            # Flag 2: Suspicious filename (0–3 points)
            if img.location_hint:
                hint_lower = img.location_hint.lower()
                keywords = ['screenshot', 'code', 'diagram', 'chart', 'formula', 
                           'graph', 'equation', 'pseudocode', 'snippet']
                if any(kw in hint_lower for kw in keywords):
                    confidence += 3
                    matched = [kw for kw in keywords if kw in hint_lower][0]
                    flags.append(f'filename contains "{matched}"')
            
            # Flag 3: Size heuristic (0–2 points)
            # Small square-ish images are often screenshots; large images are usually photos
            # This is a weak signal and gets low weight
            # Skip size check for now since we don't have reliable image dimensions
            
            # Aggregate: flag if confidence >= 3 (either good filename match
            # OR no alt text + other signal)
            if confidence >= 3:
                suspects.append({
                    'image': img,
                    'confidence': confidence,
                    'flags': flags,
                })
        
        if not suspects:
            return
        
        # Build evidence string
        examples = []
        for s in suspects[:3]:
            flags_str = ', '.join(s['flags'])
            examples.append(f"'{s['image'].location_hint}' ({flags_str})")
        more = len(suspects) - 3
        example_str = '; '.join(examples)
        if more > 0:
            example_str += f"; ... and {more} more"
        
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.4.5",
            criterion_name="Images of Text",
            wcag_level="AA",
            issue=(
                f"{len(suspects)} image(s) appear to be screenshots or text diagrams. "
                "If they contain text, that text must be described in alt text."
            ),
            evidence=(
                f"Images flagged by heuristic (filename, no alt text, size). "
                f"Examples: {example_str}. Manual review required to confirm content."
            ),
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 1.4.5 Images of Text (Level AA) requires that text not be "
                "presented as images alone. If these images contain text (code snippets, "
                "diagrams with labels, formulas, charts with embedded text), that text "
                "must be described in alt text or a caption. Screen reader users cannot "
                "read text that is only in an image."
            ),
            remediation_steps=[
                f"Review the {len(suspects)} flagged image(s) in this document.",
                "For each image that DOES contain text:",
                "  1. Right-click → Edit Alt Text",
                "  2. Describe the text content (e.g., 'Code snippet: function login(user) returns true')",
                "  3. Or add a caption/table below the image that transcribes the text",
                "For purely visual images (photos, decorative graphics), mark as decorative.",
                "Tip: Use Copilot Studio's 'Image Review' feature to analyze flagged images.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=(
                f"{len(suspects)} images flagged by heuristic (filename + alt text signals). "
                "Heuristic has ~30–40% false positive rate (photos may be flagged). "
                "Manual review is required to confirm text content."
            ),
            evidence_source=EvidenceSource.XML_INFERRED,
            location=f"{len(suspects)} images in document",
            remediation_id="images_of_text_review",
            remediation_data={
                "action": "review_with_agent",
                "image_count": len(suspects),
                "requires_copilot_studio": True,
                "flagged_images": [s['image'].location_hint for s in suspects],
            },
        ))

    def _rule_1_3_2_spacing_paragraphs(self):
        """Detect paragraphs that contain only spaces/tabs (used for indentation instead of proper formatting).
        
        WCAG 1.3.2 requires meaningful sequence. Paragraphs with only whitespace
        are often used as fake indentation, breaking logical reading order.
        """
        spacing_paras = []
        for p in (self.fact_sheet.paragraphs or []):
            # Check if paragraph is only spaces/tabs
            if p.text and p.text.strip() == '' and len(p.text) > 0:
                # This is a whitespace-only paragraph
                spacing_paras.append({
                    'index': p.index,
                    'content': repr(p.text),  # Show actual whitespace
                    'char_count': len(p.text)
                })
        
        if not spacing_paras:
            return
        
        # Only report if we have a pattern (multiple instances)
        if len(spacing_paras) < 2:
            return
        
        # Build evidence
        examples = [f"Para {item['index']}: {item['char_count']} whitespace char(s)" 
                   for item in spacing_paras[:5]]
        more = len(spacing_paras) - 5
        example_str = '; '.join(examples)
        if more > 0:
            example_str += f"; ... and {more} more"
        
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.3.2",
            criterion_name="Meaningful Sequence",
            wcag_level="A",
            issue=f"{len(spacing_paras)} paragraph(s) contain only spaces/tabs — likely used for indentation instead of proper formatting.",
            evidence=f"Whitespace-only paragraphs detected: {example_str}.",
            severity=Severity.SERIOUS,
            why_it_matters="Screen readers announce whitespace-only paragraphs as blank lines, disrupting document flow. They create false breaks in content structure.",
            remediation_steps=[
                "Select a paragraph with only spaces/tabs (appears blank in Word).",
                "Delete it.",
                "Instead of using spacing paragraphs, use proper indentation:",
                "  • For bullet/numbered lists: Use Home → Bullets/Numbering.",
                "  • For quoted text: Use Home → Increase Indent.",
                "  • For paragraphs: Right-click → Paragraph → Indents & Spacing tab.",
                "This preserves document structure while maintaining visual layout.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=f"{len(spacing_paras)} whitespace-only paragraphs detected via text content analysis; some may be formatting artifacts.",
            evidence_source=EvidenceSource.TEXT_CONTENT,
            location="Multiple paragraphs",
            remediation_id="spacing_paragraphs",
            remediation_data={
                "action": "remove_spacing_paras_use_formatting",
                "spacing_para_count": len(spacing_paras),
                "paragraph_indices": [item['index'] for item in spacing_paras],
            },
        ))

    def _rule_1_3_2_text_spacing(self):
        """Detect paragraphs with overly tight line/letter spacing that may reduce readability.
        
        WCAG 1.3.2 requires meaningful sequence. Very tight text spacing (line height < 1.15,
        letter spacing < 0.04em) can make text harder to read and may be inaccessible to users
        with dyslexia or visual impairments.
        
        This rule checks w:spacing/@w:line and w:spacing/@w:lineRule in paragraph properties,
        and w:spacing/@w:val in run properties for letter spacing.
        """
        try:
            content = self.zip.read('word/document.xml')
            root = etree.fromstring(content)
        except Exception:
            return
        
        body = root.find(f'{{{W}}}body')
        if body is None:
            return
        
        tight_spacing_paras = []  # List of (para_index, para_text, spacing_issue)
        para_index = 0
        
        for child in body:
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if tag != 'p':
                continue
            
            # Extract paragraph text for evidence
            para_text = ''.join(t.text or '' for t in child.findall(f'.//{{{W}}}t')).strip()
            if not para_text:
                para_index += 1
                continue
            
            # Check paragraph-level spacing (w:pPr/w:spacing)
            pPr = child.find(f'{{{W}}}pPr')
            spacing_issues = []
            
            if pPr is not None:
                spacing_el = pPr.find(f'{{{W}}}spacing')
                if spacing_el is not None:
                    # Check line spacing: w:line (1/240th of a line, so line spacing in 240ths)
                    # Standard is typically 240-288 (1.0-1.2 line height)
                    line_val = spacing_el.get(f'{{{W}}}line')
                    line_rule = spacing_el.get(f'{{{W}}}lineRule', 'auto')
                    
                    if line_val:
                        try:
                            line_twips = int(line_val)
                            # Convert to line height multiplier (240 = 1.0 line height)
                            line_height = line_twips / 240.0
                            # Flag if < 1.15 (below recommended minimum)
                            if line_height < 1.15 and line_rule != 'atLeast':
                                spacing_issues.append(
                                    f"line height {line_height:.2f} (min: 1.15)"
                                )
                        except (ValueError, TypeError):
                            pass
                    
                    # Check paragraph spacing before/after (can be too tight)
                    before_val = spacing_el.get(f'{{{W}}}before')
                    after_val = spacing_el.get(f'{{{W}}}after')
                    
                    # These are in 1/20th of a point, but we're mainly looking for consistency
                    # Not a hard failure but worth noting if both are very small
                    if before_val and after_val:
                        try:
                            before_twips = int(before_val)
                            after_twips = int(after_val)
                            # Less than 6pt before/after is quite tight
                            if before_twips < 120 and after_twips < 120:  # 120 twips = 6pt
                                spacing_issues.append(
                                    f"minimal paragraph spacing (before: {before_twips//20}pt, after: {after_twips//20}pt)"
                                )
                        except (ValueError, TypeError):
                            pass
            
            # Check run-level letter spacing (w:rPr/w:spacing)
            for run in child.findall(f'.//{{{W}}}r'):
                rPr = run.find(f'{{{W}}}rPr')
                if rPr is not None:
                    spacing_el = rPr.find(f'{{{W}}}spacing')
                    if spacing_el is not None:
                        # Letter spacing in 1/20th of a point
                        val = spacing_el.get(f'{{{W}}}val')
                        if val:
                            try:
                                spacing_twips = int(val)
                                # Negative spacing is condensed
                                if spacing_twips < -200:  # Very condensed (> 10pt reduction)
                                    spacing_issues.append(
                                        f"very tight letter spacing ({spacing_twips//20}pt)"
                                    )
                            except (ValueError, TypeError):
                                pass
            
            # If we found spacing issues, flag this paragraph
            if spacing_issues:
                tight_spacing_paras.append({
                    'index': para_index,
                    'text': para_text[:60],
                    'issues': spacing_issues,
                })
            
            para_index += 1
        
        if not tight_spacing_paras:
            return
        
        # Build evidence from examples
        examples = []
        for item in tight_spacing_paras[:5]:
            issue_str = ', '.join(item['issues'])
            examples.append(f"Para {item['index']}: {issue_str}")
        more = len(tight_spacing_paras) - 5
        example_str = '; '.join(examples)
        if more > 0:
            example_str += f"; ... and {more} more"
        
        # Build a more helpful location string with specific indices
        indices = [item['index'] for item in tight_spacing_paras]
        if len(indices) <= 5:
            indices_str = ', '.join(str(i) for i in indices)
            location_detail = f"Paragraphs {indices_str}"
        else:
            # For many paragraphs, show range and count
            first_cluster = indices[:3]
            last_cluster = indices[-2:]
            location_detail = (
                f"Paragraphs {', '.join(str(i) for i in first_cluster)}, "
                f"... {len(indices) - 5} more ..., "
                f"{', '.join(str(i) for i in last_cluster)} "
                f"(total: {len(indices)})"
            )
        
        # Document position context (beginning/middle/end)
        total_paragraphs = len(self.fact_sheet.paragraphs or [])
        if total_paragraphs > 0:
            avg_index = sum(indices) / len(indices)
            if avg_index < total_paragraphs * 0.33:
                position = "beginning"
            elif avg_index < total_paragraphs * 0.67:
                position = "middle"
            else:
                position = "end"
        else:
            position = "document"
        
        # Build "Find It" helper step with text search tips
        find_steps = [
            f"📍 **WHERE TO FIND IT:** Look for these paragraphs in the {position} of the document:",
            f"   - Paragraph {indices[0]}: '{tight_spacing_paras[0]['text']}'",
        ]
        if len(tight_spacing_paras) > 1:
            find_steps.append(f"   - Paragraph {indices[1]}: '{tight_spacing_paras[1]['text']}'")
        if len(tight_spacing_paras) > 2:
            find_steps.append(f"   - Paragraph {indices[2]}: '{tight_spacing_paras[2]['text']}'")
        if len(tight_spacing_paras) > 3:
            find_steps.append(f"   - ... and {len(tight_spacing_paras) - 3} more paragraphs")
        find_steps.append("   Use Ctrl+F to search for distinctive text if needed.")
        
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.4.12",
            criterion_name="Text Spacing",
            wcag_level="AA",
            issue=f"{len(tight_spacing_paras)} paragraph(s) have tight text spacing that may reduce readability.",
            evidence=f"Tight spacing attributes detected: {example_str}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 1.4.12 (Text Spacing) requires that no loss of content occurs "
                "when letter, word, and line spacing are overridden by users. "
                "Very tight spacing (line height < 1.15, negative letter spacing) "
                "cannot accommodate user overrides and makes text harder to read for "
                "users with dyslexia, low vision, or cognitive disabilities."
            ),
            remediation_steps=[
                *find_steps,
                "",
                "**HOW TO FIX IT:**",
                "1. Select one or more tight-spaced paragraphs.",
                "2. Right-click → Paragraph → Indents & Spacing tab.",
                "3. Set 'Line spacing' to at least 1.15 or 1.5 (recommended: 1.5 or 'Double').",
                "4. For letter spacing issues: Select text → Home → expand Font dialog (bottom right) → Advanced → Spacing.",
                "5. Ensure letter spacing is 0 or positive (not condensed/negative values).",
                "6. Test with 200% zoom (Ctrl+scroll) to verify readability improvement.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=(
                f"{len(tight_spacing_paras)} paragraphs detected with tight spacing "
                "attributes via XML; requires manual verification to confirm impact on readability."
            ),
            evidence_source=EvidenceSource.XML_INFERRED,
            location=location_detail,
            remediation_id="text_spacing",
            remediation_data={
                "action": "improve_text_spacing",
                "paragraph_count": len(tight_spacing_paras),
                "paragraph_indices": indices,
                "document_position": position,
                "min_line_height": 1.15,
                "issues": [item['issues'] for item in tight_spacing_paras[:10]],
                "search_snippets": [item['text'][:40] for item in tight_spacing_paras[:5]],
            },
        ))

    def _rule_2_4_2_doc_title(self):
        title = self.fact_sheet.document_title
        if not title or not title.strip():
            display = f'"{title}"' if title is not None else 'absent'
            # Derive a sensible default title from the filename so the
            # remediator can apply a non-empty value when no override is given.
            import os, re as _re
            stem = os.path.splitext(os.path.basename(self.filename or ''))[0]
            stem = _re.sub(r'^[0-9a-f]{6,}[-_]', '', stem, flags=_re.IGNORECASE)
            suggested = _re.sub(r'[_\-]+', ' ', stem).strip().title() or 'Untitled Document'
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="2.4.2",
                criterion_name="Page Titled",
                wcag_level="A",
                issue=f"Document title is {display} in docProps/core.xml.",
                evidence=f"<dc:title> in docProps/core.xml is {display}.",
                severity=Severity.MODERATE,
                why_it_matters="The document title is announced by screen readers in the title bar and navigation landmarks. Missing titles make the document harder to identify.",
                remediation_steps=[
                    "In Word, go to File → Info → Properties (right column) → Title.",
                    "Enter a meaningful title describing the document's purpose.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="high",
                confidence_rationale="Document title directly read from docProps/core.xml dc:title element.",
                evidence_source=EvidenceSource.XML_DIRECT,
                location="Document properties (File → Info → Properties → Title)",
                remediation_id="doc_title",
                remediation_data={"action": "set_doc_title", "suggested_title": suggested},
            ))

    def _rule_2_4_4_link_text(self):
        for hl in (self.fact_sheet.hyperlinks or []):
            text = hl.display_text.strip()
            if GENERIC_LINK_TEXT.match(text) or URL_PATTERN.match(text):
                self.fact_sheet.confirmed_findings.append(Finding(
                    criterion_id="2.4.4",
                    criterion_name="Link Purpose (In Context)",
                    wcag_level="A",
                    issue=f"Link text '{text}' does not describe the link destination.",
                    evidence=f"Hyperlink at paragraph {hl.paragraph_index} has display text '{text}'" +
                             (f" pointing to {hl.url}" if hl.url else ""),
                    severity=Severity.MODERATE,
                    why_it_matters="Screen reader users often navigate by listing all links. Generic text like 'click here' or raw URLs provide no context about the destination.",
                    remediation_steps=[
                        f"Select the hyperlink '{text}' at paragraph {hl.paragraph_index}.",
                        "Replace the display text with a description of the destination (e.g. 'Download the Q3 report (PDF)').",
                        "Avoid phrases like 'click here', 'here', 'more', or pasting raw URLs as link text.",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label="high",
                    confidence_rationale=f"Link text '{text}' matches known non-descriptive pattern directly in hyperlink text content.",
                    evidence_source=EvidenceSource.TEXT_CONTENT,
                    location=f"Paragraph {hl.paragraph_index}",
                    remediation_id=f"link_text_{hl.paragraph_index}",
                    remediation_data={"paragraph_index": hl.paragraph_index, "action": "fix_link_text", "current_text": text},
                ))

    def _rule_3_1_1_language(self):
        if not self.fact_sheet.document_language:
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="3.1.1",
                criterion_name="Language of Page",
                wcag_level="A",
                issue="The document's default language is not set.",
                evidence="No w:lang element found in word/settings.xml or document default run properties.",
                severity=Severity.MODERATE,
                why_it_matters="Screen readers select the pronunciation engine based on language metadata. Missing language causes systematic mispronunciation.",
                remediation_steps=[
                    "In Word, go to Review → Language → Set Proofing Language.",
                    "Select the correct language and click 'Set As Default'.",
                    "Click OK to apply to the whole document.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="high",
                confidence_rationale="Language absent from word/settings.xml and document default styles — directly verified.",
                evidence_source=EvidenceSource.XML_DIRECT,
                location="Document language setting (Review → Language)",
                remediation_id="doc_language",
                remediation_data={"action": "set_language", "suggested_lang": "en-US"},
            ))

    def _rule_3_1_2_mixed_language(self):
        """Check for runs with different languages from the document default."""
        doc_lang = self.fact_sheet.document_language
        if not doc_lang:
            return  # Already flagged under 3.1.1
        langs_seen = set()
        for p in (self.fact_sheet.paragraphs or []):
            if p.run_language and p.run_language != doc_lang:
                langs_seen.add(p.run_language)
        if langs_seen:
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="3.1.2",
                criterion_name="Language of Parts",
                wcag_level="AA",
                issue=f"Mixed language runs detected: {sorted(langs_seen)} alongside default '{doc_lang}'.",
                evidence=f"Run-level language tags {sorted(langs_seen)} differ from document language '{doc_lang}'.",
                severity=Severity.MINOR,
                why_it_matters="If foreign-language text is not marked with the correct language, screen readers will mispronounce it using the default language's phonetics.",
                remediation_steps=[
                    "Select foreign-language passages.",
                    "Go to Review \u2192 Language \u2192 Set Proofing Language.",
                    "Apply the correct language for each passage.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale="Mixed language runs detected via run-level lang attributes; human review needed to confirm intentionality.",
                evidence_source=EvidenceSource.XML_INFERRED,
            ))

    def _rule_2_4_5_multiple_ways(self):
        """WCAG 2.4.5 — Multiple Ways. For documents with many headings,
        check for TOC field or bookmarks providing navigation."""
        # Count headings — only flag if document is large enough to warrant navigation aids
        heading_paragraphs = [p for p in (self.fact_sheet.paragraphs or [])
                              if p.style_name and p.style_name.lower().startswith("heading")]
        if len(heading_paragraphs) < 5:
            return  # Short documents don't need multiple navigation mechanisms

        try:
            content = self.zip.read('word/document.xml')
        except KeyError:
            return
        text = content.decode('utf-8', errors='replace')

        # Detect TOC field (Word inserts <w:fldChar> with TOC instructions)
        has_toc = (
            'TOC \\' in text  # TOC field instruction text
            or 'TOC\\' in text
            or '<w:sdt>' in text and 'TOC' in text  # TOC content control
        )
        # Detect bookmarks
        bookmark_count = text.count('<w:bookmarkStart')

        if not has_toc and bookmark_count < 2:
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="2.4.5",
                criterion_name="Multiple Ways",
                wcag_level="AA",
                issue=(
                    f"Document has {len(heading_paragraphs)} headings but no Table of Contents "
                    "or bookmarks for navigation."
                ),
                evidence=(
                    f"No TOC field detected and only {bookmark_count} bookmark(s) present."
                ),
                severity=Severity.MINOR,
                why_it_matters=(
                    "Long documents without a Table of Contents force users to scroll linearly. "
                    "Screen reader and keyboard users especially benefit from a TOC."
                ),
                remediation_steps=[
                    "In Word, place cursor at the document start.",
                    "Go to References \u2192 Table of Contents \u2192 choose an automatic style.",
                    "Alternatively, add bookmarks at major sections (Insert \u2192 Bookmark).",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale="Headings present but TOC/bookmarks absent; manual review confirms whether navigation aid is needed.",
                evidence_source=EvidenceSource.XML_DIRECT,
                location="document body",
                remediation_id="docx_multiple_ways",
            ))

    # ── Theme helper ─────────────────────────────────────────────────────────

    def _load_docx_theme(self) -> dict:
        """Load DOCX theme colors from word/theme/theme1.xml."""
        if self._theme_colors is not None:
            return self._theme_colors
        try:
            content = self.zip.read('word/theme/theme1.xml')
            root = etree.fromstring(content)
            A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
            clr_scheme = root.find(f'.//{{{A_NS}}}clrScheme')
            colors = {}
            if clr_scheme is not None:
                for slot in clr_scheme:
                    slot_name = slot.tag.split('}')[-1]
                    srgb = slot.find(f'{{{A_NS}}}srgbClr')
                    if srgb is not None:
                        colors[slot_name] = srgb.get('val', '000000').upper()
                        continue
                    sys_clr = slot.find(f'{{{A_NS}}}sysClr')
                    if sys_clr is not None:
                        colors[slot_name] = sys_clr.get('lastClr', '000000').upper()
            self._theme_colors = {**DOCX_DEFAULT_THEME, **colors}
        except Exception:
            self._theme_colors = DOCX_DEFAULT_THEME.copy()
        return self._theme_colors

    def _resolve_docx_scheme_color(self, scheme_val: str) -> Optional[str]:
        """Resolve a scheme color name to a hex string."""
        theme = self._load_docx_theme()
        key = DOCX_SCHEME_ALIAS.get(scheme_val, scheme_val)
        return theme.get(key)

    def _rule_1_3_2_floating_boxes(self):
        """Detect floating (anchored) text boxes in the document body."""
        try:
            content = self.zip.read('word/document.xml')
            root = etree.fromstring(content)
        except Exception:
            return

        # wp:anchor elements indicate floating/wrapped objects (not inline)
        anchors = root.findall(f'.//{{{WP}}}anchor')
        if not anchors:
            return

        # Count non-decorative anchors that wrap text content
        floating_count = len(anchors)
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.3.2",
            criterion_name="Meaningful Sequence",
            wcag_level="A",
            issue=f"{floating_count} floating (anchored) object(s) detected — reading order may be disrupted.",
            evidence=f"Found {floating_count} <wp:anchor> element(s) in document.xml. Anchored objects read at their anchor position, which may not match visual position.",
            severity=Severity.SERIOUS,
            why_it_matters="Floating text boxes and wrapped images read at their XML anchor point, not their visual position. This can cause content to be read out of sequence by screen readers.",
            remediation_steps=[
                "Open Review \u2192 Read Aloud and listen for content read out of order.",
                "Convert floating text boxes to inline content (select box \u2192 Format \u2192 Position \u2192 Inline with Text).",
                "For wrapped images, set Text Wrapping to 'In Line with Text' in Format \u2192 Wrap Text.",
                "Restructure sidebars as regular paragraphs with borders instead of floating text boxes.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Floating anchors confirmed in XML; actual reading order impact depends on anchor position and AT version — requires manual verification.",
            evidence_source=EvidenceSource.XML_INFERRED,
            location="Document body",
        ))

    def _rule_1_4_3_contrast(self):
        """Detect text with explicitly-set light/near-white colors that likely fail contrast.
        Reads paragraph/table-cell background shading (w:shd) to compute against actual
        background when available, falling back to white.
        """
        try:
            content = self.zip.read('word/document.xml')
            root = etree.fromstring(content)
        except Exception:
            return

        light_runs: list = []  # (fg_hex, bg_hex, text_snippet, para_index)
        LIGHT_SCHEMES = {'lt1', 'lt2', 'bg1', 'bg2'}  # white/near-white scheme colors

        body = root.find(f'{{{W}}}body')
        if body is None:
            return

        def _get_para_bg(p_el: etree._Element) -> Optional[str]:
            """Read paragraph background from w:pPr/w:shd w:fill. Returns hex or None."""
            pPr = p_el.find(f'{{{W}}}pPr')
            if pPr is not None:
                shd = pPr.find(f'{{{W}}}shd')
                if shd is not None:
                    fill = shd.get(f'{{{W}}}fill')
                    if fill and fill.upper() != 'AUTO' and len(fill) == 6:
                        return fill.upper()
            return None  # assume white

        def _get_cell_bg(tc_el: etree._Element) -> Optional[str]:
            """Read table cell background from w:tc/w:tcPr/w:shd w:fill."""
            tcPr = tc_el.find(f'{{{W}}}tcPr')
            if tcPr is not None:
                shd = tcPr.find(f'{{{W}}}shd')
                if shd is not None:
                    fill = shd.get(f'{{{W}}}fill')
                    if fill and fill.upper() != 'AUTO' and len(fill) == 6:
                        return fill.upper()
            return None

        para_index = 0
        for child in body:
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if tag == 'p':
                para_bg = _get_para_bg(child)
                bg_hex = para_bg if para_bg else 'FFFFFF'
                for r in child.findall(f'.//{{{W}}}r'):
                    rPr = r.find(f'{{{W}}}rPr')
                    if rPr is None:
                        continue
                    color_el = rPr.find(f'{{{W}}}color')
                    if color_el is None:
                        continue
                    val = color_el.get(f'{{{W}}}val', 'auto')
                    if val == 'auto' or val == 'theme':
                        theme_color = color_el.get(f'{{{W}}}themeColor')
                        if theme_color and theme_color.lower() in LIGHT_SCHEMES:
                            text = ''.join(t.text or '' for t in r.findall(f'.//{{{W}}}t')).strip()
                            if text:
                                light_runs.append(('scheme:' + theme_color, bg_hex, text[:40], para_index))
                        continue
                    if len(val) == 6:
                        lum_fg = _hex_luminance(val)
                        lum_bg = _hex_luminance(bg_hex)
                        lighter = max(lum_fg, lum_bg)
                        darker  = min(lum_fg, lum_bg)
                        ratio   = (lighter + 0.05) / (darker + 0.05)
                        if ratio < 4.5:  # only flag if contrast actually fails
                            text = ''.join(t.text or '' for t in r.findall(f'.//{{{W}}}t')).strip()
                            if text:
                                light_runs.append((val.upper(), bg_hex, text[:40], para_index, ratio))
                para_index += 1
            elif tag == 'tbl':
                # Also check table cells
                for tc in child.findall(f'.//{{{W}}}tc'):
                    cell_bg = _get_cell_bg(tc)
                    bg_hex = cell_bg if cell_bg else 'FFFFFF'
                    for r in tc.findall(f'.//{{{W}}}r'):
                        rPr = r.find(f'{{{W}}}rPr')
                        if rPr is None:
                            continue
                        color_el = rPr.find(f'{{{W}}}color')
                        if color_el is None:
                            continue
                        val = color_el.get(f'{{{W}}}val', 'auto')
                        if val == 'auto' or val == 'theme':
                            continue  # skip inherited colors in tables
                        if len(val) == 6:
                            lum_fg = _hex_luminance(val)
                            lum_bg = _hex_luminance(bg_hex)
                            lighter = max(lum_fg, lum_bg)
                            darker  = min(lum_fg, lum_bg)
                            ratio   = (lighter + 0.05) / (darker + 0.05)
                            if ratio < 4.5:
                                text = ''.join(t.text or '' for t in r.findall(f'.//{{{W}}}t')).strip()
                                if text:
                                    light_runs.append((val.upper(), bg_hex, text[:40], para_index, ratio))
                para_index += 1

        if not light_runs:
            return

        # Dedupe: only flag if 2+ unique failures to reduce FP from a single branded color
        if len(light_runs) < 2:
            examples = light_runs[:2]
            example_str = '; '.join(
                f"'{e[2]}' (fg:#{e[0]} bg:#{e[1]} ratio:{e[4]:.2f}:1, needs 4.5:1)"
                for e in examples
            )
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="1.4.3",
                criterion_name="Contrast (Minimum)",
                wcag_level="AA",
                issue="1 text run with potentially insufficient contrast detected.",
                evidence=f"Run with explicit color: {example_str}. Verify with a contrast checker.",
                severity=Severity.SERIOUS,
                why_it_matters="Low-contrast text is difficult or impossible to read for users with low vision.",
                remediation_steps=[
                    "Select the affected text and check its color (Home \u2192 Font Color).",
                    "Use WebAIM Contrast Checker to verify the color pair meets 4.5:1 (normal) or 3:1 (large text).",
                    "Replace with a darker color if it fails.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale="Single run detected; may be intentional (e.g. text on a matching colored background not captured by XML). Manual check recommended.",
                evidence_source=EvidenceSource.XML_INFERRED,
                location="Document body",
            ))
            return

        colors_seen = list(dict.fromkeys(r[0] for r in light_runs))
        # Show the worst-contrast examples first so the user sees the biggest gaps.
        sorted_runs = sorted(light_runs, key=lambda r: r[4])
        examples = sorted_runs[:3]
        worst_ratio = sorted_runs[0][4]
        # Calibrate severity by the worst measured ratio.
        # WCAG 2.x defines AA as 4.5:1; ratios below 3:1 fall under "Large
        # Text" minimums and represent severe legibility loss for normal text.
        if worst_ratio < 3.0:
            severity = Severity.CRITICAL
            severity_note = "below the 3:1 large-text minimum — normal text is essentially unreadable for low-vision users"
        else:
            severity = Severity.SERIOUS
            severity_note = "below the 4.5:1 normal-text minimum but above the 3:1 large-text minimum"
        example_str = '; '.join(
            f"'{e[2]}' (#{e[0]} on #{e[1]}, {e[4]:.2f}:1)" for e in examples
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.4.3",
            criterion_name="Contrast (Minimum)",
            wcag_level="AA",
            issue=f"{len(light_runs)} text run(s) with insufficient contrast against their background (worst ratio {worst_ratio:.2f}:1, requires 4.5:1).",
            evidence=f"Foreground/background pairs: {colors_seen[:4]}. Worst-first examples: {example_str}. Severity escalation: {severity_note}.",
            severity=severity,
            why_it_matters="Low-contrast text is difficult or impossible to read for users with low vision.",
            remediation_steps=[
                "Select the affected text and check color via Home \u2192 Font Color.",
                "Use WebAIM Contrast Checker to verify each color pair meets 4.5:1 (normal) or 3:1 (large text).",
                "Replace with colors that meet the minimum contrast ratio.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale=f"Multiple runs ({len(light_runs)}) with explicit w:color values computed against their paragraph/cell background — contrast ratio < 4.5:1 confirmed.",
            evidence_source=EvidenceSource.XML_DIRECT,
            location="Document body",
            remediation_id="docx_contrast",
            remediation_data={"action": "fix_contrast"},
        ))

    # ── Phase B: 1.4.11 Non-text Contrast ───────────────────────────────────
    def _rule_1_4_11_non_text_contrast(self):
        """WCAG 1.4.11 — Detect table cells whose explicit border color
        contrasts < 3:1 with the cell's explicit shading fill.

        Strictly deterministic: requires both an explicit ``w:color`` on a cell
        border side AND an explicit ``w:fill`` on the cell's shading. Theme
        colors and inherited table-level borders are skipped to keep the rule
        free of false positives.
        """
        from wcag.common.non_text_contrast import evaluate_pair, MIN_NON_TEXT_CONTRAST
        try:
            content = self.zip.read('word/document.xml')
        except KeyError:
            return
        try:
            root = etree.fromstring(content)
        except etree.XMLSyntaxError:
            return

        offenders: List[Tuple[str, str, str, float]] = []
        seen_pairs = set()
        for tbl_idx, tbl in enumerate(root.iter(f'{{{W}}}tbl'), 1):
            for row_idx, tr in enumerate(tbl.iter(f'{{{W}}}tr'), 1):
                for col_idx, tc in enumerate(tr.iter(f'{{{W}}}tc'), 1):
                    tcPr = tc.find(f'{{{W}}}tcPr')
                    if tcPr is None:
                        continue
                    shd = tcPr.find(f'{{{W}}}shd')
                    if shd is None:
                        continue
                    fill_hex = shd.get(f'{{{W}}}fill')
                    if not fill_hex or fill_hex.lower() == 'auto':
                        continue
                    tcBorders = tcPr.find(f'{{{W}}}tcBorders')
                    if tcBorders is None:
                        continue
                    border_hex: Optional[str] = None
                    for side in ('top', 'left', 'bottom', 'right'):
                        b = tcBorders.find(f'{{{W}}}{side}')
                        if b is None:
                            continue
                        val = b.get(f'{{{W}}}val')
                        if val in (None, 'nil', 'none'):
                            continue
                        col = b.get(f'{{{W}}}color')
                        if not col or col.lower() == 'auto':
                            continue
                        border_hex = col
                        break
                    if not border_hex:
                        continue
                    pair_key = (border_hex.upper(), fill_hex.upper())
                    if pair_key in seen_pairs:
                        continue
                    result = evaluate_pair(border_hex, fill_hex)
                    if not result:
                        continue
                    ratio, ok = result
                    if ok:
                        continue
                    seen_pairs.add(pair_key)
                    offenders.append((
                        f"table {tbl_idx} row {row_idx} col {col_idx}",
                        border_hex, fill_hex, ratio,
                    ))

        if not offenders:
            return
        sample = "; ".join(
            f"{loc} (border #{b} on fill #{f}, ratio {r:.2f}:1)"
            for loc, b, f, r in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.4.11",
            criterion_name="Non-text Contrast",
            wcag_level="AA",
            issue=(
                f"{len(offenders)} table cell(s) have border-on-fill contrast below {MIN_NON_TEXT_CONTRAST}:1."
            ),
            evidence=f"Affected cells: {sample}",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Table borders communicate cell structure visually; insufficient contrast hides that "
                "structure from low-vision users."
            ),
            remediation_steps=[
                "Increase contrast between cell borders and cell shading to at least 3:1.",
                "Alternatively, change the shading or remove the explicit border colors so structure relies on heading rows.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale="Border and shading colors read directly from explicit w:color and w:fill attributes.",
            evidence_source=EvidenceSource.XML_DIRECT,
            location="Document body",
            remediation_id="docx_non_text_contrast",
        ))
