"""Phase M-net-new — 8 new HTML SC.

M1: 1.4.2 Audio Control
M2: 2.1.4 Character Key Shortcuts (POSSIBLE)
M3: 2.2.2 Pause, Stop, Hide
M4: 2.3.1 Three Flashes or Below
M5: 2.5.2 Pointer Cancellation
M6: 2.5.4 Motion Actuation
M7: 3.2.6 Consistent Help (WCAG 2.2 A) — POSSIBLE
M8: 3.3.7 Redundant Entry (WCAG 2.2 A) — POSSIBLE
"""
import unittest

from wcag.analyzers.html_analyzer import HtmlAnalyzer


def _confirmed(fs, criterion: str, remediation_id: str):
    return [f for f in fs.confirmed_findings
            if f.criterion_id == criterion and f.remediation_id == remediation_id]


def _possible(fs, criterion: str, remediation_id: str):
    return [f for f in fs.possible_findings
            if f.criterion_id == criterion and f.remediation_id == remediation_id]


def _analyze(html: str):
    return HtmlAnalyzer(html.encode("utf-8"), "m.html").analyze()


# ─────────────────────────────────────────────────────────────────────────────
# M1 — 1.4.2 Audio Control
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml142AudioControl(unittest.TestCase):
    def test_pass_audio_with_controls(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a><audio src='x.mp3' autoplay controls></audio></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "1.4.2", "html_audio_control")), 0)

    def test_fail_audio_autoplay_no_controls(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a><audio src='x.mp3' autoplay></audio></body></html>"""
        fs = _analyze(html)
        findings = _confirmed(fs, "1.4.2", "html_audio_control")
        self.assertEqual(len(findings), 1)

    def test_fail_video_autoplay_with_sound(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a><video src='v.mp4' autoplay></video></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "1.4.2", "html_audio_control")), 1)

    def test_pass_video_autoplay_muted(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a><video src='v.mp4' autoplay muted loop></video></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "1.4.2", "html_audio_control")), 0)


# ─────────────────────────────────────────────────────────────────────────────
# M2 — 2.1.4 Character Key Shortcuts
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml214CharacterKeyShortcuts(unittest.TestCase):
    def test_pass_no_shortcuts(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a><button>OK</button></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_possible(fs, "2.1.4", "html_character_key_shortcuts")), 0)

    def test_fail_inline_single_key(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body onkeydown="if (event.key === '/') openSearch();">
          <a href='mailto:a@b.com'>contact</a><button>OK</button>
        </body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_possible(fs, "2.1.4", "html_character_key_shortcuts")), 1)

    def test_pass_modifier_key_check(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body onkeydown="if (event.ctrlKey && event.key === '/') openSearch();">
          <a href='mailto:a@b.com'>contact</a><button>OK</button>
        </body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_possible(fs, "2.1.4", "html_character_key_shortcuts")), 0)


# ─────────────────────────────────────────────────────────────────────────────
# M3 — 2.2.2 Pause Stop Hide
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml222PauseStopHide(unittest.TestCase):
    def test_fail_marquee(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a><marquee>scrolling text</marquee></body></html>"""
        fs = _analyze(html)
        self.assertGreaterEqual(len(_confirmed(fs, "2.2.2", "html_pause_stop_hide")), 1)

    def test_fail_long_infinite_animation(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title>
        <style>.spin { animation: rotate 8s infinite; }</style></head>
        <body><a href='mailto:a@b.com'>contact</a><div class='spin'>loops 8s</div></body></html>"""
        fs = _analyze(html)
        self.assertGreaterEqual(len(_confirmed(fs, "2.2.2", "html_pause_stop_hide")), 1)

    def test_pass_no_motion(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a><p>Plain text page.</p></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "2.2.2", "html_pause_stop_hide")), 0)


# ─────────────────────────────────────────────────────────────────────────────
# M4 — 2.3.1 Three Flashes
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml231ThreeFlashes(unittest.TestCase):
    def test_fail_fast_infinite_animation(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title>
        <style>.flash { animation: blink 0.2s infinite; }</style></head>
        <body><a href='mailto:a@b.com'>contact</a><div class='flash'>flash</div></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "2.3.1", "html_three_flashes")), 1)

    def test_pass_slow_animation(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title>
        <style>.fade { animation: fade 2s infinite; }</style></head>
        <body><a href='mailto:a@b.com'>contact</a><div class='fade'>slow</div></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "2.3.1", "html_three_flashes")), 0)


# ─────────────────────────────────────────────────────────────────────────────
# M5 — 2.5.2 Pointer Cancellation
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml252PointerCancellation(unittest.TestCase):
    def test_fail_mousedown_only(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a>
          <button onmousedown="submitForm()">Submit</button>
        </body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "2.5.2", "html_pointer_cancellation")), 1)

    def test_pass_mousedown_with_click(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a>
          <button onmousedown="armSubmit()" onclick="submitForm()">Submit</button>
        </body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "2.5.2", "html_pointer_cancellation")), 0)


# ─────────────────────────────────────────────────────────────────────────────
# M6 — 2.5.4 Motion Actuation
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml254MotionActuation(unittest.TestCase):
    def test_fail_devicemotion_listener(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a>
          <script>window.addEventListener('devicemotion', handleShake);</script>
        </body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "2.5.4", "html_motion_actuation")), 1)

    def test_pass_no_motion_listeners(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a>
          <script>window.addEventListener('click', x);</script>
        </body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_confirmed(fs, "2.5.4", "html_motion_actuation")), 0)


# ─────────────────────────────────────────────────────────────────────────────
# M7 — 3.2.6 Consistent Help
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml326ConsistentHelp(unittest.TestCase):
    def test_pass_page_has_help_link(self):
        # Form-bearing page that DOES have help → no finding.
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><nav><a href='/help'>Help</a></nav>
          <form><input type='text' name='q'></form>
        </body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_possible(fs, "3.2.6", "html_consistent_help")), 0)

    def test_fail_form_page_has_no_help(self):
        # Form-bearing page with NO help mechanism → POSSIBLE.
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body>
          <form><input type='text' name='q'></form>
        </body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_possible(fs, "3.2.6", "html_consistent_help")), 1)

    def test_pass_static_page_no_form_no_help(self):
        # Static marketing page with no form and no help → must NOT fire.
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><p>Pure marketing copy with no relief in sight.</p></body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_possible(fs, "3.2.6", "html_consistent_help")), 0)


# ─────────────────────────────────────────────────────────────────────────────
# M8 — 3.3.7 Redundant Entry
# ─────────────────────────────────────────────────────────────────────────────
class TestHtml337RedundantEntry(unittest.TestCase):
    def test_fail_two_email_inputs_no_autocomplete(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a>
          <form>
            <input type='email' name='shipping_email'>
            <input type='email' name='billing_email'>
          </form>
        </body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_possible(fs, "3.3.7", "html_redundant_entry")), 1)

    def test_pass_two_emails_with_autocomplete(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a>
          <form>
            <input type='email' name='shipping_email' autocomplete='email'>
            <input type='email' name='billing_email' autocomplete='email'>
          </form>
        </body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_possible(fs, "3.3.7", "html_redundant_entry")), 0)

    def test_pass_single_email(self):
        html = """<!DOCTYPE html><html lang='en'><head><title>x</title></head>
        <body><a href='mailto:a@b.com'>contact</a>
          <form><input type='email' name='email'></form>
        </body></html>"""
        fs = _analyze(html)
        self.assertEqual(len(_possible(fs, "3.3.7", "html_redundant_entry")), 0)


if __name__ == '__main__':
    unittest.main()
