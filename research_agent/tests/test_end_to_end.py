"""The whole loop, against a simulated encoder and cluster.

These are the tests that say the system works, because they exercise the real
planner, tiers, gates, registry and ledger -- only the compiler, the encoder and
the cluster are substituted. Each one asserts on a *conclusion*, not on a code
path: what the system decided, and whether it decided it for the right reason.
"""

import os
import tempfile
import unittest

from av2ra.agent.planner import Governor
from av2ra.core.models import ExperimentStatus, Verdict
from av2ra.sim.effects import PatchTruth, make_truth

try:
  from .harness import build_loop
except ImportError:  # discovered as a flat module rather than a package
  from harness import build_loop


def drive(loop, ticks=8, stop_after=None):
  history = []
  for _ in range(ticks):
    result = loop.tick()
    history.append(result)
    if result.action == "escalate":
      for experiment_id in loop.registry.all_ids():
        loop._sim_ctc.register(experiment_id, loop._sim_truth)
    if stop_after and result.action == stop_after:
      break
    if result.action in ("halt",):
      break
  return history


class HappyPathTest(unittest.TestCase):

  def test_a_good_patch_runs_the_whole_ladder_and_clears_the_bar(self):
    loop = build_loop(tempfile.mkdtemp())
    history = drive(loop)
    actions = [h.action for h in history]
    for expected in ("propose", "implement", "screen", "escalate", "collect"):
      self.assertIn(expected, actions, actions)

    experiment = loop.registry.get("N0001")
    self.assertEqual(experiment.status, ExperimentStatus.COMPLETE)
    self.assertIn(experiment.verdict, (Verdict.CTC_PASS, Verdict.PROMOTE))

    tiers = [t.tier.value for t in experiment.tiers]
    self.assertEqual(
        tiers, ["T0_build", "T1_exactness", "T2_complexity", "T3_screen",
                "T4_heldout", "T5_ctc"],
    )
    self.assertTrue(all(t.passed for t in experiment.tiers))

    # Provenance: every measurement is scoped to a base and a metric definition.
    rows = loop.registry.measurements("N0001")
    self.assertTrue(rows)
    for row in rows:
      self.assertTrue(row["base_sha"])
      self.assertTrue(row["metric_def"])

    # The decision log is the durable memory.
    self.assertTrue(loop.ledger.entries())

  def test_the_report_carries_the_conditions_and_the_disclaimer(self):
    from av2ra.report import experiment as report_mod

    loop = build_loop(tempfile.mkdtemp())
    drive(loop)
    text = report_mod.render(loop.registry.get("N0001"))
    self.assertIn("Anchor:", text)
    self.assertIn("Evidence ladder", text)
    self.assertIn("rank and a tripwire", text)
    self.assertIn("Prediction made before measuring", text)


