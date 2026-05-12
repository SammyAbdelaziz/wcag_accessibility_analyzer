"""
PPTX WCAG Analyzer
Reads OOXML directly from a .pptx ZIP to extract structural accessibility facts.
Every finding is tied to a specific XML location — no guessing.

WCAG criteria covered:
  CONFIRMED (xml_direct):
    1.1.1  — images with missing/empty alt text (not marked decorative)
    2.4.2  — slide title is absent, generic, or placeholder text
    3.1.1  — presentation language not set
    1.3.1  — freeform text boxes used where content placeholder expected
    1.3.1  — bullet list levels inverted (e.g. lvl 2→1→0 instead of 0→1→2)
    4.1.2  — no title placeholder on slide

  CONFIRMED (xml_inferred):
    1.3.2  — title shape appears after body content in spTree (reading order)

  POSSIBLE (structural):
    1.4.3  — contrast (colors extracted, ratio computed where theme resolves)
    1.1.1  — background images set via slide master/layout (not in slide XML)
    1.3.2  — complex multi-column layouts (visual order unclear)
    3.1.2  — mixed language runs
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import List, Optional, Tuple, Dict, Any
from lxml import etree

from wcag.models import (
    FactSheet, ShapeInfo, Finding,
    Severity, ConfidenceTier, EvidenceSource, CONFIDENCE_LABEL,
)
from wcag.theme_resolver import ThemeResolver

# XML namespaces
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DC = "http://purl.org/dc/elements/1.1/"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"

# Office 2017 decorative image extension URI
DECORATIVE_URI = "{C183D7F6-B498-43B3-948B-1728B52AA6E4}"
DECORATIVE_NS = "http://schemas.microsoft.com/office/drawing/2017/decorative"

# Patterns for generic link text (mirrors docx_analyzer)
GENERIC_LINK_TEXT = re.compile(
    r'^(click here|click|here|this link|learn more|more|read more|link|url|see here)$',
    re.IGNORECASE
)
URL_PATTERN = re.compile(r'^https?://', re.IGNORECASE)

# Patterns for generic/placeholder slide titles
# NOTE: Keep these narrow enough to avoid FP on real titles.
GENERIC_TITLE_PATTERNS = [
    r"^slide\s*\d*$",
    r"^title\s*\d*$",
    r"^click to (add|edit) title$",
    r"^sample content(\s+to\s+test)?$",  # "Sample Content" OR "Sample Content to Test" — but NOT with extra qualifiers like (Corrected Ver.)
    r"^lorem ipsum",
    r"^untitled\s*$",
    r"^\s*$",
]


def _is_generic_title(text: str) -> bool:
    t = text.strip().lower()
    return any(re.match(p, t) for p in GENERIC_TITLE_PATTERNS)


def _is_decorative(cNvPr: etree._Element) -> bool:
    """Check for Office 2017 decorative extension on a cNvPr element."""
    for child in cNvPr.iter():
        if child.get('uri') == DECORATIVE_URI:
            adec = child.find(f'{{{DECORATIVE_NS}}}decorative')
            if adec is not None and adec.get('val') == '1':
                return True
    return False


def _get_alt_text(cNvPr: etree._Element) -> Optional[str]:
    """
    Returns alt text string, or None if descr attribute is absent.
    Empty string "" means explicitly set to empty (may be intentionally decorative or missing).
    """
    return cNvPr.get('descr')


def _text_from_txBody(txBody: etree._Element) -> str:
    parts = []
    for p in txBody.findall(f'{{{A}}}p'):
        run_text = ''.join(r.text or '' for r in p.findall(f'.//{{{A}}}t'))
        if run_text.strip():
            parts.append(run_text.strip())
    return ' '.join(parts)


def _list_levels_from_txBody(txBody: etree._Element) -> List[int]:
    levels = []
    for p in txBody.findall(f'{{{A}}}p'):
        pPr = p.find(f'{{{A}}}pPr')
        lvl = int(pPr.get('lvl', '0')) if pPr is not None else 0
        # Only include paragraphs that have actual text
        text = ''.join(r.text or '' for r in p.findall(f'.//{{{A}}}t')).strip()
        if text:
            levels.append(lvl)
    return levels


def _get_presentation_language(zip_file: zipfile.ZipFile) -> Optional[str]:
    """Try to read the default presentation language from presentation.xml."""
    try:
        content = zip_file.read('ppt/presentation.xml')
        root = etree.fromstring(content)
        # defaultTextStyle → defRPr → lang (top-level)
        dts = root.find(f'.//{{{P}}}defaultTextStyle')
        if dts is not None:
            defPPr = dts.find(f'{{{A}}}defPPr')
            if defPPr is not None:
                rPr = defPPr.find(f'{{{A}}}defRPr')
                if rPr is not None and rPr.get('lang'):
                    return rPr.get('lang')
            # also check lvl1pPr etc.
            for rPr in dts.findall(f'.//{{{A}}}defRPr'):
                lang = rPr.get('lang')
                if lang:
                    return lang
    except Exception:
        pass
    return None


def _extract_run_colors(txBody: etree._Element) -> List[Tuple[Optional[str], Optional[str]]]:
    """
    Extract (scheme_color_val, hex_color) pairs from all text runs in a txBody.
    Returns list of (scheme_val_or_None, hex_or_None) for each run with text.
    """
    results = []
    for p in txBody.findall(f'{{{A}}}p'):
        for r in p.findall(f'{{{A}}}r'):
            text = ''.join(t.text or '' for t in r.findall(f'{{{A}}}t')).strip()
            if not text:
                continue
            rPr = r.find(f'{{{A}}}rPr')
            if rPr is None:
                results.append((None, None))
                continue
            solidFill = rPr.find(f'{{{A}}}solidFill')
            if solidFill is None:
                results.append((None, None))
                continue
            schemeClr = solidFill.find(f'{{{A}}}schemeClr')
            if schemeClr is not None:
                val = schemeClr.get('val')
                lum_mod = int(schemeClr.find(f'{{{A}}}lumMod').get('val', 100000)) if schemeClr.find(f'{{{A}}}lumMod') is not None else 100000
                lum_off = int(schemeClr.find(f'{{{A}}}lumOff').get('val', 0)) if schemeClr.find(f'{{{A}}}lumOff') is not None else 0
                results.append(('scheme:' + val + f':lm={lum_mod}:lo={lum_off}', None))
                continue
            srgbClr = solidFill.find(f'{{{A}}}srgbClr')
            if srgbClr is not None:
                results.append((None, srgbClr.get('val')))
                continue
            results.append((None, None))
    return results


def _get_document_title(zip_file: zipfile.ZipFile) -> Optional[str]:
    try:
        content = zip_file.read('docProps/core.xml')
        root = etree.fromstring(content)
        title_el = root.find(f'{{{DC}}}title')
        if title_el is not None and title_el.text:
            return title_el.text.strip()
    except Exception:
        pass
    return None


class PptxAnalyzer:
    def __init__(self, file_bytes: bytes, filename: str):
        self.file_bytes = file_bytes
        self.filename = filename
        self.zip = zipfile.ZipFile(io.BytesIO(file_bytes))
        self.theme_resolver = ThemeResolver(self.zip)
        self.fact_sheet = FactSheet(filename=filename, file_type='pptx')

    def analyze(self) -> FactSheet:
        self.fact_sheet.document_title = _get_document_title(self.zip)
        self.fact_sheet.document_language = _get_presentation_language(self.zip)

        slide_files = sorted(
            [n for n in self.zip.namelist() if re.match(r'ppt/slides/slide\d+\.xml$', n)],
            key=lambda x: int(re.search(r'\d+', x).group())
        )
        self.fact_sheet.slide_count = len(slide_files)
        self.fact_sheet.slides = []

        for i, slide_path in enumerate(slide_files, 1):
            shapes = self._analyze_slide(slide_path, i)
            self.fact_sheet.slides.append(shapes)

        self._run_rules()
        return self.fact_sheet

    # ── Slide parsing ────────────────────────────────────────────────────────

    def _analyze_slide(self, slide_path: str, slide_num: int) -> List[ShapeInfo]:
        content = self.zip.read(slide_path)
        root = etree.fromstring(content)
        spTree = root.find(f'.//{{{P}}}spTree')
        if spTree is None:
            return []

        shapes: List[ShapeInfo] = []
        z_order = 0
        for child in spTree:
            # lxml returns a callable (not a string) for comment/PI nodes — skip them
            if not isinstance(child.tag, str):
                continue
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if tag == 'sp':
                s = self._parse_sp(child, z_order, slide_num)
            elif tag == 'pic':
                s = self._parse_pic(child, z_order, slide_num)
            elif tag == 'graphicFrame':
                s = self._parse_graphic_frame(child, z_order, slide_num)
            elif tag in ('grpSp', 'cxnSp'):
                s = self._parse_generic(child, z_order, slide_num, tag)
            else:
                z_order += 1
                continue
            if s:
                shapes.append(s)
            z_order += 1
        return shapes

    def _parse_sp(self, sp: etree._Element, z_order: int, slide_num: int) -> Optional[ShapeInfo]:
        nvSpPr = sp.find(f'{{{P}}}nvSpPr')
        if nvSpPr is None:
            return None
        cNvPr = nvSpPr.find(f'{{{P}}}cNvPr')
        if cNvPr is None:
            return None

        shape_id = int(cNvPr.get('id', 0))
        shape_name = cNvPr.get('name', f'Shape_{shape_id}')
        alt_text = _get_alt_text(cNvPr)
        decorative = _is_decorative(cNvPr)

        # Placeholder type
        nvPr = nvSpPr.find(f'{{{P}}}nvPr')
        ph = nvPr.find(f'{{{P}}}ph') if nvPr is not None else None
        ph_type = ph.get('type', 'body') if ph is not None else None
        # ph present with no type attr = body/content placeholder
        has_ph = ph is not None

        shape_type = 'freeform_text'
        if ph_type == 'title' or ph_type == 'ctrTitle':
            shape_type = 'title'
        elif has_ph:
            shape_type = 'body'

        txBody = sp.find(f'{{{P}}}txBody')
        text = _text_from_txBody(txBody) if txBody is not None else None
        list_levels = _list_levels_from_txBody(txBody) if txBody is not None else None
        has_content = bool(text and text.strip()) if text else False

        return ShapeInfo(
            shape_id=shape_id,
            shape_name=shape_name,
            shape_type=shape_type,
            placeholder_type=ph_type,
            alt_text=alt_text,
            is_decorative=decorative,
            z_order=z_order,
            slide_number=slide_num,
            text_content=text,
            list_levels=list_levels,
            has_content=has_content,
        )

    def _parse_pic(self, pic: etree._Element, z_order: int, slide_num: int) -> Optional[ShapeInfo]:
        nvPicPr = pic.find(f'{{{P}}}nvPicPr')
        if nvPicPr is None:
            return None
        cNvPr = nvPicPr.find(f'{{{P}}}cNvPr')
        if cNvPr is None:
            return None

        shape_id = int(cNvPr.get('id', 0))
        shape_name = cNvPr.get('name', f'Picture_{shape_id}')
        alt_text = _get_alt_text(cNvPr)
        decorative = _is_decorative(cNvPr)

        return ShapeInfo(
            shape_id=shape_id,
            shape_name=shape_name,
            shape_type='image',
            placeholder_type=None,
            alt_text=alt_text,
            is_decorative=decorative,
            z_order=z_order,
            slide_number=slide_num,
            text_content=None,
            list_levels=None,
            has_content=True,
        )

    def _parse_graphic_frame(self, gf: etree._Element, z_order: int, slide_num: int) -> Optional[ShapeInfo]:
        nvGrpSpPr = gf.find(f'{{{P}}}nvGraphicFramePr')
        if nvGrpSpPr is None:
            return None
        cNvPr = nvGrpSpPr.find(f'{{{P}}}cNvPr')
        if cNvPr is None:
            return None
        shape_id = int(cNvPr.get('id', 0))
        shape_name = cNvPr.get('name', f'Frame_{shape_id}')
        alt_text = _get_alt_text(cNvPr)
        decorative = _is_decorative(cNvPr)

        # Determine if chart or table
        graphic = gf.find(f'.//{{{A}}}graphic')
        shape_type = 'chart'
        if graphic is not None:
            graphicData = graphic.find(f'{{{A}}}graphicData')
            if graphicData is not None:
                uri = graphicData.get('uri', '')
                if 'table' in uri.lower():
                    shape_type = 'table'

        return ShapeInfo(
            shape_id=shape_id,
            shape_name=shape_name,
            shape_type=shape_type,
            placeholder_type=None,
            alt_text=alt_text,
            is_decorative=decorative,
            z_order=z_order,
            slide_number=slide_num,
            text_content=None,
            list_levels=None,
            has_content=True,
        )

    def _parse_generic(self, el: etree._Element, z_order: int, slide_num: int, tag: str) -> Optional[ShapeInfo]:
        cNvPr = el.find(f'.//{{{P}}}cNvPr')
        if cNvPr is None:
            return None
        shape_id = int(cNvPr.get('id', 0))
        shape_name = cNvPr.get('name', f'{tag}_{shape_id}')
        return ShapeInfo(
            shape_id=shape_id, shape_name=shape_name,
            shape_type='group', placeholder_type=None,
            alt_text=_get_alt_text(cNvPr), is_decorative=_is_decorative(cNvPr),
            z_order=z_order, slide_number=slide_num,
            text_content=None, list_levels=None,
        )

    # ── Rules engine ─────────────────────────────────────────────────────────

    def _run_rules(self):
        fs = self.fact_sheet
        if not fs.slides:
            return

        slide_files = sorted(
            [n for n in self.zip.namelist() if re.match(r'ppt/slides/slide\d+\.xml$', n)],
            key=lambda x: int(re.search(r'\d+', x).group())
        )
        # Load slide relationship files once for link checking
        slide_rels = {}
        for sf in slide_files:
            slide_name = sf.split('/')[-1]  # e.g. slide1.xml
            rels_path = f'ppt/slides/_rels/{slide_name}.rels'
            try:
                slide_rels[sf] = self.zip.read(rels_path)
            except KeyError:
                slide_rels[sf] = None

        for slide_num, shapes in enumerate(fs.slides, 1):
            loc_prefix = f"Slide {slide_num}"
            self._rule_1_1_1_images(shapes, loc_prefix)
            self._rule_1_4_5_images_of_text(shapes, loc_prefix)
            self._rule_1_3_1_freeform(shapes, loc_prefix)
            self._rule_1_3_1_list_levels(shapes, loc_prefix)
            self._rule_1_3_2_reading_order(shapes, loc_prefix)
            self._rule_2_4_2_slide_title(shapes, loc_prefix, slide_num)
            self._rule_4_1_2_no_title_placeholder(shapes, loc_prefix)
            self._rule_1_1_1_charts(shapes, loc_prefix)
            # Rules that need raw slide XML
            if slide_num <= len(slide_files):
                sf = slide_files[slide_num - 1]
                self._rule_1_4_1_color_only(sf, loc_prefix, slide_num)
                self._rule_1_4_3_contrast(sf, loc_prefix, slide_num)
                self._rule_1_4_4_resize_text(sf, loc_prefix, slide_num)
                self._rule_3_1_2_language_of_parts(sf, loc_prefix, slide_num)
                self._rule_1_4_11_non_text_contrast(sf, loc_prefix, slide_num)
                self._rule_4_1_2_actionable_shape_names(sf, loc_prefix, slide_num)  # Phase E
                self._rule_4_1_2_generic_picture_names(sf, loc_prefix, slide_num)  # Phase M-refinements R4
        self._rule_2_4_2_presentation_title()
        self._rule_1_1_1_background_possible()
        self._rule_2_4_6_slide_title_quality()  # Phase A
        self._rule_1_3_3_sensory_characteristics()  # Phase I
        self._rule_2_2_1_auto_advance_slides()  # Phase K

    def _rule_1_3_3_sensory_characteristics(self):
        """WCAG 1.3.3 — flag slide text that references UI elements only by
        color/shape/position. Strict regex on per-shape text content."""
        from wcag.common.sensory_characteristics import find_sensory_phrases
        slides = self.fact_sheet.slides or []
        if not slides:
            return
        items: List[Tuple[str, str]] = []  # (location_label, text)
        for slide_num, shapes in enumerate(slides, 1):
            for s in shapes:
                text = (getattr(s, "text_content", None) or "").strip()
                if text:
                    items.append((f"Slide {slide_num}", text))
        if not items:
            return
        offenders = find_sensory_phrases(items)
        if not offenders:
            return
        sample = "; ".join(
            f"{o['kind']}-only @ {o['index']}: \"{o['snippet']}\""
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.3.3",
            criterion_name="Sensory Characteristics",
            wcag_level="A",
            issue=(
                f"{len(offenders)} slide text block(s) reference UI elements only "
                "by color, shape, or position."
            ),
            evidence=f"Sensory-only references: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "Audience members who are blind, color-blind, or following along with a "
                "screen reader cannot identify a control or section by its visual appearance "
                "or position alone. Reference items by their name or label."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} slide text block(s).",
                "  • Add the named element: 'Click the red Submit button' instead of 'click the red button'.",
                "  • Combine color/position with text: 'See the chart titled Q3 Revenue (right side)'.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale="Slide text content scanned for sensory-only phrase patterns.",
            evidence_source=EvidenceSource.TEXT_CONTENT,
            location=f"{len(offenders)} text block(s)",
            remediation_id="pptx_sensory_characteristics",
            remediation_data={"references": offenders},
        ))

    def _rule_2_2_1_auto_advance_slides(self):
        """WCAG 2.2.1 Timing Adjustable — slides that auto-advance after a
        fixed time can rush users who need more time to read. We flag any
        slide whose <p:transition advTm='N'> is present (auto-advance).
        Strict: the advTm attribute is the unambiguous signal in PPTX.
        """
        slide_files = sorted(
            [n for n in self.zip.namelist() if re.match(r'ppt/slides/slide\d+\.xml$', n)],
            key=lambda x: int(re.search(r'\d+', x).group())
        )
        if not slide_files:
            return
        offenders: List[Dict[str, Any]] = []
        for sf in slide_files:
            try:
                content = self.zip.read(sf)
                root = etree.fromstring(content)
            except (KeyError, etree.XMLSyntaxError):
                continue
            transition = root.find(f'.//{{{P}}}transition')
            if transition is None:
                continue
            advTm = transition.get('advTm')
            if advTm:
                try:
                    ms = int(advTm)
                except ValueError:
                    continue
                slide_num = int(re.search(r'\d+', sf).group())
                offenders.append({
                    "slide": slide_num,
                    "advance_ms": ms,
                    "advance_seconds": round(ms / 1000.0, 2),
                })
        if not offenders:
            return
        sample = "; ".join(
            f"slide {o['slide']} auto-advances after {o['advance_seconds']}s"
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.2.1",
            criterion_name="Timing Adjustable",
            wcag_level="A",
            issue=(
                f"{len(offenders)} slide(s) auto-advance after a fixed time, "
                "with no per-user pause/extend control."
            ),
            evidence=f"Auto-advancing slides: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Audience members who read more slowly, use a screen reader, or simply "
                "need to absorb dense content cannot keep up if slides advance on a fixed "
                "timer. WCAG 2.2.1 requires the user to be able to turn off, adjust, or "
                "extend any time limit (with limited exceptions for synchronised media)."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} slide(s) with auto-advance enabled.",
                "  • PowerPoint: Transitions tab → uncheck 'After: ___ seconds'.",
                "  • Use 'On Mouse Click' (or keyboard advance) so the presenter or user controls pacing.",
                "  • If timing is required (e.g., kiosk mode), provide a clearly visible pause / replay control.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label="high",
            confidence_rationale="<p:transition advTm='…'> attribute parsed directly from each slide XML.",
            evidence_source=EvidenceSource.XML_DIRECT,
            location=f"{len(offenders)} slide(s)",
            remediation_id="pptx_auto_advance_slides",
            remediation_data={"slides": offenders},
        ))

    def _rule_1_1_1_images(self, shapes: List[ShapeInfo], loc: str):
        for s in shapes:
            if s.shape_type not in ('image',):
                continue
            if s.is_decorative:
                continue
            location = f"{loc} — '{s.shape_name}'"
            advisory = self._build_alt_text_advisory(s, shapes)
            if s.alt_text is None:
                # descr attribute completely absent
                self.fact_sheet.confirmed_findings.append(Finding(
                    criterion_id="1.1.1",
                    criterion_name="Non-text Content",
                    wcag_level="A",
                    issue=f"Image '{s.shape_name}' has no alt text attribute.",
                    evidence=f"<p:pic> element '{s.shape_name}' (id={s.shape_id}) has no 'descr' attribute on cNvPr — alt text is completely absent.",
                    severity=Severity.CRITICAL,
                    why_it_matters="Screen reader users receive no information about this image — complete information loss.",
                    remediation_steps=[
                        f"Right-click the image '{s.shape_name}' in PowerPoint.",
                        "Select 'Edit Alt Text...'",
                        "Write a meaningful description (e.g. 'Bar chart showing Q3 revenue by region').",
                        "If the image is decorative, check 'Mark as decorative' instead.",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label="high",
                    confidence_rationale="The descr attribute is absent from the cNvPr XML element — directly verified.",
                    evidence_source=EvidenceSource.XML_DIRECT,
                    location=location,
                    remediation_id=f"alt_text_{s.slide_number}_{s.shape_id}",
                    remediation_data={"slide": s.slide_number, "shape_id": s.shape_id, "shape_name": s.shape_name, "action": "set_alt_text"},
                    advisory_payload=advisory,
                ))
            elif s.alt_text.strip() == '':
                # Explicitly empty — may be intended as decorative or is missing
                self.fact_sheet.confirmed_findings.append(Finding(
                    criterion_id="1.1.1",
                    criterion_name="Non-text Content",
                    wcag_level="A",
                    issue=f"Image '{s.shape_name}' has an empty alt text string.",
                    evidence=f"<p:pic> '{s.shape_name}' has descr=\"\" — empty string without decorative marking.",
                    severity=Severity.CRITICAL,
                    why_it_matters="An empty alt text without a decorative marker means the image is skipped by screen readers with no indication of intent.",
                    remediation_steps=[
                        f"Right-click image '{s.shape_name}' and select 'Edit Alt Text...'",
                        "Either add a meaningful description, or check 'Mark as decorative' if it adds no information.",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label="high",
                    confidence_rationale="Empty descr attribute confirmed directly in XML — not marked decorative.",
                    evidence_source=EvidenceSource.XML_DIRECT,
                    location=location,
                    remediation_id=f"alt_text_{s.slide_number}_{s.shape_id}",
                    remediation_data={"slide": s.slide_number, "shape_id": s.shape_id, "shape_name": s.shape_name, "action": "set_alt_text"},
                    advisory_payload=advisory,
                ))

    def _build_alt_text_advisory(self, shape: ShapeInfo, sibling_shapes: List[ShapeInfo]):
        """Phase J — build advisory_payload for an image missing alt text on
        a slide. Context = slide title + concatenated text from text-bearing
        sibling shapes on the same slide.
        """
        context_parts = []
        for sib in sibling_shapes:
            if sib.shape_id == shape.shape_id:
                continue
            if sib.shape_type in ('title',) and sib.text_content:
                context_parts.insert(0, f"Slide title: {sib.text_content.strip()}")
            elif sib.text_content and sib.text_content.strip():
                context_parts.append(sib.text_content.strip())
        ctx_text = " | ".join(context_parts)[:1000]
        return {
            "advisory_kind": "alt_text",
            "target": f"slide{shape.slide_number}/shape{shape.shape_id}",
            "surface_text": "" if shape.alt_text is None else shape.alt_text,
            "context": ctx_text,
            "format_hint": "pptx",
        }

    def _rule_1_1_1_charts(self, shapes: List[ShapeInfo], loc: str):
        for s in shapes:
            if s.shape_type != 'chart':
                continue
            if s.is_decorative:
                continue
            location = f"{loc} — '{s.shape_name}'"
            if s.alt_text is None or s.alt_text.strip() == '':
                self.fact_sheet.confirmed_findings.append(Finding(
                    criterion_id="1.1.1",
                    criterion_name="Non-text Content",
                    wcag_level="A",
                    issue=f"Chart '{s.shape_name}' has no alt text.",
                    evidence=f"graphicFrame '{s.shape_name}' (id={s.shape_id}) has no descriptive alt text on cNvPr.",
                    severity=Severity.CRITICAL,
                    why_it_matters="Chart data is invisible to screen reader users without a text alternative describing the trend or key values.",
                    remediation_steps=[
                        f"Right-click chart '{s.shape_name}' → 'Edit Alt Text...'",
                        "Describe the data trend, not the chart type (e.g. 'Revenue grew 23% Q1–Q3 2025, with Q3 highest at $4.2M').",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label="high",
                    confidence_rationale="Missing or empty alt text confirmed directly on graphicFrame cNvPr element.",
                    evidence_source=EvidenceSource.XML_DIRECT,
                    location=location,
                    remediation_id=f"alt_text_{s.slide_number}_{s.shape_id}",
                    remediation_data={"slide": s.slide_number, "shape_id": s.shape_id, "action": "set_alt_text"},
                ))

    def _rule_1_3_1_freeform(self, shapes: List[ShapeInfo], loc: str):
        """Freeform text boxes with substantive content that should be in content placeholders."""
        freeform_with_text = [s for s in shapes if s.shape_type == 'freeform_text' and s.has_content]
        if not freeform_with_text:
            return
        for s in freeform_with_text:
            snippet = (s.text_content or '')[:80]
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="1.3.1",
                criterion_name="Info and Relationships",
                wcag_level="A",
                issue=f"Text in '{s.shape_name}' is in a freeform text box, not a content placeholder.",
                evidence=f"Shape '{s.shape_name}' has no <p:ph> placeholder element — text '{snippet}' is in a manually inserted text box.",
                severity=Severity.SERIOUS,
                why_it_matters="Text in freeform boxes may not be picked up correctly by screen readers and is not included in the slide's semantic structure.",
                remediation_steps=[
                    "Delete this freeform text box.",
                    "Instead, use a slide layout that includes a content placeholder (Insert → New Slide → choose a layout with Title and Content).",
                    "Paste the text into the content placeholder.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale="Absence of placeholder element is confirmed in XML; AT behavior with freeform boxes varies and requires testing.",
                evidence_source=EvidenceSource.XML_INFERRED,
                location=f"{loc} — '{s.shape_name}'",
            ))

    def _rule_1_3_1_list_levels(self, shapes: List[ShapeInfo], loc: str):
        """Detect inverted or illogical bullet list level sequences.

        Two cases caught:
          1. Purely inverted: entire sequence strictly decreasing e.g. [2,1,0]
          2. Sub-inverted: intro text at level 0, then levels go HIGH before LOW,
             meaning a child bullet (higher level) precedes its parent (lower level).
             e.g. [0, 2, 1, 0] — 'Bullet Point 1' at lvl=2 before 'Bullet Point 3' at lvl=0.
        """
        for s in shapes:
            if not s.list_levels or len(s.list_levels) < 2:
                continue
            levels = s.list_levels

            # Case 1: whole sequence strictly decreasing
            purely_inverted = all(levels[i] > levels[i + 1] for i in range(len(levels) - 1))

            # Case 2: level returns DOWN to a previously-seen level from a higher level,
            # meaning a deeper-indented item appeared before a shallower one.
            has_inverted_sub = False
            if not purely_inverted and len(levels) >= 3:
                seen = set()
                for i, lvl in enumerate(levels):
                    if lvl in seen and i > 0 and levels[i - 1] > lvl:
                        has_inverted_sub = True
                        break
                    seen.add(lvl)

            if not purely_inverted and not has_inverted_sub:
                continue

            inversion_desc = (
                "The entire bullet sequence is inverted (most-indented item first)"
                if purely_inverted
                else f"Sub-bullets appear before their parent items in the sequence"
            )
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="1.3.1",
                criterion_name="Info and Relationships",
                wcag_level="A",
                issue=f"Bullet list in '{s.shape_name}' has inverted indentation levels.",
                evidence=f"Paragraph list levels are {levels} — {inversion_desc}. WCAG expects parent items (lower level) before their children (higher level).",
                severity=Severity.SERIOUS,
                why_it_matters="Screen readers announce list nesting based on level values. An inverted hierarchy tells users sub-items come before parent items, reversing the logical reading order.",
                remediation_steps=[
                    f"Select the bullet list in slide shape '{s.shape_name}'.",
                    "Use Tab / Shift+Tab to adjust indent levels so main points (level 0) appear first, sub-bullets (level 1+) indented after.",
                    "Verify the final order in Outline View (View → Outline View).",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="high",
                confidence_rationale=f"List level sequence {levels} directly read from <a:pPr lvl> attributes — confirms parent/child inversion.",
                evidence_source=EvidenceSource.XML_DIRECT,
                location=f"{loc} — '{s.shape_name}'",
                remediation_id=f"list_levels_{s.slide_number}_{s.shape_id}",
                remediation_data={"slide": s.slide_number, "shape_id": s.shape_id, "action": "fix_list_levels", "current_levels": levels},
            ))

    def _rule_1_3_2_reading_order(self, shapes: List[ShapeInfo], loc: str):
        """Check if title placeholder appears after body content in z-order."""
        title_shapes = [s for s in shapes if s.shape_type == 'title' and s.has_content]
        body_shapes = [s for s in shapes if s.shape_type == 'body' and s.has_content]
        if not title_shapes or not body_shapes:
            return
        title_z = min(s.z_order for s in title_shapes)
        body_z_min = min(s.z_order for s in body_shapes)
        if title_z > body_z_min:
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="1.3.2",
                criterion_name="Meaningful Sequence",
                wcag_level="A",
                issue="Title placeholder appears after body content in the slide's reading order.",
                evidence=f"Title shape z-order is {title_z}; earliest body shape z-order is {body_z_min}. In PowerPoint, lower z-order = read first by screen readers.",
                severity=Severity.SERIOUS,
                why_it_matters="Screen readers will announce body content before the slide title, reversing the logical reading sequence.",
                remediation_steps=[
                    "Open Home → Arrange → Selection Pane.",
                    "Drag the title shape to the bottom of the list (bottom = read first in screen readers).",
                    "Verify order: Title → Body → Other elements.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="medium",
                confidence_rationale="Z-order derived from element position in <p:spTree>; AT reading order behavior is consistent for most screen readers.",
                evidence_source=EvidenceSource.XML_INFERRED,
                location=loc,
                remediation_id=f"reading_order_{shapes[0].slide_number}",
                remediation_data={"slide": shapes[0].slide_number, "action": "fix_reading_order"},
            ))

    def _rule_2_4_2_slide_title(self, shapes: List[ShapeInfo], loc: str, slide_num: int):
        title_shapes = [s for s in shapes if s.shape_type == 'title']
        if not title_shapes:
            return  # Handled by 4.1.2
        for s in title_shapes:
            text = (s.text_content or '').strip()
            if not text:
                self.fact_sheet.confirmed_findings.append(Finding(
                    criterion_id="2.4.2",
                    criterion_name="Page Titled",
                    wcag_level="A",
                    issue=f"Slide {slide_num} has an empty title placeholder.",
                    evidence=f"Title placeholder '{s.shape_name}' contains no text.",
                    severity=Severity.MODERATE,
                    why_it_matters="Screen reader users cannot identify the slide's purpose without a descriptive title.",
                    remediation_steps=[
                        f"Click the title placeholder on slide {slide_num}.",
                        "Type a meaningful, descriptive title that summarizes the slide's content.",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label="high",
                    confidence_rationale="Empty title confirmed directly from title placeholder text content.",
                    evidence_source=EvidenceSource.XML_DIRECT,
                    location=loc,
                    remediation_id=f"slide_title_{slide_num}",
                    remediation_data={"slide": slide_num, "action": "fix_slide_title"},
                ))
            elif _is_generic_title(text):
                self.fact_sheet.confirmed_findings.append(Finding(
                    criterion_id="2.4.2",
                    criterion_name="Page Titled",
                    wcag_level="A",
                    issue=f"Slide {slide_num} title is generic placeholder text: '{text}'",
                    evidence=f"Title placeholder reads \"{text}\" — matches generic/placeholder title pattern.",
                    severity=Severity.MODERATE,
                    why_it_matters="Generic titles do not communicate the slide's purpose. Screen readers announce the title as the slide identifier.",
                    remediation_steps=[
                        f"Replace \"{text}\" with a descriptive title summarizing slide {slide_num}'s content.",
                        "Verify via View → Outline View that the title is in the title placeholder.",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label="high",
                    confidence_rationale=f"Title text '{text}' directly matches known generic placeholder patterns.",
                    evidence_source=EvidenceSource.TEXT_CONTENT,
                    location=loc,
                    remediation_id=f"slide_title_{slide_num}",
                    remediation_data={"slide": slide_num, "action": "fix_slide_title", "current_text": text},
                ))

    def _rule_4_1_2_no_title_placeholder(self, shapes: List[ShapeInfo], loc: str):
        title_shapes = [s for s in shapes if s.shape_type == 'title']
        if not title_shapes:
            slide_num = shapes[0].slide_number if shapes else 0
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="4.1.2",
                criterion_name="Name, Role, Value",
                wcag_level="A",
                issue=f"Slide {slide_num} has no title placeholder.",
                evidence="No <p:ph type=\"title\"> or <p:ph type=\"ctrTitle\"> element found in slide spTree.",
                severity=Severity.MODERATE,
                why_it_matters="Without a title placeholder, the slide has no programmatic name. Screen readers cannot identify or navigate to this slide by title.",
                remediation_steps=[
                    "Change the slide layout to one that includes a title placeholder (Home → Layout → choose a layout with 'Title').",
                    "Alternatively, Insert → Text Box, then set it as the title via the slide's XML (not recommended — use a layout instead).",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="high",
                confidence_rationale="Absence of title placeholder element confirmed directly in slide XML.",
                evidence_source=EvidenceSource.XML_DIRECT,
                location=loc,
            ))

    def _rule_3_1_1_language(self):
        lang = self.fact_sheet.document_language
        if not lang:
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="3.1.1",
                criterion_name="Language of Page",
                wcag_level="A",
                issue="The presentation's default language is not set.",
                evidence="No lang attribute found on defaultTextStyle defRPr in presentation.xml.",
                severity=Severity.MODERATE,
                why_it_matters="Screen readers use the language setting to select the correct pronunciation engine. Missing language causes mispronunciation.",
                remediation_steps=[
                    "In PowerPoint, go to Review → Language → Set Proofing Language.",
                    "Select the correct language (e.g. English United States).",
                    "Check 'Set As Default' and apply to all slides.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="high",
                confidence_rationale="Language attribute absent from presentation.xml defaultTextStyle — directly verified.",
                evidence_source=EvidenceSource.XML_DIRECT,
                remediation_id="presentation_language",
                remediation_data={"action": "set_language", "suggested_lang": "en-US"},
            ))

    def _rule_2_4_2_presentation_title(self):
        title = self.fact_sheet.document_title
        if title is None or title.strip() == "":
            # Title is provably absent from core.xml — XML_DIRECT, CONFIRMED
            display = "absent" if title is None else '""'
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="2.4.2",
                criterion_name="Page Titled",
                wcag_level="A",
                issue=f"Presentation document title is {display}.",
                evidence=f"docProps/core.xml <dc:title> is {display}. Screen readers read this as the window title.",
                severity=Severity.MODERATE,
                why_it_matters="The presentation title appears in screen reader window titles and document navigation. Without it, users cannot identify the document.",
                remediation_steps=[
                    "In PowerPoint, go to File → Info → Properties → Title.",
                    "Set a meaningful title describing the presentation's purpose.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="high",
                confidence_rationale=f"<dc:title> is {display} in docProps/core.xml — directly verifiable from OOXML.",
                evidence_source=EvidenceSource.XML_DIRECT,
                remediation_id="presentation_doc_title",
                remediation_data={"action": "set_doc_title"},
            ))
        elif _is_generic_title(title):
            # Title is present but is a generic placeholder — heuristic, keep as POSSIBLE
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="2.4.2",
                criterion_name="Page Titled",
                wcag_level="A",
                issue=f'Presentation document title appears generic: "{title}".',
                evidence=f'docProps/core.xml <dc:title> is "{title}" — matches a known generic/placeholder title pattern.',
                severity=Severity.MINOR,
                why_it_matters="A generic title does not help users identify the presentation in screen reader window lists.",
                remediation_steps=[
                    "In PowerPoint, go to File → Info → Properties → Title.",
                    "Replace the placeholder with a title that describes the presentation's purpose.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale="Title text matched a generic pattern — may be intentional, requires human review.",
                evidence_source=EvidenceSource.TEXT_CONTENT,
                remediation_id="presentation_doc_title",
                remediation_data={"action": "set_doc_title"},
            ))

    def _rule_1_4_1_color_only(self, slide_path: str, loc: str, slide_num: int):
        """WCAG 1.4.1 Use of Color (A Level)
        
        Detect colored text that appears to use color alone to convey meaning,
        without additional non-color cues (text labels, icons, patterns, etc.).
        Common case: status indicators in red/green/amber without labels.
        """
        import re
        P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
        A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
        
        STATUS_KEYWORDS = re.compile(
            r'\b(pass|fail|success|error|warning|alert|critical|high|medium|low|red|green|amber|yellow|ok|done|pending|blocked|active|inactive)\b',
            re.IGNORECASE
        )
        NON_COLOR_CUES = re.compile(
            r'[✓✗✔✘●■▲★†‡❌⛔👍👎]|\(\s*[✓✗]\s*\)'
        )
        STATUS_COLOR_PREFIXES = {
            'red': ['#ff', '#f0', '#e0', '#d0'],
            'green': ['#0f', '#00ff', '#00e', '#0d0', '#0a0'],
            'amber': ['#ffa', '#ff8', '#ff6', '#f90'],
        }
        
        def _category(hex_val):
            """Categorize a hex color as red, green, or amber."""
            hex_upper = hex_val.upper()
            for cat, prefixes in STATUS_COLOR_PREFIXES.items():
                if any(hex_upper.startswith(p) for p in prefixes):
                    return cat
            return None

        try:
            content = self.zip.read(slide_path)
            root = etree.fromstring(content)
        except Exception:
            return

        suspects = []
        for txBody in root.findall(f'.//{{{P}}}txBody'):
            parent = txBody.getparent()
            cNvPr = parent.find(f'.//{{{P}}}cNvPr') if parent is not None else None
            shape_name = cNvPr.get('name', 'unknown') if cNvPr is not None else 'unknown'

            for p in txBody.findall(f'{{{A}}}p'):
                full_text = ''.join(t.text or '' for t in p.findall(f'.//{{{A}}}t')).strip()
                if not full_text:
                    continue
                for r in p.findall(f'{{{A}}}r'):
                    rPr = r.find(f'{{{A}}}rPr')
                    if rPr is None:
                        continue
                    solidFill = rPr.find(f'{{{A}}}solidFill')
                    if solidFill is None:
                        continue
                    srgbClr = solidFill.find(f'{{{A}}}srgbClr')
                    if srgbClr is None:
                        continue  # skip scheme colors
                    hex_val = srgbClr.get('val', '')
                    if len(hex_val) != 6:
                        continue
                    category = _category(hex_val)
                    if not category:
                        continue  # not a status color
                    run_text = ''.join(t.text or '' for t in r.findall(f'{{{A}}}t')).strip()
                    if not run_text:
                        continue
                    # Check if color is the sole carrier of meaning
                    if STATUS_KEYWORDS.search(run_text) or STATUS_KEYWORDS.search(full_text):
                        continue  # text itself indicates status
                    if NON_COLOR_CUES.search(run_text) or NON_COLOR_CUES.search(full_text):
                        continue  # has non-color cue
                    if len(run_text) > 40 or len(run_text.split()) > 6:
                        continue  # too long to be a status indicator
                    suspects.append((category, hex_val.upper(), run_text[:60], shape_name))

        if not suspects:
            return

        examples = suspects[:5]
        more = len(suspects) - 5
        examples_str = '; '.join(
            f"'{e[2]}' in '{e[3]}' ({e[0]} #{e[1]})" for e in examples
        )
        if more > 0:
            examples_str += f" (and {more} more)"
        
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.4.1",
            criterion_name="Use of Color",
            wcag_level="A",
            issue=(
                f"{len(suspects)} colored text run(s) on {loc} "
                "appear to use color alone to convey meaning."
            ),
            evidence=(
                f"Runs colored with status colors (red/green/amber) where "
                f"neither the run nor its paragraph contains a status keyword "
                f"or non-color cue. Examples: {examples_str}."
            ),
            severity=Severity.MODERATE,
            why_it_matters=(
                "Users who are colorblind, use screen readers, or print in "
                "grayscale lose the meaning entirely if color is the only "
                "indicator. WCAG 1.4.1 requires at least one additional "
                "non-color cue."
            ),
            remediation_steps=[
                f"Locate the colored runs on {loc} listed in the evidence.",
                "Add a non-color cue: a status word (e.g. 'Failed', 'High risk'), "
                "a symbol (✓ / ✗ / ⛔), or bold/italic emphasis with a label.",
                "Verify by viewing the slide in grayscale (View → Grayscale) — "
                "the meaning should still be clear.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=(
                "Color usage detected directly from a:srgbClr XML attributes; "
                "context absence is heuristic and may miss domain-specific "
                "status terms — manual review recommended."
            ),
            evidence_source=EvidenceSource.XML_INFERRED,
            location=loc,
            remediation_id=f"color_only_meaning_{slide_num}",
            remediation_data={
                "slide": slide_num,
                "action": "add_non_color_cue",
                "shapes": list(dict.fromkeys(s[3] for s in suspects)),
            },
        ))

    def _rule_1_4_3_contrast(self, slide_path: str, loc: str, slide_num: int):
        """
        Detect contrast failures using the ThemeResolver for full scheme color support.
        Strategy:
          1. Collect (fg_scheme_or_hex, bg_scheme_or_hex) pairs per run.
          2. Resolve both through ThemeResolver to get hex values.
          3. For runs where we can't determine background, assume white (most common slide bg).
          4. Compute WCAG contrast ratio; flag if < 4.5:1 (< 3:1 for large text).
          5. Also flag runs using known white/light scheme colors regardless of bg (catches the
             most common case: white text on any background).
        """
        try:
            content = self.zip.read(slide_path)
            root = etree.fromstring(content)
        except Exception:
            return

        A_NS = A  # module-level constant
        # Scheme colors that are white/near-white in standard Office themes
        WHITE_SCHEMES = {'bg1', 'lt1', 'bg2', 'lt2'}
        DARK_SCHEMES = {'tx1', 'dk1', 'tx2', 'dk2'}

        shape_issues = []

        for txBody in root.findall(f'.//{{{P}}}txBody'):
            parent = txBody.getparent()
            cNvPr = None
            if parent is not None:
                cNvPr = parent.find(f'.//{{{P}}}cNvPr')
            shape_name = cNvPr.get('name', 'unknown') if cNvPr is not None else 'unknown'

            for p in txBody.findall(f'{{{A_NS}}}p'):
                # Try to determine font size from run props
                font_size_pt = 12.0
                is_bold = False
                for r in p.findall(f'{{{A_NS}}}r'):
                    rPr = r.find(f'{{{A_NS}}}rPr')
                    if rPr is not None:
                        sz = rPr.get('sz')
                        if sz:
                            try:
                                font_size_pt = int(sz) / 100.0
                            except ValueError:
                                pass
                        if rPr.get('b') == '1':
                            is_bold = True
                        break  # use first run's size for the paragraph

                for r in p.findall(f'{{{A_NS}}}r'):
                    text = ''.join(t.text or '' for t in r.findall(f'{{{A_NS}}}t')).strip()
                    if not text:
                        continue
                    rPr = r.find(f'{{{A_NS}}}rPr')
                    if rPr is None:
                        continue
                    solidFill = rPr.find(f'{{{A_NS}}}solidFill')
                    if solidFill is None:
                        continue

                    fg_hex: Optional[str] = None
                    fg_label: str = ''

                    schemeClr = solidFill.find(f'{{{A_NS}}}schemeClr')
                    if schemeClr is not None:
                        val = schemeClr.get('val', '')
                        fg_label = val
                        # Parse modifiers
                        lum_mod = int(schemeClr.find(f'{{{A_NS}}}lumMod').get('val', '100000')) \
                            if schemeClr.find(f'{{{A_NS}}}lumMod') is not None else 100000
                        lum_off = int(schemeClr.find(f'{{{A_NS}}}lumOff').get('val', '0')) \
                            if schemeClr.find(f'{{{A_NS}}}lumOff') is not None else 0
                        shade = int(schemeClr.find(f'{{{A_NS}}}shade').get('val', '100000')) \
                            if schemeClr.find(f'{{{A_NS}}}shade') is not None else 100000
                        tint = int(schemeClr.find(f'{{{A_NS}}}tint').get('val', '100000')) \
                            if schemeClr.find(f'{{{A_NS}}}tint') is not None else 100000
                        try:
                            fg_hex = self.theme_resolver.resolve_scheme_color(
                                val, lum_mod=lum_mod, lum_off=lum_off, shade=shade, tint=tint)
                        except Exception:
                            # Fallback: if scheme is white-family, flag it directly
                            if val in WHITE_SCHEMES:
                                shape_issues.append((shape_name, val, text[:40]))
                            continue
                    else:
                        srgbClr = solidFill.find(f'{{{A_NS}}}srgbClr')
                        if srgbClr is not None:
                            fg_hex = srgbClr.get('val', '')
                            fg_label = f'#{fg_hex}'

                    if fg_hex is None:
                        continue

                    # Assume white slide background for contrast check (conservative — most slides are white)
                    bg_hex = 'FFFFFF'
                    try:
                        ratio = self.theme_resolver.contrast_ratio(fg_hex, bg_hex)
                        large_text = font_size_pt >= 18 or (is_bold and font_size_pt >= 14)
                        threshold = 3.0 if large_text else 4.5
                        if ratio < threshold:
                            shape_issues.append((shape_name, fg_label, text[:40]))
                    except Exception:
                        # Fallback: flag pure white-family scheme colors
                        if fg_label in WHITE_SCHEMES:
                            shape_issues.append((shape_name, fg_label, text[:40]))

        if shape_issues:
            shapes_affected = list(dict.fromkeys(s[0] for s in shape_issues))
            colors_seen = list(dict.fromkeys(s[1] for s in shape_issues))
            examples = shape_issues[:2]
            example_str = '; '.join(f"'{e[2]}' in '{e[0]}' (color: {e[1]})" for e in examples)
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="1.4.3",
                criterion_name="Contrast (Minimum)",
                wcag_level="AA",
                issue=f"Text with insufficient contrast detected in {len(shapes_affected)} shape(s) on slide {slide_num}.",
                evidence=f"Colors {colors_seen[:4]} resolve to insufficient contrast against white background. Examples: {example_str}",
                severity=Severity.SERIOUS,
                why_it_matters="Low-contrast text is difficult or impossible to read for users with low vision or in poor lighting conditions.",
                remediation_steps=[
                    f"Select affected text in: {', '.join(shapes_affected[:3])}.",
                    "Apply a text color with sufficient contrast: 4.5:1 for normal text, 3:1 for large text (≥18pt or ≥14pt bold).",
                    "Use Format → Font → Font Color → More Colors to set a specific color.",
                    "Verify with WebAIM Contrast Checker (webaim.org/resources/contrastchecker/).",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="high",
                confidence_rationale=f"Text color resolved via theme to hex values with contrast ratio < threshold against white background. Colors: {colors_seen[:3]}.",
                evidence_source=EvidenceSource.THEME_RESOLVED,
                location=loc,
                remediation_id=f"contrast_{slide_num}",
                remediation_data={"slide": slide_num, "action": "fix_contrast", "shapes": shapes_affected},
            ))

    def _rule_1_4_5_images_of_text(self, shapes: List[ShapeInfo], loc: str):
        """WCAG 1.4.5 Images of Text (AA)
        
        Detect images that appear to be screenshots or text diagrams using heuristics:
        - Filename keywords: screenshot, code, diagram, chart, formula, etc.
        - Missing or empty alt text
        - Confidence scoring to minimize false positives
        """
        suspects = []
        
        for shape in shapes:
            if shape.shape_type != 'image':
                continue
            if shape.is_decorative:
                continue  # Explicitly marked decorative
            
            confidence = 0
            flags = []
            
            # Flag 1: No alt text (2 points)
            if shape.alt_text is None:
                confidence += 2
                flags.append("no alt text")
            elif (shape.alt_text or "").strip() == "":
                confidence += 1
                flags.append("empty alt text")
            
            # Flag 2: Suspicious filename keywords (3 points)
            if shape.shape_name:
                name_lower = shape.shape_name.lower()
                keywords = ['screenshot', 'code', 'diagram', 'chart', 'formula',
                           'graph', 'equation', 'pseudocode', 'snippet']
                for kw in keywords:
                    if kw in name_lower:
                        confidence += 3
                        flags.append(f'filename contains "{kw}"')
                        break  # Count only one keyword match
            
            # Aggregate: flag if confidence >= 3
            if confidence >= 3:
                suspects.append({
                    'shape': shape,
                    'confidence': confidence,
                    'flags': flags,
                })
        
        if not suspects:
            return
        
        # Build evidence string with examples
        examples = []
        for s in suspects[:3]:
            flags_str = ', '.join(s['flags'])
            examples.append(f"'{s['shape'].shape_name}' ({flags_str})")
        more = len(suspects) - 3
        example_str = '; '.join(examples)
        if more > 0:
            example_str += f"; and {more} more"
        
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.4.5",
            criterion_name="Images of Text",
            wcag_level="AA",
            issue=(
                f"{len(suspects)} image(s) in {loc} appear to be screenshots or text diagrams. "
                "If they contain text, that text must be described in alt text."
            ),
            evidence=(
                f"Images flagged by heuristic (filename keywords and/or missing alt text). "
                f"Examples: {example_str}. Manual review required to confirm content."
            ),
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 1.4.5 Images of Text (Level AA) requires that text not be presented "
                "as images alone. If these images contain text (code, diagrams with labels, "
                "formulas), that text must be provided in alt text or as actual text."
            ),
            remediation_steps=[
                f"For each flagged image in {loc}:",
                "1. Review the image to confirm it contains text.",
                "2. If yes, add descriptive alt text covering all text content.",
                "3. Right-click → Edit Alt Text and provide a complete, text-based description.",
                "4. If the image is purely decorative, mark it as such.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=(
                "Heuristic-based detection: suspicious filename keywords and missing alt text. "
                "Manual review required to confirm the image actually contains text that needs alt text."
            ),
            evidence_source=EvidenceSource.XML_INFERRED,
            location=loc,
            remediation_id=f"pptx_images_of_text_{loc.replace(' ', '_')}",
            remediation_data={
                "action": "review_image_alt_text",
                "affected_images": [s['shape'].shape_name for s in suspects],
                "count": len(suspects),
            },
        ))

    def _rule_1_4_4_resize_text(self, slide_path: str, loc: str, slide_num: int):
        """WCAG 1.4.4 Resize Text (AA Level)
        
        Detect text that uses fixed font sizes, especially very small sizes that may
        become unreadable if the user's browser/viewer zoom doesn't apply.
        Note: PPTX presentations in LibreOffice/PowerPoint can be zoomed, but
        embedded fixed-size text may not scale with the presentation view.
        """
        P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
        A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
        
        TINY_THRESHOLD_PT = 8  # Warn on anything < 8pt (hard to read at 200% zoom)
        VERY_SMALL_THRESHOLD_PT = 6  # Critical on anything < 6pt

        try:
            content = self.zip.read(slide_path)
            root = etree.fromstring(content)
        except Exception:
            return

        suspects = {
            'tiny': [],      # 6-8pt
            'very_small': [] # < 6pt
        }
        
        for txBody in root.findall(f'.//{{{P}}}txBody'):
            parent = txBody.getparent()
            cNvPr = parent.find(f'.//{{{P}}}cNvPr') if parent is not None else None
            shape_name = cNvPr.get('name', 'unknown') if cNvPr is not None else 'unknown'

            for p in txBody.findall(f'{{{A}}}p'):
                for r in p.findall(f'{{{A}}}r'):
                    rPr = r.find(f'{{{A}}}rPr')
                    if rPr is None:
                        continue
                    
                    # Font size in PowerPoint is specified in 1/100 of a point (sz attribute)
                    sz_str = rPr.get('sz')
                    if not sz_str or not sz_str.isdigit():
                        continue
                    
                    sz_hundredths = int(sz_str)
                    sz_points = sz_hundredths / 100.0
                    
                    run_text = ''.join(t.text or '' for t in r.findall(f'{{{A}}}t')).strip()
                    if not run_text:
                        continue
                    
                    if sz_points < VERY_SMALL_THRESHOLD_PT:
                        suspects['very_small'].append({
                            'text': run_text[:50],
                            'size_pt': round(sz_points, 1),
                            'shape': shape_name
                        })
                    elif sz_points < TINY_THRESHOLD_PT:
                        suspects['tiny'].append({
                            'text': run_text[:50],
                            'size_pt': round(sz_points, 1),
                            'shape': shape_name
                        })

        # Build findings from very_small (critical) and tiny (moderate)
        if suspects['very_small']:
            examples = suspects['very_small'][:3]
            examples_str = '; '.join(
                f"'{e['text']}' ({e['size_pt']}pt in {e['shape']})" for e in examples
            )
            more = len(suspects['very_small']) - 3
            if more > 0:
                examples_str += f" (and {more} more)"
            
            self.fact_sheet.critical_findings.append(Finding(
                criterion_id="1.4.4",
                criterion_name="Resize Text",
                wcag_level="AA",
                issue=(
                    f"{len(suspects['very_small'])} text run(s) on {loc} "
                    "use font sizes < 6 points, which may be unreadable when zoomed."
                ),
                evidence=(
                    f"Text runs with explicitly set font sizes below 6pt. Examples: {examples_str}."
                ),
                severity=Severity.CRITICAL,
                why_it_matters=(
                    "While modern PPTX viewers generally support zoom, any text "
                    "that is already very small (< 6pt) becomes illegible at 200% zoom. "
                    "WCAG 1.4.4 requires text to be resizable to at least 200% without "
                    "loss of content or functionality. Very small fixed text fails this."
                ),
                remediation_steps=[
                    f"Open {loc} in PowerPoint/LibreOffice.",
                    "Find the very small text (look for anything under 6pt in the font size dropdown).",
                    "Select the text and increase its size to at least 11pt (preferably 12pt+).",
                    "Test by zooming the presentation to 200% — text should remain readable.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="high",
                confidence_rationale=(
                    "Font size is an explicit XML attribute; < 6pt is objectively very small. "
                    "This is a confirmed accessibility issue."
                ),
                evidence_source=EvidenceSource.XML_INFERRED,
                location=loc,
                remediation_id=f"resize_text_very_small_{slide_num}",
                remediation_data={
                    "slide": slide_num,
                    "action": "increase_font_size",
                    "threshold_pt": VERY_SMALL_THRESHOLD_PT,
                    "affected_shapes": list(dict.fromkeys(e['shape'] for e in suspects['very_small'])),
                },
            ))

        if suspects['tiny'] and not suspects['very_small']:  # Only report if not already critical
            examples = suspects['tiny'][:3]
            examples_str = '; '.join(
                f"'{e['text']}' ({e['size_pt']}pt in {e['shape']})" for e in examples
            )
            more = len(suspects['tiny']) - 3
            if more > 0:
                examples_str += f" (and {more} more)"
            
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="1.4.4",
                criterion_name="Resize Text",
                wcag_level="AA",
                issue=(
                    f"{len(suspects['tiny'])} text run(s) on {loc} "
                    "use font sizes 6–8 points, which may be hard to read at 200% zoom."
                ),
                evidence=(
                    f"Text runs with explicitly set font sizes between 6–8pt. Examples: {examples_str}."
                ),
                severity=Severity.MODERATE,
                why_it_matters=(
                    "Text smaller than 8pt may become unreadable when the presentation "
                    "is zoomed to 200% or when displayed on devices with lower resolution. "
                    "WCAG 1.4.4 requires content to remain usable at 200% zoom."
                ),
                remediation_steps=[
                    f"Open {loc} in PowerPoint/LibreOffice.",
                    "Find text in the 6–8pt range (check the font size dropdown).",
                    "Consider increasing to 11pt or larger for better readability.",
                    "Test at 200% zoom to confirm text is legible.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale=(
                    "Font size is explicit in XML; 6–8pt is objectively small. "
                    "Readability at 200% zoom is heuristic and device-dependent."
                ),
                evidence_source=EvidenceSource.XML_INFERRED,
                location=loc,
                remediation_id=f"resize_text_tiny_{slide_num}",
                remediation_data={
                    "slide": slide_num,
                    "action": "increase_font_size",
                    "threshold_pt": TINY_THRESHOLD_PT,
                    "affected_shapes": list(dict.fromkeys(e['shape'] for e in suspects['tiny'])),
                },
            ))

    def _rule_3_1_2_language_of_parts(self, slide_path: str, loc: str, slide_num: int):
        """WCAG 3.1.2 Language of Parts (AA Level)
        
        Detect text runs that explicitly declare a language (xml:lang) different from
        the document/slide language. This is usually correct (e.g., a French phrase in
        an English slide), but the presence of explicit language markers on individual
        runs is reported for verification.
        """
        P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
        A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
        XML_NS = 'http://www.w3.org/XML/1998/namespace'

        try:
            content = self.zip.read(slide_path)
            root = etree.fromstring(content)
        except Exception:
            return

        # Get document/slide language (from slide properties or root element)
        slide_lang = None
        props = root.find(f'.//{{{P}}}cSld/{{{P}}}spPr/{{{A}}}extLst')
        if props is not None:
            # Try to infer from first text element if document lang not explicit
            pass
        
        # Look for run-level language attributes
        parts_with_lang = []
        for txBody in root.findall(f'.//{{{P}}}txBody'):
            parent = txBody.getparent()
            cNvPr = parent.find(f'.//{{{P}}}cNvPr') if parent is not None else None
            shape_name = cNvPr.get('name', 'unknown') if cNvPr is not None else 'unknown'

            for p in txBody.findall(f'{{{A}}}p'):
                for r in p.findall(f'{{{A}}}r'):
                    rPr = r.find(f'{{{A}}}rPr')
                    if rPr is None:
                        continue
                    
                    # Check for xml:lang attribute on run properties
                    lang_attr = rPr.get(f'{{{XML_NS}}}lang')
                    if not lang_attr:
                        continue
                    
                    run_text = ''.join(t.text or '' for t in r.findall(f'{{{A}}}t')).strip()
                    if not run_text:
                        continue
                    
                    parts_with_lang.append({
                        'text': run_text[:60],
                        'lang': lang_attr,
                        'shape': shape_name
                    })

        if not parts_with_lang:
            return

        examples = parts_with_lang[:5]
        examples_str = '; '.join(
            f"'{e['text']}' (lang={e['lang']} in {e['shape']})" for e in examples
        )
        more = len(parts_with_lang) - 5
        if more > 0:
            examples_str += f" (and {more} more)"

        self.fact_sheet.advisory_findings.append(Finding(
            criterion_id="3.1.2",
            criterion_name="Language of Parts",
            wcag_level="AA",
            issue=(
                f"{len(parts_with_lang)} text run(s) on {loc} "
                "explicitly declare a language tag (xml:lang attribute)."
            ),
            evidence=(
                f"Text runs with explicit xml:lang attributes detected. Examples: {examples_str}. "
                f"This is usually correct when a slide contains phrases in a language different "
                f"from the document's primary language."
            ),
            severity=Severity.MINOR,
            why_it_matters=(
                "Screen readers and other assistive technologies use language markup to pronounce "
                "text correctly. When parts of text are in a different language, they should be "
                "explicitly marked. However, correct language markup depends on accurate lang values; "
                "invalid or incorrect tags can mislead assistive tools. WCAG 3.1.2 requires that "
                "when a change of language occurs, it must be programmatically determinable — "
                "which you are doing correctly with xml:lang."
            ),
            remediation_steps=[
                "Review each detected language-marked text run to verify the lang attribute is correct.",
                "Common valid codes: 'en' (English), 'fr' (French), 'es' (Spanish), 'de' (German), etc.",
                "Ensure lang codes are valid ISO 639-1 or RFC 5646 language tags.",
                "If a run is NOT actually in a different language, remove the xml:lang attribute.",
                "Test with a screen reader to confirm text is pronounced in the correct language.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=(
                "Language markup is an explicit XML attribute and is correctly detected. "
                "However, whether the markup is semantically correct (i.e., the declared language "
                "matches the actual text language) requires manual verification."
            ),
            evidence_source=EvidenceSource.XML_INFERRED,
            location=loc,
            remediation_id=f"language_of_parts_{slide_num}",
            remediation_data={
                "slide": slide_num,
                "action": "verify_language_markup",
                "count": len(parts_with_lang),
                "affected_shapes": list(dict.fromkeys(e['shape'] for e in parts_with_lang)),
                "languages_found": list(dict.fromkeys(e['lang'] for e in parts_with_lang)),
            },
        ))

    def _rule_2_4_4_link_text(self, slide_path: str, rels_bytes: Optional[bytes], loc: str, slide_num: int):
        """Check hyperlinks in slide for non-descriptive link text."""
        try:
            content = self.zip.read(slide_path)
            root = etree.fromstring(content)
        except Exception:
            return

        # Load relationship targets for this slide
        hl_rels: Dict[str, str] = {}
        if rels_bytes:
            try:
                rels_root = etree.fromstring(rels_bytes)
                HLINK_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
                for rel in rels_root:
                    if rel.get('Type', '') == HLINK_TYPE:
                        hl_rels[rel.get('Id', '')] = rel.get('Target', '')
            except Exception:
                pass

        R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        seen_remediation_ids = set()

        # Walk all text runs with hlinkClick
        for txBody in root.findall(f'.//{{{P}}}txBody'):
            for p_el in txBody.findall(f'{{{A}}}p'):
                for r_el in p_el.findall(f'{{{A}}}r'):
                    rPr = r_el.find(f'{{{A}}}rPr')
                    if rPr is None:
                        continue
                    hlinkClick = rPr.find(f'{{{A}}}hlinkClick')
                    if hlinkClick is None:
                        continue
                    rel_id = hlinkClick.get(f'{{{R_NS}}}id', '')
                    url = hl_rels.get(rel_id, '')
                    # Collect all text in this run
                    display_text = ''.join(t.text or '' for t in r_el.findall(f'{{{A}}}t')).strip()
                    if not display_text:
                        continue
                    if GENERIC_LINK_TEXT.match(display_text) or URL_PATTERN.match(display_text):
                        rid = f"pptx_link_{slide_num}_{rel_id}"
                        if rid in seen_remediation_ids:
                            continue
                        seen_remediation_ids.add(rid)
                        self.fact_sheet.confirmed_findings.append(Finding(
                            criterion_id="2.4.4",
                            criterion_name="Link Purpose (In Context)",
                            wcag_level="A",
                            issue=f"Link text '{display_text}' does not describe the destination.",
                            evidence=f"Slide {slide_num}: hlinkClick run has display text '{display_text}'"
                                     + (f" pointing to {url}" if url else ""),
                            severity=Severity.MODERATE,
                            why_it_matters="Screen reader users navigate by listing all links. Generic text like 'click here' or raw URLs provide no context about the destination.",
                            remediation_steps=[
                                f"Select the hyperlink '{display_text}' on slide {slide_num}.",
                                "Replace the display text with a description of the destination (e.g. 'Download the Q3 report').",
                                "Avoid 'click here', 'here', 'more', or pasting raw URLs as link text.",
                            ],
                            confidence_tier=ConfidenceTier.CONFIRMED,
                            confidence_label="high",
                            confidence_rationale=f"Link text '{display_text}' matches known non-descriptive pattern in hlinkClick run text.",
                            evidence_source=EvidenceSource.TEXT_CONTENT,
                            location=f"{loc}",
                            remediation_id=rid,
                            remediation_data={"slide": slide_num, "action": "fix_link_text", "current_text": display_text},
                        ))

    def _rule_1_1_1_background_possible(self):
        """Background images in slide master/layout are never in slide XML.

        Only fires when the .pptx actually contains master/layout image
        references — otherwise the advisory would be noise on every deck.
        """
        # Look for any image reference inside slideMaster/slideLayout rel files.
        master_layout_image_found = False
        for name in self.zip.namelist():
            if not (name.startswith('ppt/slideMasters/_rels/')
                    or name.startswith('ppt/slideLayouts/_rels/')):
                continue
            try:
                content = self.zip.read(name).decode('utf-8', errors='replace')
            except KeyError:
                continue
            # Image relationships use Type ending in /image
            if 'image"' in content or "/image\"" in content or '/image' in content.lower():
                master_layout_image_found = True
                break
        if not master_layout_image_found:
            return

        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.1.1",
            criterion_name="Non-text Content",
            wcag_level="A",
            issue="Background or decorative images in slide master/layout cannot be verified from slide content alone.",
            evidence="Slide master or layout relationships reference image parts that don't appear in individual slide XML.",
            severity=Severity.CRITICAL,
            why_it_matters="Decorative backgrounds are fine, but informational images in the master/layout would be inaccessible to all slides using that layout.",
            remediation_steps=[
                "Review View → Slide Master for background images.",
                "Ensure any informational images there have alt text; mark purely decorative backgrounds as decorative.",
                "Run Review → Check Accessibility in PowerPoint to catch items missed in this analysis.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="low",
            confidence_rationale="Image references found in master/layout relationships; semantic role requires manual review.",
            evidence_source=EvidenceSource.XML_INFERRED,
            location="presentation",
            remediation_id="pptx_master_images_review",
        ))

    def _rule_2_4_6_slide_title_quality(self):
        """WCAG 2.4.6 — Headings and Labels. Detect slide title quality issues
        beyond generic placeholder text (very short, duplicates, all-caps shouting)."""
        slides = self.fact_sheet.slides or []
        if len(slides) < 2:
            return
        seen_titles: Dict[str, List[int]] = {}
        very_short = []
        for slide_num, shapes in enumerate(slides, 1):
            title_shapes = [s for s in shapes if s.shape_type == 'title']
            for s in title_shapes:
                text = (s.text_content or '').strip()
                if not text:
                    continue
                # Track duplicates
                key = text.lower()
                seen_titles.setdefault(key, []).append(slide_num)
                # Very short titles (1-2 chars, just a number/letter) — likely not descriptive
                if len(text) <= 2 and not text.isdigit():
                    very_short.append((slide_num, text))

        # Duplicate titles across multiple slides
        duplicates = {t: nums for t, nums in seen_titles.items() if len(nums) > 1}
        if duplicates:
            for t, nums in list(duplicates.items())[:5]:
                # Use first slide's original-cased title for the issue text.
                first_slide_idx = nums[0] - 1
                title_text = next(
                    (s.text_content for s in slides[first_slide_idx]
                     if s.shape_type == 'title'
                     and (s.text_content or '').strip().lower() == t),
                    t,
                )
                self.fact_sheet.confirmed_findings.append(Finding(
                    criterion_id="2.4.6",
                    criterion_name="Headings and Labels",
                    wcag_level="AA",
                    issue=(
                        f"Slide title '{title_text}' is reused on slides {nums} — "
                        "screen-reader users cannot distinguish slides by title."
                    ),
                    evidence=f"Title '{title_text}' detected on slides {nums} via direct text comparison.",
                    severity=Severity.MINOR,
                    why_it_matters=(
                        "Screen reader users navigating by slide title cannot tell duplicated slides apart. "
                        "Each slide title should describe its specific content."
                    ),
                    remediation_steps=[
                        f"Rename one of the slides currently titled '{title_text}' so each title is unique.",
                        "If slides genuinely cover the same topic, append context (e.g. 'Sales (continued)').",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label=CONFIDENCE_LABEL[EvidenceSource.TEXT_CONTENT],
                    confidence_rationale="Duplicate titles detected via direct text comparison; semantic intent may still be reviewed.",
                    evidence_source=EvidenceSource.TEXT_CONTENT,
                    location=f"slides {nums}",
                    remediation_id=f"duplicate_slide_titles_{nums[0]}",
                ))
            # Keep the legacy summarizing possible-finding so Phase A remediation_id
            # 'pptx_title_duplicates' still surfaces (used by triage UI).
            sample = list(duplicates.items())[:3]
            sample_str = "; ".join(f"'{t}' on slides {nums}" for t, nums in sample)
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="2.4.6",
                criterion_name="Headings and Labels",
                wcag_level="AA",
                issue=f"{len(duplicates)} slide title(s) appear on multiple slides — titles may not be unique enough.",
                evidence=f"Duplicate titles: {sample_str}",
                severity=Severity.MINOR,
                why_it_matters=(
                    "Screen reader users navigating by slide title cannot distinguish between slides "
                    "that share identical titles. Each slide title should describe its specific content."
                ),
                remediation_steps=[
                    "Make each slide's title distinct (e.g., 'Q1 Sales' vs 'Q2 Sales' rather than both being 'Sales').",
                    "If slides genuinely cover the same topic, append context (e.g., 'Sales (continued)').",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale="Duplicate titles detected via direct text comparison; review for intentional grouping.",
                evidence_source=EvidenceSource.TEXT_CONTENT,
                location="presentation",
                remediation_id="pptx_title_duplicates",
            ))

        if very_short:
            sample = "; ".join(f"slide {n}: '{t}'" for n, t in very_short[:3])
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="2.4.6",
                criterion_name="Headings and Labels",
                wcag_level="AA",
                issue=f"{len(very_short)} slide title(s) are very short (1-2 characters) and may not be descriptive.",
                evidence=f"Short titles: {sample}",
                severity=Severity.MINOR,
                why_it_matters=(
                    "Single-character or two-character titles rarely describe slide content meaningfully."
                ),
                remediation_steps=[
                    "Expand each short title to describe what the slide covers.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale="Title length is statically measurable; semantic adequacy requires review.",
                evidence_source=EvidenceSource.TEXT_CONTENT,
                location="presentation",
                remediation_id="pptx_title_too_short",
            ))

    # ── Phase B: 1.4.11 Non-text Contrast ───────────────────────────────────
    def _rule_1_4_11_non_text_contrast(self, slide_file: str, loc: str, slide_num: int):
        """WCAG 1.4.11 — Detect shape outline vs shape fill contrast < 3:1.

        Strictly deterministic: only flags shapes that have BOTH an explicit
        srgbClr line color AND an explicit srgbClr fill color (no theme or
        inheritance guessing — keeps confidence high)."""
        from wcag.common.non_text_contrast import evaluate_pair, MIN_NON_TEXT_CONTRAST
        try:
            content = self.zip.read(slide_file)
        except KeyError:
            return
        try:
            root = etree.fromstring(content)
        except etree.XMLSyntaxError:
            return

        offenders: List[Tuple[str, str, str, float]] = []
        for sp in root.iter(f'{{{P}}}sp'):
            spPr = sp.find(f'{{{P}}}spPr')
            if spPr is None:
                continue
            # Direct fill (not inside <a:ln>)
            fill_hex: Optional[str] = None
            for child in spPr:
                if child.tag == f'{{{A}}}solidFill':
                    srgb = child.find(f'{{{A}}}srgbClr')
                    if srgb is not None:
                        fill_hex = srgb.get('val')
                    break
            # Line color
            line = spPr.find(f'{{{A}}}ln')
            line_hex: Optional[str] = None
            if line is not None:
                ln_fill = line.find(f'{{{A}}}solidFill')
                if ln_fill is not None:
                    srgb = ln_fill.find(f'{{{A}}}srgbClr')
                    if srgb is not None:
                        line_hex = srgb.get('val')
            if not (fill_hex and line_hex):
                continue
            result = evaluate_pair(line_hex, fill_hex)
            if not result:
                continue
            ratio, ok = result
            if ok:
                continue
            # Identify shape name from nvSpPr/cNvPr
            cNvPr = sp.find(f'{{{P}}}nvSpPr/{{{P}}}cNvPr')
            shape_name = cNvPr.get('name') if cNvPr is not None else 'shape'
            offenders.append((shape_name, line_hex, fill_hex, ratio))

        if not offenders:
            return
        sample = "; ".join(
            f"'{n}' (line #{l} on fill #{f}, ratio {r:.2f}:1)" for n, l, f, r in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.4.11",
            criterion_name="Non-text Contrast",
            wcag_level="AA",
            issue=(
                f"{len(offenders)} shape(s) on slide {slide_num} have outline-on-fill contrast below "
                f"{MIN_NON_TEXT_CONTRAST}:1."
            ),
            evidence=f"Affected shapes: {sample}",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "UI components and graphical objects whose boundaries depend on contrast (borders, outlines) "
                "must be distinguishable. Insufficient contrast hides shape edges from low-vision users."
            ),
            remediation_steps=[
                "Increase the contrast between shape outline and fill to at least 3:1.",
                "Either darken the outline or lighten the fill (or vice versa).",
                "If the outline is decorative only, consider removing it entirely so the shape relies on its fill.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.XML_DIRECT],
            confidence_rationale="Outline and fill colors are read directly from the slide XML as explicit srgbClr values.",
            evidence_source=EvidenceSource.XML_DIRECT,
            location=loc,
            remediation_id=f"pptx_non_text_contrast_{slide_num}",
        ))

    # ── Phase E: 4.1.2 Name, Role, Value (actionable shapes) ────────────────
    def _rule_4_1_2_actionable_shape_names(self, slide_file: str, loc: str, slide_num: int):
        """Flag shapes that are interactive (have an <a:hlinkClick>) but lack
        any accessible name — no descr, no title attribute, AND no visible
        text inside the shape. Strict-deterministic (XML-only)."""
        try:
            slide_xml = self.zip.read(slide_file)
        except Exception:
            return
        try:
            root = etree.fromstring(slide_xml)
        except Exception:
            return

        offenders: List[str] = []
        for sp in root.iter(f'{{{P}}}sp'):
            # Skip if no hyperlink anywhere in shape
            hlinks = list(sp.iter(f'{{{A}}}hlinkClick'))
            if not hlinks:
                continue
            nvSpPr = sp.find(f'{{{P}}}nvSpPr')
            if nvSpPr is None:
                continue
            cNvPr = nvSpPr.find(f'{{{P}}}cNvPr')
            if cNvPr is None:
                continue
            descr = (cNvPr.get('descr') or '').strip()
            title = (cNvPr.get('title') or '').strip()
            if descr or title:
                continue
            # Skip if marked decorative — that would actually be wrong for an
            # actionable shape, but treat as out of scope here.
            if _is_decorative(cNvPr):
                continue
            # Check shape text
            txBody = sp.find(f'{{{P}}}txBody')
            text = _text_from_txBody(txBody) if txBody is not None else ''
            if text and text.strip():
                continue  # Visible link text serves as the name
            shape_id = cNvPr.get('id', '?')
            shape_name = cNvPr.get('name', f'Shape_{shape_id}')
            offenders.append(f"{shape_name} (id={shape_id})")

        if not offenders:
            return

        sample = ', '.join(offenders[:5])
        if len(offenders) > 5:
            sample += f", and {len(offenders) - 5} more"

        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="4.1.2",
            criterion_name="Name, Role, Value",
            wcag_level="A",
            issue=(
                f"{len(offenders)} hyperlinked shape(s) on {loc.lower()} have no accessible "
                "name (no descr, no title, no visible text)."
            ),
            evidence=(
                f"Shapes with <a:hlinkClick> but empty cNvPr@descr, empty cNvPr@title, "
                f"and empty txBody: {sample}."
            ),
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Hyperlinked shapes act as buttons or links. Without an accessible name, "
                "screen readers announce them only by role (e.g. 'link') with no indication "
                "of destination or purpose."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {loc} — shapes: {sample}.",
                "  • Right-click the shape → View Alt Text → enter a meaningful description.",
                "  • Or add visible link text inside the shape that describes the destination.",
                "  • Avoid generic names like 'Shape 1' — use action-oriented names like 'Open team site'.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.XML_DIRECT],
            confidence_rationale=(
                "Hyperlink presence and absence of descr/title/text are read directly "
                "from the slide XML."
            ),
            evidence_source=EvidenceSource.XML_DIRECT,
            location=loc,
            remediation_id=f"pptx_actionable_names_{slide_num}",
        ))

    def _rule_4_1_2_generic_picture_names(self, slide_file: str, loc: str, slide_num: int):
        """WCAG 4.1.2 (refinement) — picture / image shapes whose `cNvPr/@name`
        is a default placeholder ("Picture 1", "Image 3"). The Selection Pane
        and screen-reader shape list both expose this name; defaults give users
        no idea what shape is selected. POSSIBLE: descr/alt-text usually wins
        for AT, but the Selection Pane name still matters for editors with AT.
        """
        try:
            slide_xml = self.zip.read(slide_file)
        except Exception:
            return
        try:
            root = etree.fromstring(slide_xml)
        except Exception:
            return

        import re as _re
        generic_pat = _re.compile(
            r"^(?:Picture|Image|Photo|Graphic|Bild|Imagen|Image)\s*\d+$",
            _re.IGNORECASE,
        )
        offenders: List[str] = []
        # Picture shapes are <p:pic> elements
        for pic in root.iter(f'{{{P}}}pic'):
            nvPicPr = pic.find(f'{{{P}}}nvPicPr')
            if nvPicPr is None:
                continue
            cNvPr = nvPicPr.find(f'{{{P}}}cNvPr')
            if cNvPr is None:
                continue
            if _is_decorative(cNvPr):
                continue
            name = (cNvPr.get('name') or '').strip()
            if not generic_pat.match(name):
                continue
            shape_id = cNvPr.get('id', '?')
            offenders.append(f"{name} (id={shape_id})")

        if not offenders:
            return

        sample = ', '.join(offenders[:5])
        if len(offenders) > 5:
            sample += f", and {len(offenders) - 5} more"

        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="4.1.2",
            criterion_name="Name, Role, Value",
            wcag_level="A",
            issue=(
                f"{len(offenders)} picture(s) on {loc.lower()} use default placeholder "
                "names (e.g. 'Picture 1') in the Selection Pane."
            ),
            evidence=f"Picture shapes with default cNvPr@name: {sample}.",
            severity=Severity.MINOR,
            why_it_matters=(
                "PowerPoint's Selection Pane and the macOS / Windows narrator's shape list "
                "both expose `cNvPr@name`. When every picture is 'Picture 1', 'Picture 2', "
                "users with assistive tech who edit the deck cannot distinguish shapes by "
                "name. Renaming improves the authoring experience and downstream AT "
                "behavior on platforms that prefer @name over @descr."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {loc} — pictures: {sample}.",
                "  • Open the Selection Pane (Home → Arrange → Selection Pane).",
                "  • Double-click each name and type a short, content-aware label "
                "(e.g. 'TeamLogo', 'RevenueChart').",
                "  • This is independent of alt text — both can (and should) coexist.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=(
                "cNvPr@name read directly from the slide XML; defaults are language-aware "
                "but uncommon in non-English deployments — flagged as POSSIBLE."
            ),
            evidence_source=EvidenceSource.XML_DIRECT,
            location=loc,
            remediation_id=f"pptx_generic_picture_names_{slide_num}",
        ))

