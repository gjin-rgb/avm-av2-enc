"""The methodology the prior research paid for, in machine-readable form.

Roughly fifty experiments, three CTC rounds and a 101-feature ablation study
produced fewer than ten results that cleared both class bars -- and a much
larger number of *process* findings, which is the more valuable output. Those
findings are written down here as structured rules rather than prose, for two
reasons:

* **Enforcement.** A rule with an ``enforced_by`` field is not advice; it names
  the module that makes it impossible to violate. Roughly half of these are
  enforced. The rest are injected into the ideation and analysis prompts, where
  they act as priors rather than gates.
* **Auditability.** When the agent kills an idea "because of L07", a human can
  read L07 and the evidence behind it, and disagree.

Each rule cites where it came from. Where the corpus later contradicted itself,
that is recorded too: one rule in the original work was derived from three arms
that never fired, carried for two rounds, and then retracted. A memory that
cannot represent its own retractions repeats them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rule:
  id: str
  title: str
  statement: str
  evidence: str
  applies_to: tuple[str, ...]      # ideation | implementation | measurement | analysis | planning
  enforced_by: str = ""            # module that makes violation impossible
  severity: str = "hard"           # hard | prior

  def prompt_line(self) -> str:
    marker = "ENFORCED" if self.enforced_by else "PRIOR"
    return f"[{self.id} / {marker}] {self.title}: {self.statement}"


RULES: tuple[Rule, ...] = (
    Rule(
        "L01", "An effect below the noise floor is not a small win",
        "If the confidence interval on an effect contains zero, report NO_EFFECT "
        "and say what the design could have resolved. Never rank, promote or "
        "reason about a sub-noise number.",
        "Six of ten round-1 speedups were smaller than the 3.47% minimum "
        "detectable effect of the design that produced them, and were written "
        "into a results table as wins.",
        ("measurement", "analysis"), "av2ra.measure.acceptance",
    ),
    Rule(
        "L02", "Local BD-rate ranks; it does not estimate",
        "A local screen may kill a catastrophic regression and may order "
        "variants. It may never be quoted as the patch's BD-rate, and a local "
        "sign must never be trusted.",
        "Local screens on 416x240/4-frame clips had the wrong sign against CTC "
        "on 3 of 10 patches and understated the true cost by 7-12x on a fourth; "
        "the ten-patch union read -0.63% locally and +2.50% on CTC.",
        ("measurement", "analysis"), "av2ra.measure.tiers",
    ),
    Rule(
        "L03", "Bit-exactness is a proof, and it replaces a CTC round",
        "A reuse or kernel patch must produce a byte-identical bitstream. If it "
        "does, quality risk is zero by proof and the patch is judged on speed "
        "alone. If it does not, that is a bug in the patch, not a trade-off.",
        "Three reuse patches were proven bit-exact locally in 25 minutes; the "
        "CTC round that was already running confirmed +0.00% on every metric.",
        ("measurement", "planning"), "av2ra.integrity.gates",
    ),
    Rule(
        "L04", "Bit-exactness is scoped to the configuration tested",
        "State the resolution, preset, frame count and QPs a bit-exactness proof "
        "covers. A cache-key bug on a path the test configuration never reaches "
        "is not caught by it.",
        "Three patches were proven bit-exact only at 416x240/cpu-used=3/QP "
        "110,185; the 4K re-run was owed and never done.",
        ("measurement",), "",
    ),
    Rule(
        "L05", "Assert the state you depend on",
        "Never assume a cleanup, revert or configuration step worked. Verify it, "
        "and fail loudly when it did not.",
        "A revert used a pathspec that did not exist; git rejected the whole "
        "command, the error was discarded, and three patches accumulated until "
        "the 'baseline' contained all of them.",
        ("implementation", "measurement"), "av2ra.buildkit.worktree",
    ),
    Rule(
        "L06", "A clean apply proves nothing after the base moves",
        "On any base change, re-run the cheap tiers before trusting a number. "
        "Report the intersection of upstream-changed files and patch-touched "
        "files -- that is where a clean apply means least.",
        "One upstream commit added 187 lines to a file two patches modify; all "
        "ten patches still reported APPLIES.",
        ("measurement", "planning"), "av2ra.core.basewatch",
    ),
    Rule(
        "L07", "Measure the measurement system before trusting a ratio",
        "Run an anchor-versus-anchor arm. Every acceptance ratio is a quotient "
        "whose denominator has an error bar; without a null arm that error bar "
        "is unknown.",
        "The same patch measured two commits apart gave ratio 6.6 and 26.9 with "
        "identical BD-rate. Six inert arms all read slower than the anchor, "
        "implying a systematic ~1.6-point bias. The null arm was requested four "
        "times and never run.",
        ("measurement",), "av2ra.integrity.gates",
    ),
    Rule(
        "L08", "A ratio with an unresolved denominator is not a ratio",
        "Do not divide by a BD-rate that rounds to zero or whose interval spans "
        "zero. Report the quadrant and say the ratio is undefined.",
        "Twelve of twenty-one reported 'passes' in a 101-feature study were "
        "divisions by a rounded 0.00%; the true count was nine.",
        ("analysis",), "av2ra.measure.acceptance",
    ),
    Rule(
        "L09", "Both classes pass independently, or the patch does not pass",
        "Never average 4K and 1080p results. Ten arms in the corpus were "
        "class-asymmetric, in both directions.",
        "One report declared three patches adopted on averaged ratios of 24.7, "
        "62.1 and 111.2 while their binding class scored 19.0, 29.3 and 31.0 "
        "against bars of 20, 35 and 35.",
        ("analysis",), "av2ra.measure.acceptance",
    ),
    Rule(
        "L10", "An inert patch is a failed experiment, not a safe one",
        "For an approximating patch, an identical bitstream means the code never "
        "fired. Find out why before measuring anything.",
        "Eight arms were inert. Three of them produced a rule about conditional "
        "gates that was carried for two rounds before being retracted, because "
        "the arms it rested on had tested nothing.",
        ("measurement", "analysis"), "av2ra.integrity.gates",
    ),
    Rule(
        "L11", "Measure the gate's overhead separately",
        "Build the heuristic with its gate forced never to fire. If that build "
        "is slower than the anchor, the idea is dead however good the pruning "
        "logic is.",
        "A conditional is evaluated on every block whether or not it fires; five "
        "gated arms delivered nothing while six assignment-only promotions "
        "passed.",
        ("measurement", "ideation"), "av2ra.integrity.gates",
    ),
    Rule(
        "L12", "Split a bundle before turning its threshold down",
        "When a bundled feature fails on ratio, find its cheap members. Cost and "
        "benefit are not proportionally distributed inside a feature group.",
        "Disabling a whole extended-partition family gave ratio 30.6 and failed; "
        "disabling only its uneven 4-way members kept 39% of the speed for 6% of "
        "the quality cost, ratio 207.7 -- the project's best result. Every "
        "threshold move instead slid along the trade-off.",
        ("ideation", "analysis"), "",
    ),
    Rule(
        "L13", "Use marginal ratios, not stories about which blocks are unsafe",
        "When a variant tightens a heuristic, compute (speed given up) / "
        "(quality recovered). A change improves the overall ratio only when that "
        "marginal ratio is below the ratio already achieved.",
        "Three successive variants were each built on a hypothesis about unsafe "
        "blocks; two of the three hypotheses were wrong, and the marginal ratios "
        "showed a 'variance floor' carried for four rounds was worth nothing at "
        "all (2.04% speed for 0.00% BD).",
        ("analysis", "ideation"), "av2ra.measure.acceptance",
    ),
    Rule(
        "L14", "Bracket a threshold curve; never take a single step past a peak",
        "Sweep a parameter with at least two arms on each side of the best known "
        "value, and expose it as a -D knob so the known-good point is "
        "reproducible without editing the patch.",
        "Margin 4 -> 23.6, margin 2 -> 26.7 (peak), margin 1 -> 14.7. The decay "
        "arrived at exactly the next step.",
        ("ideation", "planning"), "",
    ),
    Rule(
        "L15", "Per-sequence spread is a result, not a footnote",
        "Report the worst clip, not just the mean. Speed benefit is nearly "
        "uniform across content (CV ~12%) while quality cost varies sevenfold "
        "(CV ~89%), so one clip routinely carries the damage -- and that is a "
        "design signal that a content gate exists.",
        "Exempting one clip in eight took a failing 30.6 to a passing 37.2; on "
        "another arm the three worst clips carried 66% of the BD-rate for 35% of "
        "the speedup.",
        ("analysis", "ideation"), "av2ra.measure.acceptance",
    ),
    Rule(
        "L16", "Check whether upstream already did it",
        "Before authoring, diff the target against the current anchor and read "
        "the upstream log for the same mechanism.",
        "Four experiments died as no-ops because the idea had already landed "
        "upstream; three were discovered only after cluster time was spent.",
        ("ideation", "implementation"), "av2ra.core.basewatch",
    ),
    Rule(
        "L17", "Run the union first for go/no-go, then attribute",
        "One arm answers 'is there anything here at all'. Ten one-at-a-time arms "
        "cost ten times as much and cannot answer it.",
        "Speedups compound multiplicatively and BD-rate adds; backing the single "
        "large patch out of a ten-patch union showed the other nine were "
        "ballast.",
        ("planning",), "",
    ),
    Rule(
        "L18", "Every cluster round must answer a question nothing cheaper could",
        "Record the question and the cheaper evidence already exhausted. A patch "
        "whose speedup has never been resolved above the noise floor must not "
        "consume a slot.",
        "A round was spent on ten patches of which eight were already dead on "
        "evidence the project had.",
        ("planning",), "av2ra.ctc.contract",
    ),
    Rule(
        "L19", "Profile before theorising",
        "A feature-ablation map answers 'what is this feature worth', not 'where "
        "is the time'. Get the hotspot map first.",
        "Twenty-four patches were produced before anyone profiled; quantisation "
        "turned out to be ~47% of instructions retired, larger than partition, "
        "mode search and loop filters combined, and no patch had touched it. The "
        "profile cost 25 minutes and no cluster time.",
        ("ideation", "planning"), "av2ra.knowledge.profiles",
    ),
    Rule(
        "L20", "Apply the Amdahl gate before spending cluster time",
        "A kernel cannot save more of the encode than it occupies. Convert a "
        "kernel-level speedup into a whole-encode ceiling and check it against "
        "the claim.",
        "A 38.8% kernel reduction was correctly predicted to be worth ~2% "
        "whole-encode only if the kernel held >=5.2% of the profile; the CTC "
        "result was +1.20% on 4K.",
        ("ideation", "analysis"), "av2ra.integrity.gates",
    ),
    Rule(
        "L21", "A speed feature can be self-defeating",
        "Degrading prediction quality makes residuals larger, which makes "
        "downstream transform and partition search more expensive. Check the "
        "whole encode, not the stage you changed.",
        "Restoring smooth intra prediction was -1.08% BD-rate AND 1.25% faster; "
        "four ablation entries cost both quality and time.",
        ("ideation", "analysis"), "",
    ),
    Rule(
        "L22", "A consistency fix beats a new heuristic",
        "Where the codebase already declares a policy, look for call sites that "
        "do not honour it before inventing a new rule.",
        "One function declared skip_trellis from the dry-pass shortcut flag while "
        "another in the same file hard-coded it to 0; applying the existing "
        "policy at the second site was a clean win, and a nearby patch that "
        "invented a new tool-set rule failed.",
        ("ideation",), "",
    ),
    Rule(
        "L23", "Write the falsifier before the run",
        "State in advance what result would retire the idea rather than tune it.",
        "One arm's header said 'if BD-rate exceeds ~0.3% at shift 3 the premise "
        "is wrong and the patch should be retired, not tuned'; that pre-commitment "
        "cost one arm instead of a round of tuning.",
        ("ideation", "planning"), "",
    ),
    Rule(
        "L24", "Score your own predictions, in both directions",
        "Record the expected speedup and BD-rate before measuring, and grade "
        "them afterwards. Calibration is the only way ideation improves.",
        "A prediction scorecard across four arms found one right, one right in "
        "direction but off 5x in magnitude, and one right for the wrong reason.",
        ("ideation", "analysis"), "av2ra.agent.analyst",
    ),
    Rule(
        "L25", "Do not sum noisy near-zero estimates",
        "Three features measured at -0.00%, +0.15% and -0.31% were predicted to "
        "be free together and cost 1.9%. Summing estimates of different "
        "quantities compounds uncertainty.",
        "Recorded in the promotion-frontier round.",
        ("planning", "analysis"), "",
    ),
    Rule(
        "L26", "A guard the test set cannot reach is not dead code",
        "If a feature sits behind a resolution guard no CTC class exercises, the "
        "correct conclusion is 'CTC cannot measure this', not 'this does "
        "nothing'.",
        "Three ablation entries sat behind a 720p-or-larger guard; deleting them "
        "would have regressed small-resolution encoding invisibly.",
        ("analysis", "ideation"), "",
    ),
    Rule(
        "L27", "An upstream mechanism can substitute for your patch, not stack with it",
        "Two solutions to one problem do not compose. Check whether a new "
        "upstream feature makes the idea redundant or, worse, inverts its "
        "leverage.",
        "A source-based orientation heuristic became harmful once a two-pass dry "
        "pass landed: its prunes started steering a pass that then trusted them, "
        "so BD-rate rose while it pruned less.",
        ("ideation", "analysis"), "",
    ),
    Rule(
        "L28", "The ledger has one writer and every row carries its anchor",
        "Two documents in the prior corpus carry different CTC numbers for the "
        "same three patches and nobody reconciled them.",
        "Recorded while auditing the corpus for this system.",
        ("measurement",), "av2ra.core.registry",
    ),
    Rule(
        "L29", "Small clips and fast presets invert conclusions",
        "Sanity-check an idea at 4K and at the target preset before writing it. "
        "Reuse patches that won on small clips were slower at 4K, where a memo "
        "lookup is unpaid by its hit rate and one large arena beats nothing.",
        "All three bit-exact reuse patches were slower on 4K.",
        ("ideation",), "",
    ),
    Rule(
        "L30", "Dry-pass fidelity is more expensive to cut at faster presets",
        "The usual direction -- a heuristic buys more at slower presets -- "
        "reverses for changes to a ranking pass, because a faster preset has "
        "less downstream correction.",
        "One trellis patch gave +1.23%/+0.26% at speed 4 against +3.26%/+0.14% "
        "at speed 3.",
        ("ideation", "planning"), "",
    ),
)

RULES_BY_ID = {rule.id: rule for rule in RULES}


def for_stage(stage: str) -> list[Rule]:
  return [rule for rule in RULES if stage in rule.applies_to]


def prompt_block(stage: str, *, limit: int = 0) -> str:
  """Render the rules for one stage, for injection into an LLM prompt."""
  rules = for_stage(stage)
  if limit:
    rules = rules[:limit]
  lines = [
      "METHODOLOGY RULES (derived from ~50 prior experiments on this exact "
      "codebase; rules marked ENFORCED are checked by code and cannot be "
      "argued with):",
  ]
  lines.extend(f"  {rule.prompt_line()}" for rule in rules)
  return "\n".join(lines)


@dataclass
class LessonStore:
  """Built-in rules plus lessons this system learned itself."""

  learned: list[str] = field(default_factory=list)

  def add(self, lesson: str) -> None:
    if lesson and lesson not in self.learned:
      self.learned.append(lesson)

  def render(self, stage: str, *, learned_limit: int = 25) -> str:
    block = prompt_block(stage)
    if self.learned:
      block += "\n\nLESSONS THIS SYSTEM LEARNED SINCE:\n"
      block += "\n".join(f"  - {l}" for l in self.learned[-learned_limit:])
    return block