class FailurePathTest(unittest.TestCase):

  def test_a_patch_that_does_not_compile_fails_at_t0(self):
    loop = build_loop(
        tempfile.mkdtemp(),
        edits={
            "summary": "break the build",
            "edits": [{
                "file": "av2/encoder/tx_search.c",
                "search": "  int64_t best_rd = INT64_MAX;\n",
                "replace": "  int64_t best_rd = BUILD_BREAK;\n",
            }],
            "defines": [], "activation_note": "",
        },
    )
    drive(loop, ticks=3)
    experiment = loop.registry.get("N0001")
    self.assertEqual(experiment.verdict, Verdict.BUILD_FAIL)
    self.assertTrue(experiment.lessons)

  def test_benchmark_fitting_is_blocked_before_it_is_ever_measured(self):
    loop = build_loop(
        tempfile.mkdtemp(),
        edits={
            "summary": "special-case the test resolution",
            "edits": [{
                "file": "av2/encoder/tx_search.c",
                "search": "  int64_t best_rd = INT64_MAX;\n",
                "replace": "  if (cm->width == 1920) return 0;\n  int64_t best_rd = INT64_MAX;\n",
            }],
            "defines": [], "activation_note": "",
        },
    )
    drive(loop, ticks=3)
    experiment = loop.registry.get("N0001")
    self.assertEqual(experiment.verdict, Verdict.INTEGRITY_FAIL)
    kinds = {v["kind"] for v in experiment.integrity["policy_violations"]}
    self.assertIn("hard-coded test resolution", kinds)

  def test_an_inert_patch_is_a_failed_experiment_not_a_safe_one(self):
    loop = build_loop(tempfile.mkdtemp(), truth=PatchTruth(patch_id="inert", inert=True))
    drive(loop, ticks=4)
    experiment = loop.registry.get("N0001")
    self.assertEqual(experiment.verdict, Verdict.NO_EFFECT)
    exactness = experiment.tier_result_by_name("T1_exactness")
    self.assertIn("never fired", exactness.reason)
    self.assertTrue(any("inert" in lesson for lesson in experiment.lessons))

  def test_a_reuse_patch_that_changes_the_bitstream_is_called_a_bug(self):
    loop = build_loop(
        tempfile.mkdtemp(),
        hypothesis={
            "title": "Memoise the transform-type search",
            "subsystem": "transform", "mechanism": "reuse",
            "statement": "Cache the search result.", "rationale": "It repeats.",
            "target_functions": ["search_tx_type"], "target_files": [],
            "target_presets": [2], "expected_speedup_pct": 2.0,
            "expected_bdrate_pct": 0.0, "amdahl_ceiling_pct": 20.0,
            "confidence": 0.5, "risk_notes": [],
            "kill_criteria": ["any bitstream change is a bug"],
            "parameters": [], "why_not_already_done": "n/a",
        },
    )
    drive(loop, ticks=4)
    experiment = loop.registry.get("N0001")
    self.assertEqual(experiment.verdict, Verdict.DIVERGES)
    self.assertTrue(
        any("equivalence argument was wrong" in lesson for lesson in experiment.lessons)
    )


class GovernanceTest(unittest.TestCase):

  def test_a_cluster_round_requires_a_holdout_measurement(self):
    from av2ra.agent.planner import build_escalation_case

    loop = build_loop(tempfile.mkdtemp())
    loop.config.run_holdout = False
    drive(loop, ticks=3, stop_after="screen")
    experiment = loop.registry.get("N0001")
    case = build_escalation_case(experiment, require_holdout=True)
    self.assertFalse(case.admissible)
    self.assertTrue(any("held-out" in blocker for blocker in case.blockers))

  def test_a_second_identical_proposal_is_refused_as_a_duplicate(self):
    loop = build_loop(tempfile.mkdtemp())
    drive(loop, ticks=8)
    before = len(loop.registry.all_ids())
    result = loop.tick()
    self.assertEqual(result.action, "propose")
    self.assertFalse(result.advanced)
    self.assertEqual(len(loop.registry.all_ids()), before)

  def test_anchor_drift_halts_the_loop(self):
    loop = build_loop(tempfile.mkdtemp())
    result = loop.tick(base_drifted=True)
    self.assertEqual(result.action, "halt")
    self.assertIn("not automatically valid", result.detail)

  def test_the_ctc_daily_budget_is_enforced(self):
    loop = build_loop(
        tempfile.mkdtemp(),
        governor=Governor(ctc_min_arms_per_round=1, ctc_slots_per_day=0.0),
    )
    history = drive(loop, ticks=5)
    self.assertTrue(any("budget for the day is spent" in h.detail for h in history))


class DeterminismTest(unittest.TestCase):

  def test_two_runs_of_the_same_seed_reach_the_same_verdict(self):
    verdicts = []
    for _ in range(2):
      loop = build_loop(tempfile.mkdtemp(), truth=make_truth("fixed", difficulty=0.3))
      drive(loop)
      verdicts.append(loop.registry.get("N0001").verdict)
    self.assertEqual(verdicts[0], verdicts[1])


if __name__ == "__main__":
  unittest.main()
