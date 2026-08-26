"""The domain model: what an experiment *is*, and what evidence attaches to it.

Everything the system believes about a piece of research lives in one
:class:`Experiment` record, serialised to JSON next to its artifacts. Two
constraints shaped these types:

* **A number is never separable from the conditions that produced it.** Every
  measurement carries the base SHA, the build fingerprint, the preset, the
  clip set and the repetition count. The prior research on this codebase lost a
  whole round to this: measurements taken against one anchor were quoted after
  the anchor had moved 187 lines under two of the patches, and every patch
  still reported "APPLIES". A number without its provenance is a rumour.

* **A verdict is a function of evidence, not a field an agent can set.** The
  agent proposes and measures; :mod:`av2ra.measure.tiers` derives the verdict
  from the recorded evidence, and :mod:`av2ra.integrity` re-derives it
  independently at publish time. That is why ``Experiment.verdict`` is written
  only through :meth:`Experiment.apply_tier_result`.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class MechanismClass(str, enum.Enum):
  """How a change is supposed to buy its speed. Decides how it is evaluated.

  The distinction is not cosmetic. A ``REUSE`` change claims to compute the
  same decisions more cheaply, so bit-exactness is a *provable property* and a
  divergence is a bug, not a quality trade. Sending such a patch to a CTC round
  to "see what the BD-rate is" wastes the scarcest resource in the system to
  answer a question a five-minute local check answers exactly.
  """

  REUSE = "reuse"          # caching, memoisation, hoisting, allocation: must be bit-exact
  APPROX = "approx"        # prunes or reorders candidates: has a real quality cost
  KERNEL = "kernel"        # SIMD / arithmetic rewrite of a kernel: must be bit-exact
  DISPATCH = "dispatch"    # wiring a faster path that existed but was unreachable
  STRUCTURAL = "structural"  # changes what is searched, not just how much
  RDCOST = "rdcost"        # changes the cost function or lambda: quality-affecting by design
  UNKNOWN = "unknown"

  @property
  def must_be_bit_exact(self) -> bool:
    return self in (MechanismClass.REUSE, MechanismClass.KERNEL)


class Tier(str, enum.Enum):
  """The evaluation ladder. Each tier is cheaper than the next by ~10x."""

  T0_BUILD = "T0_build"            # compiles, encodes, decodes, MD5-matches
  T1_EXACT = "T1_exactness"        # bit-exact vs anchor (reuse/kernel classes)
  T2_COMPLEXITY = "T2_complexity"  # is the speedup real, above the noise floor
  T3_SCREEN = "T3_screen"          # local BD-rate + speed on the screening set
  T4_HELDOUT = "T4_heldout"        # same, on clips the agent never optimised against
  T5_CTC = "T5_ctc"                # the full common-test-conditions run

  @property
  def order(self) -> int:
    return list(Tier).index(self)


class Verdict(str, enum.Enum):
  PENDING = "PENDING"
  BUILD_FAIL = "BUILD_FAIL"
  CONFORMANCE_FAIL = "CONFORMANCE_FAIL"   # bitstream did not decode / MD5 mismatch
  INTEGRITY_FAIL = "INTEGRITY_FAIL"       # violated a policy gate; never promotable
  DIVERGES = "DIVERGES"                   # a reuse/kernel patch changed the bitstream
  NO_EFFECT = "NO_EFFECT"                 # effect is inside the noise floor
  REGRESSION = "REGRESSION"               # worse on both axes
  BELOW_BAR = "BELOW_BAR"                 # real trade-off, does not clear the ratio bar
  SCREEN_PASS = "SCREEN_PASS"             # cleared local tiers, earns a CTC slot
  CTC_PASS = "CTC_PASS"                   # cleared the bar on the real test conditions
  PROMOTE = "PROMOTE"                     # recommended for upstream submission
  SUPERSEDED = "SUPERSEDED"               # a descendant strictly dominates it

  @property
  def is_terminal_failure(self) -> bool:
    return self in (
        Verdict.BUILD_FAIL, Verdict.CONFORMANCE_FAIL, Verdict.INTEGRITY_FAIL,
        Verdict.DIVERGES, Verdict.REGRESSION,
    )


class Quadrant(str, enum.Enum):
  """Where a change lands on the (quality, speed) plane.

  Signs follow the CTC convention: BD-rate below zero is *better* compression;
  a time ratio below 100% is *faster*.
  """

  Q1_ABSOLUTE_WIN = "Q1_absolute_win"      # better compression and faster
  Q2_CODING_TRADE = "Q2_coding_tradeoff"   # better compression, slower
  Q3_SPEED_TRADE = "Q3_speedup_tradeoff"   # faster, worse compression
  Q4_REGRESSION = "Q4_pure_regression"     # worse and slower
  UNRESOLVED = "unresolved"                # confidence interval spans an axis


class ExperimentStatus(str, enum.Enum):
  PROPOSED = "proposed"
  IMPLEMENTING = "implementing"
  BUILDING = "building"
  SCREENING = "screening"
  AWAITING_CTC = "awaiting_ctc"
  CTC_RUNNING = "ctc_running"
  COMPLETE = "complete"
  ABORTED = "aborted"


# --------------------------------------------------------------------------
# Serialisation helper
# --------------------------------------------------------------------------


def _to_jsonable(value: Any) -> Any:
  if dataclasses.is_dataclass(value) and not isinstance(value, type):
    return {k: _to_jsonable(v) for k, v in dataclasses.asdict(value).items()}
  if isinstance(value, enum.Enum):
    return value.value
  if isinstance(value, dict):
    return {k: _to_jsonable(v) for k, v in value.items()}
  if isinstance(value, (list, tuple)):
    return [_to_jsonable(v) for v in value]
  return value


class Jsonable:
  """Mixin giving dataclasses a stable dict form and a content hash."""

  def to_dict(self) -> dict:
    return _to_jsonable(self)

  def content_hash(self) -> str:
    import json

    blob = json.dumps(self.to_dict(), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Measurement primitives
# --------------------------------------------------------------------------


@dataclass
class EncodeResult(Jsonable):
  """One encode of one clip at one QP, with everything needed to compare it."""

  sequence: str
  qp: int
  preset: int
  arm: str                      # "anchor" or "candidate" (or a variant label)
  ok: bool = False
  bitrate_kbps: float = 0.0
  psnr_y: float = 0.0
  psnr_u: float = 0.0
  psnr_v: float = 0.0
  psnr_avg: float = 0.0
  psnr_overall: float = 0.0
  frames: int = 0
  bytes_out: int = 0
  # Three independent cost measures. ``cx_time_s`` is the encoder's own timer
  # (excludes file I/O and setup), ``cpu_s`` is the process CPU time from
  # wait4, ``instructions`` is the perf counter when available. Which one the
  # verdict uses is decided by whichever has the lowest measured noise.
  cx_time_s: float = 0.0
  cpu_s: float = 0.0
  wall_s: float = 0.0
  instructions: int | None = None
  cycles: int | None = None
  max_rss_kib: int = 0
  bitstream_md5: str = ""       # md5 of the .obu file
  decoded_md5: str = ""         # md5 of decoded frames, from avmdec --md5
  rep: int = 0                  # repetition index within a paired design
  error: str = ""

  @property
  def rd_point(self) -> tuple[float, float]:
    return (self.bitrate_kbps, self.psnr_y)


@dataclass
class Interval(Jsonable):
  """A point estimate with a two-sided 95% interval, in percent."""

  point: float
  lo: float
  hi: float
  n: int = 0
  method: str = ""

  @property
  def excludes_zero(self) -> bool:
    return self.lo > 0.0 or self.hi < 0.0

  @property
  def sign(self) -> int:
    if self.lo > 0.0:
      return 1
    if self.hi < 0.0:
      return -1
    return 0

  def __str__(self) -> str:
    return f"{self.point:+.2f}% [{self.lo:+.2f}, {self.hi:+.2f}]"


@dataclass
class BDRateResult(Jsonable):
  """BD-rate for one sequence and one quality metric.

  ``value`` follows the CTC sign convention: positive means the candidate needs
  *more* bits for the same quality, i.e. worse.
  """

  sequence: str
  metric: str
  value: float
  ok: bool = True
  error: str = ""
  anchor_points: list[tuple[float, float]] = field(default_factory=list)
  candidate_points: list[tuple[float, float]] = field(default_factory=list)
  overlap_lo: float = 0.0
  overlap_hi: float = 0.0


@dataclass
class ComplexityResult(Jsonable):
  """Speed comparison for one sequence, expressed as a percentage change."""

  sequence: str
  metric: str                    # "instructions" | "cx_time_s" | "cpu_s"
  delta_pct: Interval            # positive = candidate is SLOWER
  anchor_mean: float = 0.0
  candidate_mean: float = 0.0
  anchor_cv_pct: float = 0.0     # measured noise floor of the anchor arm
  mde_pct: float = 0.0           # smallest effect this design could resolve
  reps: int = 0


@dataclass
class ScreenSummary(Jsonable):
  """Aggregate of a screening pass: the object a verdict is computed from."""

  preset: int
  clip_set: str
  metric_for_speed: str = "instructions"
  bdrate_by_metric: dict[str, Interval] = field(default_factory=dict)
  speed_delta: Interval | None = None       # positive = slower
  per_sequence_bdrate: dict[str, float] = field(default_factory=dict)
  per_sequence_speed: dict[str, float] = field(default_factory=dict)
  worst_sequence_bdrate: float = 0.0
  worst_sequence_name: str = ""
  n_encodes: int = 0
  reps: int = 0
  notes: list[str] = field(default_factory=list)

  @property
  def bdrate_yuv(self) -> Interval | None:
    return self.bdrate_by_metric.get("PSNR-YUV") or self.bdrate_by_metric.get("PSNR_Y")


@dataclass
class TierResult(Jsonable):
  """The outcome of one rung of the ladder."""

  tier: Tier
  passed: bool
  verdict: Verdict = Verdict.PENDING
  quadrant: Quadrant = Quadrant.UNRESOLVED
  reason: str = ""
  summary: ScreenSummary | None = None
  ratio: float | None = None        # speed given up per unit of BD-rate bought
  bar: float | None = None          # the bar that ratio had to clear
  started_ts: float = field(default_factory=time.time)
  finished_ts: float = 0.0
  evidence: dict[str, Any] = field(default_factory=dict)
  artifacts: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Research objects
# --------------------------------------------------------------------------


@dataclass
class Hypothesis(Jsonable):
  """A falsifiable claim about the encoder, with a mechanism and a prediction.

  The prediction fields are not decoration. An idea whose author cannot say in
  advance what it should buy cannot be wrong, and an agent that is never wrong
  never learns. :mod:`av2ra.agent.analyst` scores realised effect against
  ``expected_speedup_pct`` / ``expected_bdrate_pct`` and feeds the calibration
  error back into ideation.
  """

  id: str
  title: str
  subsystem: str
  mechanism: MechanismClass
  lens: str                              # which generator produced it
  statement: str                         # the claim, in one paragraph
  rationale: str                         # why it should work, mechanistically
  target_presets: list[int] = field(default_factory=lambda: [2])
  target_functions: list[str] = field(default_factory=list)
  target_files: list[str] = field(default_factory=list)
  expected_speedup_pct: float = 0.0      # positive = faster
  expected_bdrate_pct: float = 0.0       # positive = worse compression
  confidence: float = 0.5                # the generator's own prior, 0..1
  risk_notes: list[str] = field(default_factory=list)
  kill_criteria: list[str] = field(default_factory=list)
  parameters: dict[str, Any] = field(default_factory=dict)  # sweepable -D knobs
  parent_experiment: str | None = None   # lineage, for tuning chains
  novelty: dict[str, Any] = field(default_factory=dict)
  created_ts: float = field(default_factory=time.time)

  def signature(self) -> str:
    """Stable identity for dedup: what it touches and how, not how it is worded."""
    key = "|".join([
        self.subsystem,
        self.mechanism.value,
        ",".join(sorted(self.target_functions)),
        ",".join(sorted(self.target_files)),
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class PatchInfo(Jsonable):
  """A diff plus the facts about it that policy and dedup need."""

  diff_text: str = ""
  base_sha: str = ""
  files: list[str] = field(default_factory=list)
  added_lines: int = 0
  removed_lines: int = 0
  touched_functions: list[str] = field(default_factory=list)
  build_defines: dict[str, str] = field(default_factory=dict)
  normalized_hash: str = ""     # whitespace/comment-insensitive identity

  @property
  def size(self) -> int:
    return self.added_lines + self.removed_lines


@dataclass
class CtcRequest(Jsonable):
  """What the core asks a compute back end for. Deliberately infra-free.

  Nothing here mentions EDA, blade, CNS or a shell script: the Google back end
  translates these fields into ``kick_off_av2ctc_eda.sh`` arguments, and the
  local back end turns them into a queue of encodes on whatever machines it has.
  """

  experiment_id: str
  base_sha: str
  patch_ref: str                    # how the back end obtains the code
  presets: list[int] = field(default_factory=lambda: [2])
  testsets: list[str] = field(default_factory=lambda: ["a1", "a2"])
  configs: list[str] = field(default_factory=lambda: ["ra"])
  frame_count: int = 33
  metrics: list[str] = field(default_factory=lambda: ["PSNR-Y", "PSNR-YUV"])
  timing_accuracy: str = "high"
  question: str = ""                # what this run decides; required by policy
  anchor_ref: str = ""              # reuse a cached anchor invocation if given
  priority: int = 0
  submitted_ts: float = 0.0


@dataclass
class CtcSequenceResult(Jsonable):
  sequence: str
  bdrate: dict[str, float] = field(default_factory=dict)  # metric -> percent
  enc_time_ratio_pct: float = 100.0    # <100 means faster than anchor
  dec_time_ratio_pct: float = 100.0


@dataclass
class CtcClassResult(Jsonable):
  """One (testset, config, preset) cell of a CTC run."""

  testset: str
  config: str
  preset: int
  sequences: list[CtcSequenceResult] = field(default_factory=list)
  average_bdrate: dict[str, float] = field(default_factory=dict)
  average_enc_time_ratio_pct: float = 100.0
  average_dec_time_ratio_pct: float = 100.0
  complete: bool = True
  n_expected: int = 0

  @property
  def speedup_pct(self) -> float:
    """Positive means faster, matching how the registry records speedups."""
    return 100.0 - self.average_enc_time_ratio_pct


@dataclass
class CtcResult(Jsonable):
  experiment_id: str
  job_id: str = ""
  state: str = "unknown"        # queued | running | partial | done | failed
  classes: list[CtcClassResult] = field(default_factory=list)
  anchor_job_id: str = ""
  candidate_job_id: str = ""
  fraction_complete: float = 0.0
  error: str = ""
  raw_paths: list[str] = field(default_factory=list)
  submitted_ts: float = 0.0
  finished_ts: float = 0.0


@dataclass
class Experiment(Jsonable):
  """The unit of research. One hypothesis, one patch, one evidence trail."""

  id: str
  hypothesis: Hypothesis
  status: ExperimentStatus = ExperimentStatus.PROPOSED
  verdict: Verdict = Verdict.PENDING
  quadrant: Quadrant = Quadrant.UNRESOLVED
  base_sha: str = ""
  build_fingerprint: str = ""
  patch: PatchInfo = field(default_factory=PatchInfo)
  tiers: list[TierResult] = field(default_factory=list)
  ctc: CtcResult | None = None
  node_id: str = ""
  worktree: str = ""
  branch: str = ""
  created_ts: float = field(default_factory=time.time)
  updated_ts: float = field(default_factory=time.time)
  lessons: list[str] = field(default_factory=list)
  integrity: dict[str, Any] = field(default_factory=dict)
  cost: dict[str, float] = field(default_factory=dict)  # cpu-hours, ctc slots, llm tokens
  human_notes: list[str] = field(default_factory=list)
  supersedes: list[str] = field(default_factory=list)
  superseded_by: str | None = None

  # -- evidence -----------------------------------------------------------

  def tier_result(self, tier: Tier) -> TierResult | None:
    for result in reversed(self.tiers):
      if result.tier == tier:
        return result
    return None

  def tier_result_by_name(self, name: str) -> TierResult | None:
    """Look a tier up by its string value, for reports and tests."""
    for result in self.tiers:
      if result.tier.value == name:
        return result
    return None

  def apply_tier_result(self, result: TierResult) -> None:
    """Record a tier outcome and let it move the experiment's verdict.

    The verdict only ever moves along the ladder: a later tier can overturn an
    earlier optimistic reading, but a passing T3 cannot erase a failing T0.
    """
    self.tiers = [t for t in self.tiers if t.tier != result.tier] + [result]
    self.tiers.sort(key=lambda t: t.tier.order)
    result.finished_ts = result.finished_ts or time.time()
    self.updated_ts = time.time()

    for tier_result in self.tiers:
      if tier_result.verdict.is_terminal_failure:
        self.verdict = tier_result.verdict
        self.quadrant = tier_result.quadrant
        self.status = ExperimentStatus.COMPLETE
        return
    highest = max(self.tiers, key=lambda t: t.tier.order)
    self.verdict = highest.verdict
    self.quadrant = highest.quadrant

  @property
  def highest_tier_passed(self) -> Tier | None:
    passed = [t.tier for t in self.tiers if t.passed]
    return max(passed, key=lambda t: t.order) if passed else None

  @property
  def is_alive(self) -> bool:
    return self.status not in (ExperimentStatus.COMPLETE, ExperimentStatus.ABORTED)

  def add_lesson(self, text: str) -> None:
    if text and text not in self.lessons:
      self.lessons.append(text)
