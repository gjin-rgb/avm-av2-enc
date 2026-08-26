"""Turning two intervals into a verdict, conservatively.

The acceptance rule is the only place in the system where evidence becomes a
decision, so it is written to be paranoid in three specific ways that the prior
research paid for:

**1. Decisions use interval bounds, not point estimates.** A patch is credited
with the *pessimistic* end of each interval: the smallest speedup its data
supports and the largest quality cost. Round 1 of the prior effort promoted on
point estimates and returned 1 promote / 8 discard from 10; every one of the
discards had an interval that already contained "no effect".

**2. A ratio is undefined when its denominator is unresolved.** The corpus is
full of "infinite" complexity-to-efficiency ratios produced by dividing a
speedup by a BD-rate that rounded to 0.00%. Twelve of twenty-one reported
passes in one study were this artifact. Here, if the BD-rate interval contains
zero, the ratio is ``None`` and the verdict says so, rather than reporting a
number that is really a division by the reporting precision.

**3. Classes pass independently.** A patch that clears the bar on 1080p and
fails on 4K has not passed; averaging the two is how a class failure gets
shipped. The prior corpus states this rule and then violates it three times in
one report.

Sign conventions throughout, matching CTC:
  * ``bdrate`` positive = candidate needs more bits = worse.
  * ``time_delta`` positive = candidate is slower.
  * ``speedup = -time_delta``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import Interval, Quadrant, Verdict


@dataclass
class PresetBars:
  """The acceptance bar for one speed preset.

  Values follow the prior CTC practice on this codebase (S1 >= 35, S2 >= 30,
  S3 >= 25, S4 >= 20 for the speed-for-quality ratio) extended with the draft
  guide's S0 bar and absolute BD-rate caps. The caps matter: a ratio of 30
  earned by giving up 2% BD-rate is not the same trade as one earned by giving
  up 0.1%, because BD-rate costs compound when patches are combined while the
  ratio does not.
  """

  preset: int
  q3_min_ratio: float          # speedup% per unit BD-rate% given up
  q3_max_bdrate_pct: float     # absolute ceiling on the quality cost
  q2_max_ratio: float          # time% you may pay per unit BD-rate% gained


DEFAULT_BARS: dict[int, PresetBars] = {
    0: PresetBars(0, 150.0, 0.03, 20.0),
    1: PresetBars(1, 35.0, 0.15, 10.0),
    2: PresetBars(2, 30.0, 0.20, 5.0),
    3: PresetBars(3, 25.0, 0.30, 2.0),
    4: PresetBars(4, 20.0, 0.40, 1.0),
    5: PresetBars(5, 18.0, 0.50, 1.0),
    6: PresetBars(6, 15.0, 0.60, 1.0),
}

#: How the absolute BD-rate cap is applied.
#:
#: The cap exists for a real reason: BD-rate costs *add* when patches are
#: combined while ratios do not, so a patch that buys its ratio with a large
#: absolute quality cost cannot be stacked with others. But it is not a reason
#: to reject the patch on its own -- a ratio of 75 earned at 0.3% BD-rate is a
#: good patch that happens not to combine well, and the prior CTC rounds on this
#: codebase judged on ratio alone and would have discarded several real wins
#: under a hard cap.
#:
#: So the default is ``warn``: the verdict follows the ratio, and the assessment
#: records that the arm is not combinable. Set ``reject`` when assembling a
#: combination round, where the cap is exactly the right rule.
BDRATE_CAP_MODES = ("warn", "reject")

#: Below these magnitudes a measurement is treated as "not resolved" even if
#: the interval technically excludes zero -- the CTC cluster's own reporting
#: precision is 0.01% on BD-rate and roughly 1% on encode time, and the prior
#: corpus never measured the cluster's repeatability at all.
DEFAULT_BDRATE_RESOLUTION_PCT = 0.02
DEFAULT_TIME_RESOLUTION_PCT = 0.50


#: How strictly the bar is applied.
#:
#: ``point``        - apply the bar to the point estimate. This is what CTC
#:                    practice on this codebase does today, and it is the right
#:                    mode for *ranking* arms and choosing what to build next.
#: ``conservative`` - apply the bar to the pessimistic end of each interval.
#:                    Required before recommending a patch for upstream
#:                    submission, where being wrong is expensive and public.
#:
#: Both numbers are always computed and recorded; the mode only decides which
#: one the ``passed`` flag is derived from. Reporting only the strict one would
#: make the system unable to ship anything, because per-sequence BD-rate spread
#: on this codebase has a coefficient of variation near 90%; reporting only the
#: point estimate is how the prior effort promoted eight patches that did not
#: survive contact with a second measurement.
ACCEPT_MODES = ("point", "conservative")


@dataclass
class Assessment:
  """A verdict plus every number that produced it."""

  quadrant: Quadrant = Quadrant.UNRESOLVED
  verdict: Verdict = Verdict.PENDING
  passed: bool = False
  ratio: float | None = None
  ratio_conservative: float | None = None
  bar: float | None = None
  mode: str = "conservative"
  passed_point: bool = False
  passed_conservative: bool = False
  reason: str = ""
  warnings: list[str] = field(default_factory=list)
  bdrate: Interval | None = None
  time_delta: Interval | None = None

  @property
  def speedup_pct(self) -> float:
    return -self.time_delta.point if self.time_delta else 0.0


def _resolved(interval: Interval | None, floor: float) -> bool:
  """True when the interval excludes zero *and* clears the reporting floor."""
  if interval is None:
    return False
  if not interval.excludes_zero:
    return False
  return abs(interval.point) >= floor


def assess(
    bdrate: Interval | None,
    time_delta: Interval | None,
    *,
    preset: int,
    bars: dict[int, PresetBars] | None = None,
    bdrate_floor: float = DEFAULT_BDRATE_RESOLUTION_PCT,
    time_floor: float = DEFAULT_TIME_RESOLUTION_PCT,
    mechanism_requires_bit_exact: bool = False,
    mode: str = "conservative",
    bdrate_cap_mode: str = "warn",
) -> Assessment:
  """Classify one (quality, speed) result pair for one preset and class."""
  bar_set = (bars or DEFAULT_BARS).get(preset) or DEFAULT_BARS[4]
  out = Assessment(
      bdrate=bdrate, time_delta=time_delta, bar=bar_set.q3_min_ratio, mode=mode
  )

  if bdrate is None or time_delta is None:
    out.verdict = Verdict.PENDING
    out.reason = "missing measurement"
    return out

  quality_resolved = _resolved(bdrate, bdrate_floor)
  speed_resolved = _resolved(time_delta, time_floor)

  if not quality_resolved and not speed_resolved:
    out.verdict = Verdict.NO_EFFECT
    out.quadrant = Quadrant.UNRESOLVED
    out.reason = (
        f"neither axis resolved: BD-rate {bdrate}, time {time_delta}. "
        "This is not a small win, it is an unmeasured quantity."
    )
    if mechanism_requires_bit_exact:
      out.reason += " (expected for a bit-exact mechanism; judge on speed once resolved)"
    return out

  quality_better = quality_resolved and bdrate.hi < 0.0
  quality_worse = quality_resolved and bdrate.lo > 0.0
  faster = speed_resolved and time_delta.hi < 0.0
  slower = speed_resolved and time_delta.lo > 0.0

  # -- Quadrant 1: better on both axes ------------------------------------
  if quality_better and faster:
    out.quadrant = Quadrant.Q1_ABSOLUTE_WIN
    out.verdict = Verdict.CTC_PASS
    out.passed = out.passed_point = out.passed_conservative = True
    out.reason = (
        f"absolute win: BD-rate {bdrate} and time {time_delta}; "
        "no trade-off to weigh"
    )
    return out

  # -- Quadrant 4: worse on both ------------------------------------------
  if quality_worse and slower:
    out.quadrant = Quadrant.Q4_REGRESSION
    out.verdict = Verdict.REGRESSION
    out.reason = f"worse on both axes: BD-rate {bdrate}, time {time_delta}"
    return out

  # -- Quadrant 3: faster, at a quality cost ------------------------------
  if faster and quality_worse:
    out.quadrant = Quadrant.Q3_SPEED_TRADE
    speedup = -time_delta.point
    # Conservative ratio: least speedup the data supports over the largest
    # quality cost it supports. This is the number the bar is applied to.
    speedup_low = -time_delta.hi
    bdrate_high = bdrate.hi
    out.ratio = speedup / bdrate.point if bdrate.point > 0 else None
    out.ratio_conservative = (
        speedup_low / bdrate_high if bdrate_high > 0 and speedup_low > 0 else None
    )
    if bdrate_high > bar_set.q3_max_bdrate_pct:
      note = (
          f"quality cost {bdrate} exceeds the absolute cap "
          f"{bar_set.q3_max_bdrate_pct:.2f}% for speed {preset}: BD-rate costs "
          "add when patches are combined, so this arm should not be stacked "
          "with others even if it clears the ratio bar on its own"
      )
      if bdrate_cap_mode == "reject":
        out.verdict = Verdict.BELOW_BAR
        out.reason = note
        return out
      out.warnings.append(note)
    if out.ratio_conservative is None:
      out.verdict = Verdict.NO_EFFECT
      out.reason = "ratio undefined: the conservative speedup bound is not positive"
      return out
    out.passed_point = bool(out.ratio is not None and out.ratio >= bar_set.q3_min_ratio)
    out.passed_conservative = out.ratio_conservative >= bar_set.q3_min_ratio
    out.passed = out.passed_conservative if mode == "conservative" else out.passed_point
    ratio_used = out.ratio_conservative if mode == "conservative" else out.ratio
    if out.passed:
      out.verdict = Verdict.CTC_PASS
      out.reason = (
          f"ratio {ratio_used:.1f} ({mode}) clears the speed-{preset} bar of "
          f"{bar_set.q3_min_ratio:.0f}"
      )
      if not out.passed_conservative:
        out.warnings.append(
            f"point estimate clears the bar but the conservative bound "
            f"({out.ratio_conservative:.1f}) does not: not safe for upstream "
            "submission without more replication"
        )
    else:
      out.verdict = Verdict.BELOW_BAR
      out.reason = (
          f"ratio {ratio_used:.1f} ({mode}; point {out.ratio:.1f}, conservative "
          f"{out.ratio_conservative:.1f}) is below the speed-{preset} bar of "
          f"{bar_set.q3_min_ratio:.0f}"
      )
    return out

  # -- Quadrant 2: better compression, slower -----------------------------
  if quality_better and slower:
    out.quadrant = Quadrant.Q2_CODING_TRADE
    out.bar = bar_set.q2_max_ratio
    gain = -bdrate.point
    gain_low = -bdrate.hi
    out.ratio = time_delta.point / gain if gain > 0 else None
    out.ratio_conservative = (
        time_delta.hi / gain_low if gain_low > 0 and time_delta.hi > 0 else None
    )
    if out.ratio_conservative is None:
      out.verdict = Verdict.NO_EFFECT
      out.reason = "ratio undefined: the conservative quality gain is not positive"
      return out
    out.passed_point = bool(out.ratio is not None and out.ratio <= bar_set.q2_max_ratio)
    out.passed_conservative = out.ratio_conservative <= bar_set.q2_max_ratio
    out.passed = out.passed_conservative if mode == "conservative" else out.passed_point
    ratio_used = out.ratio_conservative if mode == "conservative" else out.ratio
    if out.passed:
      out.verdict = Verdict.CTC_PASS
      out.reason = (
          f"pays {ratio_used:.1f}% time per 1% BD-rate gained ({mode}), within "
          f"the speed-{preset} allowance of {bar_set.q2_max_ratio:.0f}"
      )
      if not out.passed_conservative:
        out.warnings.append(
            "point estimate clears the allowance but the conservative bound "
            "does not: not safe for upstream submission"
        )
    else:
      out.verdict = Verdict.BELOW_BAR
      out.reason = (
          f"pays {ratio_used:.1f}% time per 1% BD-rate gained ({mode}), above "
          f"the speed-{preset} allowance of {bar_set.q2_max_ratio:.0f}"
      )
    return out

  # -- One axis resolved, the other not -----------------------------------
  if faster and not quality_resolved:
    out.quadrant = (
        Quadrant.Q1_ABSOLUTE_WIN if bdrate.point <= 0 else Quadrant.Q3_SPEED_TRADE
    )
    out.verdict = Verdict.SCREEN_PASS
    out.passed = out.passed_point = out.passed_conservative = True
    out.reason = (
        f"faster by {-time_delta.point:.2f}% {time_delta} with no resolved "
        f"quality change ({bdrate}); the quality question needs a stronger test"
    )
    return out
  if slower and not quality_resolved:
    out.quadrant = (
        Quadrant.Q4_REGRESSION if bdrate.point >= 0 else Quadrant.Q2_CODING_TRADE
    )
    out.verdict = Verdict.BELOW_BAR
    out.reason = (
        f"slower by {time_delta.point:.2f}% {time_delta} with no resolved "
        f"quality gain ({bdrate})"
    )
    return out
  if quality_better and not speed_resolved:
    out.quadrant = Quadrant.Q2_CODING_TRADE
    out.verdict = Verdict.SCREEN_PASS
    out.passed = out.passed_point = out.passed_conservative = True
    out.reason = (
        f"compression gain {bdrate} with no resolved time change ({time_delta})"
    )
    return out
  out.quadrant = Quadrant.Q4_REGRESSION
  out.verdict = Verdict.BELOW_BAR
  out.reason = f"quality cost {bdrate} with no resolved speedup ({time_delta})"
  return out


def assess_all_classes(
    per_class: dict[str, tuple[Interval, Interval]],
    *,
    preset: int,
    bars: dict[int, PresetBars] | None = None,
    mode: str = "conservative",
    bdrate_cap_mode: str = "warn",
) -> tuple[Assessment, dict[str, Assessment]]:
  """Assess each CTC class independently and require all of them to pass.

  Returns the combined assessment and the per-class detail. The combined
  verdict is the *worst* class, never an average: on this codebase A1 (4K) and
  A2 (1080p) disagree on the ratio by up to a factor of three in both
  directions, and ten of the corpus's arms are class-asymmetric.
  """
  details = {
      name: assess(
          bd, time, preset=preset, bars=bars, mode=mode,
          bdrate_cap_mode=bdrate_cap_mode,
      )
      for name, (bd, time) in sorted(per_class.items())
  }
  if not details:
    return (Assessment(reason="no class results"), details)

  failing = {n: a for n, a in details.items() if not a.passed}
  worst_name = min(
      details,
      key=lambda n: (
          details[n].passed,
          details[n].ratio_conservative
          if details[n].ratio_conservative is not None
          else -1e9,
      ),
  )
  combined = Assessment(
      quadrant=details[worst_name].quadrant,
      verdict=details[worst_name].verdict,
      mode=mode,
      passed=not failing,
      passed_point=all(a.passed_point for a in details.values()),
      passed_conservative=all(a.passed_conservative for a in details.values()),
      ratio=details[worst_name].ratio,
      ratio_conservative=details[worst_name].ratio_conservative,
      bar=details[worst_name].bar,
      bdrate=details[worst_name].bdrate,
      time_delta=details[worst_name].time_delta,
      warnings=[w for a in details.values() for w in a.warnings],
  )
  if failing:
    combined.reason = (
        "class "
        + ", ".join(sorted(failing))
        + " did not clear the bar: "
        + "; ".join(f"{n}: {a.reason}" for n, a in sorted(failing.items()))
    )
  else:
    combined.reason = "every class cleared the bar independently: " + "; ".join(
        f"{n}: {a.reason}" for n, a in sorted(details.items())
    )
  return (combined, details)


def marginal_ratio(
    base_speedup_pct: float,
    base_bdrate_pct: float,
    variant_speedup_pct: float,
    variant_bdrate_pct: float,
) -> tuple[float | None, str]:
  """Ratio of the decisions a variant *removed* relative to its parent.

  The technique that ended two rounds of guesswork on this codebase. When a
  variant tightens a heuristic, it gives up some speed and recovers some
  quality; the ratio of those two deltas is the ratio of the decisions the
  change removed. Removing decisions raises the overall ratio only when their
  marginal ratio is *below* the ratio already achieved -- otherwise you have
  removed the good prunes and kept the bad ones, which is exactly what happened
  when a block-size floor was raised on 1080p.

  Returns ``(marginal, explanation)``; ``marginal`` is ``None`` when the
  quality recovered is zero, which is itself the finding: the removed decisions
  cost nothing, so removing them was pure loss.
  """
  speed_given_up = base_speedup_pct - variant_speedup_pct
  quality_recovered = base_bdrate_pct - variant_bdrate_pct
  if abs(quality_recovered) < 1e-9:
    return (
        None,
        f"gave up {speed_given_up:+.2f}% speed and recovered no BD-rate: the "
        "decisions this variant removed were free. Removing them was pure loss.",
    )
  marginal = speed_given_up / quality_recovered
  base_ratio = base_speedup_pct / base_bdrate_pct if base_bdrate_pct else float("inf")
  direction = "raises" if marginal < base_ratio else "lowers"
  return (
      marginal,
      f"the removed decisions were worth {marginal:.1f} speed per unit BD-rate; "
      f"the parent already achieves {base_ratio:.1f}, so this change {direction} "
      "the overall ratio",
  )


def exemption_analysis(
    per_sequence_bdrate: dict[str, float],
    per_sequence_speedup: dict[str, float],
    *,
    bar: float,
) -> list[dict]:
  """Which single clip, if exempted, would most improve the ratio.

  Per-sequence spread is not a footnote. On this codebase the speed benefit of
  a pruning heuristic is nearly uniform across content (CV ~12%) while its
  quality cost varies sevenfold (CV ~89%), so one or two clips routinely carry
  most of the damage while contributing an average share of the speedup.
  Exempting one clip in eight has twice been enough to clear a bar the mean
  failed. That is a *design signal*: it says a content gate exists and is worth
  building, not that the clip should be dropped from the test set.
  """
  names = sorted(set(per_sequence_bdrate) & set(per_sequence_speedup))
  if len(names) < 3:
    return []
  total_bd = sum(per_sequence_bdrate[n] for n in names)
  total_speed = sum(per_sequence_speedup[n] for n in names)
  out = []
  for name in names:
    n_rest = len(names) - 1
    bd_rest = (total_bd - per_sequence_bdrate[name]) / n_rest
    speed_rest = (total_speed - per_sequence_speedup[name]) / n_rest
    ratio_rest = speed_rest / bd_rest if bd_rest > 0 else None
    out.append({
        "sequence": name,
        "bdrate": per_sequence_bdrate[name],
        "speedup": per_sequence_speedup[name],
        "ratio_without": ratio_rest,
        "clears_bar_without": bool(ratio_rest is not None and ratio_rest >= bar),
        "bdrate_share": (per_sequence_bdrate[name] / total_bd) if total_bd else 0.0,
        "speedup_share": (per_sequence_speedup[name] / total_speed) if total_speed else 0.0,
    })
  out.sort(key=lambda row: (row["ratio_without"] is None, -(row["ratio_without"] or 0.0)))
  return out
