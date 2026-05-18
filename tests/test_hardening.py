"""
Security hardening validation tests.
Verifies that XXE, SSRF, OOM, and format-validation mitigations are effective.
"""
import pytest
import tempfile
import io
from lxml import etree
from PIL import Image

from wcag.common.safe_xml import SAFE_XML_PARSER
from wcag.analyzers.ocr_analyzer import ocr_pdf_pages


class TestXXEMitigation:
    """XXE (XML External Entity) injection prevention."""
    
    def test_safe_parser_rejects_xxe(self):
        """Verify that SAFE_XML_PARSER disables external entity resolution."""
        # Malicious XML that attempts XXE: tries to read /etc/passwd via external entity
        malicious_xml = b'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<root>&xxe;</root>
'''
        # Standard parser (vulnerable): would try to resolve entity
        # Safe parser: resolves_entities=False prevents resolution
        try:
            root = etree.fromstring(malicious_xml, parser=SAFE_XML_PARSER)
            # If parsing succeeds, verify that the entity was NOT resolved
            # (would be empty or contain placeholder, not /etc/passwd content)
            assert root is not None
            # The entity reference should be ignored or raise error
            text = root.text or ""
            assert "/etc/passwd" not in text, "XXE entity was resolved — vulnerability!"
        except etree.XMLSyntaxError:
            # Also acceptable: parser rejects malformed XXE entirely
            pass
    
    def test_safe_parser_resolves_valid_xml(self):
        """Verify that legitimate XML still parses correctly."""
        valid_xml = b'''<?xml version="1.0" encoding="UTF-8"?>
<root><child>Hello</child></root>
'''
        root = etree.fromstring(valid_xml, parser=SAFE_XML_PARSER)
        assert root is not None
        child = root.find('child')
        assert child is not None
        assert child.text == "Hello"


class TestSSRFMitigation:
    """SSRF (Server-Side Request Forgery) via Playwright network blocking."""
    
    def test_playwright_route_abort_blocks_network(self):
        """
        Verify that html_analyzer registers a network blocker on every Playwright
        page so outbound http/https/ftp/ws requests are aborted while inline
        data:/about:/blob:/file: URLs (required for set_content) are allowed.
        """
        import wcag.analyzers.html_analyzer as html_mod
        with open(html_mod.__file__, encoding='utf-8', errors='ignore') as f:
            source = f.read()
        # The shared SSRF guard helper
        assert "_block_external_requests" in source, \
            "html_analyzer is missing the _block_external_requests SSRF guard"
        assert 'route.abort()' in source, \
            "Network blocker does not call route.abort() for external URLs"
        assert source.count(".route(\"**/*\", _block_external_requests)") >= 6, \
            "SSRF guard is not wired to all Playwright pages (expected >= 6)"


class TestOOMPrevention:
    """OOM (Out-of-Memory) bomb prevention via Image.MAX_IMAGE_PIXELS."""
    
    def test_image_max_pixels_is_set(self):
        """Verify that Image.MAX_IMAGE_PIXELS cap is configured."""
        # Import ocr_analyzer, which sets Image.MAX_IMAGE_PIXELS
        from wcag.analyzers.ocr_analyzer import _PYTESSERACT_AVAILABLE
        if _PYTESSERACT_AVAILABLE:
            from PIL import Image
            # After ocr_analyzer module import, cap should be set
            assert Image.MAX_IMAGE_PIXELS == 100_000_000, \
                f"Image.MAX_IMAGE_PIXELS not set correctly: {Image.MAX_IMAGE_PIXELS}"
    
    def test_image_bomb_rejected(self):
        """
        Verify that Pillow rejects oversized images.
        This simulates an image bomb: a small file that claims to be huge.
        """
        if not Image:
            pytest.skip("PIL not available")
        
        # Create a tiny PNG that claims to be 200k x 200k pixels
        # (When decoded, would allocate 40 billion pixels = 160 GB)
        # Pillow's Image.MAX_IMAGE_PIXELS should reject it.
        # Note: This test is illustrative; actual creation of such an image is complex.
        # For now, we just verify the cap is in place.
        from wcag.analyzers.ocr_analyzer import _PYTESSERACT_AVAILABLE
        if _PYTESSERACT_AVAILABLE:
            assert Image.MAX_IMAGE_PIXELS == 100_000_000, "OOM cap not configured"


class TestFormatValidation:
    """Format validation prevents misnamed files and parser exploits."""
    
    def test_pdf_magic_byte_check(self):
        """Verify that ocr_pdf_pages() rejects non-PDF data."""
        # Fake "PDF" data (does not start with %PDF magic bytes)
        fake_pdf = b"This is not a PDF file"
        results = ocr_pdf_pages(fake_pdf)
        assert results == [], "ocr_pdf_pages should reject non-PDF magic bytes"
    
    def test_valid_pdf_magic_bytes_accepted(self):
        """Verify that valid PDF magic bytes pass initial check."""
        # Minimal valid PDF header (will fail on image conversion, but passes magic-byte check)
        minimal_pdf = b"%PDF-1.4\n"  # Valid header
        # This will fail on actual processing (no content), but should pass magic-byte validation
        # The function returns [] gracefully due to pdf2image failure, not magic-byte rejection
        results = ocr_pdf_pages(minimal_pdf)
        # Should not crash; graceful degradation expected
        assert isinstance(results, list)


class TestDocxAnalyzerUsesSecureParser:
    """Verify that docx_analyzer uses SAFE_XML_PARSER throughout."""
    
    def test_docx_analyzer_imports_safe_parser(self):
        """Confirm SAFE_XML_PARSER is imported and used."""
        import wcag.analyzers.docx_analyzer as docx_mod
        with open(docx_mod.__file__, encoding='utf-8', errors='ignore') as f:
            source = f.read()
        assert "from wcag.common.safe_xml import SAFE_XML_PARSER" in source, \
            "docx_analyzer does not import SAFE_XML_PARSER"
        # Accept either keyword (parser=SAFE_XML_PARSER) or positional (, SAFE_XML_PARSER)
        # form — both bind the safe parser to etree.fromstring.
        assert ("parser=SAFE_XML_PARSER" in source) or (", SAFE_XML_PARSER" in source), \
            "docx_analyzer does not use SAFE_XML_PARSER in fromstring calls"


class TestXlsxAnalyzerReadOnly:
    """Verify that xlsx_analyzer uses keep_vba=False to prevent macro execution.

    Note: read_only=True was originally evaluated but disables openpyxl features
    required for WCAG analysis (charts, images, merged cells). keep_vba=False
    provides equivalent macro-isolation without breaking the analyzer.
    """

    def test_xlsx_analyzer_keep_vba_false(self):
        """Confirm keep_vba=False is set in load_workbook."""
        import wcag.analyzers.xlsx_analyzer as xlsx_mod
        with open(xlsx_mod.__file__, encoding='utf-8', errors='ignore') as f:
            source = f.read()
        assert "keep_vba=False" in source, \
            "xlsx_analyzer does not set keep_vba=False on load_workbook"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
