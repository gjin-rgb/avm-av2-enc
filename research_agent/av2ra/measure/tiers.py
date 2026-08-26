"""The evaluation ladder: cheap proofs first, cluster time last.

Each rung answers a question the rung below cannot, and costs roughly ten times
as much. The ordering is not a convenience -- it is the only way a system with
one scarce resource (CTC cluster rounds) and one abundant one (local CPU) can
make progress. The rule the prior research arrived at, after spending a round
on ten patches of which eight were already dead on cheaper evidence: *every CTC
slot must answer a question nothing cheaper could*.

  T0  build + conformance   Does it compile, encode, and decode? Minutes.
  T1  exactness             For reuse/kernel patches: is the bitstream identical?
                            A proof, not an estimate. Replaces a CTC round outright.
  T2  complexity            Is the speedup real and above the noise floor?
                            Paired, pinned, instruction counts where available.
  T3  screen                Local RD behaviour on the screening clips. Ranks and
                            detects gross regressions. **Never quotes a BD-rate
                            magnitude** -- see below.
  T4  holdout               Same, on clips ideation never sees. Overfit detector.
  T5  ctc                   The real test conditions. The only number that ships.

The T3 restriction is the sharpest lesson in the corpus and the biggest
correction to the draft protocol. Local BD-rate on short, low-resolution clips
did not merely have wide error bars against CTC -- it had the **wrong sign** on
three of ten patches, and understated the true cost by 7 to 12x on a fourth.
A local screen that reports "-0.63% BD-rate" for a patch that turns out to cost
"+2.50%" is worse than no screen, because it is acted on. So T3 is allowed to
say "this crashes", "this never fires", "this is catastrophically worse", and
"of these five variants, this is the ordering" -- and nothing else about
quality.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ..core.models import (
    EncodeResult,
    Experiment,
    Interval,
    MechanismClass,
    ScreenSummary,
    Tier,
    TierResult,
    Verdict,
)
from ..integrity import gates
from ..util import log
from . import bdrate as bdrate_mod
from . import screen as screen_mod
from . import stats
from .acceptance import PresetBars, assess

LOG = log.get("tiers")

#: What T3 is permitted to conclude about quality. Anything stronger is a
#: statement the data cannot support.
T3_QUALITY_DISCLAIMER = (
    "Local screening BD-rate is a rank and a tripwire, not an estimate. On this "
    "codebase local screens have disagreed with CTC in sign on 3 of 10 patches "
    "and understated the cost by up to 12x. Use it to order variants and to "
    "catch gross regressions; never quote it as the patch's BD-rate."
)

#: A local screen may kill a patch outright only when the damage is far beyond
#: what sign error could explain.
T3_CATASTROPHIC_BDRATE_PCT = 3.0


@dataclass
class TierContext:
  """Everything a tier needs that is not the experiment itself."""

  workdir: str
  preset: int
  decoder: str = ""
  bars: dict[int, PresetBars] | None = None
  care_about_speedup_pct: float = 3.0
  conformance_sample: int = 0
  seed: str = "av2ra"
  profile_share_pct: float = 0.0
  holdout_speed: Interval | None = None
  holdout_bdrate: Interval | None = None
  acceptance_mode: str = "conservative"
  bdrate_cap_mode: str = "warn"


def _summary_from_results(
    results: list[EncodeResult], *, preset: int, clip_set: str, seed: str
) -> tuple[ScreenSummary, str]:
  metric = screen_mod.choose_cost_metric(results)
  pairs = screen_mod.pairs_for_metric(results, metric)
  speed = stats.paired_log_ratio_ci(pairs) if pairs else None

  summary = ScreenSummary(
      preset=preset,
      clip_set=clip_set,
      metric_for_speed=metric,
      speed_delta=speed,
      n_encodes=len(results),
      reps=1 + max((r.rep for r in results), default=0),
  )
  for quality_metric in ("PSNR-Y", "PSNR-YUV"):
    aggregate = bdrate_mod.aggregate_bdrate(results, metric=quality_metric, seed=seed)
    if aggregate.interval:
      summary.bdrate_by_metric[quality_metric] = aggregate.interval
    if quality_metric == "PSNR-YUV" or "PSNR-YUV" not in summary.bdrate_by_metric:
      summary.per_sequence_bdrate = dict(aggregate.per_sequence)
      summary.worst_sequence_name = aggregate.worst_sequence
      summary.worst_sequence_bdrate = aggregate.worst_value
    for sequence, error in aggregate.errors.items():
      summary.notes.append(f"BD-rate unavailable for {sequence}: {error}")

  for sequence, seq_pairs in screen_mod.per_sequence_pairs(results, metric).items():
    if seq_pairs:
      summary.per_sequence_speed[sequence] = -stats.paired_log_ratio_ci(seq_pairs).point
  return (summary, metric)


# --------------------------------------------------------------------------
# T0 / T1
# --------------------------------------------------------------------------


def evaluate_t0(
    experiment: Experiment, results: list[EncodeResult], context: TierContext
) -> TierResult:
  """Build succeeded (checked earlier), encodes ran, bitstreams decode."""
  started = time.time()
  report = gates.IntegrityReport()
  failures = [r for r in results if not r.ok]
  if not results:
    return TierResult(
        tier=Tier.T0_BUILD, passed=False, verdict=Verdict.BUILD_FAIL,
        reason="no encodes completed", started_ts=started, finished_ts=time.time(),
    )
  if failures:
    detail = "; ".join(f"{f.sequence}@{f.qp}/{f.arm}: {f.error[:120]}" for f in failures[:4])
    return TierResult(
        tier=Tier.T0_BUILD, passed=False, verdict=Verdict.CONFORMANCE_FAIL,
        reason=f"{len(failures)}/{len(results)} encodes failed: {detail}",
        started_ts=started, finished_ts=time.time(),
        evidence={"failures": [f.to_dict() for f in failures[:20]]},
    )

  report.add(gates.check_determinism(results))
  if context.decoder:
    report.add(
        gates.check_conformance(
            context.decoder, results,
            os.path.join(context.workdir, "bitstreams"),
            sample=context.conformance_sample,
        )
    )
  passed = not report.blocked
  return TierResult(
      tier=Tier.T0_BUILD,
      passed=passed,
      verdict=Verdict.PENDING if passed else Verdict.CONFORMANCE_FAIL,
      reason=(
          "encoded and decoded cleanly, deterministically"
          if passed
          else "; ".join(g.detail for g in report.blockers)
      ),
      started_ts=started, finished_ts=time.time(),
      evidence=report.to_dict(),
  )


def evaluate_t1(
    experiment: Experiment, results: list[EncodeResult], context: TierContext
) -> TierResult:
  """Activation: did the patch fire, and did it fire in the way it claimed?"""
  started = time.time()
  mechanism = experiment.hypothesis.mechanism
  gate = gates.check_activation(results, mechanism)
  if not gate.passed:
    verdict = (
        Verdict.DIVERGES
        if mechanism.must_be_bit_exact and "changed the bitstream" in gate.detail
        else Verdict.NO_EFFECT
    )
    return TierResult(
        tier=Tier.T1_EXACT, passed=False, verdict=verdict, reason=gate.detail,
        started_ts=started, finished_ts=time.time(), evidence=gate.data,
    )
  return TierResult(
      tier=Tier.T1_EXACT, passed=True, verdict=Verdict.PENDING, reason=gate.detail,
      started_ts=started, finished_ts=time.time(), evidence=gate.data,
  )


# --------------------------------------------------------------------------
# T2: is the speedup real?
# --------------------------------------------------------------------------


def evaluate_t2(
    experiment: Experiment,
    results: list[EncodeResult],
    context: TierContext,
    *,
    null_arm: gates.GateResult | None = None,
) -> TierResult:
  started = time.time()
  metric = screen_mod.choose_cost_metric(results)
  pairs = screen_mod.pairs_for_metric(results, metric)
  if len(pairs) < 2:
    return TierResult(
        tier=Tier.T2_COMPLEXITY, passed=False, verdict=Verdict.NO_EFFECT,
        reason="not enough paired observations to compare cost",
        started_ts=started, finished_ts=time.time(),
    )
  speed = stats.paired_log_ratio_ci(pairs)
  anchor_values = [a for a, _ in pairs]
  noise = stats.cv_pct(anchor_values)
  report = gates.IntegrityReport()
  power = report.add(
      gates.check_underpowered(
          speed, noise, len(pairs), care_about_pct=context.care_about_speedup_pct
      )
  )
  if null_arm:
    report.add(null_arm)

  evidence = {
      "metric": metric,
      "pairs": len(pairs),
      "anchor_cv_pct": noise,
      "speed_delta": [speed.lo, speed.point, speed.hi],
      "p_value": stats.paired_p_value(pairs),
      "gates": report.to_dict(),
  }

  if metric != "instructions":
    evidence["metric_warning"] = (
        "instruction counts were unavailable, so this comparison uses "
        f"'{metric}'. Instruction counts are reproducible to well under 0.1% "
        "and immune to co-tenancy; wall-derived metrics on a shared machine "
        "have a noise floor around 2%, which is larger than most effects worth "
        "finding. Install perf or run on a machine that permits counting."
    )

  if not speed.excludes_zero:
    return TierResult(
        tier=Tier.T2_COMPLEXITY, passed=False, verdict=Verdict.NO_EFFECT,
        reason=(
            f"cost change {speed} on {metric} does not exclude zero. "
            + power.detail
        ),
        started_ts=started, finished_ts=time.time(), evidence=evidence,
    )
  if speed.lo > 0:
    return TierResult(
        tier=Tier.T2_COMPLEXITY, passed=False, verdict=Verdict.REGRESSION,
        reason=f"the patch is slower: {speed} on {metric}",
        started_ts=started, finished_ts=time.time(), evidence=evidence,
    )
  return TierResult(
      tier=Tier.T2_COMPLEXITY, passed=True, verdict=Verdict.PENDING,
      reason=f"cost reduced by {-speed.point:.2f}% {speed} on {metric}",
      started_ts=started, finished_ts=time.time(), evidence=evidence,
  )


# --------------------------------------------------------------------------
# T3 / T4: local RD behaviour
# --------------------------------------------------------------------------


def evaluate_t3(
    experiment: Experiment,
    results: list[EncodeResult],
    context: TierContext,
    *,
    clip_set: str = "screen",
    tier: Tier = Tier.T3_SCREEN,
) -> TierResult:
  started = time.time()
  summary, metric = _summary_from_results(
      results, preset=context.preset, clip_set=clip_set, seed=context.seed
  )
  summary.notes.append(T3_QUALITY_DISCLAIMER)
  quality = summary.bdrate_yuv
  speed = summary.speed_delta
  evidence: dict = {
      "metric_for_speed": metric,
      "per_sequence_bdrate": summary.per_sequence_bdrate,
      "per_sequence_speedup": summary.per_sequence_speed,
      "quality_is_indicative_only": True,
    }

  if quality is None or speed is None:
    return TierResult(
        tier=tier, passed=False, verdict=Verdict.NO_EFFECT,
        reason="screening produced no usable RD curves",
        summary=summary, started_ts=started, finished_ts=time.time(),
        evidence=evidence,
    )

  # The only quality conclusion a local screen may draw on its own.
  if quality.lo > T3_CATASTROPHIC_BDRATE_PCT:
    return TierResult(
        tier=tier, passed=False, verdict=Verdict.REGRESSION,
        reason=(
            f"local BD-rate {quality} is beyond what local/CTC disagreement can "
            f"explain (threshold {T3_CATASTROPHIC_BDRATE_PCT:.1f}%). This is a "
            "real regression, not a screening artifact."
        ),
        summary=summary, started_ts=started, finished_ts=time.time(),
        evidence=evidence,
    )

  worst = summary.worst_sequence_bdrate
  if worst > max(4.0 * max(quality.point, 0.05), T3_CATASTROPHIC_BDRATE_PCT):
    summary.notes.append(
        f"per-sequence spread is extreme: {summary.worst_sequence_name} alone "
        f"costs {worst:+.2f}% against a mean of {quality.point:+.2f}%. A mean "
        "that hides one bad clip is a regression risk, and it is also a design "
        "signal: a content gate may exist here."
    )

  if speed.lo >= 0:
    return TierResult(
        tier=tier, passed=False,
        verdict=Verdict.REGRESSION if speed.lo > 0 else Verdict.NO_EFFECT,
        reason=f"no local speedup: {speed} on {metric}",
        summary=summary, started_ts=started, finished_ts=time.time(),
        evidence=evidence,
    )

  indicative = assess(
      quality, speed, preset=context.preset, bars=context.bars,
      mechanism_requires_bit_exact=experiment.hypothesis.mechanism.must_be_bit_exact,
      mode="point",
  )
  evidence["indicative_assessment"] = {
      "quadrant": indicative.quadrant.value,
      "ratio_point": indicative.ratio,
      "reason": indicative.reason,
  }
  return TierResult(
      tier=tier, passed=True, verdict=Verdict.SCREEN_PASS,
      quadrant=indicative.quadrant, ratio=indicative.ratio, bar=indicative.bar,
      reason=(
          f"local screen: {-speed.point:.2f}% faster {speed} on {metric}; "
          f"indicative BD-rate {quality} (rank only). "
          + T3_QUALITY_DISCLAIMER
      ),
      summary=summary, started_ts=started, finished_ts=time.time(),
      evidence=evidence,
  )


def evaluate_t4(
    experiment: Experiment,
    holdout_results: list[EncodeResult],
    context: TierContext,
) -> TierResult:
  """The holdout tier: same measurement, clips the search never saw."""
  started = time.time()
  result = evaluate_t3(
      experiment, holdout_results, context, clip_set="holdout", tier=Tier.T4_HELDOUT
  )
  screen_tier = experiment.tier_result(Tier.T3_SCREEN)
  if screen_tier and screen_tier.summary and result.summary:
    report = gates.IntegrityReport()
    report.add(
        gates.check_holdout(
            screen_tier.summary.speed_delta, result.summary.speed_delta, label="speed"
        )
    )
    report.add(
        gates.check_holdout(
            screen_tier.summary.bdrate_yuv, result.summary.bdrate_yuv,
            label="BD-rate", shrink_threshold=0.0,
        )
    )
    result.evidence["holdout_gates"] = report.to_dict()
    if report.blocked:
      result.passed = False
      result.verdict = Verdict.INTEGRITY_FAIL
      result.reason = "; ".join(g.detail for g in report.blockers)
    else:
      for warning in report.warnings:
        result.summary.notes.append(warning.detail)
  return result


# --------------------------------------------------------------------------
# T5: CTC
# --------------------------------------------------------------------------


def evaluate_t5(experiment: Experiment, context: TierContext) -> TierResult:
  """Assess a completed CTC run: every class, independently, against the bar."""
  from .acceptance import assess_all_classes

  started = time.time()
  ctc = experiment.ctc
  if not ctc or not ctc.classes:
    return TierResult(
        tier=Tier.T5_CTC, passed=False, verdict=Verdict.PENDING,
        reason="no CTC results available", started_ts=started, finished_ts=time.time(),
    )

  per_class: dict[str, tuple[Interval, Interval]] = {}
  detail: dict = {"classes": {}, "partial": [], "metric_definition": "bdrate/pchip/PSNR-YUV"}
  for cls in ctc.classes:
    key = f"{cls.testset.upper()}"
    bd_values = [
        s.bdrate.get("PSNR-YUV", s.bdrate.get("PSNR-Y", 0.0)) for s in cls.sequences
    ]
    time_values = [s.enc_time_ratio_pct - 100.0 for s in cls.sequences]
    if bd_values:
      bd_interval = stats.bootstrap_mean_ci(bd_values, seed=f"{experiment.id}:{key}:bd")
    else:
      value = cls.average_bdrate.get("PSNR-YUV", cls.average_bdrate.get("PSNR-Y", 0.0))
      bd_interval = Interval(value, value, value, 0, "reported-average")
    if time_values:
      time_interval = stats.bootstrap_mean_ci(
          time_values, seed=f"{experiment.id}:{key}:time"
      )
    else:
      value = cls.average_enc_time_ratio_pct - 100.0
      time_interval = Interval(value, value, value, 0, "reported-average")
    per_class[key] = (bd_interval, time_interval)
    detail["classes"][key] = {
        "sequences": len(cls.sequences),
        "bdrate": [bd_interval.lo, bd_interval.point, bd_interval.hi],
        "enc_time_delta": [time_interval.lo, time_interval.point, time_interval.hi],
        "complete": cls.complete,
    }
    if not cls.complete:
      detail["partial"].append(key)

  combined, per_class_assessments = assess_all_classes(
      per_class, preset=context.preset, bars=context.bars,
      mode=context.acceptance_mode, bdrate_cap_mode=context.bdrate_cap_mode,
  )
  detail["per_class_verdict"] = {
      name: {
          "verdict": a.verdict.value, "ratio": a.ratio,
          "ratio_conservative": a.ratio_conservative, "reason": a.reason,
      }
      for name, a in per_class_assessments.items()
  }
  reason = combined.reason
  if combined.warnings:
    detail["warnings"] = combined.warnings
    reason = reason + " | " + "; ".join(combined.warnings[:2])
  if detail["partial"]:
    reason = (
        f"[PARTIAL: {', '.join(detail['partial'])} incomplete] " + reason
    )
  return TierResult(
      tier=Tier.T5_CTC,
      passed=combined.passed and not detail["partial"],
      verdict=combined.verdict,
      quadrant=combined.quadrant,
      ratio=combined.ratio,
      bar=combined.bar,
      reason=reason,
      started_ts=started, finished_ts=time.time(), evidence=detail,
  )
