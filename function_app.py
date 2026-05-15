"""
WCAG Analyzer — Azure Function v2 Entry Point
Two routes:
  POST /api/analyze    — Accepts multipart/form-data with 'file' field.
                         Returns FactSheet JSON.
  POST /api/remediate  — Accepts multipart/form-data with 'file' field
                         and JSON body fields:
                           remediation_ids: list of IDs to apply
                           remediation_overrides: dict of id → override data
                         Returns:
                           result JSON with applied/skipped/errors
                           file_bytes as base64 in result.file_b64
"""
import azure.functions as func
import json
import base64
import logging
from typing import Optional, Tuple

# Lazy imports: heavy analyzer/remediator modules (lxml, pikepdf, openpyxl,
# python-pptx, python-docx, pytesseract, pdf2image, etc.) are imported inside
# the route handlers based on detected file type. This keeps cold-start cost
# proportional to a single request's needs instead of the union of all formats.
from wcag.file_types import detect_type
from wcag.models import FactSheet, RemediationResult

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

# OCR tuning for "auto" vs "deep" paths
OCR_MAX_PAGES_DEEP = 20
OCR_MAX_PAGES_AUTO = 3


def _build_analyzer(file_type: str, file_bytes: bytes, filename: str):
    """Lazily import and instantiate the analyzer for a given file type."""
    if file_type == 'pptx':
        from wcag.analyzers.pptx_analyzer import PptxAnalyzer
        return PptxAnalyzer(file_bytes, filename)
    if file_type == 'docx':
        from wcag.analyzers.docx_analyzer import DocxAnalyzer
        return DocxAnalyzer(file_bytes, filename)
    if file_type == 'pdf':
        from wcag.analyzers.pdf_analyzer import PdfAnalyzer
        return PdfAnalyzer(file_bytes, filename)
    if file_type == 'xlsx':
        from wcag.analyzers.xlsx_analyzer import XlsxAnalyzer
        return XlsxAnalyzer(file_bytes, filename)
    from wcag.analyzers.html_analyzer import HtmlAnalyzer
    return HtmlAnalyzer(file_bytes, filename)


def _build_remediator(file_type: str, file_bytes: bytes):
    """Lazily import and instantiate the remediator for a given file type."""
    if file_type == 'pptx':
        from wcag.remediators.pptx_remediator import PptxRemediator
        return PptxRemediator(file_bytes)
    from wcag.remediators.docx_remediator import DocxRemediator
    return DocxRemediator(file_bytes)


def _get_file_from_request(req: func.HttpRequest):
    """
    Extract file bytes and filename from a multipart/form-data request.
    Returns (file_bytes, filename, error_response).
    """
    try:
        files = req.files
        if 'file' in files:
            f = files['file']
            file_bytes = f.read()
            filename = f.filename or 'upload.bin'
            return file_bytes, filename, None
    except Exception:
        pass

    # Fallback: raw body with filename from query string or header
    body = req.get_body()
    if not body:
        return None, None, func.HttpResponse(
            json.dumps({"error": "No file provided. Send a multipart/form-data request with a 'file' field."}),
            status_code=400, mimetype='application/json'
        )
    filename = req.params.get('filename', 'upload.bin')
    return body, filename, None


def _normalize_ocr_mode(raw_value: Optional[str]) -> str:
    """
    Normalize OCR mode input.
    Supported values:
      - auto  : smart middle-ground, run lightweight OCR only when likely useful
      - true  : always run deep OCR
      - false : skip OCR entirely

    Backward-compatible aliases:
      true aliases  -> "1", "yes", "on"
      false aliases -> "0", "no", "off"
    """
    value = (raw_value or 'auto').strip().lower()
    if value in ('true', '1', 'yes', 'on'):
        return 'true'
    if value in ('false', '0', 'no', 'off'):
        return 'false'
    if value == 'auto':
        return 'auto'
    logging.warning("Unknown includeOCR value '%s'; falling back to 'auto'.", value)
    return 'auto'


