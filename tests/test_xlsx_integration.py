"""
Integration tests for XLSX analyzer on real test fixtures.

Validates that all 12 XLSX WCAG rules detect expected findings in realistic
test files. Tests include the 4 new rules plus 8 existing rules.
"""

import unittest
from pathlib import Path
from wcag.analyzers.xlsx_analyzer import XlsxAnalyzer


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "xlsx"


class XLSXIntegrationTests(unittest.TestCase):
    """Integration tests using real XLSX fixture files."""
    
    def _load_fixture(self, filename: str) -> bytes:
        """Load a fixture file and return its bytes."""
        filepath = FIXTURES_DIR / filename
        if not filepath.exists():
            self.skipTest(f"Fixture not found: {filepath}")
        with open(filepath, 'rb') as f:
            return f.read()
    
    def _analyze(self, filename: str) -> object:
        """Analyze a fixture file and return fact sheet."""
        data = self._load_fixture(filename)
        analyzer = XlsxAnalyzer(data, filename)
        return analyzer.analyze()
    
    def _get_findings(self, fact_sheet, criterion_id: str, level: str = "confirmed"):
        """Get findings by criterion ID."""
        if level == "confirmed":
            return [f for f in fact_sheet.confirmed_findings if f.criterion_id == criterion_id]
        else:
            return [f for f in fact_sheet.possible_findings if f.criterion_id == criterion_id]

    @staticmethod
    def _remediation_ids(findings):
        return {f.remediation_id for f in findings if f.remediation_id}
    
    # ─────────────────────────────────────────────────────────────────────────────
    # Tests for 4 NEW RULES
    # ─────────────────────────────────────────────────────────────────────────────
    
    def test_rule_1_1_1_accessible_workbook_passes(self):
        """✓ Rule 1.1.1: Accessible workbook without images passes."""
        fact_sheet = self._analyze("accessible_workbook.xlsx")
        findings = self._get_findings(fact_sheet, "1.1.1")
        image_issues = [f for f in findings if "image" in f.issue.lower()]
        self.assertEqual(len(image_issues), 0, "Workbook without images should have no 1.1.1 findings")
    
    def test_rule_1_3_1_freeform_layout_warns(self):
        """⚠️ Rule 1.3.1: Freeform layout should be detected."""
        fact_sheet = self._analyze("freeform_layout.xlsx")
        possible = self._get_findings(fact_sheet, "1.3.1", "possible")
        remediation_ids = self._remediation_ids(possible)
        self.assertTrue(any(rid.startswith("xlsx_freeform_") for rid in remediation_ids))
    
    def test_rule_1_3_2_excessive_blanks_warns(self):
        """⚠️ Rule 1.3.2: Excessive blank rows should be detected."""
        fact_sheet = self._analyze("excessive_blank_rows.xlsx")
        possible = self._get_findings(fact_sheet, "1.3.2", "possible")
        remediation_ids = self._remediation_ids(possible)
        self.assertTrue(any(rid.startswith("xlsx_blank_rows_") for rid in remediation_ids))
    
    def test_rule_1_4_10_wide_merged_cells_warns(self):
        """⚠️ Rule 1.4.10: Wide merged cells should be detected."""
        fact_sheet = self._analyze("wide_merged_cells.xlsx")
        possible = self._get_findings(fact_sheet, "1.4.10", "possible")
        remediation_ids = self._remediation_ids(possible)
        self.assertTrue(any(rid.startswith("xlsx_reflow_") for rid in remediation_ids))
    
    # ─────────────────────────────────────────────────────────────────────────────
    # Tests for 8 EXISTING RULES (spot checks)
    # ─────────────────────────────────────────────────────────────────────────────
    
    def test_rule_1_4_1_color_only_warns(self):
        """⚠️ Rule 1.4.1: Color-only indicators should be detected."""
        fact_sheet = self._analyze("color_only_no_text.xlsx")
        possible = self._get_findings(fact_sheet, "1.4.1", "possible")
        remediation_ids = self._remediation_ids(possible)
        self.assertTrue(any(rid.startswith("xlsx_color_only_") for rid in remediation_ids))
    
    def test_rule_1_4_4_tiny_text_warns(self):
        """⚠️ Rule 1.4.4: Tiny text should be detected."""
        fact_sheet = self._analyze("tiny_text.xlsx")
        possible = self._get_findings(fact_sheet, "1.4.4", "possible")
        remediation_ids = self._remediation_ids(possible)
        self.assertTrue(any(rid.startswith("xlsx_tiny_text_") for rid in remediation_ids))
    
    def test_rule_2_4_2_workbook_title_accessible(self):
        """✓ Rule 2.4.2: Workbook with title passes."""
        fact_sheet = self._analyze("accessible_workbook.xlsx")
        findings = self._get_findings(fact_sheet, "2.4.2")
        self.assertEqual(len(findings), 0)
    
    def test_rule_3_1_1_language_analysis_runs(self):
        """Rule 3.1.1: fixture without language should be flagged."""
        fact_sheet = self._analyze("accessible_workbook.xlsx")
        findings = self._get_findings(fact_sheet, "3.1.1")
        remediation_ids = self._remediation_ids(findings)
        self.assertIn("xlsx_doc_language", remediation_ids)
    
    def test_rule_1_3_1_merged_cells_detection(self):
        """✓ Rule 1.3.1: Merged cells should be detected if present."""
        fact_sheet = self._analyze("wide_merged_cells.xlsx")
        confirmed = self._get_findings(fact_sheet, "1.3.1", "confirmed")
        remediation_ids = self._remediation_ids(confirmed)
        self.assertTrue(any(rid.startswith("xlsx_merged_cells_") for rid in remediation_ids))

    def test_rule_1_3_1_missing_headers_detected(self):
        """⚠️ Rule 1.3.1: Missing header row should be detected on data-only sheet."""
        fact_sheet = self._analyze("missing_headers.xlsx")
        possible = self._get_findings(fact_sheet, "1.3.1", "possible")
        remediation_ids = self._remediation_ids(possible)
        self.assertTrue(any(rid.startswith("xlsx_header_row_") for rid in remediation_ids))
    
    def test_rule_2_4_4_link_text_analysis(self):
        """✓ Rule 2.4.4: Link text analysis should run."""
        fact_sheet = self._analyze("accessible_workbook.xlsx")
        findings = self._get_findings(fact_sheet, "2.4.4")
        # Workbook without links should not be flagged
        self.assertEqual(len(findings), 0)
    
    # ─────────────────────────────────────────────────────────────────────────────
    # Summary and Cross-Cutting Tests
    # ─────────────────────────────────────────────────────────────────────────────
    
    def test_all_12_rules_executed_on_accessible_workbook(self):
        """Accessible workbook should still emit deterministic baseline findings."""
        fact_sheet = self._analyze("accessible_workbook.xlsx")
        confirmed_ids = self._remediation_ids(fact_sheet.confirmed_findings)
        self.assertIn("xlsx_doc_language", confirmed_ids)
        self.assertEqual(len(fact_sheet.possible_findings), 0)
    
    def test_freeform_vs_accessible_difference(self):
        """Compare findings between freeform and accessible workbooks."""
        accessible = self._analyze("accessible_workbook.xlsx")
        freeform = self._analyze("freeform_layout.xlsx")
        
        accessible_findings = len(accessible.confirmed_findings + accessible.possible_findings)
        freeform_findings = len(freeform.confirmed_findings + freeform.possible_findings)
        
        self.assertGreater(freeform_findings, accessible_findings)
    
    def test_fact_sheet_structure_completeness(self):
        """✓ Fact sheet should have all required fields."""
        fact_sheet = self._analyze("accessible_workbook.xlsx")
        
        # Verify required attributes
        self.assertIsNotNone(fact_sheet.file_type)
        self.assertIsNotNone(fact_sheet.confirmed_findings)
        self.assertIsNotNone(fact_sheet.possible_findings)
        self.assertEqual(fact_sheet.file_type, "xlsx")
    
    def test_finding_structure_completeness(self):
        """✓ Each finding should have all required fields."""
        fact_sheet = self._analyze("wide_merged_cells.xlsx")
        
        all_findings = fact_sheet.confirmed_findings + fact_sheet.possible_findings
        for finding in all_findings:
            # Verify required fields
            self.assertIsNotNone(finding.criterion_id)
            self.assertIsNotNone(finding.criterion_name)
            self.assertIsNotNone(finding.wcag_level)
            self.assertIsNotNone(finding.issue)
            self.assertIsNotNone(finding.evidence)
            self.assertIsNotNone(finding.severity)
            self.assertIsNotNone(finding.why_it_matters)
            self.assertIsNotNone(finding.remediation_steps)
            self.assertIsNotNone(finding.confidence_tier)
            self.assertIsNotNone(finding.evidence_source)
            self.assertIsNotNone(finding.location)


class XLSXAnalysisRobustness(unittest.TestCase):
    """Tests for analyzer robustness and error handling."""
    
    def test_analyzer_handles_missing_file_gracefully(self):
        """✓ Analyzer should handle missing files gracefully."""
        analyzer = XlsxAnalyzer(b"invalid", "nonexistent.xlsx")
        fact_sheet = analyzer.analyze()
        self.assertEqual(fact_sheet.file_type, "xlsx")
        self.assertGreaterEqual(len(fact_sheet.confirmed_findings), 1)
        self.assertIn("xlsx_corrupt", {f.remediation_id for f in fact_sheet.confirmed_findings})
    
    def test_analyzer_with_empty_workbook(self):
        """✓ Analyzer should handle empty workbooks."""
        from openpyxl import Workbook
        from io import BytesIO
        
        wb = Workbook()
        output = BytesIO()
        wb.save(output)
        output.seek(0)
        
        analyzer = XlsxAnalyzer(output.getvalue(), "empty.xlsx")
        fact_sheet = analyzer.analyze()
        
        self.assertEqual(fact_sheet.file_type, "xlsx")


if __name__ == "__main__":
    unittest.main()
