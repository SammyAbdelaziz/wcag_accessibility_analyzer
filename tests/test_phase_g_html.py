"""Phase G — five new HTML strict-deterministic rules.

G1: 2.5.3 Label in Name (visible text vs aria-label substring check)
G2: 1.3.4 Orientation (viewport meta lock + CSS @media orientation hide)
G3: 2.1.1 Keyboard (onclick on non-interactive without keyboard equivalent)
G4: 2.2.1 Timing Adjustable (meta refresh)
G5: 1.4.12 Text Spacing (!important on text-spacing properties)
"""
import unittest

from wcag.analyzers.html_analyzer import HtmlAnalyzer


def _confirmed(fs, criterion: str):
    return [f for f in fs.confirmed_findings if f.criterion_id == criterion]


def _possible(fs, criterion: str):
    return [f for f in fs.possible_findings if f.criterion_id == criterion]


def _analyze(html: str):
    return HtmlAnalyzer(html.encode("utf-8"), "g.html").analyze()


# ─────────────────────────────────────────────────────────────────────────────
# G1 — 2.5.3 Label in Name
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml253LabelInName(unittest.TestCase):
    def test_pass_aria_label_contains_visible_text(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><button aria-label="Search products">Search</button></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "2.5.3")), 0)

    def test_fail_aria_label_does_not_contain_visible(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><button aria-label="Submit form">Send</button></body></html>"""
        fs = _analyze(html)
        findings = _confirmed(fs, "2.5.3")
        self.assertEqual(len(findings), 1)
        self.assertIn("send", findings[0].evidence.lower())

    def test_pass_no_aria_label(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><button>Send</button></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "2.5.3")), 0)


# ─────────────────────────────────────────────────────────────────────────────
# G2 — 1.3.4 Orientation
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml134Orientation(unittest.TestCase):
    def test_pass_normal_viewport(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title>
        <meta name='viewport' content='width=device-width, initial-scale=1'>
        </head><body><h1>Hi</h1></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "1.3.4")), 0)
        self.assertEqual(len(_possible(fs, "1.3.4")), 0)

    def test_fail_viewport_orientation_lock(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title>
        <meta name='viewport' content='width=device-width, orientation=portrait'>
        </head><body><h1>Hi</h1></body></html>"""
        fs = _analyze(html)
        findings = _confirmed(fs, "1.3.4")
        self.assertEqual(len(findings), 1)

    def test_possible_css_orientation_hide(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title>
        <style>@media (orientation: landscape) { body { display: none; } }</style>
        </head><body><h1>Hi</h1></body></html>"""
        fs = _analyze(html)
        # CSS hide is reported as POSSIBLE (heuristic).
        self.assertGreaterEqual(len(_possible(fs, "1.3.4")), 1)


# ─────────────────────────────────────────────────────────────────────────────
# G3 — 2.1.1 Keyboard
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml211KeyboardHandlers(unittest.TestCase):
    def test_pass_button_with_onclick(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><button onclick="doIt()">Go</button></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "2.1.1")), 0)

    def test_fail_div_with_onclick_no_role(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><div onclick="doIt()">Click</div></body></html>"""
        fs = _analyze(html)
        findings = _confirmed(fs, "2.1.1")
        self.assertEqual(len(findings), 1)
        self.assertIn("role", findings[0].evidence.lower())

    def test_pass_div_with_onclick_role_tabindex_keydown(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><div onclick="doIt()" role="button" tabindex="0"
        onkeydown="if(event.key==='Enter')doIt()">Click</div></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "2.1.1")), 0)


# ─────────────────────────────────────────────────────────────────────────────
# G4 — 2.2.1 Meta Refresh
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml221MetaRefresh(unittest.TestCase):
    def test_pass_no_refresh(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><h1>Hi</h1></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "2.2.1")), 0)

    def test_fail_meta_refresh_5_seconds(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title>
        <meta http-equiv="refresh" content="5; url=/next"></head>
        <body><h1>Hi</h1></body></html>"""
        fs = _analyze(html)
        findings = _confirmed(fs, "2.2.1")
        self.assertEqual(len(findings), 1)
        self.assertIn("5s refresh", findings[0].evidence.lower())

    def test_fail_meta_refresh_zero_seconds(self):
        # N=0 still flagged as a redirect that the user can't control client-side
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title>
        <meta http-equiv="refresh" content="0; url=/next"></head>
        <body></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "2.2.1")), 1)


# ─────────────────────────────────────────────────────────────────────────────
# G5 — 1.4.12 Text Spacing
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml1412TextSpacing(unittest.TestCase):
    def test_pass_normal_css(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title>
        <style>p { line-height: 1.5; }</style></head>
        <body><p>Hello</p></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "1.4.12")), 0)

    def test_fail_line_height_important(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title>
        <style>p { line-height: 1.2 !important; }</style></head>
        <body><p>Hello</p></body></html>"""
        fs = _analyze(html)
        findings = _confirmed(fs, "1.4.12")
        self.assertEqual(len(findings), 1)
        self.assertIn("line-height", findings[0].evidence.lower())

    def test_fail_letter_spacing_important(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title>
        <style>.brand { letter-spacing: 0 !important; }</style></head>
        <body><p class='brand'>Hello</p></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "1.4.12")), 1)

    def test_pass_margin_important_only(self):
        # margin/padding !important alone do NOT trigger 1.4.12 (not core props).
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title>
        <style>p { margin-top: 10px !important; }</style></head>
        <body><p>Hello</p></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "1.4.12")), 0)


if __name__ == "__main__":
    unittest.main()
