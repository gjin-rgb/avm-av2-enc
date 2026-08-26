"""The statistics that decide whether a measured difference is real.

This module exists because of a specific, documented failure in the prior
research on this codebase: a first round of speed measurements reported ten
speedups, of which six were smaller than the machine's own run-to-run noise.
They were written into a results table as wins, and two later rounds were spent
reasoning about numbers that were never measurements at all.

The rules encoded here follow from that:

* An effect that the design cannot resolve is reported as ``NO_EFFECT``, never
  as a small win. :class:`Interval` carries the bounds, and callers branch on
  ``excludes_zero``, not on the point estimate.
* Every comparison reports the minimum detectable effect alongside the result,
  so "we found nothing" can be distinguished from "we could not have found it".
* Timing is compared in log space and paired by (sequence, QP, slot). Encode
  times across clips span two orders of magnitude; an unpaired test on raw
  seconds is dominated by which clips happen to be in the set.
* Everything is deterministic. The bootstrap is seeded from the experiment id,
  so re-running an analysis on the same data cannot change a verdict.

No numpy or scipy: the same code must run on a Cloudtop, a laptop and inside a
minimal CI container, and a verdict that depends on which distribution is
installed is not a verdict.
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from typing import Iterable, Sequence

from ..core.models import Interval


# --------------------------------------------------------------------------
# Student-t quantiles, computed rather than tabulated
# --------------------------------------------------------------------------


def _log_beta(a: float, b: float) -> float:
  return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float) -> float:
  """Continued fraction for the incomplete beta function (Lentz's method)."""
  tiny = 1e-30
  qab, qap, qam = a + b, a + 1.0, a - 1.0
  c = 1.0
  d = 1.0 - qab * x / qap
  if abs(d) < tiny:
    d = tiny
  d = 1.0 / d
  h = d
  for m in range(1, 300):
    m2 = 2 * m
    aa = m * (b - m) * x / ((qam + m2) * (a + m2))
    d = 1.0 + aa * d
    if abs(d) < tiny:
      d = tiny
    c = 1.0 + aa / c
    if abs(c) < tiny:
      c = tiny
    d = 1.0 / d
    h *= d * c
    aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
    d = 1.0 + aa * d
    if abs(d) < tiny:
      d = tiny
    c = 1.0 + aa / c
    if abs(c) < tiny:
      c = tiny
    d = 1.0 / d
    delta = d * c
    h *= delta
    if abs(delta - 1.0) < 3e-15:
      break
  return h


def incomplete_beta(a: float, b: float, x: float) -> float:
  """Regularised incomplete beta I_x(a, b)."""
  if x <= 0.0:
    return 0.0
  if x >= 1.0:
    return 1.0
  front = math.exp(a * math.log(x) + b * math.log1p(-x) - _log_beta(a, b))
  if x < (a + 1.0) / (a + b + 2.0):
    return front * _betacf(a, b, x) / a
  return 1.0 - math.exp(
      b * math.log1p(-x) + a * math.log(x) - _log_beta(a, b)
  ) * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t: float, df: float) -> float:
  if df <= 0:
    return float("nan")
  x = df / (df + t * t)
  prob = 0.5 * incomplete_beta(0.5 * df, 0.5, x)
  return 1.0 - prob if t > 0 else prob


def student_t_ppf(p: float, df: float) -> float:
  """Inverse Student-t CDF by bisection.

  Bisection rather than a lookup table: the Welch-Satterthwaite degrees of
  freedom for two arms with unequal spread is fractional and often non-integer
  and small (n=3 per arm is normal here, because each observation is a full
  encode). Rounding df down to the nearest tabulated value widens every
  interval slightly, which biases the system toward calling real effects noise.
  """
  if df <= 0 or not 0.0 < p < 1.0:
    return float("nan")
  if p == 0.5:
    return 0.0
  lo, hi = -1e3, 1e3
  for _ in range(200):
    mid = 0.5 * (lo + hi)
    if student_t_cdf(mid, df) < p:
      lo = mid
    else:
      hi = mid
    if hi - lo < 1e-12:
      break
  return 0.5 * (lo + hi)


def t_crit_95(df: float) -> float:
  return student_t_ppf(0.975, df)


