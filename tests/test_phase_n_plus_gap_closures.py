"""Smoke tests for Phase N+ (2026-05-18) — 3 additional AAA closures:

    • 2.4.13 Focus Appearance
    • 3.3.5 Help
    • 3.3.6 Error Prevention (All)

All three are POSSIBLE-tier source-only heuristics. Tests verify each
rule fires on a known-bad fixture and stays silent on a known-clean one.
"""

from __future__ import annotations

import unittest

from wcag.analyzers.html_analyzer import HtmlAnalyzer


def _build(html: str, name: str = "phase_n_plus_test.html") -> HtmlAnalyzer:
    """Build an HtmlAnalyzer with `html` as the body, bypassing decode."""
    a = HtmlAnalyzer(html.encode("utf-8"), name)
    a._html_text = html  # avoid _decode_html() round-trip
    return a


def _find(findings, criterion_id: str):
    """Return the first finding with matching criterion_id, or None."""
    return next((f for f in findings if f.criterion_id == criterion_id), None)


# ─────────────────────────────────────────────────────────────────────────────
# 2.4.13 Focus Appearance (AAA)
# ─────────────────────────────────────────────────────────────────────────────
class Test_2_4_13_FocusAppearance(unittest.TestCase):
    def test_fires_on_outline_none_without_replacement(self):
        html = """
        <html><head><style>
        button:focus { outline: none; }
        a:focus { outline: 0; }
        </style></head><body><button>Go</button></body></html>
        """
        a = _build(html)
        a._rule_2_4_13_focus_appearance()
        f = _find(a.fact_sheet.possible_findings, "2.4.13")
        self.assertIsNotNone(f, "Expected 2.4.13 finding on outline:none with no replacement")
        self.assertEqual(f.wcag_level, "AAA")

    def test_silent_when_outline_replaced_with_thick_outline(self):
        html = """
        <html><head><style>
        button:focus { outline: none; outline: 3px solid #1A73E8; outline-offset: 2px; }
        </style></head><body><button>Go</button></body></html>
        """
        a = _build(html)
        a._rule_2_4_13_focus_appearance()
        self.assertIsNone(
            _find(a.fact_sheet.possible_findings, "2.4.13"),
            "Should not fire when a ≥2px replacement outline is in the same rule",
        )

    def test_silent_when_replaced_with_box_shadow_ring(self):
        html = """
        <html><head><style>
        a:focus-visible { outline: none; box-shadow: 0 0 0 3px #1A73E8; }
        </style></head><body><a href="#">Link</a></body></html>
        """
        a = _build(html)
        a._rule_2_4_13_focus_appearance()
        self.assertIsNone(_find(a.fact_sheet.possible_findings, "2.4.13"))

    def test_silent_when_no_style_block(self):
        html = "<html><body><button>Go</button></body></html>"
        a = _build(html)
        a._rule_2_4_13_focus_appearance()
        self.assertIsNone(_find(a.fact_sheet.possible_findings, "2.4.13"))

    def test_silent_on_non_focusable_selectors(self):
        # outline:none on a div container is harmless
        html = """
        <html><head><style>
        .wrapper { outline: none; }
        </style></head><body><div class="wrapper">x</div></body></html>
        """
        a = _build(html)
        a._rule_2_4_13_focus_appearance()
        self.assertIsNone(
            _find(a.fact_sheet.possible_findings, "2.4.13"),
            "Should not fire on outline-suppressing rules that don't target focusable elements",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3.3.5 Help (AAA)
# ─────────────────────────────────────────────────────────────────────────────
class Test_3_3_5_Help(unittest.TestCase):
    def test_fires_on_form_with_required_inputs_and_no_help(self):
        html = """
        <html><body>
        <form>
          <label>Email <input type="email" required></label>
          <label>SSN <input type="text" required></label>
          <button type="submit">Submit</button>
        </form>
        </body></html>
        """
        a = _build(html)
        a._rule_3_3_5_help()
        f = _find(a.fact_sheet.possible_findings, "3.3.5")
        self.assertIsNotNone(f)
        self.assertEqual(f.wcag_level, "AAA")

    def test_silent_when_aria_describedby_present(self):
        html = """
        <html><body>
        <form>
          <label>Email <input type="email" required aria-describedby="email-hint"></label>
          <small id="email-hint">e.g. you@example.com</small>
          <button type="submit">Submit</button>
        </form>
        </body></html>
        """
        a = _build(html)
        a._rule_3_3_5_help()
        self.assertIsNone(_find(a.fact_sheet.possible_findings, "3.3.5"))

    def test_silent_when_help_link_nearby(self):
        html = """
        <html><body>
        <form>
          <label>Email <input type="email" required></label>
          <button type="submit">Submit</button>
        </form>
        <a href="/help">Need help filling this out?</a>
        </body></html>
        """
        a = _build(html)
        a._rule_3_3_5_help()
        self.assertIsNone(_find(a.fact_sheet.possible_findings, "3.3.5"))

    def test_silent_on_form_with_no_required_inputs(self):
        html = """
        <html><body>
        <form><input type="text"><button type="submit">x</button></form>
        </body></html>
        """
        a = _build(html)
        a._rule_3_3_5_help()
        self.assertIsNone(_find(a.fact_sheet.possible_findings, "3.3.5"))

    def test_silent_when_substantive_placeholder_present(self):
        html = """
        <html><body>
        <form>
          <input type="text" required placeholder="Format: 555-123-4567 (US numbers only)">
          <button type="submit">Submit</button>
        </form>
        </body></html>
        """
        a = _build(html)
        a._rule_3_3_5_help()
        self.assertIsNone(_find(a.fact_sheet.possible_findings, "3.3.5"))


# ─────────────────────────────────────────────────────────────────────────────
# 3.3.6 Error Prevention All (AAA)
# ─────────────────────────────────────────────────────────────────────────────
class Test_3_3_6_ErrorPreventionAll(unittest.TestCase):
    def test_fires_on_plain_submission_form(self):
        html = """
        <html><body>
        <form action="/api/save" method="post">
          <input type="text" name="title">
          <button type="submit">Save</button>
        </form>
        </body></html>
        """
        a = _build(html)
        a._rule_3_3_6_error_prevention_all()
        f = _find(a.fact_sheet.possible_findings, "3.3.6")
        self.assertIsNotNone(f)
        self.assertEqual(f.wcag_level, "AAA")

    def test_silent_when_confirm_text_nearby(self):
        html = """
        <html><body>
        <form action="/api/save" method="post">
          <input type="text" name="title">
          <button type="submit">Save</button>
        </form>
        <p>Please review your entry before submitting.</p>
        </body></html>
        """
        a = _build(html)
        a._rule_3_3_6_error_prevention_all()
        self.assertIsNone(_find(a.fact_sheet.possible_findings, "3.3.6"))

    def test_silent_on_search_form(self):
        html = """
        <html><body>
        <form role="search">
          <input type="search" name="q">
          <button type="submit">Search</button>
        </form>
        </body></html>
        """
        a = _build(html)
        a._rule_3_3_6_error_prevention_all()
        self.assertIsNone(
            _find(a.fact_sheet.possible_findings, "3.3.6"),
            "Pure search forms must be exempted",
        )

    def test_silent_on_input_type_search(self):
        html = """
        <html><body>
        <form>
          <input type="search" name="q">
          <button type="submit">Find</button>
        </form>
        </body></html>
        """
        a = _build(html)
        a._rule_3_3_6_error_prevention_all()
        self.assertIsNone(_find(a.fact_sheet.possible_findings, "3.3.6"))

    def test_silent_when_data_confirm_present(self):
        html = """
        <html><body>
        <form action="/api/save" method="post">
          <input type="text" name="title">
          <button type="submit" data-confirm="Save now?">Save</button>
        </form>
        </body></html>
        """
        a = _build(html)
        a._rule_3_3_6_error_prevention_all()
        self.assertIsNone(_find(a.fact_sheet.possible_findings, "3.3.6"))

    def test_silent_when_dialog_present(self):
        html = """
        <html><body>
        <form action="/api/save" method="post">
          <input type="text" name="title">
          <button type="submit">Save</button>
        </form>
        <dialog id="review-dialog">Review your changes</dialog>
        </body></html>
        """
        a = _build(html)
        a._rule_3_3_6_error_prevention_all()
        self.assertIsNone(_find(a.fact_sheet.possible_findings, "3.3.6"))


# ─────────────────────────────────────────────────────────────────────────────
# Wiring: confirm all 3 are reachable through _run_rules
# ─────────────────────────────────────────────────────────────────────────────
class TestPhaseNPlusWiring(unittest.TestCase):
    def test_run_rules_dispatches_all_three(self):
        html = """
        <html><head><style>button:focus { outline: none; }</style></head><body>
        <form>
          <input type="text" required>
          <button type="submit">Submit</button>
        </form>
        </body></html>
        """
        a = _build(html)
        a._run_rules()
        ids = {f.criterion_id for f in a.fact_sheet.possible_findings}
        self.assertIn("2.4.13", ids)
        self.assertIn("3.3.5", ids)
        self.assertIn("3.3.6", ids)


if __name__ == "__main__":
    unittest.main()
