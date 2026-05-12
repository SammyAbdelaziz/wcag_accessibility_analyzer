"""Unit tests for the shared 1.4.11 Non-text Contrast evaluator."""
import unittest

from wcag.common.non_text_contrast import (
    MIN_NON_TEXT_CONTRAST,
    evaluate_pair,
    normalize_hex,
    passes,
)


class TestNormalizeHex(unittest.TestCase):
    def test_six_char_lowercase(self):
        self.assertEqual(normalize_hex('ffffff'), 'FFFFFF')

    def test_six_char_with_hash(self):
        self.assertEqual(normalize_hex('#abcdef'), 'ABCDEF')

    def test_three_char_shorthand(self):
        self.assertEqual(normalize_hex('#fff'), 'FFFFFF')
        self.assertEqual(normalize_hex('abc'), 'AABBCC')

    def test_argb_strips_alpha(self):
        self.assertEqual(normalize_hex('FF112233'), '112233')

    def test_invalid_length(self):
        self.assertIsNone(normalize_hex('12345'))

    def test_invalid_chars(self):
        self.assertIsNone(normalize_hex('GGGGGG'))

    def test_empty(self):
        self.assertIsNone(normalize_hex(''))
        self.assertIsNone(normalize_hex(None))


class TestEvaluatePair(unittest.TestCase):
    def test_black_on_white_passes(self):
        result = evaluate_pair('000000', 'FFFFFF')
        self.assertIsNotNone(result)
        ratio, ok = result
        self.assertGreater(ratio, 20.0)
        self.assertTrue(ok)

    def test_identical_colors_fail(self):
        result = evaluate_pair('888888', '888888')
        self.assertIsNotNone(result)
        ratio, ok = result
        self.assertAlmostEqual(ratio, 1.0, places=2)
        self.assertFalse(ok)

    def test_borderline_just_below_3_to_1(self):
        # Light gray on white — known < 3:1
        result = evaluate_pair('CCCCCC', 'FFFFFF')
        self.assertIsNotNone(result)
        ratio, ok = result
        self.assertLess(ratio, MIN_NON_TEXT_CONTRAST)
        self.assertFalse(ok)

    def test_dark_blue_on_white_passes(self):
        result = evaluate_pair('1A4D8C', 'FFFFFF')
        self.assertIsNotNone(result)
        _, ok = result
        self.assertTrue(ok)

    def test_unparsable_returns_none(self):
        self.assertIsNone(evaluate_pair('not-a-color', 'FFFFFF'))
        self.assertIsNone(evaluate_pair('FFFFFF', None))


class TestPassesHelper(unittest.TestCase):
    def test_passes_true_for_strong_contrast(self):
        self.assertTrue(passes('000000', 'FFFFFF'))

    def test_passes_false_for_weak_contrast(self):
        self.assertFalse(passes('CCCCCC', 'FFFFFF'))

    def test_passes_false_for_invalid(self):
        self.assertFalse(passes('xyz', 'FFFFFF'))


if __name__ == '__main__':
    unittest.main()
