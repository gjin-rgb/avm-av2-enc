"""Bjontegaard-Delta rate, computed the way AV2 CTC computes it.

The reference is ``tools/convexhull_framework/src/CalcBDRate.py`` in the AVM
tree, which is what the CTC spreadsheets and the Cloud EDA comparison tooling
use. This module reproduces that algorithm exactly -- same sort order, same
duplicate handling, same overlap interval, same 100-sample trapezoid, same
final rounding -- so that a local screening number and a CTC number differ
because the *encodes* differed, never because the arithmetic did.

Where this module goes beyond the reference is in what it returns around the
number: an aggregate across sequences with a bootstrap interval, the worst
single sequence, and an explicit failure value when the curves do not overlap
or are non-monotonic. A single mean BD-rate that hides one badly regressed clip
is a regression risk being reported as a win; the prior research on this
codebase called that out explicitly and it is enforced here.

Sign convention, as in CTC: **positive BD-rate is worse** (the candidate needs
more bits for the same quality).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from ..core.models import BDRateResult, EncodeResult, Interval
from . import stats
from .pchip import pchip_interpolate

# Quality metrics whose curves saturate near a ceiling and can wander
# non-monotonically there. CalcBDRate.py truncates those tails.
_SATURATING = ("VMAF", "VMAF_Y", "VMAF_Y-NEG", "vmaf", "vmaf-neg", "VMAF-NEG")

NON_MONOTONIC = "non-monotonic"
NO_OVERLAP = "no-overlap"
TOO_FEW_POINTS = "too-few-points"


def _filter_saturating(pairs: list[tuple[float, float]]) -> list[tuple[float, float]]:
  """Drop the saturated tail of a metric that has stopped discriminating."""
  out: list[tuple[float, float]] = []
  for i, pair in enumerate(sorted(pairs, key=lambda p: (p[0], p[1]))):
    if (
        i != 0
        and pair[0] >= out[-1][0]
        and pair[1] < out[-1][1]
        and out[-1][1] >= 99.5
    ):
      break
    out.append(pair)
  return out


def _is_non_decreasing(values: Sequence[float]) -> bool:
  return all(a <= b for a, b in zip(values, values[1:]))


def bd_rate(
    anchor_rates: Sequence[float],
    anchor_quality: Sequence[float],
    cand_rates: Sequence[float],
    cand_quality: Sequence[float],
    *,
    metric: str = "PSNR-Y",
) -> tuple[float, str]:
  """Return ``(bd_rate_percent, error)``; ``error`` is empty on success.

  Mirrors ``CalcBDRate.BD_RATE(qty_type, br1, qty1, br2, qty2)`` with curve 1 as
  the anchor.
  """
  pairs_a = [
      (float(r), float(q))
      for r, q in zip(anchor_rates, anchor_quality)
      if r not in ("", None) and q not in ("", None)
  ]
  pairs_b = [
      (float(r), float(q))
      for r, q in zip(cand_rates, cand_quality)
      if r not in ("", None) and q not in ("", None)
  ]
  if any(token in metric.upper() for token in ("VMAF",)):
    pairs_a = _filter_saturating(pairs_a)
    pairs_b = _filter_saturating(pairs_b)

  # Sort by quality then rate, exactly as the reference does.
  pairs_a.sort(key=lambda p: (p[1], p[0]))
  pairs_b.sort(key=lambda p: (p[1], p[0]))
  if len(pairs_a) < 2 or len(pairs_b) < 2:
    return (float("nan"), TOO_FEW_POINTS)

  if not (
      _is_non_decreasing([p[0] for p in pairs_a])
      and _is_non_decreasing([p[1] for p in pairs_a])
      and _is_non_decreasing([p[0] for p in pairs_b])
      and _is_non_decreasing([p[1] for p in pairs_b])
  ):
    # A rate that falls as quality rises means the RD points are not on a
    # coherent curve: usually a failed encode, a mixed-up QP mapping, or a
    # patch whose behaviour changes with QP in a way that breaks the ladder.
    return (float("nan"), NON_MONOTONIC)

  try:
    log_rate_a = [math.log(p[0]) for p in pairs_a]
    log_rate_b = [math.log(p[0]) for p in pairs_b]
  except ValueError:
    return (float("nan"), "invalid-rate")
  quality_a = [100.0 if math.isinf(p[1]) else p[1] for p in pairs_a]
  quality_b = [100.0 if math.isinf(p[1]) else p[1] for p in pairs_b]

  # Equal quality values would make the interpolation abscissa non-strict;
  # the reference drops the later (higher-rate) duplicate.
  def _dedupe(quality: list[float], log_rate: list[float]) -> None:
    for idx in reversed([i for i in range(1, len(quality)) if quality[i - 1] == quality[i]]):
      del quality[idx]
      del log_rate[idx]

  _dedupe(quality_a, log_rate_a)
  _dedupe(quality_b, log_rate_b)
  if len(quality_a) < 2 or len(quality_b) < 2:
    return (float("nan"), TOO_FEW_POINTS)

  lo = max(min(quality_a), min(quality_b))
  hi = min(max(quality_a), max(quality_b))
  if lo >= hi:
    # Disjoint quality ranges. This happens when a patch shifts the operating
    # points enough that the QP ladder no longer covers a common quality band;
    # the honest answer is "not comparable", not an extrapolated number.
    return (float("nan"), NO_OVERLAP)

  samples = [lo + (hi - lo) * i / 99.0 for i in range(100)]
  interval = (hi - lo) / 99.0
  curve_a = pchip_interpolate(quality_a, log_rate_a, samples)
  curve_b = pchip_interpolate(quality_b, log_rate_b, samples)

  def _trapezoid(values: list[float]) -> float:
    total = 0.0
    for i in range(len(values) - 1):
      total += 0.5 * (values[i] + values[i + 1]) * interval
    return total

  avg_exp_diff = (_trapezoid(curve_b) - _trapezoid(curve_a)) / (hi - lo)
  return (round((math.exp(avg_exp_diff) - 1.0) * 100.0, 4), "")


# --------------------------------------------------------------------------
# Metric extraction from encode results
# --------------------------------------------------------------------------

#: Which field of :class:`EncodeResult` backs each CTC metric name.
METRIC_FIELDS = {
    "PSNR-Y": "psnr_y",
    "PSNR-U": "psnr_u",
    "PSNR-V": "psnr_v",
    "PSNR-YUV": "psnr_overall",
    "PSNR-AVG": "psnr_avg",
}


def curve_for(
    results: Sequence[EncodeResult], sequence: str, arm: str, metric: str
) -> tuple[list[float], list[float]]:
  """Collect the RD curve for one clip and one arm, averaged over repetitions.

  Repetitions of the same (clip, QP, arm) must be identical in rate and PSNR --
  the encoder is deterministic for a fixed thread configuration. Averaging is
  therefore a no-op that also catches the case where it is *not* a no-op: a
  spread here means the encode is nondeterministic, which invalidates every
  quality number in the pass. :func:`check_determinism` reports it.
  """
  field_name = METRIC_FIELDS.get(metric, "psnr_y")
  by_qp: dict[int, list[EncodeResult]] = {}
  for res in results:
    if res.sequence == sequence and res.arm == arm and res.ok:
      by_qp.setdefault(res.qp, []).append(res)
  rates, qualities = [], []
  for qp in sorted(by_qp):
    group = by_qp[qp]
    rates.append(stats.mean([r.bitrate_kbps for r in group]))
    qualities.append(stats.mean([getattr(r, field_name) for r in group]))
  return rates, qualities


def check_determinism(results: Sequence[EncodeResult]) -> list[str]:
  """Report any (clip, QP, arm) whose repetitions disagree on the bitstream.

  A quality difference between two runs of the same binary on the same input is
  not noise to be averaged away: it means the encode depends on thread timing,
  and no BD-rate taken from it is reproducible.
  """
  seen: dict[tuple[str, int, str], set[str]] = {}
  for res in results:
    if not res.ok or not res.bitstream_md5:
      continue
    seen.setdefault((res.sequence, res.qp, res.arm), set()).add(res.bitstream_md5)
  return [
      f"nondeterministic encode: {seq} qp={qp} arm={arm} produced {len(md5s)} distinct bitstreams"
      for (seq, qp, arm), md5s in sorted(seen.items())
      if len(md5s) > 1
  ]


@dataclass
class BDRateAggregate:
  """BD-rate across a clip set, with the spread that a mean would hide."""

  metric: str
  per_sequence: dict[str, float] = field(default_factory=dict)
  errors: dict[str, str] = field(default_factory=dict)
  interval: Interval | None = None
  worst_sequence: str = ""
  worst_value: float = 0.0

  @property
  def mean(self) -> float:
    return self.interval.point if self.interval else float("nan")

  @property
  def ok(self) -> bool:
    return bool(self.per_sequence) and not self.errors


def aggregate_bdrate(
    results: Sequence[EncodeResult],
    *,
    metric: str = "PSNR-Y",
    seed: str = "av2ra",
) -> BDRateAggregate:
  """BD-rate per sequence, then a bootstrap interval over sequences."""
  sequences = sorted({r.sequence for r in results})
  agg = BDRateAggregate(metric=metric)
  for sequence in sequences:
    ra, qa = curve_for(results, sequence, "anchor", metric)
    rb, qb = curve_for(results, sequence, "candidate", metric)
    value, error = bd_rate(ra, qa, rb, qb, metric=metric)
    if error:
      agg.errors[sequence] = error
    else:
      agg.per_sequence[sequence] = value
  if agg.per_sequence:
    agg.interval = stats.bootstrap_mean_ci(
        list(agg.per_sequence.values()), seed=f"{seed}:{metric}"
    )
    agg.worst_sequence = max(agg.per_sequence, key=lambda s: agg.per_sequence[s])
    agg.worst_value = agg.per_sequence[agg.worst_sequence]
  return agg


def detailed_results(
    results: Sequence[EncodeResult], *, metric: str = "PSNR-Y"
) -> list[BDRateResult]:
  """Per-sequence records including the curves, for the experiment report."""
  out = []
  for sequence in sorted({r.sequence for r in results}):
    ra, qa = curve_for(results, sequence, "anchor", metric)
    rb, qb = curve_for(results, sequence, "candidate", metric)
    value, error = bd_rate(ra, qa, rb, qb, metric=metric)
    out.append(
        BDRateResult(
            sequence=sequence,
            metric=metric,
            value=0.0 if error else value,
            ok=not error,
            error=error,
            anchor_points=list(zip(ra, qa)),
            candidate_points=list(zip(rb, qb)),
            overlap_lo=max(min(qa, default=0.0), min(qb, default=0.0)),
            overlap_hi=min(max(qa, default=0.0), max(qb, default=0.0)),
        )
    )
  return out
