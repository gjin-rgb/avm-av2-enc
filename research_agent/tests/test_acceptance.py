"""Acceptance decisions, checked against results the prior CTC rounds produced."""

import unittest

from av2ra.core.models import Interval, Quadrant, Verdict
from av2ra.measure.acceptance import (
    assess,
    assess_all_classes,
    exemption_analysis,
    marginal_ratio,
)


def point(value: float) -> Interval:
  return Interval(value, value, value, 1, "point")


class AcceptanceTest(unittest.TestCase):

  def test_reproduces_recorded_ctc_verdicts(self):
    cases = [
        # (name, BD-rate %, time delta %, preset, expected pass, expected ratio)
        ("i09b A1", 0.15, -4.01, 4, True, 26.7),
        ("i09 A1", 0.11, -2.60, 4, True, 23.6),
        ("i06 A1", 0.75, -14.00, 4, False, 18.7),
        ("i01 A1", 1.22, -4.27, 4, False, 3.5),
        ("0021 A1", 0.10, -6.20, 1, True, 62.0),
    ]
    for name, bdrate, delta, preset, expected_pass, expected_ratio in cases:
      with self.subTest(name):
        result = assess(point(bdrate), point(delta), preset=preset, mode="point")
        self.assertAlmostEqual(result.ratio, expected_ratio, delta=0.15)
        self.assertEqual(result.passed, expected_pass, result.reason)

  def test_speedup_below_the_reporting_floor_yields_no_ratio(self):
    """i07 was recorded with ratio 0.7 from a 0.22% speedup.

    A 0.22% encode-time difference is below the repeatability the cluster has
    ever been shown to have -- its repeatability was in fact never measured --
    so this system declines to form a ratio from it at all. The verdict is the
    same (rejected); the difference is that it no longer implies a measurement
    that was not made.
    """
    result = assess(point(0.30), point(-0.22), preset=4, mode="point")
    self.assertIsNone(result.ratio)
    self.assertFalse(result.passed)
    self.assertIn("no resolved speedup", result.reason)

  def test_infinite_ratio_artifact_is_refused(self):
    """A speedup divided by a BD-rate that rounds to zero is not a ratio."""
    result = assess(
        Interval(-0.01, -0.05, 0.03), Interval(-0.91, -1.60, -0.20), preset=4
    )
    self.assertIsNone(result.ratio)
    self.assertNotEqual(result.verdict, Verdict.CTC_PASS)

  def test_effect_inside_the_noise_is_no_effect(self):
    result = assess(Interval(0.02, -0.10, 0.14), Interval(-0.4, -1.2, 0.4), preset=2)
    self.assertEqual(result.verdict, Verdict.NO_EFFECT)
    self.assertEqual(result.quadrant, Quadrant.UNRESOLVED)
    self.assertIn("not a small win", result.reason)

  def test_absolute_win_needs_no_ratio(self):
    result = assess(point(-0.20), point(-3.0), preset=2)
    self.assertEqual(result.quadrant, Quadrant.Q1_ABSOLUTE_WIN)
    self.assertTrue(result.passed)

  def test_regression_on_both_axes(self):
    result = assess(point(0.5), point(2.0), preset=2)
    self.assertEqual(result.verdict, Verdict.REGRESSION)

  def test_absolute_bdrate_cap_warns_by_default_and_rejects_on_request(self):
    """A high ratio bought with a large absolute quality cost is not combinable.

    It is still a win on its own -- the prior CTC rounds judged on ratio alone
    and a hard cap would have discarded several real ones -- so the default is a
    warning that the arm should not be stacked, and ``reject`` is available for
    assembling a combination round, where the cap is exactly the right rule.
    """
    warned = assess(point(0.9), point(-40.0), preset=4, mode="point")
    self.assertTrue(warned.passed)
    self.assertTrue(any("should not be stacked" in w for w in warned.warnings))

    rejected = assess(
        point(0.9), point(-40.0), preset=4, mode="point", bdrate_cap_mode="reject"
    )
    self.assertFalse(rejected.passed)
    self.assertIn("cap", rejected.reason)

  def test_classes_pass_independently(self):
    combined, detail = assess_all_classes(
        {"A1": (point(0.75), point(-14.0)), "A2": (point(1.29), point(-18.5))},
        preset=4, mode="point",
    )
    self.assertFalse(combined.passed)
    self.assertFalse(detail["A1"].passed)

  def test_conservative_mode_is_stricter_than_point(self):
    bdrate = Interval(0.15, 0.10, 0.22)
    delta = Interval(-4.01, -4.60, -3.20)
    self.assertTrue(assess(bdrate, delta, preset=4, mode="point").passed)
    strict = assess(bdrate, delta, preset=4, mode="conservative")
    self.assertFalse(strict.passed)
    self.assertLess(strict.ratio_conservative, strict.ratio)

  def test_marginal_ratio_flags_a_worthless_tightening(self):
    """The variance floor cost 2.04% of speed and recovered no BD-rate."""
    value, explanation = marginal_ratio(14.0, 0.75, 11.96, 0.75)
    self.assertIsNone(value)
    self.assertIn("pure loss", explanation)

  def test_marginal_ratio_supports_a_good_tightening(self):
    value, explanation = marginal_ratio(20.0, 1.00, 14.0, 0.50)
    self.assertAlmostEqual(value, 12.0, places=6)
    self.assertIn("raises", explanation)

  def test_exemption_finds_the_clip_carrying_the_damage(self):
    rows = exemption_analysis(
        {"a": 0.10, "b": 1.90, "c": 0.20, "d": 0.15},
        {"a": 15.0, "b": 16.0, "c": 15.0, "d": 14.0},
        bar=20.0,
    )
    self.assertEqual(rows[0]["sequence"], "b")
    self.assertTrue(rows[0]["clears_bar_without"])
    self.assertGreater(rows[0]["bdrate_share"], 0.7)


if __name__ == "__main__":
  unittest.main()