def _has_possible_criterion(fact_sheet: FactSheet, criterion_id: str) -> bool:
    return any(
        f.criterion_id.split(';')[0].strip() == criterion_id
        for f in fact_sheet.possible_findings
    )


def _image_risk_score(file_type: str, fact_sheet: FactSheet) -> int:
    """Estimate visual/text-in-image risk from Layer 1 structural facts."""
    if file_type == 'docx':
        return len(fact_sheet.images or [])

    # PPTX: count image/chart shapes as likely visual content carriers.
    score = 0
    for slide in (fact_sheet.slides or []):
        for shape in slide:
            if shape.shape_type in ('image', 'chart'):
                score += 1
    return score


def _should_run_auto_ocr(file_type: str, fact_sheet: FactSheet) -> Tuple[bool, str]:
    """
    Smart OCR trigger for in-between mode.
    Runs lightweight OCR only when there is a meaningful chance of OCR-only value.
    """
    if file_type == 'pdf':
        has_scanned_hint = any(
            getattr(finding, 'remediation_id', None) == 'pdf_scanned_image_only'
            for finding in fact_sheet.possible_findings
        )
        if has_scanned_hint:
            return True, 'pdf-scanned-image-only-hint'
        return False, 'pdf-no-scanned-image-only-hint'

    if file_type not in ('pptx', 'docx'):
        return False, 'non-office-format'

    # Strongest signal: Layer 1 already suspects image-of-text.
    if _has_possible_criterion(fact_sheet, '1.4.5'):
        return True, 'possible-1.4.5-present'

    risk_score = _image_risk_score(file_type, fact_sheet)
    if risk_score >= 3:
        return True, f'visual-density-score-{risk_score}'

    return False, f'low-visual-density-score-{risk_score}'


@app.route(route="analyze", methods=["POST"])
def analyze(req: func.HttpRequest) -> func.HttpResponse:
    """
    POST /api/analyze
    Accepts a PPTX, DOCX, or HTML file and returns a WCAG fact sheet JSON.
    """
    file_bytes, filename, err = _get_file_from_request(req)
    if err:
        return err

    if len(file_bytes) > MAX_FILE_SIZE:
        return func.HttpResponse(
            json.dumps({"error": f"File exceeds maximum size of {MAX_FILE_SIZE // (1024*1024)}MB."}),
            status_code=413, mimetype='application/json'
        )

    content_type = req.headers.get('Content-Type', '')
    file_type = detect_type(filename, content_type, file_bytes)

    if file_type not in ('pptx', 'docx', 'html', 'pdf', 'xlsx'):
        return func.HttpResponse(
            json.dumps({"error": f"Unsupported file type. Send a .pptx, .docx, .html, .pdf, or .xlsx file. Detected: '{filename}'"}),
            status_code=415, mimetype='application/json'
        )

    try:
        analyzer = _build_analyzer(file_type, file_bytes, filename)
        fact_sheet: FactSheet = analyzer.analyze()

        # Layer 3: OCR analysis modes via includeOCR query parameter:
        #   auto  (default): run lightweight OCR only when likely useful
        #   true           : always run deep OCR
        #   false          : never run OCR
        ocr_mode = _normalize_ocr_mode(req.params.get('includeOCR', 'auto'))
        run_ocr = False
        ocr_max_pages = OCR_MAX_PAGES_DEEP

        if file_type in ('pptx', 'docx', 'pdf'):
            if ocr_mode == 'true':
                run_ocr = True
                ocr_max_pages = OCR_MAX_PAGES_DEEP
            elif ocr_mode == 'auto':
                run_ocr, reason = _should_run_auto_ocr(file_type, fact_sheet)
                if run_ocr:
                    ocr_max_pages = OCR_MAX_PAGES_AUTO
                    logging.info("OCR auto enabled for %s (%s)", filename, reason)
                else:
                    logging.info("OCR auto skipped for %s (%s)", filename, reason)

        if run_ocr:
            from wcag.analyzers.ocr_analyzer import OcrAnalyzer
            ocr = OcrAnalyzer(file_bytes, filename, fact_sheet)
            ocr.run(max_pages=ocr_max_pages)

        return func.HttpResponse(
            json.dumps(fact_sheet.to_dict(), indent=2),
            status_code=200, mimetype='application/json'
        )
    except Exception as e:
        logging.exception("analyze: error processing file")
        return func.HttpResponse(
            json.dumps({"error": f"Failed to analyze file: {str(e)}"}),
            status_code=500, mimetype='application/json'
        )


