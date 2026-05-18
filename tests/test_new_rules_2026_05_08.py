"""Smoke tests for the two free-gap-closure rules added on 2026-05-08:

* WCAG 1.2.5 Audio Description (Prerecorded) — AA — source-only
* WCAG 2.4.12 Focus Not Obscured (Enhanced) — AAA — relies on rendered JS
  harness, so we verify the rule fires when actions["partiallyObscuredFocus"]
  is non-empty (we drive the rule directly with a stub payload).
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from wcag.analyzers.html_analyzer import HtmlAnalyzer  # noqa: E402


def _build(html: str, name: str = "fixture.html") -> HtmlAnalyzer:
    a = HtmlAnalyzer(html.encode("utf-8"), name)
    a._html_text = html  # bypass _decode_html so static rules can read source
    return a


class TestRule125AudioDescription(unittest.TestCase):
    def test_video_without_description_track_emits_finding(self):
        html = """
        <!doctype html><html lang="en"><head><title>Demo</title></head><body>
          <h1>Welcome</h1>
          <video controls>
            <source src="movie.mp4" type="video/mp4">
            <track kind="captions" srclang="en" src="captions.vtt" label="English">
          </video>
        </body></html>
        """
        a = _build(html)
        a._rule_1_2_5_audio_description_prerecorded()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertIn("1.2.5", ids, f"Expected 1.2.5 finding; got {ids}")

    def test_video_with_description_track_does_not_emit(self):
        html = """
        <!doctype html><html lang="en"><head><title>Demo</title></head><body>
          <video controls>
            <source src="movie.mp4" type="video/mp4">
            <track kind="captions" srclang="en" src="captions.vtt">
            <track kind="descriptions" srclang="en" src="descriptions.vtt">
          </video>
        </body></html>
        """
        a = _build(html)
        a._rule_1_2_5_audio_description_prerecorded()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertNotIn("1.2.5", ids, f"Did not expect 1.2.5; got {ids}")

    def test_audio_only_does_not_trigger_1_2_5(self):
        html = """
        <!doctype html><html lang="en"><head><title>Demo</title></head><body>
          <audio controls><source src="pod.mp3" type="audio/mpeg"></audio>
        </body></html>
        """
        a = _build(html)
        a._rule_1_2_5_audio_description_prerecorded()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertNotIn("1.2.5", ids, f"Audio-only should not trigger 1.2.5; got {ids}")

    def test_singular_description_kind_also_satisfies(self):
        # The regex allows both 'description' and 'descriptions'.
        html = """
        <!doctype html><html lang="en"><head><title>Demo</title></head><body>
          <video controls>
            <source src="movie.mp4" type="video/mp4">
            <track kind="description" srclang="en" src="d.vtt">
          </video>
        </body></html>
        """
        a = _build(html)
        a._rule_1_2_5_audio_description_prerecorded()
        ids = [f.criterion_id for f in a.fact_sheet.possible_findings]
        self.assertNotIn("1.2.5", ids)


class TestRule2412FocusNotObscuredEnhanced(unittest.TestCase):
    def test_partial_obscure_emits_finding(self):
        a = _build("<html><body><button>x</button></body></html>")
        stub = {
            "partiallyObscuredFocus": [
                {"location": "button#submit", "obscuredBy": "div.cookie-banner", "coverage": "partial"},
            ],
            "obscuredFocus": [],
        }
        a._rule_2_4_12_focus_not_obscured_enhanced(stub)
        ids = [f.criterion_id for f in a.fact_sheet.confirmed_findings]
        self.assertIn("2.4.12", ids, f"Expected 2.4.12; got {ids}")

    def test_full_obscure_also_emits_2_4_12(self):
        a = _build("<html><body></body></html>")
        stub = {
            "partiallyObscuredFocus": [
                {"location": "a", "obscuredBy": "header", "coverage": "full"},
            ],
        }
        a._rule_2_4_12_focus_not_obscured_enhanced(stub)
        ids = [f.criterion_id for f in a.fact_sheet.confirmed_findings]
        self.assertIn("2.4.12", ids)

    def test_no_obscure_emits_nothing(self):
        a = _build("<html><body></body></html>")
        a._rule_2_4_12_focus_not_obscured_enhanced({"partiallyObscuredFocus": []})
        ids = [f.criterion_id for f in a.fact_sheet.confirmed_findings]
        self.assertNotIn("2.4.12", ids)

    def test_missing_key_is_safe(self):
        a = _build("<html><body></body></html>")
        a._rule_2_4_12_focus_not_obscured_enhanced({})
        ids = [f.criterion_id for f in a.fact_sheet.confirmed_findings]
        self.assertNotIn("2.4.12", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
