"""
DOCX Remediator
Applies targeted OOXML fixes to a .docx file based on remediation_id values
produced by DocxAnalyzer. Returns the modified file as bytes.

Supported remediation actions:
  img_alt_{index}         — set alt text (wp:docPr @descr) on drawing
  table_header_{index}    — add w:tblHeader to first row of table
  heading_style_{idx}     — change paragraph style to Heading N
  doc_title               — set dc:title in docProps/core.xml
  doc_language            — set w:lang in word/settings.xml
  link_text_{para}        — requires human-provided new_text override
  unicode_checkboxes      — flag only (structural fix varies by intent)
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import List, Dict, Optional, Any
from lxml import etree

from wcag.models import Finding, RemediationResult

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
DC = "http://purl.org/dc/elements/1.1/"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"


class DocxRemediator:
    def __init__(self, file_bytes: bytes):
        self.original_bytes = file_bytes

    def apply(self, findings: List[Finding],
               remediation_ids: Optional[List[str]] = None,
               remediation_overrides: Optional[Dict[str, Any]] = None) -> RemediationResult:
        if remediation_overrides is None:
            remediation_overrides = {}

        targets = [f for f in findings if f.remediation_id and
                   (remediation_ids is None or f.remediation_id in remediation_ids)]

        if not targets:
            return RemediationResult(
                success=True,
                skipped_remediations=[f.remediation_id for f in findings if f.remediation_id],
                file_bytes=self.original_bytes,
            )

        result = RemediationResult(success=True)
        in_buf = io.BytesIO(self.original_bytes)
        out_buf = io.BytesIO()

        with zipfile.ZipFile(in_buf, 'r') as zin, zipfile.ZipFile(out_buf, 'w', zipfile.ZIP_DEFLATED) as zout:
            file_cache: Dict[str, bytes] = {}
            for name in zin.namelist():
                file_cache[name] = zin.read(name)

            for finding in targets:
                rid = finding.remediation_id
                rdata = {**(finding.remediation_data or {}), **remediation_overrides.get(rid, {})}
                action = rdata.get('action', '')
                try:
                    if action == 'set_alt_text':
                        self._apply_set_alt_text(file_cache, rdata)
                        result.applied_remediations.append(rid)
                    elif action == 'add_table_header':
                        self._apply_add_table_header(file_cache, rdata)
                        result.applied_remediations.append(rid)
                    elif action == 'apply_heading_style':
                        self._apply_heading_style(file_cache, rdata)
                        result.applied_remediations.append(rid)
                    elif action == 'set_doc_title':
                        self._apply_set_doc_title(file_cache, rdata)
                        result.applied_remediations.append(rid)
                    elif action == 'set_language':
                        self._apply_set_language(file_cache, rdata)
                        result.applied_remediations.append(rid)
                    elif action == 'fix_link_text':
                        new_text = rdata.get('new_text')
                        if new_text:
                            self._apply_fix_link_text(file_cache, rdata, new_text)
                            result.applied_remediations.append(rid)
                        else:
                            result.skipped_remediations.append(rid)
                    elif action == 'fix_contrast':
                        fixed_count = self._apply_fix_contrast(file_cache, rdata)
                        if fixed_count > 0:
                            result.applied_remediations.append(rid)
                        else:
                            result.skipped_remediations.append(rid)
                    else:
                        # replace_checkboxes and other structural changes: skip (human judgment needed)
                        result.skipped_remediations.append(rid)
                except Exception as e:
                    result.errors.append(f"{rid}: {e}")

            for name, data in file_cache.items():
                zout.writestr(name, data)

        if result.errors:
            result.success = len(result.errors) < len(targets)
        result.file_bytes = out_buf.getvalue()
        return result

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _apply_set_alt_text(self, cache: Dict[str, bytes], rdata: dict):
        img_index = rdata['image_index']
        alt_text = rdata.get('text', '')
        path = 'word/document.xml'
        if path not in cache:
            raise ValueError("word/document.xml not found")
        root = etree.fromstring(cache[path])

        # Find the Nth docPr element in order
        all_docPr = root.findall(f'.//{{{WP}}}docPr')
        if img_index >= len(all_docPr):
            raise ValueError(f"Image index {img_index} out of range (found {len(all_docPr)})")
        docPr = all_docPr[img_index]
        docPr.set('descr', alt_text)
        cache[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)

    def _apply_add_table_header(self, cache: Dict[str, bytes], rdata: dict):
        table_index = rdata['table_index']
        path = 'word/document.xml'
        if path not in cache:
            raise ValueError("word/document.xml not found")
        root = etree.fromstring(cache[path])
        body = root.find(f'{{{W}}}body')
        if body is None:
            raise ValueError("body not found")

        tables = [c for c in body.iter(f'{{{W}}}tbl')]
        if table_index >= len(tables):
            raise ValueError(f"Table index {table_index} out of range")
        tbl = tables[table_index]

        rows = tbl.findall(f'{{{W}}}tr')
        if not rows:
            raise ValueError(f"Table {table_index} has no rows")
        first_row = rows[0]
        trPr = first_row.find(f'{{{W}}}trPr')
        if trPr is None:
            trPr = etree.Element(f'{{{W}}}trPr')
            first_row.insert(0, trPr)
        # Add tblHeader if not present
        tblHeader = trPr.find(f'{{{W}}}tblHeader')
        if tblHeader is None:
            tblHeader = etree.SubElement(trPr, f'{{{W}}}tblHeader')
        # Ensure it's not set to val="0"
        if tblHeader.get(f'{{{W}}}val') == '0':
            del tblHeader.attrib[f'{{{W}}}val']
        cache[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)

    def _apply_heading_style(self, cache: Dict[str, bytes], rdata: dict):
        # Accept either a single 'paragraph_index' or a list 'paragraph_indices'.
        if 'paragraph_indices' in rdata:
            indices = list(rdata['paragraph_indices'])
        elif 'paragraph_index' in rdata:
            indices = [rdata['paragraph_index']]
        else:
            raise ValueError("apply_heading_style requires 'paragraph_index' or 'paragraph_indices'")

        heading_style = rdata.get('heading_style', 'Heading 2')
        path = 'word/document.xml'
        if path not in cache:
            raise ValueError("word/document.xml not found")
        root = etree.fromstring(cache[path])
        body = root.find(f'{{{W}}}body')
        if body is None:
            raise ValueError("body not found")

        # Collect only direct paragraph children (not inside tables)
        paras = [child for child in body if child.tag == f'{{{W}}}p']

        out_of_range = [i for i in indices if i >= len(paras) or i < 0]
        if out_of_range:
            raise ValueError(
                f"Paragraph index/indices out of range: {out_of_range} (have {len(paras)})"
            )

        for para_index in indices:
            p = paras[para_index]
            pPr = p.find(f'{{{W}}}pPr')
            if pPr is None:
                pPr = etree.Element(f'{{{W}}}pPr')
                p.insert(0, pPr)
            pStyle = pPr.find(f'{{{W}}}pStyle')
            if pStyle is None:
                pStyle = etree.SubElement(pPr, f'{{{W}}}pStyle')
                pPr.insert(0, pStyle)
            pStyle.set(f'{{{W}}}val', heading_style)

        cache[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)

    def _apply_set_doc_title(self, cache: Dict[str, bytes], rdata: dict):
        new_title = (rdata.get('new_title') or rdata.get('suggested_title') or '').strip()
        if not new_title:
            # Refuse to write an empty title — analyzer would re-flag it.
            raise ValueError(
                "doc_title requires a non-empty 'new_title' (or 'suggested_title') value"
            )
        path = 'docProps/core.xml'
        if path not in cache:
            raise ValueError("docProps/core.xml not found")
        root = etree.fromstring(cache[path])
        title_el = root.find(f'{{{DC}}}title')
        if title_el is None:
            title_el = etree.SubElement(root, f'{{{DC}}}title')
        title_el.text = new_title
        cache[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)

    def _apply_set_language(self, cache: Dict[str, bytes], rdata: dict):
        lang = rdata.get('suggested_lang', 'en-US')
        path = 'word/settings.xml'
        if path not in cache:
            raise ValueError("word/settings.xml not found")
        root = etree.fromstring(cache[path])
        # Find or create w:lang element at document defaults level
        lang_el = root.find(f'.//{{{W}}}lang')
        if lang_el is None:
            # Create under settings root
            lang_el = etree.SubElement(root, f'{{{W}}}lang')
        lang_el.set(f'{{{W}}}val', lang)
        cache[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)

    def _apply_fix_link_text(self, cache: Dict[str, bytes], rdata: dict, new_text: str):
        para_index = rdata['paragraph_index']
        path = 'word/document.xml'
        if path not in cache:
            raise ValueError("word/document.xml not found")
        root = etree.fromstring(cache[path])
        body = root.find(f'{{{W}}}body')
        if body is None:
            raise ValueError("body not found")

        paras = [c for c in body if c.tag == f'{{{W}}}p']
        if para_index >= len(paras):
            raise ValueError(f"Paragraph index {para_index} out of range")
        p = paras[para_index]

        # Find first hyperlink and update its text runs
        hl = p.find(f'.//{{{W}}}hyperlink')
        if hl is None:
            raise ValueError(f"No hyperlink found in paragraph {para_index}")
        text_els = hl.findall(f'.//{{{W}}}t')
        if text_els:
            text_els[0].text = new_text
            for t in text_els[1:]:
                t.text = ''
        cache[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)

    # ---- contrast helpers --------------------------------------------------
    @staticmethod
    def _hex_to_rgb(h: str):
        h = h.lstrip('#').upper()
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    @staticmethod
    def _rgb_to_hex(r: int, g: int, b: int) -> str:
        return f"{r:02X}{g:02X}{b:02X}"

    @staticmethod
    def _luminance(rgb) -> float:
        def lin(c):
            c = c / 255.0
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = rgb
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)

    @classmethod
    def _contrast_ratio(cls, fg_rgb, bg_rgb) -> float:
        l1 = cls._luminance(fg_rgb)
        l2 = cls._luminance(bg_rgb)
        lighter, darker = max(l1, l2), min(l1, l2)
        return (lighter + 0.05) / (darker + 0.05)

    @classmethod
    def _adjust_color_for_contrast(cls, fg_hex: str, bg_hex: str, target: float = 4.5) -> Optional[str]:
        """Return a new fg hex that achieves the target ratio against bg by
        interpolating fg toward black (if bg is light) or white (if bg is dark).
        Preserves hue; changes only lightness via linear blend.
        Returns None if already passing.
        """
        fg = cls._hex_to_rgb(fg_hex)
        bg = cls._hex_to_rgb(bg_hex)
        if cls._contrast_ratio(fg, bg) >= target:
            return None
        # Decide direction: if bg is lighter than fg, push fg toward black; else toward white.
        bg_lum = cls._luminance(bg)
        fg_lum = cls._luminance(fg)
        anchor = (0, 0, 0) if bg_lum >= fg_lum else (255, 255, 255)
        # Binary search blend factor t in [0,1]
        lo, hi = 0.0, 1.0
        best_hex: Optional[str] = None
        for _ in range(20):
            mid = (lo + hi) / 2.0
            new = (
                int(fg[0] + (anchor[0] - fg[0]) * mid),
                int(fg[1] + (anchor[1] - fg[1]) * mid),
                int(fg[2] + (anchor[2] - fg[2]) * mid),
            )
            if cls._contrast_ratio(new, bg) >= target:
                best_hex = cls._rgb_to_hex(*new)
                hi = mid
            else:
                lo = mid
        if best_hex is None:
            # Fallback to pure anchor color
            best_hex = cls._rgb_to_hex(*anchor)
        return best_hex

    def _apply_fix_contrast(self, cache: Dict[str, bytes], rdata: dict) -> int:
        """Re-scan runs with explicit colors against their paragraph/cell background;
        when ratio < target, replace the foreground color with the smallest
        luminance-shifted variant that meets the threshold (preserving hue).
        Returns the number of runs modified.
        """
        target = float(rdata.get('target_ratio', 4.5))
        path = 'word/document.xml'
        if path not in cache:
            raise ValueError("word/document.xml not found")
        root = etree.fromstring(cache[path])
        body = root.find(f'{{{W}}}body')
        if body is None:
            raise ValueError("body not found")

        def _read_shd_fill(parent_props):
            if parent_props is None:
                return None
            shd = parent_props.find(f'{{{W}}}shd')
            if shd is None:
                return None
            fill = shd.get(f'{{{W}}}fill')
            if fill and fill.upper() != 'AUTO' and len(fill) == 6:
                return fill.upper()
            return None

        fixed = 0

        def _process_runs(runs, bg_hex: str):
            nonlocal fixed
            for r in runs:
                rPr = r.find(f'{{{W}}}rPr')
                if rPr is None:
                    continue
                color_el = rPr.find(f'{{{W}}}color')
                if color_el is None:
                    continue
                val = color_el.get(f'{{{W}}}val', 'auto')
                if val in ('auto', 'theme') or len(val) != 6:
                    continue
                new_hex = self._adjust_color_for_contrast(val, bg_hex, target)
                if new_hex and new_hex.upper() != val.upper():
                    color_el.set(f'{{{W}}}val', new_hex)
                    # Clear themeColor / themeShade so explicit color wins
                    for attr in (f'{{{W}}}themeColor', f'{{{W}}}themeShade', f'{{{W}}}themeTint'):
                        if attr in color_el.attrib:
                            del color_el.attrib[attr]
                    fixed += 1

        for child in body:
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if tag == 'p':
                bg_hex = _read_shd_fill(child.find(f'{{{W}}}pPr')) or 'FFFFFF'
                _process_runs(child.findall(f'.//{{{W}}}r'), bg_hex)
            elif tag == 'tbl':
                for tc in child.findall(f'.//{{{W}}}tc'):
                    cell_bg = _read_shd_fill(tc.find(f'{{{W}}}tcPr')) or 'FFFFFF'
                    _process_runs(tc.findall(f'.//{{{W}}}r'), cell_bg)

        if fixed > 0:
            cache[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
        return fixed
