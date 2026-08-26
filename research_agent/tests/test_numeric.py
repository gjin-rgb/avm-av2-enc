"""The arithmetic that verdicts rest on, checked against the references."""

import math
import random
import unittest

from av2ra.measure import stats
from av2ra.measure.bdrate import NO_OVERLAP, NON_MONOTONIC, bd_rate
from av2ra.measure.pchip import pchip_interpolate

try:
  import numpy as np
  import scipy.interpolate as si
  import scipy.stats as ss
  HAVE_SCIPY = True
except ImportError:  # the fallbacks exist precisely for this case
  HAVE_SCIPY = False


class PchipTest(unittest.TestCase):

  @unittest.skipUnless(HAVE_SCIPY, "scipy not installed")
  def test_matches_scipy(self):
    rng = np.random.default_rng(7)
    for _ in range(200):
      n = int(rng.integers(3, 9))
      x = np.sort(rng.uniform(0, 10, n))
      if len(set(np.round(x, 9))) < n:
        continue
      y = rng.uniform(-5, 5, n)
      xi = np.linspace(x[0], x[-1], 37)
      mine = np.array(pchip_interpolate(list(x), list(y), list(xi)))
      theirs = si.pchip_interpolate(x, y, xi)
      self.assertLess(float(np.max(np.abs(mine - theirs))), 1e-10)

  def test_monotone_data_stays_monotone(self):
    x = [0.0, 1.0, 2.0, 3.0]
    y = [0.0, 1.0, 1.0, 5.0]
    samples = [i / 60.0 * 3.0 for i in range(61)]
    values = pchip_interpolate(x, y, samples)
    self.assertTrue(all(b >= a - 1e-12 for a, b in zip(values, values[1:])))

  def test_rejects_unsorted_input(self):
    with self.assertRaises(ValueError):
      pchip_interpolate([1.0, 0.0, 2.0], [0.0, 1.0, 2.0], [0.5])


class BdRateTest(unittest.TestCase):

  def test_identical_curves_are_zero(self):
    rates = [1000.0, 2000.0, 4000.0, 8000.0]
    quality = [32.0, 35.0, 38.0, 41.0]
    value, error = bd_rate(rates, quality, rates, quality)
    self.assertEqual(error, "")
    self.assertAlmostEqual(value, 0.0, places=6)

  def test_uniform_rate_increase_is_that_percentage(self):
    rates = [1000.0, 2000.0, 4000.0, 8000.0]
    quality = [32.0, 35.0, 38.0, 41.0]
    worse = [r * 1.10 for r in rates]
    value, error = bd_rate(rates, quality, worse, quality)
    self.assertEqual(error, "")
    self.assertAlmostEqual(value, 10.0, places=3)

  def test_non_monotonic_is_refused_not_guessed(self):
    _value, error = bd_rate(
        [1000.0, 900.0, 4000.0], [32.0, 35.0, 38.0],
        [1000.0, 2000.0, 4000.0], [32.0, 35.0, 38.0],
    )
    self.assertEqual(error, NON_MONOTONIC)

  def test_disjoint_quality_ranges_are_refused(self):
    _value, error = bd_rate(
        [1000.0, 2000.0], [20.0, 22.0], [1000.0, 2000.0], [40.0, 42.0]
    )
    self.assertEqual(error, NO_OVERLAP)

  @unittest.skipUnless(HAVE_SCIPY, "scipy not installed")
  def test_matches_the_avm_reference(self):
    import os
    import sys
    import types

    reference_dir = "/home/user/avm/tools/convexhull_framework/src"
    if not os.path.isdir(reference_dir):
      self.skipTest("AVM tree not available")
    sys.path.insert(0, reference_dir)
    sys.modules.setdefault("Config", types.ModuleType("Config")).LoggerName = "x"
    import CalcBDRate as reference

    rng = random.Random(3)
    for _ in range(200):
      n = rng.choice([4, 5, 6])
      anchor_rates = sorted(rng.uniform(300, 9000) for _ in range(n))
      anchor_quality = sorted(rng.uniform(30, 50) for _ in range(n))
      scale = rng.uniform(0.9, 1.1)
      cand_rates = sorted(r * scale * rng.uniform(0.98, 1.02) for r in anchor_rates)
      cand_quality = sorted(q + rng.uniform(-0.3, 0.3) for q in anchor_quality)
      mine = bd_rate(anchor_rates, anchor_quality, cand_rates, cand_quality)
      theirs = reference.BD_RATE(
          "PSNR_Y", anchor_rates, anchor_quality, cand_rates, cand_quality
      )
      if theirs[0] == -1:
        self.assertTrue(mine[1])
      else:
        self.assertAlmostEqual(mine[0], theirs[1], places=6)


class StatsTest(unittest.TestCase):

  @unittest.skipUnless(HAVE_SCIPY, "scipy not installed")
  def test_t_quantiles_match_scipy(self):
    for df in (1, 2, 3, 5, 7.4, 12, 30.2, 120):
      self.assertAlmostEqual(stats.student_t_ppf(0.975, df), float(ss.t.ppf(0.975, df)), places=8)

  def test_noise_only_data_does_not_exclude_zero(self):
    rng = random.Random(11)
    anchor = [100 + rng.gauss(0, 2.4) for _ in range(5)]
    candidate = [100 + rng.gauss(0, 2.4) for _ in range(5)]
    self.assertFalse(stats.welch_pct_ci(anchor, candidate).excludes_zero)

  def test_paired_design_resolves_what_unpaired_cannot(self):
    """Pairing removes the clip-to-clip spread that dominates an encode set."""
    rng = random.Random(5)
    pairs = []
    for _ in range(12):
      base = rng.uniform(50, 5000)          # clips differ by two orders of magnitude
      pairs.append((base, base * 0.97 * (1 + rng.gauss(0, 0.004))))
    paired = stats.paired_log_ratio_ci(pairs)
    unpaired = stats.welch_pct_ci([a for a, _ in pairs], [b for _, b in pairs])
    self.assertTrue(paired.excludes_zero)
    self.assertFalse(unpaired.excludes_zero)
    self.assertAlmostEqual(paired.point, -3.0, delta=0.6)

  def test_mde_reproduces_the_documented_underpowered_design(self):
    # The prior round measured a 2.4% noise floor with n=5 and could not resolve
    # anything under about 3.5%.
    self.assertAlmostEqual(stats.mde_pct(2.4, 5), 3.5, delta=0.4)
    self.assertGreater(stats.required_reps(2.4, 1.0), 20)

  def test_bootstrap_is_deterministic(self):
    values = [0.1, 0.4, 1.9, 0.2, 0.15]
    first = stats.bootstrap_mean_ci(values, seed="N0001")
    second = stats.bootstrap_mean_ci(values, seed="N0001")
    self.assertEqual((first.lo, first.point, first.hi), (second.lo, second.point, second.hi))

  def test_holm_controls_the_family(self):
    self.assertEqual(stats.holm_bonferroni([0.001, 0.02, 0.04, 0.9]), [True, False, False, False])


if __name__ == "__main__":
  unittest.main()
