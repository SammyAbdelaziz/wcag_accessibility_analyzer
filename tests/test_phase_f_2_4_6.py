"""Phase F — HTML 2.4.6 Headings and Labels quality.

Strict-deterministic checks: empty headings, exact-match generic heading
text patterns, and exact-match generic label text patterns.
"""
import unittest

from wcag.analyzers.html_analyzer import HtmlAnalyzer


def _confirmed_2_4_6(fs):
    return [f for f in fs.confirmed_findings if f.criterion_id == "2.4.6"]


def _analyze(html: str):
    return HtmlAnalyzer(html.encode('utf-8'), 'h.html').analyze()


class TestHtml246HeadingsAndLabels(unittest.TestCase):
    def test_pass_descriptive_headings_and_labels(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>Patients</title></head>
        <body>
        <h1>Patient Intake Workflow</h1>
        <h2>Personal Details</h2>
        <form>
          <label for='fn'>First name</label><input id='fn' type='text'>
          <label for='em'>Email address</label><input id='em' type='email'>
        </form>
        </body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed_2_4_6(fs)), 0)

    def test_fail_empty_heading(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><h1></h1><p>Body content here.</p></body></html>"""
        fs = _analyze(html)
        findings = _confirmed_2_4_6(fs)
        self.assertEqual(len(findings), 1)
        self.assertIn("empty", findings[0].evidence.lower())

    def test_fail_generic_heading_text(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><h1>Heading 1</h1><h2>Section</h2></body></html>"""
        fs = _analyze(html)
        findings = _confirmed_2_4_6(fs)
        self.assertEqual(len(findings), 1)
        self.assertIn("generic heading", findings[0].evidence.lower())

    def test_fail_generic_label_text(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body>
        <h1>Real Title</h1>
        <form>
          <label for='a'>Label</label><input id='a' type='text'>
          <label for='b'>Field 2</label><input id='b' type='text'>
        </form>
        </body></html>"""
        fs = _analyze(html)
        findings = _confirmed_2_4_6(fs)
        self.assertEqual(len(findings), 1)
        self.assertIn("generic label", findings[0].evidence.lower())

    def test_pass_meaningful_label_with_field_in_name(self):
        # "Email field" should NOT match — contains real description.
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body>
        <h1>Real Title</h1>
        <form>
          <label for='em'>Email Address Field</label><input id='em' type='email'>
        </form>
        </body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed_2_4_6(fs)), 0)


if __name__ == '__main__':
    unittest.main()
