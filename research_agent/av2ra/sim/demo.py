"""A complete, self-contained demonstration run.

``av2ra demo`` builds a miniature encoder repository, wires the real loop to a
simulated encoder and a simulated cluster, and runs the full research cycle to
completion in a few seconds -- writing the same registry, ledger, reports and
dashboard a real run would.

It exists for two reasons. Someone evaluating this system should be able to see
what it produces before spending a day building an encoder and syncing test
clips. And an operator debugging a real deployment needs a known-good reference:
if the demo works and the real run does not, the problem is in the environment,
not in the agent.

Nothing here is fake in the sense of pre-recorded. The planner, the tier ladder,
the integrity gates, the statistics and the registry are the production ones;
only the compiler, the encoder and the cluster are substituted. The numbers
describe a synthetic encoder and are labelled as such everywhere they appear.
"""

from __future__ import annotations

import os
import textwrap

from ..agent.analyst import Analyst
from ..agent.ideation import IdeationInputs, Ideator
from ..agent.implementer import Implementer
from ..agent.lenses import LensStats
from ..agent.llm import LLMClient, OfflineClient
from ..agent.loop import LoopConfig, ResearchLoop
from ..agent.planner import Governor, Planner
from ..buildkit.builder import BuildResult, Builder
from ..buildkit.worktree import WorktreeManager
from ..core.ledger import Ledger
from ..core.registry import Registry
from ..ctc.sim import SimCtcBackend
from ..knowledge import codemap as codemap_mod
from ..measure.encode import ClipSpec
from ..measure.screen import ClipSet
from ..sim.effects import make_truth
from ..sim.encoder import SimulatedEncoder
from ..util import proc
from ..util.io import ensure_dir

