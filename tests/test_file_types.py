import io
import zipfile

from wcag.file_types import detect_type


def _ooxml_bytes(member_name: str, content: bytes = b'<xml/>') -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('[Content_Types].xml', b'<Types/>')
        archive.writestr(member_name, content)
    return buffer.getvalue()


def test_detect_type_uses_filename_when_available():
    assert detect_type('sample.docx', 'application/octet-stream') == 'docx'
    assert detect_type('sample.pptx', 'application/octet-stream') == 'pptx'
    assert detect_type('sample.xlsx', 'application/octet-stream') == 'xlsx'
    assert detect_type('sample.pdf', 'application/octet-stream') == 'pdf'
    assert detect_type('sample.html', 'application/octet-stream') == 'html'


def test_detect_type_sniffs_docx_when_filename_is_generic():
    payload = _ooxml_bytes('word/document.xml')
    assert detect_type('upload.bin', 'application/octet-stream', payload) == 'docx'


def test_detect_type_sniffs_pptx_when_filename_is_generic():
    payload = _ooxml_bytes('ppt/presentation.xml')
    assert detect_type('upload.bin', 'application/octet-stream', payload) == 'pptx'


def test_detect_type_sniffs_xlsx_when_filename_is_generic():
    payload = _ooxml_bytes('xl/workbook.xml')
    assert detect_type('upload.bin', 'application/octet-stream', payload) == 'xlsx'


def test_detect_type_sniffs_pdf_when_filename_is_generic():
    payload = b'%PDF-1.7\n1 0 obj\n<<>>\nendobj\n'
    assert detect_type('upload.bin', 'application/octet-stream', payload) == 'pdf'


def test_detect_type_sniffs_html_when_filename_is_generic():
    payload = b'<!DOCTYPE html><html><head><title>x</title></head><body></body></html>'
    assert detect_type('upload.bin', 'application/octet-stream', payload) == 'html'


def test_detect_type_returns_none_for_unknown_bytes():
    payload = b'not-a-supported-file-format'
    assert detect_type('upload.bin', 'application/octet-stream', payload) is None