"""
PPTX Remediator
Applies targeted OOXML fixes to a .pptx file based on remediation_id values
produced by PptxAnalyzer. Returns the modified file as bytes.

Supported remediation actions:
  alt_text_{slide}_{shape_id}    — set descr on cNvPr for image/chart
  list_levels_{slide}_{shape_id} — fix inverted list level sequence
  reading_order_{slide}          — move title to front of spTree
  slide_title_{slide}            — flag only (text fix requires human input)
  presentation_language          — set lang on defaultTextStyle defRPr
  presentation_doc_title         — set dc:title in docProps/core.xml
"""
from __future__ import annotations

import io
import re
import zipfile
import copy
from typing import List, Dict, Optional, Any
from lxml import etree

from wcag.models import Finding, RemediationResult

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
DC = "http://purl.org/dc/elements/1.1/"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"


def _parse_slide_shape_id(remediation_id: str):
    m = re.match(r'.*_(\d+)_(\d+)$', remediation_id)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _parse_slide_num(remediation_id: str) -> Optional[int]:
    m = re.match(r'.*_(\d+)$', remediation_id)
    if m:
        return int(m.group(1))
    return None


def _find_cNvPr_by_shape_id(spTree: etree._Element, shape_id: int) -> Optional[etree._Element]:
    for el in spTree.iter():
        tag = el.tag.split('}')[-1] if '}' in el.tag else el.tag
        if tag == 'cNvPr':
            try:
                if int(el.get('id', -1)) == shape_id:
                    return el
            except ValueError:
                pass
    return None


def _get_slide_path(slide_num: int) -> str:
    return f'ppt/slides/slide{slide_num}.xml'