# --------------------------------------------------------------------------
# Descriptive helpers
# --------------------------------------------------------------------------


def mean(values: Sequence[float]) -> float:
  return statistics.fmean(values) if values else 0.0


def stdev(values: Sequence[float]) -> float:
  return statistics.stdev(values) if len(values) > 1 else 0.0


def cv_pct(values: Sequence[float]) -> float:
  """Coefficient of variation in percent: the measured noise floor of an arm."""
  m = mean(values)
  return 100.0 * stdev(values) / m if m else 0.0


# --------------------------------------------------------------------------
# Two-arm comparisons
# --------------------------------------------------------------------------


def welch_pct_ci(anchor: Sequence[float], candidate: Sequence[float]) -> Interval:
  """Percent change of ``candidate`` relative to ``anchor``, unpaired.

  Positive means the candidate's value is larger. For a cost metric that reads
  as "slower".
  """
  if not anchor or not candidate:
    return Interval(0.0, float("-inf"), float("inf"), 0, "welch/empty")
  am, bm = mean(anchor), mean(candidate)
  if am == 0.0:
    return Interval(0.0, float("-inf"), float("inf"), 0, "welch/zero-anchor")
  na, nb = len(anchor), len(candidate)
  if na < 2 or nb < 2:
    return Interval(100.0 * (bm - am) / am, float("-inf"), float("inf"),
                    min(na, nb), "welch/underpowered")
  va, vb = statistics.variance(anchor), statistics.variance(candidate)
  se = math.sqrt(va / na + vb / nb)
  if se == 0.0:
    point = 100.0 * (bm - am) / am
    return Interval(point, point, point, min(na, nb), "welch/zero-variance")
  # Welch-Satterthwaite. The arms routinely have very different spread: a patch
  # that prunes work also prunes the variance of the work it did not do.
  df = (va / na + vb / nb) ** 2 / (
      (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
  )
  half = t_crit_95(df) * se
  return Interval(
      point=100.0 * (bm - am) / am,
      lo=100.0 * (bm - am - half) / am,
      hi=100.0 * (bm - am + half) / am,
      n=min(na, nb),
      method="welch",
  )


def paired_log_ratio_ci(pairs: Sequence[tuple[float, float]]) -> Interval:
  """Percent change from paired (anchor, candidate) observations, in log space.

  Pairing is what makes a screening pass sensitive. Encode cost varies by 100x
  across the clip set and by 5x across the QP ladder; those are nuisance
  factors that cancel exactly when each candidate observation is differenced
  against the anchor observation taken on the *same* clip, QP and interleave
  slot. Log space because the effect a speed patch has is multiplicative -- it
  removes a fraction of the work, not a fixed number of seconds.

  The returned interval is on the multiplicative change, converted to percent:
  ``+3.0%`` means the candidate costs 3% more.
  """
  usable = [(a, b) for a, b in pairs if a > 0.0 and b > 0.0]
  if not usable:
    return Interval(0.0, float("-inf"), float("inf"), 0, "paired/empty")
  diffs = [math.log(b) - math.log(a) for a, b in usable]
  n = len(diffs)
  m = mean(diffs)
  if n < 2:
    return Interval(100.0 * (math.exp(m) - 1.0), float("-inf"), float("inf"),
                    n, "paired/underpowered")
  sd = stdev(diffs)
  if sd == 0.0:
    point = 100.0 * (math.exp(m) - 1.0)
    return Interval(point, point, point, n, "paired/zero-variance")
  half = t_crit_95(n - 1) * sd / math.sqrt(n)
  return Interval(
      point=100.0 * (math.exp(m) - 1.0),
      lo=100.0 * (math.exp(m - half) - 1.0),
      hi=100.0 * (math.exp(m + half) - 1.0),
      n=n,
      method="paired-log",
  )


def bootstrap_mean_ci(
    values: Sequence[float], *, seed: str = "av2ra", n_boot: int = 4000
) -> Interval:
  """Percentile bootstrap CI for a mean over a small, non-normal sample.

  Used for aggregating per-sequence BD-rate. Twelve sequences is too few to
  lean on a normal approximation, and the distribution across clips is skewed:
  most clips move a little and one moves a lot. Resampling clips also gives the
  interval the right meaning -- it is the uncertainty over *which clips you
  happened to screen on*, which is exactly the risk being managed.
  """
  vals = [float(v) for v in values]
  if not vals:
    return Interval(0.0, float("-inf"), float("inf"), 0, "bootstrap/empty")
  if len(vals) == 1:
    return Interval(vals[0], float("-inf"), float("inf"), 1, "bootstrap/n=1")
  rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16))
  n = len(vals)
  means = []
  for _ in range(n_boot):
    means.append(sum(vals[rng.randrange(n)] for _ in range(n)) / n)
  means.sort()
  lo = means[int(0.025 * (n_boot - 1))]
  hi = means[int(0.975 * (n_boot - 1))]
  return Interval(mean(vals), lo, hi, n, f"bootstrap/{n_boot}")


