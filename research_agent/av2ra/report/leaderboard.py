"""Leaderboard, promotion frontier and daily digest.

The leaderboard answers "where does everything stand"; the frontier answers
"what is worth building next"; the digest answers "what happened while I was
away". All three read the registry and the ledger and add nothing of their own,
so a number in a digest can always be traced to the row that produced it.

The frontier is the one that repays explanation. A ranked list of results is not
a research plan: what a plan needs is the set of arms that are *not dominated* --
each one better than every other on at least one axis -- plus the open threads
whose next step is already determined by the evidence. The prior effort produced
its best result by noticing that a failing bundled feature had cheap members,
which is a frontier observation, not a leaderboard one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..core.ledger import Ledger
from ..core.models import Experiment, Tier, Verdict
from ..core.registry import Registry


@dataclass
class Row:
  experiment: Experiment
  ratio: float | None
  bar: float | None
  speedup: float | None
  bdrate: float | None
  tier: str
  stale: bool = False

  @property
  def margin(self) -> float | None:
    if self.ratio is None or not self.bar:
      return None
    return self.ratio - self.bar


def _best_tier(experiment: Experiment):
  for tier in (Tier.T5_CTC, Tier.T4_HELDOUT, Tier.T3_SCREEN, Tier.T2_COMPLEXITY):
    result = experiment.tier_result(tier)
    if result:
      return result
  return None


def rows(registry: Registry, *, limit: int = 200, current_base: str = "") -> list[Row]:
  out = []
  for experiment in registry.query(limit=limit):
    tier = _best_tier(experiment)
    speedup = bdrate = None
    if tier and tier.summary:
      if tier.summary.speed_delta:
        speedup = -tier.summary.speed_delta.point
      interval = tier.summary.bdrate_yuv
      if interval:
        bdrate = interval.point
    if experiment.ctc and experiment.ctc.classes:
      worst = min(experiment.ctc.classes, key=lambda c: c.speedup_pct)
      speedup = worst.speedup_pct
      bdrate = worst.average_bdrate.get(
          "PSNR-YUV", worst.average_bdrate.get("PSNR-Y", bdrate)
      )
    out.append(
        Row(
            experiment=experiment,
            ratio=tier.ratio if tier else None,
            bar=tier.bar if tier else None,
            speedup=speedup, bdrate=bdrate,
            tier=tier.tier.value if tier else "-",
            stale=bool(current_base and experiment.base_sha and experiment.base_sha != current_base),
        )
    )
  return out


def leaderboard(registry: Registry, *, limit: int = 40, current_base: str = "") -> str:
  ranked = [r for r in rows(registry, current_base=current_base)]
  ranked.sort(key=lambda r: (r.ratio is None, -(r.ratio or 0.0)))
  lines = [
      "id        verdict           tier          ratio  bar   speed%   bd%     subsystem      title",
      "-" * 118,
  ]
  for row in ranked[:limit]:
    experiment = row.experiment
    lines.append(
        f"{experiment.id:<9} {experiment.verdict.value:<17} {row.tier:<13} "
        f"{_num(row.ratio, 5, 1)} {_num(row.bar, 4, 0)}  "
        f"{_num(row.speedup, 7, 2)} {_num(row.bdrate, 6, 2)}  "
        f"{experiment.hypothesis.subsystem:<14} "
        f"{experiment.hypothesis.title[:44]}"
        + ("   [STALE BASE]" if row.stale else "")
    )
  if not ranked:
    lines.append("(no experiments yet)")
  lines.append("")
  lines.append(
      "Ratio is speedup% per 1% BD-rate given up, computed conservatively from "
      "interval bounds. A ratio from a local screen ranks; it does not estimate."
  )
  return "\n".join(lines)


def promotion_frontier(registry: Registry, *, current_base: str = "") -> str:
  """Non-dominated arms plus the open threads whose next move is already decided."""
  candidates = [
      r for r in rows(registry, current_base=current_base)
      if r.speedup is not None and r.bdrate is not None
  ]
  frontier: list[Row] = []
  for row in candidates:
    dominated = any(
        other is not row
        and (other.speedup or 0) >= (row.speedup or 0)
        and (other.bdrate if other.bdrate is not None else 1e9) <= (row.bdrate or 1e9)
        and (
            (other.speedup or 0) > (row.speedup or 0)
            or (other.bdrate or 0) < (row.bdrate or 0)
        )
        for other in candidates
    )
    if not dominated:
      frontier.append(row)
  frontier.sort(key=lambda r: -(r.speedup or 0.0))

  lines = ["PROMOTION FRONTIER (arms not dominated on both axes)", ""]
  if not frontier:
    lines.append("  (nothing measured on both axes yet)")
  for row in frontier:
    lines.append(
        f"  {row.experiment.id:<9} {row.speedup:+7.2f}% speed  {row.bdrate:+6.2f}% BD  "
        f"ratio {_num(row.ratio, 5, 1)}  {row.experiment.hypothesis.title[:50]}"
        + ("  [STALE BASE]" if row.stale else "")
    )
  lines += ["", "OPEN THREADS", ""]
  open_ones = [
      e for e in registry.query(limit=200)
      if e.is_alive and e.verdict not in (Verdict.PENDING,)
  ]
  if not open_ones:
    lines.append("  (none)")
  for experiment in open_ones[:20]:
    lines.append(
        f"  {experiment.id:<9} {experiment.status.value:<14} {experiment.verdict.value:<16} "
        f"{experiment.hypothesis.title[:50]}"
    )
  return "\n".join(lines)


@dataclass
class DigestData:
  window_hours: float
  new_experiments: list[Experiment] = field(default_factory=list)
  completed: list[Experiment] = field(default_factory=list)
  promoted: list[Experiment] = field(default_factory=list)
  killed: list[Experiment] = field(default_factory=list)
  decisions: list[dict] = field(default_factory=list)
  lessons: list[str] = field(default_factory=list)
  ctc_slots: float = 0.0
  conflicts: int = 0
  health: list[str] = field(default_factory=list)


def collect_digest(
    registry: Registry, ledger: Ledger, *, hours: float = 24.0,
    health_lines: list[str] | None = None,
) -> DigestData:
  since = time.time() - hours * 3600.0
  data = DigestData(window_hours=hours, health=health_lines or [])
  for experiment in registry.query(limit=500):
    if experiment.created_ts >= since:
      data.new_experiments.append(experiment)
    if experiment.updated_ts >= since and not experiment.is_alive:
      data.completed.append(experiment)
      if experiment.verdict in (Verdict.PROMOTE, Verdict.CTC_PASS):
        data.promoted.append(experiment)
      elif experiment.verdict.is_terminal_failure or experiment.verdict in (
          Verdict.NO_EFFECT, Verdict.BELOW_BAR
      ):
        data.killed.append(experiment)
  data.decisions = [d for d in ledger.entries() if d.get("ts", 0) >= since]
  data.lessons = [
      lesson for entry in data.decisions for lesson in (entry.get("lessons") or [])
  ]
  data.ctc_slots = registry.ctc_slots_used(since_ts=since)
  data.conflicts = len(registry.conflicting_measurements())
  return data


def render_digest(data: DigestData, *, title: str = "AV2 research digest") -> str:
  lines = [
      f"{title} - last {data.window_hours:.0f}h",
      "=" * 72,
      "",
      f"proposed {len(data.new_experiments)} | completed {len(data.completed)} | "
      f"promoted {len(data.promoted)} | killed {len(data.killed)} | "
      f"CTC slots spent {data.ctc_slots:.1f}",
      "",
  ]
  if data.health:
    lines += ["HEALTH", ""] + [f"  {line}" for line in data.health] + [""]
  if data.conflicts:
    lines += [
        "ATTENTION",
        "",
        f"  {data.conflicts} measurement conflict(s): the same quantity was "
        "measured twice under the same conditions and disagreed. That is either "
        "a repeatability problem in the back end or a provenance bug in the "
        "harness, and it needs a human.",
        "",
    ]
  if data.promoted:
    lines += ["CLEARED THE BAR", ""]
    for experiment in data.promoted:
      tier = experiment.tier_result(Tier.T5_CTC)
      lines.append(
          f"  {experiment.id} {experiment.hypothesis.title}"
          + (f"\n      {tier.reason[:300]}" if tier else "")
      )
    lines.append("")
  if data.killed:
    lines += ["KILLED", ""]
    for experiment in data.killed[:12]:
      lines.append(
          f"  {experiment.id} [{experiment.verdict.value}] {experiment.hypothesis.title}"
      )
      for lesson in experiment.lessons[:1]:
        lines.append(f"      {lesson[:200]}")
    lines.append("")
  if data.lessons:
    lines += ["LESSONS RECORDED", ""]
    seen = set()
    for lesson in data.lessons:
      if lesson in seen:
        continue
      seen.add(lesson)
      lines.append(f"  - {lesson[:220]}")
    lines.append("")
  if not (data.promoted or data.killed or data.new_experiments):
    lines.append("Nothing moved in this window.")
  return "\n".join(lines)


def _num(value: float | None, width: int, places: int) -> str:
  if value is None:
    return "-".rjust(width)
  return f"{value:>{width}.{places}f}"