MINI_SOURCES = {
    "av2/encoder/tx_search.c": textwrap.dedent(
        """\
        /* Transform search (miniature stand-in for the real encoder). */
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
    ),
    "av2/encoder/trellis_quant.c": textwrap.dedent(
        """\
        /* Trellis quantisation (miniature stand-in). */
        #include "av2/encoder/trellis_quant.h"

        int av2_trellis_quant(const AV2_COMP *cpi, MACROBLOCK *x, int plane) {
          int states = 8;
          for (int i = 0; i < states; ++i) av2_decide_states(x, i);
          return states;
        }
        """
    ),
    "av2/encoder/partition_search.c": textwrap.dedent(
        """\
        /* Partition search (miniature stand-in). */
        #include "av2/encoder/partition_search.h"

        void av2_rd_pick_partition(AV2_COMP *cpi, MACROBLOCK *x, BLOCK_SIZE bsize) {
          evaluate_square(cpi, x, bsize);
          evaluate_rect(cpi, x, bsize);
          evaluate_ext(cpi, x, bsize);
        }
        """
    ),
    "av2/encoder/tx_search.h": "#pragma once\n",
    "av2/encoder/trellis_quant.h": "#pragma once\n",
    "av2/encoder/partition_search.h": "#pragma once\n",
    "CMakeLists.txt": "project(mini_avm)\n",
}

DEMO_HYPOTHESES = [
    {
        "title": "Gate the transform-partition search on the stationarity margin",
        "subsystem": "transform", "mechanism": "approx",
        "statement": (
            "Skip the transform-partition search when the stationarity margin is "
            "small, because the split almost never wins there."
        ),
        "rationale": (
            "The margin already measures how close the unsplit and split costs "
            "are; below a threshold the search cannot change the outcome."
        ),
        "target_functions": ["av2_tx_partition_search"], "target_files": [],
        "target_presets": [2], "expected_speedup_pct": 6.0,
        "expected_bdrate_pct": 0.12, "amdahl_ceiling_pct": 20.0, "confidence": 0.55,
        "risk_notes": ["may not fire on small blocks"],
        "kill_criteria": [
            "BD-rate above 0.4% retires the idea rather than prompting another "
            "margin value: the curve was already bracketed at margin 2"
        ],
        "parameters": [
            {"define": "TX_PART_STATIONARITY_MARGIN", "default": "2", "sweep": ["1", "2", "4"]}
        ],
        "why_not_already_done": "prior work stopped at margin 2 without bracketing the other side",
    },
    {
        "title": "Reduce trellis states during the ranking pass only",
        "subsystem": "quantization", "mechanism": "approx",
        "statement": (
            "Run the trellis with fewer states while ranking candidates, at full "
            "strength for the final decision."
        ),
        "rationale": (
            "Quantisation dominates the profile, and the ranking pass only needs "
            "an ordering, not an exact cost."
        ),
        "target_functions": ["av2_trellis_quant"], "target_files": [],
        "target_presets": [2], "expected_speedup_pct": 9.0,
        "expected_bdrate_pct": 0.25, "amdahl_ceiling_pct": 37.0, "confidence": 0.45,
        "risk_notes": ["a ranking-pass fidelity cut is more expensive at faster presets"],
        "kill_criteria": ["a BD-rate above 0.5% means the trellis is reordering, not refining"],
        "parameters": [{"define": "TRELLIS_RANK_STATES", "default": "4", "sweep": ["2", "4"]}],
        "why_not_already_done": "the corpus attacked call-site policy, never the state count",
    },
]

DEMO_EDITS = [
    {
        "summary": "gate the transform-partition search on the margin",
        "edits": [{
            "file": "av2/encoder/tx_search.c",
            "search": "  const int margin = compute_partition_margin(x);\n  (void)margin;\n",
            "replace": (
                "  const int margin = compute_partition_margin(x);\n"
                "  if (margin < TX_PART_STATIONARITY_MARGIN) return;\n"
            ),
            "why": "skip a search the margin says cannot change the outcome",
        }],
        "defines": [{"name": "TX_PART_STATIONARITY_MARGIN", "default": "2"}],
        "activation_note": "fires on blocks whose margin is under the threshold",
    },
    {
        "summary": "reduce trellis states in the ranking pass",
        "edits": [{
            "file": "av2/encoder/trellis_quant.c",
            "search": "  int states = 8;\n",
            "replace": "  int states = TRELLIS_RANK_STATES;\n",
            "why": "the ranking pass needs an ordering, not an exact cost",
        }],
        "defines": [{"name": "TRELLIS_RANK_STATES", "default": "4"}],
        "activation_note": "changes the state count on every trellis call",
    },
]


def make_mini_repo(root: str) -> str:
  ensure_dir(root)
  proc.run(["git", "init", "-q", "-b", "av2-enc", root], check=True)
  for relative, body in MINI_SOURCES.items():
    path = os.path.join(root, relative)
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
      handle.write(body)
  proc.run(["git", "add", "-A"], cwd=root, check=True)
  proc.run(
      ["git", "-c", "user.name=av2ra", "-c", "user.email=av2ra@localhost",
       "commit", "-qm", "miniature encoder for the demo"],
      cwd=root, check=True,
  )
  return root


class InstantBuilder(Builder):
  """Stands in for cmake+ninja. Fails only when the patch is deliberately broken."""

  def build(self, spec, *, allow_cache: bool = True) -> BuildResult:
    ensure_dir(spec.build_dir)
    for name in ("avmenc", "avmdec"):
      with open(os.path.join(spec.build_dir, name), "w", encoding="utf-8") as handle:
        handle.write("#!/bin/true\n")
    for relative in MINI_SOURCES:
      path = os.path.join(spec.source_dir, relative)
      if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
          if "BUILD_BREAK" in handle.read():
            return BuildResult(
                ok=False, build_dir=spec.build_dir,
                config_fingerprint=spec.config_fingerprint(),
                fingerprint=spec.fingerprint(),
                error="mini-encoder build failed: BUILD_BREAK marker present",
            )
    return BuildResult(
        ok=True, build_dir=spec.build_dir,
        encoder=os.path.join(spec.build_dir, "avmenc"),
        decoder=os.path.join(spec.build_dir, "avmdec"),
        fingerprint=spec.fingerprint(), config_fingerprint=spec.config_fingerprint(),
    )


def _scripted_client() -> LLMClient:
  from ..agent.llm import LLMResponse

  state = {"index": 0}

  class _Client(LLMClient):
    name = "scripted"

    def complete(self, *, system, prompt, schema=None, max_tokens=16000,
                 effort="high", cache_system=True):
      import json

      properties = set((schema or {}).get("properties") or {})
      if "edits" in properties:
        payload = DEMO_EDITS[min(state["index"], len(DEMO_EDITS) - 1)]
      elif "move" in properties:
        payload = {
            "move": "escalate",
            "reasoning": "the local tiers are exhausted and only CTC can settle the quality question",
            "lesson": "",
            "confidence": 0.6,
        }
      else:
        payload = DEMO_HYPOTHESES[min(state["index"], len(DEMO_HYPOTHESES) - 1)]
        state["index"] = min(state["index"] + 1, len(DEMO_HYPOTHESES) - 1)
      return LLMResponse(text=json.dumps(payload), data=payload, model="scripted")

  return _Client()


def build_demo_loop(workspace: str, *, seed: str = "demo") -> ResearchLoop:
  workspace = os.path.abspath(os.path.expanduser(workspace))
  ensure_dir(workspace)
  repo = make_mini_repo(os.path.join(workspace, "mini-avm"))

  registry = Registry(os.path.join(workspace, "registry.sqlite"))
  ledger = Ledger(os.path.join(workspace, "ledger"))
  worktrees = WorktreeManager(repo, os.path.join(workspace, "worktrees"), min_free_gib=0.0)
  builder = InstantBuilder(os.path.join(workspace, "buildcache"))
  codemap = codemap_mod.build(repo)

  screen = ClipSet(
      name="demo-screen", role="screen",
      clips=[
          ClipSpec(name=f"screen_clip{i}", path="/dev/null", width=1920, height=1080,
                   bit_depth=10, file_class="A2")
          for i in range(4)
      ],
      qps=[160, 185, 210], frames=17,
  )
  holdout = ClipSet(
      name="demo-holdout", role="holdout",
      clips=[
          ClipSpec(name=f"holdout_clip{i}", path="/dev/null", width=1920, height=1080,
                   bit_depth=10, file_class="A2")
          for i in range(3)
      ],
      qps=[160, 210], frames=17,
  )
  roles = {c.name: "screen" for c in screen.clips}
  roles.update({c.name: "holdout" for c in holdout.clips})

  # Each experiment gets its own ground truth, so the demo shows the system
  # reaching different conclusions rather than the same one twice.
  truths = {
      "N0001": make_truth(f"{seed}-1", profile_share=0.35, aggressiveness=0.35, difficulty=0.25),
      "N0002": make_truth(f"{seed}-2", profile_share=0.40, aggressiveness=0.70, difficulty=1.0),
  }
  default_truth = truths["N0001"]
  encoder = SimulatedEncoder(truth=default_truth, class_of_clip=roles)
  ctc = SimCtcBackend()

  client = _scripted_client()
  stats = LensStats()
  loop = ResearchLoop(
      config=LoopConfig(
          workspace=workspace, anchor_ref="av2-enc", preset=2, reps=2, workers=2,
          cpus_per_worker=1, node_id="demo", conformance_sample=0,
          acceptance_mode="point", run_null_arm_every=3,
      ),
      registry=registry, ledger=ledger,
      planner=Planner(registry, Governor(ctc_min_arms_per_round=1)),
      ideator=Ideator(client, stats=stats),
      implementer=Implementer(client, worktrees, builder),
      analyst=Analyst(client), worktrees=worktrees, builder=builder, ctc=ctc,
      inputs=IdeationInputs(codemap=codemap, base_sha=""),
      screen_set=screen, holdout_set=holdout,
      lens_stats=stats, encode_runner=encoder.run,
  )
  loop.demo_truths = truths
  loop.demo_truth = default_truth
  loop.demo_encoder = encoder
  loop.demo_ctc = ctc
  return loop


def run_demo(workspace: str, *, ticks: int = 14, printer=print) -> ResearchLoop:
  loop = build_demo_loop(workspace)
  printer(
      "Running the full research cycle against a simulated encoder and cluster.\n"
      "Every component except the compiler, the encoder and the cluster is the\n"
      "production one. The numbers describe a synthetic encoder.\n"
  )
  for index in range(ticks):
    # Switch the simulated encoder to whichever experiment is being measured.
    for experiment in loop.registry.query(limit=10):
      if experiment.is_alive and experiment.id in loop.demo_truths:
        loop.demo_encoder.truth = loop.demo_truths[experiment.id]
    result = loop.tick()
    printer(f"[{index + 1:2d}] {result}")
    if result.action == "escalate":
      for experiment_id in loop.registry.all_ids():
        loop.demo_ctc.register(
            experiment_id, loop.demo_truths.get(experiment_id, loop.demo_truth)
        )
    if result.action == "halt":
      break
  return loop
