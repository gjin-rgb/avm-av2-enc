"""A miniature encoder repository and a wired-up loop, for end-to-end tests.

The point of this harness is to exercise *the real code* -- the real planner,
the real tiers, the real gates, the real registry -- against a fake encoder and
a fake cluster, so a full research cycle runs in under a second and a test can
assert on what the system concluded.

Only three things are substituted: the source tree (a handful of small C files
with realistic paths), the compiler (a stub that succeeds unless the patch says
otherwise), and the encoder itself (:class:`av2ra.sim.encoder.SimulatedEncoder`).
Everything between them is production code.
"""

from __future__ import annotations

import os
import textwrap

from av2ra.agent.analyst import Analyst
from av2ra.agent.ideation import IdeationInputs, Ideator
from av2ra.agent.implementer import Implementer
from av2ra.agent.lenses import LensStats
from av2ra.agent.llm import OfflineClient
from av2ra.agent.loop import LoopConfig, ResearchLoop
from av2ra.agent.planner import Governor, Planner
from av2ra.buildkit.builder import BuildResult, Builder
from av2ra.buildkit.worktree import WorktreeManager
from av2ra.core.ledger import Ledger
from av2ra.core.registry import Registry
from av2ra.ctc.sim import SimCtcBackend
from av2ra.knowledge import codemap as codemap_mod
from av2ra.measure.encode import ClipSpec
from av2ra.measure.screen import ClipSet
from av2ra.sim.effects import PatchTruth, make_truth
from av2ra.sim.encoder import SimulatedEncoder
from av2ra.util import proc

TX_SEARCH_C = textwrap.dedent(
    """\
    /* Transform search. */
    #include "av2/encoder/tx_search.h"

    #define TX_PART_STATIONARITY_MARGIN 4

    static int64_t search_tx_type(const AV2_COMP *cpi, MACROBLOCK *x, int plane,
                                  int block, TX_SIZE tx_size) {
      int64_t best_rd = INT64_MAX;
      for (int type = 0; type < TX_TYPES; ++type) {
        const int64_t rd = evaluate_tx_type(cpi, x, plane, block, tx_size, type);
        if (rd < best_rd) best_rd = rd;
      }
      return best_rd;
    }

    void av2_tx_partition_search(const AV2_COMP *cpi, MACROBLOCK *x) {
      const int margin = compute_partition_margin(x);
      (void)margin;
      search_tx_type(cpi, x, 0, 0, TX_8X8);
    }
    """
)

TRELLIS_C = textwrap.dedent(
    """\
    /* Trellis quantisation. */
    #include "av2/encoder/trellis_quant.h"

    int av2_trellis_quant(const AV2_COMP *cpi, MACROBLOCK *x, int plane) {
      int states = 8;
      for (int i = 0; i < states; ++i) {
        av2_decide_states(x, i);
      }
      return states;
    }
    """
)

PARTITION_C = textwrap.dedent(
    """\
    /* Partition search. */
    #include "av2/encoder/partition_search.h"

    void av2_rd_pick_partition(AV2_COMP *cpi, MACROBLOCK *x, BLOCK_SIZE bsize) {
      evaluate_square(cpi, x, bsize);
      evaluate_rect(cpi, x, bsize);
      evaluate_ext(cpi, x, bsize);
    }
    """
)

FILES = {
    "av2/encoder/tx_search.c": TX_SEARCH_C,
    "av2/encoder/trellis_quant.c": TRELLIS_C,
    "av2/encoder/partition_search.c": PARTITION_C,
    "av2/encoder/tx_search.h": "#pragma once\n",
    "av2/encoder/trellis_quant.h": "#pragma once\n",
    "av2/encoder/partition_search.h": "#pragma once\n",
    "CMakeLists.txt": "project(mini_avm)\n",
}


