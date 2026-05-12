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


def detect_type(filename: str, content_type: str) -> Optional[str]:
    """Determine file type from filename extension or content-type."""
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
    return SUPPORTED_TYPES.get(content_type)