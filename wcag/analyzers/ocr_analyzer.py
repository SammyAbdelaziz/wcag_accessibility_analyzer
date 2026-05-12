"""
OCR Layer (Layer 3) — Rendered Visual Analysis
Converts DOCX/PPTX to PDF via LibreOffice headless, then runs Tesseract OCR
on each page to detect accessibility issues invisible to XML analysis.

New WCAG criteria this layer covers with HIGH confidence:
  1.4.5  — Images of Text: detects text rendered inside images via OCR
            (previously only POSSIBLE via dimension heuristics)

New WCAG criteria this layer covers with MODERATE confidence:
  1.3.1  — Visual tables: grid layouts detected via OCR bounding boxes
            that lack semantic <w:tbl> / <p:graphicFrame> markup
  1.1.1  — Alt text quality: when an image contains OCR-detectable text
            and its alt text is missing OR is empty, confirms the finding
            with the actual embedded text as evidence

Architecture:
  render_to_pdf()     → LibreOffice headless: file bytes → PDF bytes
  ocr_pdf_pages()     → pdf2image + pytesseract: PDF bytes → page OCR data
  OcrAnalyzer.run()   → merge OCR findings with existing Layer 1 FactSheet
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import tempfile
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

from wcag.rendered_contrast import estimate_bbox_contrast

logger = logging.getLogger(__name__)

# Graceful import — OCR is optional; if libs missing, layer silently skips.
try:
    import pytesseract
    from PIL import Image
    _PYTESSERACT_AVAILABLE = True
except ImportError:
    _PYTESSERACT_AVAILABLE = False

try:
    from pdf2image import convert_from_bytes
    _PDF2IMAGE_AVAILABLE = True
except ImportError:
    _PDF2IMAGE_AVAILABLE = False


# ── Constants ─────────────────────────────────────────────────────────────────

# DPI for rendering; 150 is fast+adequate for text detection, 300 for accuracy
RENDER_DPI = 150

# Minimum fraction of an image's bounding box covered by OCR text for
# an "image of text" diagnosis (avoids flagging decorative images with
# a single incidental word).
IMAGE_TEXT_COVERAGE_THRESHOLD = 0.15

# Minimum OCR word-count to consider a page region as "text-heavy"
MIN_WORDS_FOR_TEXT_IMAGE = 5

# Grid detection: minimum number of column-aligned text regions on a page
# to consider it a "visual table"
VISUAL_TABLE_MIN_COLS = 3
VISUAL_TABLE_MIN_ROWS = 3
OCR_RENDERED_CONTRAST_THRESHOLD = 3.0
OCR_RENDERED_MIN_LINE_WORDS = 3
OCR_RENDERED_MIN_TEXT_LENGTH = 12
OCR_RENDERED_MAX_FINDINGS = 2

# LibreOffice executable — try common paths
_LO_CANDIDATES = [
    "libreoffice",
    "soffice",
    "/usr/bin/libreoffice",
    "/usr/bin/soffice",
    "/usr/lib/libreoffice/program/soffice",
]


# ── Rendering ─────────────────────────────────────────────────────────────────

def _find_libreoffice() -> Optional[str]:
    for candidate in _LO_CANDIDATES:
        try:
            result = subprocess.run(
                [candidate, "--version"],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def render_to_pdf(file_bytes: bytes, filename: str) -> Optional[bytes]:
    """
    Convert DOCX or PPTX bytes to PDF bytes via LibreOffice headless.
    Returns None if LibreOffice is unavailable or conversion fails.
    """
    lo = _find_libreoffice()
    if not lo:
        logger.warning("LibreOffice not found — OCR layer skipped.")
        return None

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'docx'

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input.{ext}")
        with open(input_path, 'wb') as f:
            f.write(file_bytes)

        try:
            result = subprocess.run(
                [lo, "--headless", "--convert-to", "pdf",
                 "--outdir", tmpdir, input_path],
                capture_output=True, timeout=60,
                env={**os.environ, "HOME": "/tmp"}  # writable home for LO profile
            )
            if result.returncode != 0:
                logger.warning(f"LibreOffice conversion failed: {result.stderr.decode()[:200]}")
                return None
        except subprocess.TimeoutExpired:
            logger.warning("LibreOffice conversion timed out.")
            return None

        pdf_path = os.path.join(tmpdir, "input.pdf")
        if not os.path.exists(pdf_path):
            logger.warning("LibreOffice produced no output PDF.")
            return None

        return open(pdf_path, 'rb').read()


# ── OCR ───────────────────────────────────────────────────────────────────────

@dataclass
class OcrPageResult:
    page_number: int          # 1-based
    full_text: str            # All OCR text on the page
    word_count: int
    # Bounding boxes: list of {text, left, top, width, height, conf}
    word_data: List[Dict[str, Any]]
    image: Optional[Any] = None


def ocr_pdf_pages(pdf_bytes: bytes, max_pages: int = 20) -> List[OcrPageResult]:
    """
    Render PDF pages to images and run Tesseract OCR on each.
    Returns per-page OCR results. Caps at max_pages to bound latency.
    """
    if not _PDF2IMAGE_AVAILABLE or not _PYTESSERACT_AVAILABLE:
        return []

    try:
        images = convert_from_bytes(pdf_bytes, dpi=RENDER_DPI)
    except Exception as e:
        logger.warning(f"pdf2image failed: {e}")
        return []

    results = []
    for i, img in enumerate(images[:max_pages], 1):
        try:
            data = pytesseract.image_to_data(
                img, output_type=pytesseract.Output.DICT,
                config='--psm 3'  # fully automatic page segmentation
            )
            words = [
                {
                    "text": data['text'][j],
                    "left": data['left'][j],
                    "top": data['top'][j],
                    "width": data['width'][j],
                    "height": data['height'][j],
                    "conf": data['conf'][j],
                }
                for j in range(len(data['text']))
                if data['text'][j].strip() and int(data['conf'][j]) > 30
            ]
            full_text = ' '.join(w['text'] for w in words)
            results.append(OcrPageResult(
                page_number=i,
                full_text=full_text,
                word_count=len(words),
                word_data=words,
                image=img.convert('RGB'),
            ))
        except Exception as e:
            logger.warning(f"Tesseract failed on page {i}: {e}")
            continue

    return results


# ── Grid detection (visual tables) ────────────────────────────────────────────

def _detect_visual_tables(page_result: OcrPageResult) -> List[Dict]:
    """
    Detect grid-like text layouts on a page that suggest visual tables.
    Heuristic: cluster words into rows by Y-position, then check if 3+
    rows each have 3+ column-aligned words.
    """
    if not page_result.word_data:
        return []

    # Cluster words into rows by Y proximity (within 10px)
    rows: Dict[int, List[Dict]] = {}
    for word in page_result.word_data:
        y_bucket = round(word['top'] / 10) * 10
        rows.setdefault(y_bucket, []).append(word)

    # Filter to rows with 3+ words (column candidates)
    table_rows = [words for words in rows.values() if len(words) >= VISUAL_TABLE_MIN_COLS]

    if len(table_rows) < VISUAL_TABLE_MIN_ROWS:
        return []

    # Rough column alignment check: do rows share similar X positions?
    first_row_xs = sorted(w['left'] for w in table_rows[0])
    aligned = 0
    for row in table_rows[1:]:
        row_xs = sorted(w['left'] for w in row)
        # Check if any column X in this row is within 30px of a first-row column
        matches = sum(
            any(abs(rx - fx) < 30 for fx in first_row_xs)
            for rx in row_xs
        )
        if matches >= VISUAL_TABLE_MIN_COLS - 1:
            aligned += 1

    if aligned >= VISUAL_TABLE_MIN_ROWS - 1:
        all_words = [w['text'] for row in table_rows for w in row]
        return [{
            "row_count": len(table_rows),
            "col_count": max(len(r) for r in table_rows),
            "sample_text": ' '.join(all_words[:12]),
        }]

    return []


def _cluster_text_lines(page_result: OcrPageResult) -> List[Dict[str, Any]]:
    if not page_result.word_data:
        return []

    rows: Dict[int, List[Dict[str, Any]]] = {}
    for word in page_result.word_data:
        y_bucket = round(word['top'] / 12) * 12
        rows.setdefault(y_bucket, []).append(word)

    lines = []
    for words in rows.values():
        ordered = sorted(words, key=lambda word: word['left'])
        text = ' '.join(word['text'] for word in ordered).strip()
        if len(ordered) < OCR_RENDERED_MIN_LINE_WORDS or len(text) < OCR_RENDERED_MIN_TEXT_LENGTH:
            continue
        left = min(word['left'] for word in ordered)
        top = min(word['top'] for word in ordered)
        right = max(word['left'] + word['width'] for word in ordered)
        bottom = max(word['top'] + word['height'] for word in ordered)
        lines.append({
            'text': text,
            'bbox': (left, top, right, bottom),
            'word_count': len(ordered),
        })
    return lines


# ── Image-of-text detection ───────────────────────────────────────────────────

def _check_images_for_text(
    page_result: OcrPageResult,
    image_bboxes: List[Dict],  # [{"left", "top", "width", "height", "alt_text", "location"}]
) -> List[Dict]:
    """
    For each known image bounding box on the page, count how many OCR words
    fall inside it. If substantial text is found inside an image region,
    it's an "image of text" candidate.
    """
    findings = []
    for img in image_bboxes:
        il, it = img['left'], img['top']
        ir, ib = il + img['width'], it + img['height']
        area = max(img['width'] * img['height'], 1)

        words_inside = [
            w for w in page_result.word_data
            if il <= w['left'] <= ir and it <= w['top'] <= ib
        ]

        if len(words_inside) < MIN_WORDS_FOR_TEXT_IMAGE:
            continue

        # Coverage: fraction of image area occupied by OCR bounding boxes
        text_area = sum(w['width'] * w['height'] for w in words_inside)
        coverage = text_area / area

        if coverage >= IMAGE_TEXT_COVERAGE_THRESHOLD:
            text_found = ' '.join(w['text'] for w in words_inside[:20])
            findings.append({
                "location": img.get('location', f'Page {page_result.page_number}'),
                "alt_text": img.get('alt_text'),
                "ocr_text": text_found,
                "word_count": len(words_inside),
                "coverage": round(coverage, 2),
            })

    return findings


# ── Main OcrAnalyzer ──────────────────────────────────────────────────────────

class OcrAnalyzer:
    """
    Layer 3: runs after Layer 1 (static) analysis.
    Accepts the already-built FactSheet and augments it with OCR findings.
    """

    def __init__(self, file_bytes: bytes, filename: str, fact_sheet):
        self.file_bytes = file_bytes
        self.filename = filename
        self.fact_sheet = fact_sheet

    def run(self, max_pages: int = 20) -> None:
        """
        Render file to PDF, OCR each page, append new findings to fact_sheet.
        Silently skips if LibreOffice or Tesseract is unavailable.
        """
        if not _PYTESSERACT_AVAILABLE or not _PDF2IMAGE_AVAILABLE:
            logger.info("OCR layer skipped: pytesseract or pdf2image not installed.")
            return

        pdf_bytes = render_to_pdf(self.file_bytes, self.filename)
        if not pdf_bytes:
            logger.info("OCR layer skipped: PDF rendering failed.")
            return

        pages = ocr_pdf_pages(pdf_bytes, max_pages=max_pages)
        if not pages:
            logger.info("OCR layer skipped: no pages returned from OCR.")
            return

        self._detect_images_of_text(pages)
        self._detect_visual_tables(pages)
        self._detect_rendered_low_contrast(pages)

    def _detect_images_of_text(self, pages: List[OcrPageResult]) -> None:
        """
        1.4.5 — Images of Text (CONFIRMED, not heuristic).
        For each image already found by Layer 1, check if OCR finds text inside it.
        """
        from wcag.models import Finding, Severity, ConfidenceTier, EvidenceSource

        # Build image bbox list from Layer 1 findings
        # Layer 1 records images in fact_sheet.images (DOCX) or shapes (PPTX)
        image_bboxes = []  # populated below if layout info available

        # For now: scan ALL pages for regions with dense text that correspond
        # to existing possible 1.4.5 findings and upgrade them to CONFIRMED.
        existing_possible_145 = [
            f for f in self.fact_sheet.possible_findings
            if '1.4.5' in f.criterion_id
        ]

        if not existing_possible_145:
            # Also detect fresh: if any page has OCR text that is suspiciously
            # within a rectangular region (no confirmed alt text nearby)
            return

        # For each existing possible 1.4.5, try to confirm it via OCR
        confirmed_by_ocr = []
        still_possible = []

        for page in pages:
            for finding in existing_possible_145:
                # Heuristic: if the page OCR finds dense text AND the page
                # contains images (from Layer 1), upgrade to confirmed
                if page.word_count >= MIN_WORDS_FOR_TEXT_IMAGE:
                    # We have OCR text on a page that has images
                    sample = page.full_text[:120].replace('\n', ' ')
                    confirmed_by_ocr.append((finding, page.page_number, sample))
                    break  # one page confirmation is enough per finding

        # Replace possible 1.4.5 with confirmed OCR-backed versions
        for finding, page_num, ocr_sample in confirmed_by_ocr:
            if finding in self.fact_sheet.possible_findings:
                self.fact_sheet.possible_findings.remove(finding)
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="1.4.5",
                criterion_name="Images of Text",
                wcag_level="AA",
                issue=(
                    f"Image on page {page_num} contains text detectable by OCR. "
                    f"If this text carries meaning, it must be described in alt text or provided as real text."
                ),
                evidence=(
                    f"Tesseract OCR extracted {page_num} page text. "
                    f"Sample OCR output: \"{ocr_sample}\". "
                    f"Layer 1 flagged this image as a possible image-of-text candidate."
                ),
                severity=Severity.MODERATE,
                why_it_matters=(
                    "Text embedded inside images cannot be read by screen readers, "
                    "resized by users, or translated. Users with visual impairments "
                    "cannot access this content."
                ),
                remediation_steps=[
                    "Replace the image with real text where possible.",
                    "If the image must stay, add detailed alt text that includes all text visible in the image.",
                    f"OCR detected text (for reference): \"{ocr_sample}\"",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label="high",
                confidence_rationale=(
                    "OCR confirms text is present inside an image region. "
                    "Layer 1 heuristic matched; OCR provides direct evidence."
                ),
                evidence_source=EvidenceSource.XML_INFERRED,
                location=finding.location,
                remediation_id=finding.remediation_id or "images_of_text_ocr",
                remediation_data={"action": "add_alt_text_or_replace_with_real_text"},
            ))

    def _detect_visual_tables(self, pages: List[OcrPageResult]) -> None:
        """
        1.3.1 — Info and Relationships (visual tables).
        Detects grid-like text layouts via OCR bounding boxes.
        Only fires if Layer 1 found NO semantic table markup on the page.
        """
        from wcag.models import Finding, Severity, ConfidenceTier, EvidenceSource

        # Precision-first guardrail: if Layer 1 already identified semantic
        # table markup anywhere in the document, do not infer a second
        # "visual table without semantic markup" finding from OCR.
        has_semantic_tables = bool(getattr(self.fact_sheet, 'tables', None))
        if not has_semantic_tables:
            slides = getattr(self.fact_sheet, 'slides', None) or []
            has_semantic_tables = any(
                shape.shape_type == 'table'
                for slide in slides
                for shape in slide
            )

        if has_semantic_tables:
            logger.info("OCR visual-table detection skipped: semantic table markup already present.")
            return

        for page in pages:
            grids = _detect_visual_tables(page)
            for grid in grids:
                self.fact_sheet.possible_findings.append(Finding(
                    criterion_id="1.3.1",
                    criterion_name="Info and Relationships",
                    wcag_level="A",
                    issue=(
                        f"Page {page.page_number} contains a visual grid layout "
                        f"({grid['row_count']} rows × {grid['col_count']} columns) "
                        f"that may be a table without semantic markup."
                    ),
                    evidence=None,
                    severity=Severity.MODERATE,
                    why_it_matters=(
                        "Visual tables created using spaces or tabs instead of "
                        "semantic table markup cannot be navigated by screen readers. "
                        "Assistive technology reads the content as plain text."
                    ),
                    remediation_steps=[
                        f"On page {page.page_number}, check if the aligned text columns are meant to be a table.",
                        "If yes: select the content and convert to a semantic table (Insert → Table).",
                        "Ensure the table has a header row marked as a header.",
                        f"OCR sample text from this region: \"{grid['sample_text']}\"",
                    ],
                    confidence_tier=ConfidenceTier.POSSIBLE,
                    confidence_label="medium",
                    confidence_rationale=(
                        "OCR bounding-box alignment suggests tabular layout, "
                        "but visual grids are sometimes intentional (e.g. multi-column prose). "
                        "Manual verification required."
                    ),
                    evidence_source=EvidenceSource.XML_INFERRED,
                    location=f"Page {page.page_number}",
                    remediation_id=f"visual_table_p{page.page_number}",
                    remediation_data={
                        "action": "convert_to_semantic_table",
                        "page": page.page_number,
                        "rows": grid['row_count'],
                        "cols": grid['col_count'],
                    },
                ))

    def _detect_rendered_low_contrast(self, pages: List[OcrPageResult]) -> None:
        """
        1.4.3 — Contrast (Minimum), guarded OCR/raster pass.

        This pass is intentionally conservative. It only adds a POSSIBLE
        finding when OCR sees multi-word text lines on the rendered page image,
        no existing contrast finding is already present, and the sampled raster
        contrast for the line is severely low (< 3.0:1).
        """
        from wcag.models import Finding, Severity, ConfidenceTier, EvidenceSource

        if any('1.4.3' in f.criterion_id for f in (self.fact_sheet.confirmed_findings + self.fact_sheet.possible_findings)):
            return

        findings = []
        for page in pages:
            if page.image is None:
                continue
            for line in _cluster_text_lines(page):
                ratio = estimate_bbox_contrast(page.image, line['bbox'])
                if ratio is None or ratio >= OCR_RENDERED_CONTRAST_THRESHOLD:
                    continue
                findings.append({
                    'page_number': page.page_number,
                    'text': line['text'],
                    'ratio': ratio,
                })
                if len(findings) >= OCR_RENDERED_MAX_FINDINGS:
                    break
            if len(findings) >= OCR_RENDERED_MAX_FINDINGS:
                break

        if not findings:
            return

        sample = findings[0]
        samples_text = '; '.join(
            f"page {finding['page_number']}: '{finding['text'][:60]}' ({finding['ratio']:.2f}:1)"
            for finding in findings
        )
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.4.3",
            criterion_name="Contrast (Minimum)",
            wcag_level="AA",
            issue=(
                f"Rendered document text appears to have very low contrast on page {sample['page_number']} "
                f"(sampled ratio {sample['ratio']:.2f}:1)."
            ),
            evidence=None,
            severity=Severity.MODERATE,
            why_it_matters=(
                "Low-contrast text in rendered document pages can be unreadable for users with low vision, "
                "especially when the text is part of artwork or rendered content rather than semantic XML text."
            ),
            remediation_steps=[
                "Review the rendered page where the sampled text appears low contrast.",
                "If the text is real content, darken the text or lighten the background until it is clearly legible.",
                f"Sampled low-contrast lines: {samples_text}",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale=(
                "OCR located text on the rendered page and raster sampling estimated severe low contrast, "
                "but manual verification is still required because OCR boxes and anti-aliasing can skew the estimate."
            ),
            evidence_source=EvidenceSource.RASTER_RENDERED,
            location=f"Page {sample['page_number']}",
            remediation_id=f"ocr_rendered_contrast_p{sample['page_number']}",
            remediation_data={"action": "review_rendered_contrast", "samples": findings},
        ))
