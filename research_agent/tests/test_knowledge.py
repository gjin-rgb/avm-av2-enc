"""Code map, corpus ingestion and profile attribution."""

import os
import tempfile
import unittest

from av2ra.knowledge import codemap as codemap_mod
from av2ra.knowledge import corpus as corpus_mod
from av2ra.knowledge import lessons, profiles, retrieval

AVM = "/home/user/avm"
PRIOR = "/tmp/claude-0/-home-user-avm/b9c652ff-af93-5fcc-912c-9ef4312a7d5c/scratchpad/avm-patches"


class CodeMapTest(unittest.TestCase):

  def setUp(self):
    if not os.path.isdir(os.path.join(AVM, "av2", "encoder")):
      self.skipTest("AVM tree not available")
    self.map = codemap_mod.build(AVM)

  def test_indexes_real_functions(self):
    self.assertGreater(len(self.map.functions), 2000)
    for name in ("av2_rd_pick_partition", "av2_trellis_quant", "search_tx_type"):
      self.assertTrue(self.map.exists(name), name)

  def test_rejects_a_plausible_but_absent_symbol(self):
    """The failure this exists to prevent: an idea grounded in a name that is not there."""
    self.assertFalse(self.map.exists("av2_prune_intra_modes_fast"))
    found, missing = self.map.resolve(["av2_trellis_quant", "search_txk_type"])
    self.assertEqual(found, ["av2_trellis_quant"])
    self.assertEqual(missing, ["search_txk_type"])
    self.assertIn("search_tx_type", self.map.suggest("search_txk_type"))

  def test_round_trips_through_disk(self):
    path = os.path.join(tempfile.mkdtemp(), "codemap.json")
    self.map.save(path)
    reloaded = codemap_mod.CodeMap.load(path)
    self.assertEqual(len(reloaded.functions), len(self.map.functions))

  def test_speed_features_are_indexed_with_their_preset_ladder(self):
    self.assertGreater(len(self.map.speed_features), 50)
    varying = [f for f in self.map.speed_features.values() if f.varies_by_preset]
    self.assertGreater(len(varying), 10)


class CorpusTest(unittest.TestCase):

  def setUp(self):
    if not os.path.isdir(PRIOR):
      self.skipTest("prior research corpus not available")
    self.corpus = corpus_mod.ingest(PRIOR)

  def test_reads_real_results_not_keywords(self):
    self.assertGreater(len(self.corpus.attempts), 30)
    by_id = {a.id: a for a in self.corpus.attempts}
    self.assertIn("i09b", by_id)
    self.assertAlmostEqual(by_id["i09b"].a1_ratio, 26.7, delta=0.1)
    self.assertEqual(by_id["i09b"].status, "ADOPT-FINAL")

  def test_negative_speedups_keep_their_sign(self):
    by_id = {a.id: a for a in self.corpus.attempts}
    self.assertLess(by_id["i08"].a1_speedup, 0)
    self.assertLess(by_id["i04"].a1_speedup, 0)

  def test_structural_overlap_finds_prior_work_on_the_same_functions(self):
    overlapping = self.corpus.touching(["search_tx_type"], ["av2/encoder/tx_search.c"])
    self.assertTrue(overlapping)

  def test_hit_rate_matches_the_recorded_history(self):
    hit = self.corpus.hit_rate()
    self.assertGreater(hit["resolved"], 10)
    self.assertLess(hit["rate"], 0.3)

  def test_retractions_are_preserved(self):
    self.assertTrue(any(l.retracted for l in self.corpus.lessons))


class ProfileTest(unittest.TestCase):

  def test_parses_callgrind_and_attributes_kernels_to_their_algorithm(self):
    path = os.path.join(PRIOR, "Claude", "PROFILE_5d628d8_cpu4.txt")
    if not os.path.exists(path) or not os.path.isdir(AVM):
      self.skipTest("profile or AVM tree not available")
    with open(path, encoding="utf-8") as handle:
      profile = profiles.parse(handle.read(), preset=4)
    self.assertEqual(profile.source, "callgrind")
    self.assertTrue(profile.deterministic)
    profile = profiles.attribute(profile, codemap_mod.build(AVM))
    shares = profile.by_subsystem()
    # The corpus's headline finding: quantisation dominates.
    self.assertGreater(shares.get("quantization", 0.0), 25.0)
    self.assertGreater(profile.share_of(["av2_trellis_quant"]), 9.0)

  def test_perf_report_is_flagged_as_sampled(self):
    text = "  9.95%  avmenc  avmenc  [.] av2_trellis_quant\n  3.00%  avmenc  avmenc  [.] search_tx_type\n"
    profile = profiles.parse(text)
    self.assertEqual(profile.source, "perf")
    self.assertFalse(profile.deterministic)
    self.assertTrue(profile.notes)


class LessonsTest(unittest.TestCase):

  def test_rules_are_mostly_enforced_by_code(self):
    enforced = [r for r in lessons.RULES if r.enforced_by]
    self.assertGreater(len(enforced), len(lessons.RULES) // 2)

  def test_prompt_block_is_stable(self):
    self.assertEqual(
        lessons.prompt_block("measurement"), lessons.prompt_block("measurement")
    )


class RetrievalTest(unittest.TestCase):

  def test_frozen_prefix_is_identical_across_calls(self):
    if not os.path.isdir(AVM):
      self.skipTest("AVM tree not available")
    code = codemap_mod.build(AVM)
    first = retrieval.build_pack(prompt="a", codemap=code)
    second = retrieval.build_pack(prompt="b", codemap=code)
    self.assertEqual(first.fingerprint, second.fingerprint)
    self.assertNotEqual(first.prompt, second.prompt)


if __name__ == "__main__":
  unittest.main()
