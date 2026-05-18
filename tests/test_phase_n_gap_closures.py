"""Smoke tests for Phase N gap closures (2026-05-18).

Adds 9 new WCAG rules to the HTML analyzer:

AAA quick wins (forks of existing AA rules):
* 1.4.6 Contrast (Enhanced) \u2014 driven via rendered text_nodes
* 2.5.5 Target Size (Enhanced) \u2014 source-only
* 2.4.10 Section Headings \u2014 uses parsed paragraph data
* 2.4.9 Link Purpose (Link Only) \u2014 uses parsed hyperlinks
* 2.1.3 Keyboard (No Exception) \u2014 source-only
* 1.4.9 Images of Text (No Exception) \u2014 source-only

A/AA single-page heuristics (POSSIBLE tier):
* 2.5.1 Pointer Gestures \u2014 source-only
* 3.3.3 Error Suggestion \u2014 source-only
* 3.3.4 Error Prevention \u2014 source-only
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from wcag.analyzers.html_analyzer import HtmlAnalyzer  # noqa: E402
from wcag.models import HyperlinkInfo, ParagraphInfo  # noqa: E402


def _build(html: str, name: str = "fixture.html") -> HtmlAnalyzer:
    a = HtmlAnalyzer(html.encode("utf-8"), name)
    a._html_text = html
    return a


# ─────────────────────────────────────────────────────────────────────────────
# 1.4.6 Contrast (Enhanced)
# ─────────────────────────────────────────────────────────────────────────────
class TestRule146ContrastEnhanced(unittest.TestCase):
    def test_low_aaa_contrast_emits(self):
        a = _build("<html><body><p>x</p></body></html>")
        nodes = [{
            "color": "rgb(110, 110, 110)",
            "backgroundColor": "rgb(255, 255, 255)",
            "backgroundImage": "none",
            "fontSizePx": 16,
            "fontWeight": "400",
            "text": "Hello world",
            "location": "p",
            "tag": "p",
        }]
        a._rule_1_4_6_contrast_enhanced(nodes)
        ids = [f.criterion_id for f in a.fact_sheet.confirmed_findings]
        self.assertIn("1.4.6", ids)

    def test_aaa_compliant_does_not_emit(self):
        a = _build("<html><body><p>x</p></body></html>")
        nodes = [{
            "color": "rgb(0, 0, 0)",
            "backgroundColor": "rgb(255, 255, 255)",
            "backgroundImage": "none",
            "fontSizePx": 16,
            "fontWeight": "400",
            "text": "Hello",
            "location": "p",
        }]
        a._rule_1_4_6_contrast_enhanced(nodes)
        ids = [f.criterion_id for f in a.fact_sheet.confirmed_findings]
        self.assertNotIn("1.4.6", ids)

    def test_empty_node_list_safe(self):
        a = _build("<html></html>")
        a._rule_1_4_6_contrast_enhanced([])
        self.assertEqual(a.fact_sheet.confirmed_findings, [])


# ─────────────────────────────────────────────────────────────────────────────
# 2.5.5 Target Size (Enhanced)
# ─────────────────────────────────────────────────────────────────────────────
class TestRule255TargetSizeEnhanced(unittest.TestCase):
    def test_small_button_emits(self):
        html = '<html><body><button style="width:30px;height:30px">x</button></body></html>'
        a = _build(html)
        a._rule_2_5_5_target_size_enhanced()
        ids = [f.criterion_id for f in a.fact_sheet.confirmed_findings]
        self.assertIn("2.5.5", ids)

    def test_44px_button_does_not_emit(self):
        html = '<html><body><button style="width:44px;height:44px">x</button></body></html>'
        a = _build(html)
        a._rule_2_5_5_target_size_enhanced()
        ids = [f.criterion_id for f in a.fact_sheet.confirmed_findings]
        self.assertNotIn("2.5.5", ids)


# ─────────────────────────────────────────────────────────────────────────────
# 2.4.10 Section Headings
# ─────────────────────────────────────────────────────────────────────────────
class TestRule2410SectionHeadings(unittest.TestCase):
    def test_long_doc_with_no_headings_emits(self):
        a = _build("<html><body><p>x</p></body></html>")
        # Inject 300+ words via fact_sheet.paragraphs (the rule reads this)
        words = " ".join(["word"] * 350)
        a.fact_sheet.paragraphs = [ParagraphInfo(
            index=0, style_name="p", text=words, list_level=None, is_empty=False, run_language=None
        )]
        a._rule_2_4_10_section_headings()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertIn("2.4.10", ids)

    def test_long_doc_with_headings_does_not_emit(self):
        a = _build("<html><body></body></html>")
        words = " ".join(["word"] * 350)
        a.fact_sheet.paragraphs = [
            ParagraphInfo(index=0, style_name="H1", text="Section 1", list_level=None, is_empty=False, run_language=None),
            ParagraphInfo(index=1, style_name="H2", text="Section 2", list_level=None, is_empty=False, run_language=None),
            ParagraphInfo(index=2, style_name="p", text=words, list_level=None, is_empty=False, run_language=None),
        ]
        a._rule_2_4_10_section_headings()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertNotIn("2.4.10", ids)

    def test_short_doc_skipped(self):
        a = _build("<html><body></body></html>")
        a.fact_sheet.paragraphs = [ParagraphInfo(
            index=0, style_name="p", text="short", list_level=None, is_empty=False, run_language=None
        )]
        a._rule_2_4_10_section_headings()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertNotIn("2.4.10", ids)


# ─────────────────────────────────────────────────────────────────────────────
# 2.4.9 Link Purpose (Link Only)
# ─────────────────────────────────────────────────────────────────────────────
class TestRule249LinkPurposeLinkOnly(unittest.TestCase):
    def test_generic_link_emits(self):
        a = _build("<html><body><a href='/x'>click here</a></body></html>")
        a.fact_sheet.hyperlinks = [HyperlinkInfo(paragraph_index=0, display_text="click here", url="/x")]
        a._rule_2_4_9_link_purpose_link_only()
        ids = [f.criterion_id for f in a.fact_sheet.confirmed_findings]
        self.assertIn("2.4.9", ids)

    def test_descriptive_link_does_not_emit(self):
        a = _build("<html><body><a href='/x'>Annual report 2025</a></body></html>")
        a.fact_sheet.hyperlinks = [HyperlinkInfo(paragraph_index=0, display_text="Annual report 2025", url="/x")]
        a._rule_2_4_9_link_purpose_link_only()
        ids = [f.criterion_id for f in a.fact_sheet.confirmed_findings]
        self.assertNotIn("2.4.9", ids)


# ─────────────────────────────────────────────────────────────────────────────
# 2.1.3 Keyboard (No Exception)
# ─────────────────────────────────────────────────────────────────────────────
class TestRule213KeyboardNoException(unittest.TestCase):
    def test_div_onclick_emits(self):
        html = "<html><body><div onclick='go()'>Tap</div></body></html>"
        a = _build(html)
        a._rule_2_1_3_keyboard_no_exception()
        ids = [f.criterion_id for f in a.fact_sheet.confirmed_findings]
        self.assertIn("2.1.3", ids)

    def test_proper_button_does_not_emit(self):
        html = "<html><body><button>OK</button></body></html>"
        a = _build(html)
        a._rule_2_1_3_keyboard_no_exception()
        ids = [f.criterion_id for f in a.fact_sheet.confirmed_findings]
        self.assertNotIn("2.1.3", ids)


# ─────────────────────────────────────────────────────────────────────────────
# 1.4.9 Images of Text (No Exception)
# ─────────────────────────────────────────────────────────────────────────────
class TestRule149ImagesOfTextNoException(unittest.TestCase):
    def test_svg_with_text_emits(self):
        html = "<html><body><svg><text x='10' y='20'>Title</text></svg></body></html>"
        a = _build(html)
        a._rule_1_4_9_images_of_text_no_exception()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertIn("1.4.9", ids)

    def test_plain_svg_does_not_emit(self):
        html = "<html><body><svg><circle cx='5' cy='5' r='2'/></svg></body></html>"
        a = _build(html)
        a._rule_1_4_9_images_of_text_no_exception()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertNotIn("1.4.9", ids)


# ─────────────────────────────────────────────────────────────────────────────
# 2.5.1 Pointer Gestures  (closes the last Level A gap)
# ─────────────────────────────────────────────────────────────────────────────
class TestRule251PointerGestures(unittest.TestCase):
    def test_touchmove_without_click_fallback_emits(self):
        html = """<html><body><script>
            element.addEventListener('touchmove', handler);
            element.addEventListener('touchend', save);
        </script></body></html>"""
        a = _build(html)
        a._rule_2_5_1_pointer_gestures()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertIn("2.5.1", ids)

    def test_touchmove_with_click_fallback_does_not_emit(self):
        html = """<html><body><script>
            element.addEventListener('touchmove', handler);
            element.addEventListener('click', handler);
        </script></body></html>"""
        a = _build(html)
        a._rule_2_5_1_pointer_gestures()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertNotIn("2.5.1", ids)

    def test_multitouch_detection(self):
        html = """<html><body><script>
            if (event.touches.length > 1) { pinchZoom(); }
        </script></body></html>"""
        a = _build(html)
        a._rule_2_5_1_pointer_gestures()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertIn("2.5.1", ids)


# ─────────────────────────────────────────────────────────────────────────────
# 3.3.3 Error Suggestion
# ─────────────────────────────────────────────────────────────────────────────
class TestRule333ErrorSuggestion(unittest.TestCase):
    def test_required_form_without_error_surface_emits(self):
        html = """<html><body>
            <form><input type='text' required><button>Submit</button></form>
        </body></html>"""
        a = _build(html)
        a._rule_3_3_3_error_suggestion()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertIn("3.3.3", ids)

    def test_required_form_with_aria_live_does_not_emit(self):
        html = """<html><body>
            <form><input type='text' required><div role='alert' id='err'></div><button>Submit</button></form>
        </body></html>"""
        a = _build(html)
        a._rule_3_3_3_error_suggestion()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertNotIn("3.3.3", ids)

    def test_form_with_no_required_does_not_emit(self):
        html = """<html><body>
            <form><input type='text'><button>Search</button></form>
        </body></html>"""
        a = _build(html)
        a._rule_3_3_3_error_suggestion()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertNotIn("3.3.3", ids)


# ─────────────────────────────────────────────────────────────────────────────
# 3.3.4 Error Prevention
# ─────────────────────────────────────────────────────────────────────────────
class TestRule334ErrorPrevention(unittest.TestCase):
    def test_delete_button_without_confirm_emits(self):
        html = "<html><body><form><button>Delete account</button></form></body></html>"
        a = _build(html)
        a._rule_3_3_4_error_prevention()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertIn("3.3.4", ids)

    def test_delete_button_with_confirm_modal_nearby_does_not_emit(self):
        html = """<html><body>
            <div>Are you sure you want to proceed?</div>
            <form><button>Delete account</button></form>
        </body></html>"""
        a = _build(html)
        a._rule_3_3_4_error_prevention()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertNotIn("3.3.4", ids)

    def test_harmless_button_does_not_emit(self):
        html = "<html><body><form><button>Search</button></form></body></html>"
        a = _build(html)
        a._rule_3_3_4_error_prevention()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertNotIn("3.3.4", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