# --------------------------------------------------------------------------
# Design power
# --------------------------------------------------------------------------


def mde_pct(noise_cv_pct: float, n_per_arm: int) -> float:
  """Smallest effect a two-arm design with this noise and n could resolve.

  Report this next to every null result. "No effect" and "no power" look
  identical in a results table and mean opposite things.
  """
  if n_per_arm < 2 or noise_cv_pct <= 0.0:
    return float("inf")
  return t_crit_95(2 * n_per_arm - 2) * noise_cv_pct * math.sqrt(2.0 / n_per_arm)


def paired_mde_pct(diff_sd_log: float, n_pairs: int) -> float:
  """MDE for the paired design, given the SD of paired log differences."""
  if n_pairs < 2 or diff_sd_log <= 0.0:
    return float("inf")
  half = t_crit_95(n_pairs - 1) * diff_sd_log / math.sqrt(n_pairs)
  return 100.0 * (math.exp(half) - 1.0)


def required_reps(noise_cv_pct: float, target_effect_pct: float) -> int:
  """Repetitions per arm needed to resolve ``target_effect_pct``."""
  if target_effect_pct <= 0.0 or noise_cv_pct <= 0.0:
    return 1
  n = 2
  for _ in range(200):
    if mde_pct(noise_cv_pct, n) <= target_effect_pct:
      return n
    n += 1
  return n


def holm_bonferroni(pvalues: Sequence[float], alpha: float = 0.05) -> list[bool]:
  """Step-down multiple-comparison correction.

  An autonomous agent runs dozens of arms per day. At alpha=0.05 and thirty
  arms of pure noise, one or two "significant" results per day are guaranteed,
  and the agent would faithfully promote them. Any batch decision -- a sweep, a
  set of variants, a screen across several presets -- goes through this.
  """
  indexed = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
  out = [False] * len(pvalues)
  m = len(pvalues)
  for rank, idx in enumerate(indexed):
    if pvalues[idx] <= alpha / (m - rank):
      out[idx] = True
    else:
      break
  return out


def paired_p_value(pairs: Sequence[tuple[float, float]]) -> float:
  """Two-sided p-value for the paired log-ratio test."""
  usable = [(a, b) for a, b in pairs if a > 0.0 and b > 0.0]
  if len(usable) < 2:
    return 1.0
  diffs = [math.log(b) - math.log(a) for a, b in usable]
  n = len(diffs)
  sd = stdev(diffs)
  if sd == 0.0:
    return 0.0 if mean(diffs) != 0.0 else 1.0
  t = mean(diffs) / (sd / math.sqrt(n))
  return 2.0 * (1.0 - student_t_cdf(abs(t), n - 1))


def summarize_noise(values_by_group: dict[str, Sequence[float]]) -> dict[str, float]:
  """Per-group coefficient of variation, for a harness self-check.

  The harness measures its own noise floor before it measures anything else. If
  the anchor arm's CV on a given metric exceeds the effect size the tier is
  meant to resolve, the tier reports UNDERPOWERED rather than a verdict.
  """
  return {name: cv_pct(vals) for name, vals in values_by_group.items()}


def geometric_mean(values: Iterable[float]) -> float:
  vals = [v for v in values if v > 0.0]
  if not vals:
    return 0.0
  return math.exp(sum(math.log(v) for v in vals) / len(vals))