def make_repo(root: str) -> str:
  """A tiny git repository shaped like the encoder, on a branch named av2-enc."""
  os.makedirs(root, exist_ok=True)
  proc.run(["git", "init", "-q", "-b", "av2-enc", root], check=True)
  for relative, body in FILES.items():
    path = os.path.join(root, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
      handle.write(body)
  proc.run(["git", "add", "-A"], cwd=root, check=True)
  proc.run(
      ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
      cwd=root, check=True,
  )
  return root


class StubBuilder(Builder):
  """Succeeds instantly, unless the patch contains a deliberate build-break marker."""

  def __init__(self, cache_root: str):
    super().__init__(cache_root)
    self.builds = 0

  def build(self, spec, *, allow_cache: bool = True) -> BuildResult:
    self.builds += 1
    encoder = os.path.join(spec.build_dir, "avmenc")
    os.makedirs(spec.build_dir, exist_ok=True)
    for name in ("avmenc", "avmdec"):
      with open(os.path.join(spec.build_dir, name), "w", encoding="utf-8") as handle:
        handle.write("#!/bin/true\n")
    broken = False
    for relative in FILES:
      path = os.path.join(spec.source_dir, relative)
      if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
          if "BUILD_BREAK" in handle.read():
            broken = True
    if broken:
      return BuildResult(
          ok=False, build_dir=spec.build_dir,
          config_fingerprint=spec.config_fingerprint(), fingerprint=spec.fingerprint(),
          error="../av2/encoder/tx_search.c:9:3: error: use of undeclared identifier 'BUILD_BREAK'",
      )
    return BuildResult(
        ok=True, build_dir=spec.build_dir, encoder=encoder,
        decoder=os.path.join(spec.build_dir, "avmdec"),
        fingerprint=spec.fingerprint(), config_fingerprint=spec.config_fingerprint(),
    )


def clip_sets(n_screen: int = 4, n_holdout: int = 3) -> tuple[ClipSet, ClipSet]:
  screen = ClipSet(
      name="screen-a2", role="screen",
      clips=[
          ClipSpec(name=f"screen_clip{i}", path="/dev/null", width=1920, height=1080,
                   bit_depth=10, file_class="A2")
          for i in range(n_screen)
      ],
      qps=[160, 185, 210], frames=17,
  )
  holdout = ClipSet(
      name="holdout-a2", role="holdout",
      clips=[
          ClipSpec(name=f"holdout_clip{i}", path="/dev/null", width=1920, height=1080,
                   bit_depth=10, file_class="A2")
          for i in range(n_holdout)
      ],
      qps=[160, 210], frames=17,
  )
  return (screen, holdout)


def scripted_responder(hypothesis: dict, edits: dict, analysis: dict | None = None):
  """An offline 'model' that answers each stage with a fixed, valid response."""

  def responder(system: str, prompt: str, schema):
    properties = set((schema or {}).get("properties") or {})
    if "edits" in properties:
      return edits
    if "move" in properties:
      return analysis or {
          "move": "escalate", "reasoning": "local tiers exhausted",
          "lesson": "", "confidence": 0.6,
      }
    return hypothesis

  return responder


def build_loop(
    tmp: str,
    *,
    truth: PatchTruth | None = None,
    hypothesis: dict | None = None,
    edits: dict | None = None,
    analysis: dict | None = None,
    governor: Governor | None = None,
    reps: int = 2,
) -> ResearchLoop:
  repo = make_repo(os.path.join(tmp, "avm"))
  workspace = os.path.join(tmp, "ws")
  os.makedirs(workspace, exist_ok=True)

  registry = Registry(os.path.join(workspace, "registry.sqlite"))
  ledger = Ledger(os.path.join(workspace, "ledger"))
  worktrees = WorktreeManager(repo, os.path.join(workspace, "worktrees"), min_free_gib=0.0)
  builder = StubBuilder(os.path.join(workspace, "buildcache"))
  codemap = codemap_mod.build(repo)

  hypothesis = hypothesis or {
      "title": "Gate transform-partition search on stationarity margin",
      "subsystem": "transform", "mechanism": "approx",
      "statement": "Skip the transform-partition search when the margin is small.",
      "rationale": "A small margin means the split rarely wins.",
      "target_functions": ["av2_tx_partition_search"], "target_files": [],
      "target_presets": [2], "expected_speedup_pct": 6.0, "expected_bdrate_pct": 0.12,
      "amdahl_ceiling_pct": 20.0, "confidence": 0.55,
      "risk_notes": ["may not fire on small blocks"],
      "kill_criteria": ["BD-rate above 0.4% retires it rather than tuning the margin"],
      "parameters": [{"define": "TX_PART_STATIONARITY_MARGIN", "default": "2", "sweep": ["1", "2", "4"]}],
      "why_not_already_done": "prior work stopped at margin 2 without bracketing",
  }
  edits = edits or {
      "summary": "gate the partition search on the margin",
      "edits": [
          {
              "file": "av2/encoder/tx_search.c",
              "search": "  const int margin = compute_partition_margin(x);\n  (void)margin;\n",
              "replace": (
                  "  const int margin = compute_partition_margin(x);\n"
                  "  if (margin < TX_PART_STATIONARITY_MARGIN) return;\n"
              ),
              "why": "skip the search when the margin cannot pay for it",
          }
      ],
      "defines": [{"name": "TX_PART_STATIONARITY_MARGIN", "default": "2"}],
      "activation_note": "fires on blocks whose margin is under the threshold",
  }
  client = OfflineClient(responder=scripted_responder(hypothesis, edits, analysis))

  truth = truth or make_truth("e2e", profile_share=0.35, aggressiveness=0.4, difficulty=0.25)
  screen_set, holdout_set = clip_sets()
  class_of_clip = {c.name: "screen" for c in screen_set.clips}
  class_of_clip.update({c.name: "holdout" for c in holdout_set.clips})
  encoder = SimulatedEncoder(truth=truth, class_of_clip=class_of_clip)

  ctc = SimCtcBackend()
  ctc.register("__default__", truth)

  config = LoopConfig(
      workspace=workspace, anchor_ref="av2-enc", preset=2, reps=reps,
      workers=2, cpus_per_worker=1, node_id="test",
      conformance_sample=0, acceptance_mode="point",
  )
  inputs = IdeationInputs(codemap=codemap, corpus=None, base_sha="")
  loop = ResearchLoop(
      config=config, registry=registry, ledger=ledger,
      planner=Planner(registry, governor or Governor(ctc_min_arms_per_round=1)),
      ideator=Ideator(client, stats=LensStats()),
      implementer=Implementer(client, worktrees, builder),
      analyst=Analyst(client), worktrees=worktrees, builder=builder, ctc=ctc,
      inputs=inputs, screen_set=screen_set, holdout_set=holdout_set,
      encode_runner=encoder.run,
  )
  loop._sim_truth = truth          # test hook
  loop._sim_ctc = ctc
  return loop
