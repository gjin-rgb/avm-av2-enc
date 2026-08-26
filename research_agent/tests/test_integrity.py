"""The gates that make an autonomous result believable."""

import unittest

from av2ra.core.models import EncodeResult, Interval, MechanismClass
from av2ra.integrity import gates, policy


def result(sequence, qp, arm, md5, rep=0, ok=True):
  out = EncodeResult(sequence=sequence, qp=qp, preset=4, arm=arm, ok=ok)
  out.bitstream_md5 = md5
  out.rep = rep
  return out


class PolicyTest(unittest.TestCase):

  def test_encoder_sources_are_writable_and_nothing_else_is(self):
    check = policy.PatchPolicy()
    self.assertTrue(check.path_allowed("av2/encoder/tx_search.c")[0])
    self.assertTrue(check.path_allowed("avm_dsp/x86/fwd_txfm_avx2.c")[0])
    for forbidden in (
        "tools/convexhull_framework/src/CalcBDRate.py",
        "research_agent/av2ra/measure/acceptance.py",
        "test/temporal_filter_test.cc",
        "CMakeLists.txt",
        "av2/decoder/decodeframe.c",
        "third_party/libyuv/foo.c",
    ):
      with self.subTest(forbidden):
        self.assertFalse(check.path_allowed(forbidden)[0])

  def test_benchmark_fitting_is_caught(self):
    diff = (
        "+++ b/av2/encoder/rdopt.c\n"
        "@@ -10,0 +11,6 @@\n"
        "+  if (cm->width == 1920 && cm->height == 1080) return 0;\n"
        "+  const char *mode = getenv(\"AV2_FAST\");\n"
        "+  int r = rand();\n"
        "+  if (frame_number < 17) skip = 1;\n"
        "+  if (qindex == 185) return 1;\n"
        "+  // OldTownCross needs care\n"
    )
    kinds = {v.kind for v in policy.scan_diff(diff)}
    self.assertIn("hard-coded test resolution", kinds)
    self.assertIn("environment-dependent behaviour", kinds)
    self.assertIn("nondeterminism", kinds)
    self.assertIn("hard-coded test frame count", kinds)
    self.assertIn("hard-coded CTC QP", kinds)

  def test_comments_are_not_scanned(self):
    diff = (
        "+++ b/av2/encoder/rdopt.c\n@@ -1,0 +2,1 @@\n"
        "+  // tuned on RitualDance and OldTownCross\n"
    )
    self.assertEqual(policy.scan_diff(diff), [])

  def test_a_define_the_patch_never_mentions_is_refused(self):
    diff = "+++ b/av2/encoder/rdopt.c\n@@ -1,0 +2,1 @@\n+  int x = 1;\n"
    violations = policy.check_defines({"NDEBUG": "1"}, diff)
    self.assertEqual(len(violations), 1)
    self.assertIn("never appears in the patch", violations[0].detail)

  def test_a_define_the_patch_introduces_is_accepted(self):
    diff = (
        "+++ b/av2/encoder/tx_search.c\n@@ -1,0 +2,2 @@\n"
        "+#define TX_MARGIN 2\n+  if (m < TX_MARGIN) return 0;\n"
    )
    self.assertEqual(policy.check_defines({"TX_MARGIN": "2"}, diff), [])


class ActivationTest(unittest.TestCase):

  def test_identical_bitstream_fails_an_approximating_patch(self):
    results = [
        result("a", 185, "anchor", "X"), result("a", 185, "candidate", "X"),
        result("b", 185, "anchor", "Y"), result("b", 185, "candidate", "Y"),
    ]
    gate = gates.check_activation(results, MechanismClass.APPROX)
    self.assertFalse(gate.passed)
    self.assertIn("never fired", gate.detail)

  def test_identical_bitstream_passes_a_reuse_patch(self):
    results = [result("a", 185, "anchor", "X"), result("a", 185, "candidate", "X")]
    gate = gates.check_activation(results, MechanismClass.REUSE)
    self.assertTrue(gate.passed)
    self.assertIn("zero by proof", gate.detail)

  def test_a_reuse_patch_that_changes_the_bitstream_is_a_bug(self):
    results = [result("a", 185, "anchor", "X"), result("a", 185, "candidate", "Z")]
    gate = gates.check_activation(results, MechanismClass.REUSE)
    self.assertFalse(gate.passed)
    self.assertIn("has a bug", gate.detail)

  def test_partial_activation_warns_without_blocking(self):
    results = []
    for index in range(8):
      results.append(result(f"c{index}", 185, "anchor", f"A{index}"))
      results.append(
          result(f"c{index}", 185, "candidate", f"A{index}" if index else "B0")
      )
    gate = gates.check_activation(results, MechanismClass.APPROX)
    self.assertTrue(gate.passed)
    self.assertFalse(gate.blocking)


class OtherGatesTest(unittest.TestCase):

  def test_determinism(self):
    results = [result("a", 185, "anchor", "X", 0), result("a", 185, "anchor", "W", 1)]
    self.assertFalse(gates.check_determinism(results).passed)

  def test_holdout_sign_flip_blocks(self):
    gate = gates.check_holdout(Interval(-10, -12, -8), Interval(3, 1, 5))
    self.assertFalse(gate.passed)
    self.assertTrue(gate.blocking)

  def test_holdout_shrinkage_warns(self):
    gate = gates.check_holdout(Interval(-10, -12, -8), Interval(-2, -4, -0.5))
    self.assertFalse(gate.passed)
    self.assertFalse(gate.blocking)

  def test_amdahl_blocks_an_impossible_claim(self):
    self.assertFalse(gates.check_amdahl(2.0, 1.0).passed)
    self.assertTrue(gates.check_amdahl(2.0, 5.2).passed)

  def test_power_distinguishes_no_effect_from_no_power(self):
    underpowered = gates.check_underpowered(None, 2.4, 3, care_about_pct=3.0)
    self.assertFalse(underpowered.passed)
    self.assertIn("cannot resolve", underpowered.detail)
    powered = gates.check_underpowered(None, 0.05, 6, care_about_pct=3.0)
    self.assertTrue(powered.passed)


if __name__ == "__main__":
  unittest.main()
