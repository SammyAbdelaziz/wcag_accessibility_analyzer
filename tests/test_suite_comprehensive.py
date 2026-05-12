"""
Comprehensive Test Suite Runner
Processes all test fixtures through the WCAG analyzer and generates detailed reports.
Use to:
1. Identify false positives / negatives
2. Show coverage by rule
3. Compare against MS Accessibility Checker
4. Benchmark accuracy improvements
"""

import os
import json
from pathlib import Path
from typing import Dict, List, Tuple
from wcag.analyzers.pptx_analyzer import PptxAnalyzer
from wcag.analyzers.docx_analyzer import DocxAnalyzer
from wcag.models import FactSheet, Finding, ConfidenceTier, Severity


class ComprehensiveTestSuite:
    """Run all test fixtures and generate detailed findings report."""
    
    def __init__(self, fixtures_dir: str):
        self.fixtures_dir = Path(fixtures_dir)
        self.results = {
            'total_files': 0,
            'files_processed': 0,
            'files_with_findings': 0,
            'by_format': {'docx': [], 'pptx': []},
            'by_rule': {},
            'by_confidence': {'confirmed': [], 'possible': []},
            'by_severity': {'critical': [], 'serious': [], 'moderate': [], 'minor': []},
            'false_positives_suspected': [],
            'coverage_gaps': [],
        }
    
    def run_all_tests(self) -> Dict:
        """Process all fixture files and aggregate results."""
        all_files = list(self.fixtures_dir.glob('*.docx')) + list(self.fixtures_dir.glob('*.pptx'))
        self.results['total_files'] = len(all_files)
        
        print(f"\n{'='*80}")
        print(f"COMPREHENSIVE TEST SUITE: {self.results['total_files']} files")
        print(f"{'='*80}\n")
        
        for idx, file_path in enumerate(sorted(all_files), 1):
            self._test_single_file(file_path, idx)
        
        return self._generate_report()
    
    def _test_single_file(self, file_path: Path, file_num: int):
        """Run analyzer on single fixture and collect results."""
        file_format = 'docx' if file_path.suffix == '.docx' else 'pptx'
        file_type = 'DOCX' if file_format == 'docx' else 'PPTX'
        
        print(f"[{file_num}/{self.results['total_files']}] {file_path.name}")
        
        try:
            # Read file bytes
            with open(file_path, 'rb') as f:
                file_bytes = f.read()
            
            # Run appropriate analyzer
            if file_format == 'docx':
                analyzer = DocxAnalyzer(file_bytes, file_path.name)
            else:
                analyzer = PptxAnalyzer(file_bytes, file_path.name)
            
            fact_sheet = analyzer.analyze()
            self.results['files_processed'] += 1
            
            # Get all findings
            all_findings = (fact_sheet.confirmed_findings or []) + (fact_sheet.possible_findings or [])
            
            if not all_findings:
                print(f"        → No findings (accessible)")
                return
            
            self.results['files_with_findings'] += 1
            
            # Group by format
            self.results['by_format'][file_format].append({
                'file': file_path.name,
                'finding_count': len(all_findings),
                'findings': [self._finding_to_dict(f) for f in all_findings]
            })
            
            # Aggregate by rule, confidence, severity
            for finding in all_findings:
                self._aggregate_finding(finding, file_path.name)
            
            # Print summary
            critical = len([f for f in all_findings if f.severity == Severity.CRITICAL])
            serious = len([f for f in all_findings if f.severity == Severity.SERIOUS])
            total = len(all_findings)
            print(f"        -> {total} findings ({critical} critical, {serious} serious)")
            
        except Exception as e:
            print(f"        [ERROR] {e}")
    def _aggregate_finding(self, finding: Finding, filename: str):
        """Add finding to aggregated results."""
        criterion = finding.criterion_id
        
        # By rule
        if criterion not in self.results['by_rule']:
            self.results['by_rule'][criterion] = {
                'name': finding.criterion_name,
                'level': finding.wcag_level,
                'count': 0,
                'files': [],
                'examples': []
            }
        
        self.results['by_rule'][criterion]['count'] += 1
        if filename not in self.results['by_rule'][criterion]['files']:
            self.results['by_rule'][criterion]['files'].append(filename)
        if len(self.results['by_rule'][criterion]['examples']) < 3:
            self.results['by_rule'][criterion]['examples'].append({
                'file': filename,
                'issue': finding.issue[:80] + ('...' if len(finding.issue) > 80 else '')
            })
        
        # By confidence
        conf_tier = finding.confidence_tier
        self.results['by_confidence'][conf_tier.value].append({
            'rule': criterion,
            'file': filename,
            'issue': finding.issue
        })
        
        # By severity
        sev = finding.severity.value
        self.results['by_severity'][sev].append({
            'rule': criterion,
            'file': filename,
            'issue': finding.issue
        })
    
    def _finding_to_dict(self, finding: Finding) -> Dict:
        """Convert Finding to dict for JSON serialization."""
        return {
            'criterion_id': finding.criterion_id,
            'criterion_name': finding.criterion_name,
            'wcag_level': finding.wcag_level,
            'issue': finding.issue,
            'severity': finding.severity.value,
            'confidence_tier': finding.confidence_tier.value,
            'confidence_label': finding.confidence_label,
            'evidence_source': finding.evidence_source.value,
            'location': finding.location,
        }
    
    def _generate_report(self) -> Dict:
        """Generate comprehensive report with analysis."""
        print(f"\n{'='*80}")
        print(f"ANALYSIS & COVERAGE")
        print(f"{'='*80}\n")
        
        # Rule coverage
        print("[RULE COVERAGE]\n")
        rules_by_count = sorted(self.results['by_rule'].items(), 
                               key=lambda x: x[1]['count'], reverse=True)
        
        for criterion, data in rules_by_count:
            count = data['count']
            files = len(data['files'])
            print(f"  {criterion} ({data['name']})")
            print(f"    → Found in {count} findings across {files} files")
            for ex in data['examples'][:1]:
                print(f"    Example: {ex['issue']}")
            print()
        
        # Confidence breakdown
        print("\n[CONFIDENCE BREAKDOWN]\n")
        confirmed = len(self.results['by_confidence']['confirmed'])
        possible = len(self.results['by_confidence']['possible'])
        total = confirmed + possible
        
        if total > 0:
            confirmed_pct = (confirmed / total) * 100
            possible_pct = (possible / total) * 100
            print(f"  CONFIRMED: {confirmed} findings ({confirmed_pct:.1f}%)")
            print(f"  POSSIBLE:  {possible} findings ({possible_pct:.1f}%)")
        
        # Severity breakdown
        print("\n[SEVERITY BREAKDOWN]\n")
        for sev in ['critical', 'serious', 'moderate', 'minor']:
            count = len(self.results['by_severity'][sev])
            print(f"  {sev.upper()}: {count} findings")
        
        # Format breakdown
        print("\n[FORMAT BREAKDOWN]\n")
        docx_files = self.results['by_format']['docx']
        pptx_files = self.results['by_format']['pptx']
        
        print(f"  DOCX: {len(docx_files)} files analyzed")
        print(f"  PPTX: {len(pptx_files)} files analyzed")
        
        return self.results
    
    def export_json(self, output_path: str):
        """Export full results as JSON for detailed analysis."""
        with open(output_path, 'w') as f:
            # Convert non-serializable objects
            export_data = {
                'summary': {
                    'total_files': self.results['total_files'],
                    'files_processed': self.results['files_processed'],
                    'files_with_findings': self.results['files_with_findings'],
                    'total_findings': len(self.results['by_confidence']['confirmed']) + 
                                    len(self.results['by_confidence']['possible']),
                },
                'by_rule': self.results['by_rule'],
                'by_format': {
                    'docx': len(self.results['by_format']['docx']),
                    'pptx': len(self.results['by_format']['pptx']),
                },
                'by_severity': {
                    'critical': len(self.results['by_severity']['critical']),
                    'serious': len(self.results['by_severity']['serious']),
                    'moderate': len(self.results['by_severity']['moderate']),
                    'minor': len(self.results['by_severity']['minor']),
                },
                'confidence_breakdown': {
                    'confirmed': len(self.results['by_confidence']['confirmed']),
                    'possible': len(self.results['by_confidence']['possible']),
                }
            }
            json.dump(export_data, f, indent=2)
        
        print(f"\n[REPORT] Report exported to: {output_path}")


def run_comprehensive_test():
    """Main entry point."""
    import os
    from pathlib import Path

    default_fixtures = str(Path(__file__).parent / "fixtures" / "uploads")
    fixtures_dir = os.environ.get("WCAG_UPLOADS_DIR", default_fixtures)
    output_json = os.environ.get(
        "WCAG_COMPREHENSIVE_REPORT",
        str(Path(__file__).parent / "test_results_comprehensive.json"),
    )

    if not Path(fixtures_dir).exists():
        print(
            f"[SKIP] fixtures directory not found: {fixtures_dir}\n"
            f"       Set WCAG_UPLOADS_DIR to a directory containing test documents\n"
            f"       to run the comprehensive suite."
        )
        return

    suite = ComprehensiveTestSuite(fixtures_dir)
    results = suite.run_all_tests()

    suite.export_json(output_json)

    print(f"\n{'='*80}")
    print(f"SUMMARY: {results['files_processed']} files processed")
    print(f"         {results['files_with_findings']} files with findings")
    print(f"{'='*80}\n")


if __name__ == '__main__':
    run_comprehensive_test()