class PptxRemediator:
    def __init__(self, file_bytes: bytes):
        self.original_bytes = file_bytes

    def apply(self, findings: List[Finding],
               remediation_ids: Optional[List[str]] = None,
               remediation_overrides: Optional[Dict[str, Any]] = None) -> RemediationResult:
        """
        Apply remediations for a list of findings.
        If remediation_ids is provided, only apply those specific IDs.
        remediation_overrides: map of remediation_id → override data (e.g. {"alt_text_1_5": {"text": "A bar chart..."}})
        """
        if remediation_overrides is None:
            remediation_overrides = {}

        # Filter findings to only those with remediation_ids that we should apply
        targets = [f for f in findings if f.remediation_id and
                   (remediation_ids is None or f.remediation_id in remediation_ids)]

        if not targets:
            return RemediationResult(
                success=True,
                skipped_remediations=[f.remediation_id for f in findings if f.remediation_id],
            )

        result = RemediationResult(success=True)

        # Work on the zip in memory
        in_buf = io.BytesIO(self.original_bytes)
        out_buf = io.BytesIO()

        with zipfile.ZipFile(in_buf, 'r') as zin, zipfile.ZipFile(out_buf, 'w', zipfile.ZIP_DEFLATED) as zout:
            # Read all files into memory; we'll modify specific ones
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
                    elif action == 'fix_list_levels':
                        self._apply_fix_list_levels(file_cache, rdata)
                        result.applied_remediations.append(rid)
                    elif action == 'fix_reading_order':
                        self._apply_fix_reading_order(file_cache, rdata)
                        result.applied_remediations.append(rid)
                    elif action == 'set_language':
                        self._apply_set_language(file_cache, rdata)
                        result.applied_remediations.append(rid)
                    elif action == 'set_doc_title':
                        self._apply_set_doc_title(file_cache, rdata)
                        result.applied_remediations.append(rid)
                    elif action in ('fix_slide_title',):
                        # Requires human-provided text — skip unless override given
                        new_text = rdata.get('new_text')
                        if new_text:
                            self._apply_fix_slide_title(file_cache, rdata, new_text)
                            result.applied_remediations.append(rid)
                        else:
                            result.skipped_remediations.append(rid)
                    else:
                        result.skipped_remediations.append(rid)
                except Exception as e:
                    result.errors.append(f"{rid}: {e}")

            # Write all (modified) files to output zip
            for name, data in file_cache.items():
                zout.writestr(name, data)

        if result.errors:
            result.success = len(result.errors) < len(targets)
        result.file_bytes = out_buf.getvalue()
        return result

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _apply_set_alt_text(self, cache: Dict[str, bytes], rdata: dict):
        slide_num = rdata['slide']
        shape_id = rdata['shape_id']
        alt_text = rdata.get('text', '')
        path = _get_slide_path(slide_num)
        if path not in cache:
            raise ValueError(f"Slide file not found: {path}")
        root = etree.fromstring(cache[path])
        spTree = root.find(f'.//{{{P}}}spTree')
        if spTree is None:
            raise ValueError("spTree not found in slide")
        cNvPr = _find_cNvPr_by_shape_id(spTree, shape_id)
        if cNvPr is None:
            raise ValueError(f"Shape id={shape_id} not found on slide {slide_num}")
        cNvPr.set('descr', alt_text)
        cache[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)

    def _apply_fix_list_levels(self, cache: Dict[str, bytes], rdata: dict):
        """Reverse an inverted list level sequence."""
        slide_num = rdata['slide']
        shape_id = rdata['shape_id']
        path = _get_slide_path(slide_num)
        if path not in cache:
            raise ValueError(f"Slide file not found: {path}")
        root = etree.fromstring(cache[path])
        spTree = root.find(f'.//{{{P}}}spTree')
        if spTree is None:
            raise ValueError("spTree not found")
        cNvPr = _find_cNvPr_by_shape_id(spTree, shape_id)
        if cNvPr is None:
            raise ValueError(f"Shape id={shape_id} not found")
        # Navigate up to the sp element to find txBody
        sp = cNvPr
        while sp is not None and sp.tag != f'{{{P}}}sp':
            sp = sp.getparent()
        if sp is None:
            raise ValueError("Parent sp element not found")
        txBody = sp.find(f'{{{P}}}txBody')
        if txBody is None:
            raise ValueError("txBody not found")

        # Collect paragraphs with text and their current levels
        text_paras = []
        for p in txBody.findall(f'{{{A}}}p'):
            text = ''.join(r.text or '' for r in p.findall(f'.//{{{A}}}t')).strip()
            if text:
                text_paras.append(p)

        if not text_paras:
            return

        # Collect current levels
        current_levels = []
        for p in text_paras:
            pPr = p.find(f'{{{A}}}pPr')
            lvl = int(pPr.get('lvl', '0')) if pPr is not None else 0
            current_levels.append(lvl)

        # Compute max level and invert: new_lvl = max - old_lvl
        max_lvl = max(current_levels)
        for p, old_lvl in zip(text_paras, current_levels):
            new_lvl = max_lvl - old_lvl
            pPr = p.find(f'{{{A}}}pPr')
            if pPr is None:
                pPr = etree.SubElement(p, f'{{{A}}}pPr')
                p.insert(0, pPr)
            if new_lvl == 0:
                # Remove lvl attribute entirely for level 0 (default)
                if 'lvl' in pPr.attrib:
                    del pPr.attrib['lvl']
            else:
                pPr.set('lvl', str(new_lvl))

        cache[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)

    def _apply_fix_reading_order(self, cache: Dict[str, bytes], rdata: dict):
        """Move title shape to be first in spTree (lowest z-order)."""
        slide_num = rdata['slide']
        path = _get_slide_path(slide_num)
        if path not in cache:
            raise ValueError(f"Slide file not found: {path}")
        root = etree.fromstring(cache[path])
        spTree = root.find(f'.//{{{P}}}spTree')
        if spTree is None:
            raise ValueError("spTree not found")

        # Find title sp element
        title_el = None
        for sp in spTree.findall(f'{{{P}}}sp'):
            nvSpPr = sp.find(f'{{{P}}}nvSpPr')
            if nvSpPr is not None:
                nvPr = nvSpPr.find(f'{{{P}}}nvPr')
                if nvPr is not None:
                    ph = nvPr.find(f'{{{P}}}ph')
                    if ph is not None and ph.get('type') in ('title', 'ctrTitle'):
                        title_el = sp
                        break

        if title_el is None:
            raise ValueError("Title shape not found")

        # Move title to first sp position in spTree
        # spTree children: spTreePr, grpSpPr, then shapes
        # Find first non-metadata child
        spTree.remove(title_el)
        # Insert after spGrpSpPr and grpSpPr
        insert_at = 0
        for i, child in enumerate(spTree):
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if tag in ('nvGrpSpPr', 'grpSpPr'):
                insert_at = i + 1
        spTree.insert(insert_at, title_el)
        cache[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)

    def _apply_set_language(self, cache: Dict[str, bytes], rdata: dict):
        lang = rdata.get('suggested_lang', 'en-US')
        path = 'ppt/presentation.xml'
        if path not in cache:
            raise ValueError("presentation.xml not found")
        root = etree.fromstring(cache[path])
        dts = root.find(f'.//{{{P}}}defaultTextStyle')
        if dts is None:
            raise ValueError("defaultTextStyle not found in presentation.xml")
        # Set lang on all defRPr elements
        for rPr in dts.findall(f'.//{{{A}}}defRPr'):
            rPr.set('lang', lang)
        cache[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)

    def _apply_set_doc_title(self, cache: Dict[str, bytes], rdata: dict):
        new_title = rdata.get('new_title', '')
        path = 'docProps/core.xml'
        if path not in cache:
            raise ValueError("docProps/core.xml not found")
        root = etree.fromstring(cache[path])
        title_el = root.find(f'{{{DC}}}title')
        if title_el is None:
            title_el = etree.SubElement(root, f'{{{DC}}}title')
        title_el.text = new_title
        cache[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)

    def _apply_fix_slide_title(self, cache: Dict[str, bytes], rdata: dict, new_text: str):
        slide_num = rdata['slide']
        path = _get_slide_path(slide_num)
        if path not in cache:
            raise ValueError(f"Slide file not found: {path}")
        root = etree.fromstring(cache[path])
        for sp in root.findall(f'.//{{{P}}}sp'):
            nvSpPr = sp.find(f'{{{P}}}nvSpPr')
            if nvSpPr is not None:
                nvPr = nvSpPr.find(f'{{{P}}}nvPr')
                if nvPr is not None:
                    ph = nvPr.find(f'{{{P}}}ph')
                    if ph is not None and ph.get('type') in ('title', 'ctrTitle'):
                        txBody = sp.find(f'{{{P}}}txBody')
                        if txBody is not None:
                            for p_el in txBody.findall(f'{{{A}}}p'):
                                for r_el in p_el.findall(f'{{{A}}}r'):
                                    for t_el in r_el.findall(f'{{{A}}}t'):
                                        t_el.text = ''
                            # Set text in first paragraph's first run
                            p_els = txBody.findall(f'{{{A}}}p')
                            if p_els:
                                first_p = p_els[0]
                                r_els = first_p.findall(f'{{{A}}}r')
                                if r_els:
                                    t_el = r_els[0].find(f'{{{A}}}t')
                                    if t_el is not None:
                                        t_el.text = new_text
                                else:
                                    r_new = etree.SubElement(first_p, f'{{{A}}}r')
                                    t_new = etree.SubElement(r_new, f'{{{A}}}t')
                                    t_new.text = new_text
                        break
        cache[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
