import io
import zipfile
from typing import Optional


SUPPORTED_TYPES = {
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
    'text/html': 'html',
    'application/xhtml+xml': 'html',
    'application/pdf': 'pdf',
    'application/octet-stream': None,
}


def _detect_type_from_bytes(file_bytes: Optional[bytes]) -> Optional[str]:
    """Best-effort content sniffing when filename is missing or generic."""
    if not file_bytes:
        return None

    sample = file_bytes[:4096].lstrip()
    if sample.startswith(b'%PDF-'):
        return 'pdf'

    lower_sample = sample.lower()
    if (
        lower_sample.startswith(b'<!doctype html')
        or lower_sample.startswith(b'<html')
        or b'<html' in lower_sample[:512]
    ):
        return 'html'

    if not file_bytes.startswith(b'PK'):
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return None

    if 'word/document.xml' in names:
        return 'docx'
    if 'ppt/presentation.xml' in names:
        return 'pptx'
    if 'xl/workbook.xml' in names:
        return 'xlsx'
    return None


def detect_type(filename: str, content_type: str, file_bytes: Optional[bytes] = None) -> Optional[str]:
    """Determine file type from filename extension, content-type, or file bytes."""
    name = (filename or '').lower()
    if name.endswith('.pptx'):
        return 'pptx'
    if name.endswith('.docx'):
        return 'docx'
    if name.endswith('.xlsx'):
        return 'xlsx'
    if name.endswith('.html') or name.endswith('.htm'):
        return 'html'
    if name.endswith('.pdf'):
        return 'pdf'
    detected = SUPPORTED_TYPES.get(content_type)
    if detected:
        return detected
    return _detect_type_from_bytes(file_bytes)