@app.route(route="remediate", methods=["POST"])
def remediate(req: func.HttpRequest) -> func.HttpResponse:
    """
    POST /api/remediate
    Body (multipart/form-data):
      file               — the original .pptx or .docx
      fact_sheet         — JSON string of FactSheet from /analyze
      remediation_ids    — JSON array of remediation IDs to apply (optional — applies all if omitted)
      remediation_overrides — JSON object of id → override data (optional)
    Returns JSON:
      {
        "success": bool,
        "applied_remediations": [...],
        "skipped_remediations": [...],
        "errors": [...],
        "file_size_bytes": int,
        "file_b64": "<base64-encoded fixed file>"
      }
    """
    file_bytes, filename, err = _get_file_from_request(req)
    if err:
        return err

    if len(file_bytes) > MAX_FILE_SIZE:
        return func.HttpResponse(
            json.dumps({"error": "File exceeds maximum size."}),
            status_code=413, mimetype='application/json'
        )

    # Parse additional form fields
    try:
        form = req.form
        fact_sheet_json = form.get('fact_sheet', '')
        remediation_ids_json = form.get('remediation_ids', 'null')
        overrides_json = form.get('remediation_overrides', '{}')
    except Exception:
        fact_sheet_json = ''
        remediation_ids_json = 'null'
        overrides_json = '{}'

    # Parse fact sheet to get findings
    try:
        fs_data = json.loads(fact_sheet_json) if fact_sheet_json else {}
    except json.JSONDecodeError as e:
        return func.HttpResponse(
            json.dumps({"error": f"Invalid fact_sheet JSON: {e}"}),
            status_code=400, mimetype='application/json'
        )

    try:
        remediation_ids = json.loads(remediation_ids_json)
    except json.JSONDecodeError:
        remediation_ids = None

    try:
        overrides = json.loads(overrides_json)
    except json.JSONDecodeError:
        overrides = {}

    content_type = req.headers.get('Content-Type', '')
    file_type = detect_type(filename, content_type)

    if file_type not in ('pptx', 'docx'):
        return func.HttpResponse(
            json.dumps({"error": f"Unsupported file type: '{filename}'"}),
            status_code=415, mimetype='application/json'
        )

    # Re-analyze to get Finding objects (fact_sheet_json is for context only)
    try:
        analyzer = _build_analyzer(file_type, file_bytes, filename)
        fact_sheet = analyzer.analyze()
    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": f"Failed to re-analyze file: {str(e)}"}),
            status_code=500, mimetype='application/json'
        )

    all_findings = fact_sheet.confirmed_findings + fact_sheet.possible_findings

    try:
        remediator = _build_remediator(file_type, file_bytes)
        result: RemediationResult = remediator.apply(
            findings=all_findings,
            remediation_ids=remediation_ids,
            remediation_overrides=overrides,
        )
    except Exception as e:
        logging.exception("remediate: error applying remediations")
        return func.HttpResponse(
            json.dumps({"error": f"Failed to remediate file: {str(e)}"}),
            status_code=500, mimetype='application/json'
        )

    result_dict = result.to_dict()
    if result.file_bytes:
        result_dict['file_b64'] = base64.b64encode(result.file_bytes).decode('ascii')
    else:
        result_dict['file_b64'] = None

    return func.HttpResponse(
        json.dumps(result_dict),
        status_code=200, mimetype='application/json'
    )
