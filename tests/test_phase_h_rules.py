"""Phase H — five new strict-deterministic rules.

H1: HTML 3.2.1 On Focus — onfocus that auto-submits or navigates
H2: HTML 3.2.2 On Input — onchange/oninput that auto-submits or navigates
H3: HTML 3.3.1 Error Identification — required field with no error wiring
H4: HTML 1.3.3 Sensory Characteristics — color/shape/position-only references
H5: PDF 4.1.2 Name, Role, Value — AcroForm fields without /TU tooltip
"""
import io
import unittest

import pikepdf
from pikepdf import Array, Dictionary, Name, Pdf

from wcag.analyzers.html_analyzer import HtmlAnalyzer
from wcag.analyzers.pdf_analyzer import PdfAnalyzer


def _confirmed(fs, criterion: str):
    return [f for f in fs.confirmed_findings if f.criterion_id == criterion]


def _possible(fs, criterion: str):
    return [f for f in fs.possible_findings if f.criterion_id == criterion]


def _analyze_html(html: str):
    return HtmlAnalyzer(html.encode("utf-8"), "h.html").analyze()


def _make_pdf_with_form(field_definitions):
    """Build a tiny PDF with an AcroForm.

    field_definitions: list of dicts with {name, type ('Tx'|'Btn'|'Ch'),
    has_tu (bool)}.
    Returns raw bytes.
    """
    pdf = Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    fields = []
    for d in field_definitions:
        field = pdf.make_indirect(Dictionary(
            T=d["name"],
            FT=Name(f"/{d['type']}"),
        ))
        if d.get("has_tu"):
            field.TU = d.get("tu_text", d["name"])
        fields.append(field)
    acroform = Dictionary(Fields=Array(fields))
    pdf.Root.AcroForm = acroform
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# H1 — HTML 3.2.1 On Focus
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml321OnFocus(unittest.TestCase):
    def test_pass_no_onfocus(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><form><input type='text' name='q'></form></body></html>"""
        fs = _analyze_html(html)
        self.assertEqual(len(_confirmed(fs, "3.2.1")), 0)

    def test_pass_onfocus_does_not_change_context(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><form><input type='text' name='q' onfocus="highlight(this)"></form></body></html>"""
        fs = _analyze_html(html)
        self.assertEqual(len(_confirmed(fs, "3.2.1")), 0)

    def test_fail_onfocus_form_submit(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><form><input type='text' name='q' onfocus="this.form.submit()"></form></body></html>"""
        fs = _analyze_html(html)
        findings = _confirmed(fs, "3.2.1")
        self.assertEqual(len(findings), 1)
        self.assertIn("submit", findings[0].evidence.lower())

    def test_fail_onfocus_window_location(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='#' onfocus="window.location='/next'">Next</a></body></html>"""
        fs = _analyze_html(html)
        findings = _confirmed(fs, "3.2.1")
        self.assertEqual(len(findings), 1)


# ─────────────────────────────────────────────────────────────────────────────
# H2 — HTML 3.2.2 On Input
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml322OnInput(unittest.TestCase):
    def test_pass_no_onchange(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><form><select name='c'><option>A</option><option>B</option></select>
        <button type='submit'>Go</button></form></body></html>"""
        fs = _analyze_html(html)
        self.assertEqual(len(_confirmed(fs, "3.2.2")), 0)

    def test_pass_onchange_updates_view_only(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><form><select name='c' onchange="updatePreview(this.value)">
        <option>A</option></select></form></body></html>"""
        fs = _analyze_html(html)
        self.assertEqual(len(_confirmed(fs, "3.2.2")), 0)

    def test_fail_select_onchange_submit(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><form><select name='c' onchange="this.form.submit()">
        <option>A</option><option>B</option></select></form></body></html>"""
        fs = _analyze_html(html)
        findings = _confirmed(fs, "3.2.2")
        self.assertEqual(len(findings), 1)

    def test_fail_select_onchange_navigate(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><select onchange="window.location.href=this.value">
        <option value='/a'>A</option></select></body></html>"""
        fs = _analyze_html(html)
        findings = _confirmed(fs, "3.2.2")
        self.assertEqual(len(findings), 1)


# ─────────────────────────────────────────────────────────────────────────────
# H3 — HTML 3.3.1 Error Identification readiness
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml331ErrorIdentification(unittest.TestCase):
    def test_pass_required_with_describedby(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><form>
          <label for='e'>Email</label>
          <input id='e' type='email' required aria-describedby='e-err'>
          <span id='e-err'>Please enter a valid email.</span>
        </form></body></html>"""
        fs = _analyze_html(html)
        self.assertEqual(len(_confirmed(fs, "3.3.1")), 0)

    def test_pass_required_with_aria_invalid(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><form>
          <label for='e'>Email</label>
          <input id='e' type='email' required aria-invalid='false'>
        </form></body></html>"""
        fs = _analyze_html(html)
        self.assertEqual(len(_confirmed(fs, "3.3.1")), 0)

    def test_pass_no_required_fields(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><form><input type='text' name='q'></form></body></html>"""
        fs = _analyze_html(html)
        self.assertEqual(len(_confirmed(fs, "3.3.1")), 0)

    def test_fail_required_without_error_wiring(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><form>
          <label for='e'>Email</label>
          <input id='e' type='email' required>
          <label for='p'>Password</label>
          <input id='p' type='password' required>
        </form></body></html>"""
        fs = _analyze_html(html)
        findings = _confirmed(fs, "3.3.1")
        self.assertEqual(len(findings), 1)
        # Two offenders rolled up into one finding.
        self.assertIn("2 required", findings[0].issue)

    def test_pass_required_on_hidden_input_ignored(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><form><input type='hidden' name='csrf' required></form></body></html>"""
        fs = _analyze_html(html)
        self.assertEqual(len(_confirmed(fs, "3.3.1")), 0)


# ─────────────────────────────────────────────────────────────────────────────
# H4 — HTML 1.3.3 Sensory Characteristics
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml133SensoryCharacteristics(unittest.TestCase):
    def test_pass_no_sensory_phrases(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><p>Click the Submit button to continue.</p></body></html>"""
        fs = _analyze_html(html)
        self.assertEqual(len(_confirmed(fs, "1.3.3")), 0)

    def test_fail_color_only_reference(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><p>Click the red button to continue.</p></body></html>"""
        fs = _analyze_html(html)
        findings = _confirmed(fs, "1.3.3")
        self.assertEqual(len(findings), 1)
        self.assertIn("color", findings[0].evidence.lower())

    def test_fail_position_only_reference(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><p>See the menu on the right for more options.</p></body></html>"""
        fs = _analyze_html(html)
        findings = _confirmed(fs, "1.3.3")
        self.assertEqual(len(findings), 1)
        self.assertIn("position", findings[0].evidence.lower())

    def test_fail_shape_only_reference(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><p>Press the round icon to begin.</p></body></html>"""
        fs = _analyze_html(html)
        findings = _confirmed(fs, "1.3.3")
        self.assertEqual(len(findings), 1)
        self.assertIn("shape", findings[0].evidence.lower())

    def test_pass_color_with_label(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><p>This information is shown in red on the dashboard for emphasis.</p></body></html>"""
        fs = _analyze_html(html)
        # Mentioning color in a sentence isn't a violation; the rule fires only
        # for action-verb + color/shape/position + control-noun phrasing.
        self.assertEqual(len(_confirmed(fs, "1.3.3")), 0)


# ─────────────────────────────────────────────────────────────────────────────
# H5 — PDF 4.1.2 Name, Role, Value (AcroForm fields without /TU)
# ─────────────────────────────────────────────────────────────────────────────
class TestPdf412FormFieldNames(unittest.TestCase):
    def test_pass_all_fields_have_tu(self):
        pdf_bytes = _make_pdf_with_form([
            {"name": "first_name", "type": "Tx", "has_tu": True, "tu_text": "First name"},
            {"name": "email", "type": "Tx", "has_tu": True, "tu_text": "Email address"},
        ])
        fs = PdfAnalyzer(pdf_bytes, "form.pdf").analyze()
        # No form-field-name failures should be emitted.
        form_412 = [f for f in fs.confirmed_findings
                    if f.remediation_id == "pdf_form_names_412"]
        form_131 = [f for f in fs.confirmed_findings
                    if f.remediation_id == "pdf_form_labels"]
        self.assertEqual(len(form_412), 0)
        self.assertEqual(len(form_131), 0)

    def test_fail_fields_without_tu_emit_both_131_and_412(self):
        pdf_bytes = _make_pdf_with_form([
            {"name": "field_a", "type": "Tx", "has_tu": False},
            {"name": "field_b", "type": "Tx", "has_tu": False},
        ])
        fs = PdfAnalyzer(pdf_bytes, "form.pdf").analyze()
        # Both paired findings present; filter by remediation_id to ignore
        # other 1.3.1 rules (e.g., missing /StructTreeRoot) that fire on the
        # same minimal test PDF.
        form_131 = [f for f in fs.confirmed_findings
                    if f.remediation_id == "pdf_form_labels"]
        form_412 = [f for f in fs.confirmed_findings
                    if f.remediation_id == "pdf_form_names_412"]
        self.assertEqual(len(form_131), 1)
        self.assertEqual(len(form_412), 1)
        self.assertEqual(form_131[0].criterion_id, "1.3.1")
        self.assertEqual(form_412[0].criterion_id, "4.1.2")
        self.assertIn("2", form_412[0].issue)

    def test_no_acroform_no_finding(self):
        pdf = Pdf.new()
        pdf.add_blank_page(page_size=(612, 792))
        buf = io.BytesIO()
        pdf.save(buf)
        fs = PdfAnalyzer(buf.getvalue(), "noform.pdf").analyze()
        form_412 = [f for f in fs.confirmed_findings
                    if f.remediation_id == "pdf_form_names_412"]
        self.assertEqual(len(form_412), 0)


if __name__ == "__main__":
    unittest.main()
