import io
import os
import unittest
from unittest.mock import patch

import pikepdf

from wcag.analyzers.html_analyzer import HtmlAnalyzer
from wcag.analyzers.pdf_analyzer import PdfAnalyzer


class TestMilestoneAHtmlStability(unittest.TestCase):
    def test_rendered_checks_can_be_disabled_via_env(self):
        html = b"<!doctype html><html lang='en'><head><title>T</title></head><body><h1>Hello</h1></body></html>"
        with patch.dict(os.environ, {"WCAG_DISABLE_RENDERED_HTML": "1"}, clear=False):
            fs = HtmlAnalyzer(html, "t.html").analyze()
        self.assertIsNotNone(fs)
        self.assertEqual(fs.file_type, "html")


class TestMilestoneBPdfScannedHint(unittest.TestCase):
    @staticmethod
    def _blank_pdf_bytes() -> bytes:
        pdf = pikepdf.Pdf.new()
        pdf.add_blank_page()
        out = io.BytesIO()
        pdf.save(out)
        pdf.close()
        return out.getvalue()

    def test_scanned_hint_emitted_when_untagged_image_only(self):
        data = self._blank_pdf_bytes()
        analyzer = PdfAnalyzer(data, "scan.pdf")
        analyzer._pdf = pikepdf.Pdf.open(io.BytesIO(data))
        try:
            analyzer._collect_images = lambda: [{"page": 1, "name": "Im1", "has_alt": False, "alt_text": None}]
            analyzer._is_tagged = lambda: False
            analyzer._has_extractable_text_signals = lambda: False

            analyzer._rule_1_1_1_scanned_image_only_hint()
            analyzer._rule_1_3_1_tagged_pdf()

            hits = [
                f for f in analyzer.fact_sheet.possible_findings
                if f.remediation_id == "pdf_scanned_image_only" and f.criterion_id == "1.1.1"
            ]
            self.assertEqual(len(hits), 1)
            buckets = [
                f.remediation_data.get("triage_bucket")
                for f in analyzer.fact_sheet.confirmed_findings
                if f.remediation_id == "pdf_untagged" and f.remediation_data
            ]
            self.assertIn("untagged_image_only", buckets)
        finally:
            analyzer._pdf.close()

    def test_scanned_hint_not_emitted_when_text_signals_exist(self):
        data = self._blank_pdf_bytes()
        analyzer = PdfAnalyzer(data, "text.pdf")
        analyzer._pdf = pikepdf.Pdf.open(io.BytesIO(data))
        try:
            analyzer._collect_images = lambda: [{"page": 1, "name": "Im1", "has_alt": True, "alt_text": "Logo"}]
            analyzer._is_tagged = lambda: False
            analyzer._has_extractable_text_signals = lambda: True
            analyzer._has_structure_tree = lambda: False

            analyzer._rule_1_1_1_scanned_image_only_hint()
            analyzer._rule_1_3_1_tagged_pdf()

            hits = [f for f in analyzer.fact_sheet.possible_findings if f.remediation_id == "pdf_scanned_image_only"]
            self.assertEqual(len(hits), 0)

            review_hits = [
                f for f in analyzer.fact_sheet.possible_findings
                if f.remediation_id == "pdf_untagged_text_layer_review"
            ]
            self.assertEqual(len(review_hits), 1)
            buckets = [
                f.remediation_data.get("triage_bucket")
                for f in analyzer.fact_sheet.confirmed_findings
                if f.remediation_id == "pdf_untagged" and f.remediation_data
            ]
            self.assertIn("untagged_text_layer", buckets)
        finally:
            analyzer._pdf.close()


if __name__ == "__main__":
    unittest.main()
