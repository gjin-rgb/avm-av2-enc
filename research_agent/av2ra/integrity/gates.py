"""Runtime integrity gates: the checks that make an autonomous result believable.

Static policy stops a patch from editing the scoreboard. These gates deal with
the harder problem: a patch that is entirely within policy and still produces a
number that does not mean what it appears to mean. Each gate below exists
because the prior research on this codebase lost time to exactly that failure.

  ``activation``      -- did the patch do anything at all? Eight arms in the
                         corpus were *inert*: they compiled, ran, and never
                         fired, and their 0.00% BD-rate was read as "harmless"
                         rather than "untested". For an approximating patch,
                         an identical bitstream is a failed experiment, not a
                         safe one. For a reuse patch it is the required proof.
  ``determinism``     -- does the same binary on the same input produce the same
                         bitstream twice? If not, every quality number in the
                         pass is noise and every bit-exactness check is void.
  ``conformance``     -- does a conformant decoder accept the bitstream? The
                         cheapest possible catch for a pruning bug that leaves
                         syntax behind.
  ``null_arm``        -- anchor measured against anchor. Gives the *measurement
                         system's* own noise floor and bias. The corpus asked
                         for this four times and never ran it, and as a result
                         every ratio it reports has an unknown denominator
                         error; six inert arms later revealed a systematic
                         +1.6% bias on the patched arm.
  ``gate_overhead``   -- build the heuristic with its gate forced to "never
                         fire" and check that build is not slower than the
                         anchor. If the gate costs more than the pruning saves,
                         the idea is dead regardless of how good the pruning is.
  ``amdahl``          -- is the claimed whole-encode speedup even possible given
                         how much of the profile the touched functions occupy?
  ``holdout``         -- does the effect survive on clips the search never saw?

Every gate returns a :class:`GateResult` with an explicit ``blocking`` flag.
Non-blocking gates annotate the experiment; blocking ones stop it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..core.models import EncodeResult, Interval, MechanismClass
from ..measure import stats
from ..measure.encode import EncodeJob, decode_verify, run_encode
from ..util import log

LOG = log.get("gates")


@dataclass
class GateResult:
  name: str
  passed: bool
  blocking: bool
  detail: str
  data: dict = field(default_factory=dict)

  def __str__(self) -> str:
    mark = "PASS" if self.passed else ("FAIL" if self.blocking else "WARN")
    return f"[{mark}] {self.name}: {self.detail}"


@dataclass
class IntegrityReport:
  results: list[GateResult] = field(default_factory=list)

  def add(self, result: GateResult) -> GateResult:
    self.results.append(result)
    log.event(
        "integrity_gate", gate=result.name, passed=result.passed,
        blocking=result.blocking, detail=result.detail[:200],
    )
    return result

  @property
  def blocked(self) -> bool:
    return any(not r.passed and r.blocking for r in self.results)

  @property
  def blockers(self) -> list[GateResult]:
    return [r for r in self.results if not r.passed and r.blocking]

  @property
  def warnings(self) -> list[GateResult]:
    return [r for r in self.results if not r.passed and not r.blocking]

  def to_dict(self) -> dict:
    return {
        "blocked": self.blocked,
        "gates": [
            {
                "name": r.name, "passed": r.passed, "blocking": r.blocking,
                "detail": r.detail, "data": r.data,
            }
            for r in self.results
        ],
    }


# --------------------------------------------------------------------------
# Activation
# --------------------------------------------------------------------------


def check_activation(
    results: Sequence[EncodeResult], mechanism: MechanismClass
) -> GateResult:
  """Compare anchor and candidate bitstreams point by point.

  This single comparison answers two opposite questions depending on the
  declared mechanism, which is why the mechanism is a required field on every
  hypothesis rather than a label.
  """
  pairs: dict[tuple[str, int], dict[str, str]] = {}
  for res in results:
    if res.ok and res.bitstream_md5:
      pairs.setdefault((res.sequence, res.qp), {})[res.arm] = res.bitstream_md5

  comparable = {k: v for k, v in pairs.items() if "anchor" in v and "candidate" in v}
  if not comparable:
    return GateResult(
        "activation", False, True,
        "no (clip, QP) point produced both an anchor and a candidate bitstream, "
        "so nothing can be compared",
    )
  differing = [k for k, v in comparable.items() if v["anchor"] != v["candidate"]]
  identical = [k for k, v in comparable.items() if v["anchor"] == v["candidate"]]

  if mechanism.must_be_bit_exact:
    if differing:
      return GateResult(
          "activation", False, True,
          f"a {mechanism.value} patch changed the bitstream on "
          f"{len(differing)}/{len(comparable)} points "
          f"(e.g. {differing[0][0]} qp={differing[0][1]}). A reuse or kernel "
          "change that alters an encode decision has a bug -- a wrong cache key, "
          "a stale buffer, a non-equivalent kernel -- not a quality trade-off.",
          {"differing": [f"{s}@{q}" for s, q in differing[:10]]},
      )
    return GateResult(
        "activation", True, True,
        f"bit-exact on all {len(comparable)} points: quality risk is zero by "
        "proof, so this patch is judged on speed alone and never needs a CTC "
        "quality round",
        {"points": len(comparable)},
    )

  if not differing:
    return GateResult(
        "activation", False, True,
        f"the patch produced an identical bitstream on all {len(comparable)} "
        "points: it never fired. A 0.00% BD-rate here means the experiment did "
        "not run, not that the change is safe. Find out why the code path is "
        "unreachable before measuring anything.",
        {"points": len(comparable)},
    )
  if len(differing) < max(1, len(comparable) // 4):
    return GateResult(
        "activation", True, False,
        f"fired on only {len(differing)}/{len(comparable)} points; the effect "
        "will be diluted and the measurement may be underpowered. Consider a "
        "clip set that exercises the path.",
        {"fired": len(differing), "total": len(comparable),
         "inert_points": [f"{s}@{q}" for s, q in identical[:10]]},
    )
  return GateResult(
      "activation", True, True,
      f"fired on {len(differing)}/{len(comparable)} points",
      {"fired": len(differing), "total": len(comparable)},
  )


# --------------------------------------------------------------------------
# Determinism and conformance
# --------------------------------------------------------------------------


def check_determinism(results: Sequence[EncodeResult]) -> GateResult:
  """Repetitions of the same encode must agree byte for byte."""
  seen: dict[tuple[str, int, str], set[str]] = {}
  for res in results:
    if res.ok and res.bitstream_md5:
      seen.setdefault((res.sequence, res.qp, res.arm), set()).add(res.bitstream_md5)
  offenders = {k: v for k, v in seen.items() if len(v) > 1}
  if offenders:
    example = next(iter(offenders))
    return GateResult(
        "determinism", False, True,
        f"{len(offenders)} (clip, QP, arm) combination(s) produced more than one "
        f"distinct bitstream across repetitions, e.g. {example[0]} qp={example[1]} "
        f"arm={example[2]}. Every quality number in this pass is unreproducible; "
        "check threading, row-mt and any use of uninitialised memory.",
        {"offenders": [f"{s}@{q}/{a}" for s, q, a in list(offenders)[:10]]},
    )
  repeated = sum(1 for v in seen.values() if v)
  return GateResult(
      "determinism", True, True,
      f"all {repeated} measured points reproduced identically across repetitions",
  )


def check_conformance(
    decoder: str, results: Sequence[EncodeResult], bitstream_dir: str,
    *, sample: int = 0,
) -> GateResult:
  """Decode the produced bitstreams. A stream that will not decode is a bug.

  ``sample`` limits how many streams are decoded when a full pass is expensive;
  0 means all of them. Sampling is recorded in the result, because "we decoded
  four of forty-eight" is a materially weaker statement than "we decoded them
  all" and the report must not blur the two.
  """
  candidates = [
      r for r in results
      if r.arm == "candidate" and r.ok and r.bitstream_md5
  ]
  if not candidates:
    return GateResult("conformance", False, True, "no candidate bitstreams to verify")
  chosen = candidates if sample <= 0 else candidates[:sample]
  failures = []
  for res in chosen:
    path = _find_bitstream(bitstream_dir, res)
    if not path:
      failures.append(f"{res.sequence}@{res.qp}: bitstream not retained for decode")
      continue
    ok, _md5, message = decode_verify(decoder, path)
    if not ok:
      failures.append(f"{res.sequence}@{res.qp}: {message}")
  if failures:
    return GateResult(
        "conformance", False, True,
        f"{len(failures)}/{len(chosen)} candidate bitstreams failed to decode: "
        + "; ".join(failures[:4]),
        {"failures": failures[:20]},
    )
  scope = "all" if sample <= 0 else f"a {len(chosen)}-stream sample of"
  return GateResult(
      "conformance", True, True,
      f"{scope} {len(candidates)} candidate bitstream(s) decoded cleanly",
      {"decoded": len(chosen), "available": len(candidates), "sampled": sample > 0},
  )


def _find_bitstream(directory: str, result: EncodeResult) -> str:
  stem = f"{result.arm}_{result.sequence}_q{result.qp}_r{result.rep}.obu"
  path = os.path.join(directory, stem)
  return path if os.path.exists(path) else ""


# --------------------------------------------------------------------------
# Null arm: the measurement system measuring itself
# --------------------------------------------------------------------------


def run_null_arm(
    encoder: str,
    jobs: Sequence[EncodeJob],
    *,
    runner: Callable[[EncodeJob], EncodeResult] = run_encode,
    metric: str = "cx_time_s",
) -> GateResult:
  """Measure the anchor against itself and report the apparent effect.

  Every ratio this system produces is a quotient. Without this arm the error
  bar on the denominator is unknown, and the prior corpus turned two decisions
  on differences of 0.1 to 1.7 ratio points that it had no basis to resolve.
  A null arm is also the only way to detect *bias*: six inert arms in that
  corpus all read slower than the anchor, implying a systematic ~1.6-point
  understatement of every speedup measured the same way.

  The expected result is an interval centred on zero. Anything else is a
  property of the harness, and it is subtracted from nothing -- it is reported,
  loudly, because a biased harness needs fixing, not correcting for.
  """
  paired: list[tuple[float, float]] = []
  observations: list[float] = []
  for job in jobs:
    a = runner(_clone_job(job, arm="anchor", rep=job.rep, encoder=encoder))
    b = runner(_clone_job(job, arm="anchor_null", rep=job.rep + 100, encoder=encoder))
    if a.ok and b.ok:
      va, vb = getattr(a, metric, 0.0), getattr(b, metric, 0.0)
      if va and vb:
        paired.append((float(va), float(vb)))
        observations.extend([float(va), float(vb)])
      if a.bitstream_md5 and a.bitstream_md5 != b.bitstream_md5:
        return GateResult(
            "null_arm", False, True,
            f"the same binary produced different bitstreams for {a.sequence} "
            f"qp={a.qp}: the encoder is nondeterministic in this configuration",
        )
  if len(paired) < 2:
    return GateResult(
        "null_arm", False, False,
        "not enough successful null-arm encodes to estimate the harness noise floor",
    )
  interval = stats.paired_log_ratio_ci(paired)
  noise = stats.cv_pct(observations)
  biased = interval.excludes_zero
  return GateResult(
      "null_arm", not biased, False,
      (
          f"anchor vs anchor reads {interval} on {metric} (noise floor "
          f"{noise:.2f}% CV, n={len(paired)} pairs). "
          + (
              "This is a systematic bias in the harness, not in any patch: "
              "every speedup measured this way is off by about this much."
              if biased
              else "No systematic bias detected; effects smaller than the "
              "interval width are not measurable with this design."
          )
      ),
      {
          "interval": [interval.lo, interval.point, interval.hi],
          "noise_cv_pct": noise,
          "pairs": len(paired),
          "mde_pct": stats.mde_pct(noise, len(paired)),
      },
  )


def _clone_job(job: EncodeJob, *, arm: str, rep: int, encoder: str) -> EncodeJob:
  import dataclasses

  stem = f"{arm}_{job.clip.name}_q{job.cfg.qp}_r{rep}"
  return dataclasses.replace(
      job,
      arm=arm,
      rep=rep,
      encoder=encoder,
      out_path=os.path.join(os.path.dirname(job.out_path), stem + ".obu"),
      log_path=(
          os.path.join(os.path.dirname(job.log_path), stem + ".log")
          if job.log_path else None
      ),
  )


# --------------------------------------------------------------------------
# Overfitting and plausibility
# --------------------------------------------------------------------------


def check_holdout(
    screen_effect: Interval | None,
    holdout_effect: Interval | None,
    *,
    label: str = "speed",
    shrink_threshold: float = 0.5,
) -> GateResult:
  """Does the effect survive on clips the agent never optimised against?

  The screening set is small and the agent is allowed to look at it as often as
  it likes, so an effect measured there is an upper bound. The holdout set is
  never shown to ideation and never used to pick a threshold. A large gap is
  the signature of a heuristic tuned to the screen clips rather than to the
  content property it claims to detect.
  """
  if screen_effect is None or holdout_effect is None:
    return GateResult(
        "holdout", True, False, "no holdout comparison available for this experiment"
    )
  if not screen_effect.excludes_zero:
    return GateResult(
        "holdout", True, False,
        f"screening did not resolve a {label} effect, so there is nothing to "
        "check for overfitting",
    )
  if screen_effect.point * holdout_effect.point < 0:
    return GateResult(
        "holdout", False, True,
        f"the {label} effect reverses sign on the holdout clips "
        f"(screen {screen_effect}, holdout {holdout_effect}). The heuristic is "
        "responding to a property of the screening set, not of the content.",
        {"screen": screen_effect.point, "holdout": holdout_effect.point},
    )
  ratio = abs(holdout_effect.point) / abs(screen_effect.point) if screen_effect.point else 0.0
  if ratio < shrink_threshold:
    return GateResult(
        "holdout", False, False,
        f"the {label} effect shrinks to {ratio * 100:.0f}% of its screening value "
        f"on held-out clips (screen {screen_effect}, holdout {holdout_effect}). "
        "Treat the screening number as an upper bound and expect CTC to land "
        "nearer the holdout figure.",
        {"retention": ratio},
    )
  return GateResult(
      "holdout", True, False,
      f"the {label} effect retains {ratio * 100:.0f}% of its screening value on "
      f"held-out clips (holdout {holdout_effect})",
      {"retention": ratio},
  )


def check_amdahl(
    claimed_speedup_pct: float,
    profile_share_pct: float,
    *,
    functions: Sequence[str] = (),
    slack: float = 1.15,
) -> GateResult:
  """Is the claimed whole-encode effect achievable given the profile?

  A kernel cannot save more of the encode than it occupies. Applied *before*
  spending cluster time, this gate turns "38.8% faster kernel" into "at most
  2% whole-encode, and only if this kernel is at least 5.2% of the profile" --
  which is exactly the reasoning that correctly predicted a 1.2% CTC result in
  the prior corpus.
  """
  if profile_share_pct <= 0:
    return GateResult(
        "amdahl", True, False,
        "no profile share available for the touched functions; run "
        "'av2ra profile' so future ideas in this area can be sanity-checked",
        {"functions": list(functions)},
    )
  ceiling = profile_share_pct * slack
  if claimed_speedup_pct > ceiling:
    return GateResult(
        "amdahl", False, True,
        f"claims {claimed_speedup_pct:.2f}% whole-encode speedup from functions "
        f"holding {profile_share_pct:.2f}% of the profile. Even eliminating them "
        "entirely could not buy that. Either the profile is wrong for this "
        "configuration or the measurement is.",
        {"claimed": claimed_speedup_pct, "share": profile_share_pct},
    )
  return GateResult(
      "amdahl", True, False,
      f"{claimed_speedup_pct:.2f}% whole-encode speedup is within the "
      f"{profile_share_pct:.2f}% the touched functions occupy",
      {"claimed": claimed_speedup_pct, "share": profile_share_pct},
  )


def check_underpowered(
    effect: Interval | None, noise_cv_pct: float, reps: int, *, care_about_pct: float
) -> GateResult:
  """Distinguish "no effect" from "no power". They look identical in a table."""
  mde = stats.mde_pct(noise_cv_pct, reps)
  if effect is not None and effect.excludes_zero:
    return GateResult(
        "power", True, False,
        f"effect {effect} is resolved; this design could see {mde:.2f}%",
        {"mde_pct": mde},
    )
  if mde > care_about_pct:
    need = stats.required_reps(noise_cv_pct, care_about_pct)
    return GateResult(
        "power", False, False,
        f"the design cannot resolve the {care_about_pct:.1f}% effect this "
        f"experiment cares about: with {noise_cv_pct:.2f}% noise and {reps} "
        f"repetitions the smallest visible effect is {mde:.2f}%. "
        f"About {need} repetitions per arm would be needed. Reporting 'no "
        "effect' from this data would be reporting the design, not the patch.",
        {"mde_pct": mde, "required_reps": need},
    )
  return GateResult(
      "power", True, False,
      f"no effect, and the design had the power to see one "
      f"({mde:.2f}% detectable, {care_about_pct:.1f}% cared about)",
      {"mde_pct": mde},
  )
