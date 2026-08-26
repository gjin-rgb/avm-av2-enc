"""Deciding what to do next, and refusing to spend the scarce resource carelessly.

Two resources, three orders of magnitude apart: local CPU, which is cheap and
abundant, and CTC cluster rounds, which take hours to a day and are the only
measurement that ships. Almost every planning decision in this system is really
a decision about which of those to spend.

The governor rules exist because an autonomous loop degenerates in predictable
ways, and every one of these was visible in the prior corpus:

  * **Subsystem collapse.** Twenty-four consecutive patches attacked partition
    and transform search while quantisation held ~47% of the profile. A cap on
    concentration forces the portfolio to look elsewhere.
  * **Infinite tuning.** A threshold chain that has already been bracketed on
    both sides is finished; the corpus bracketed one and correctly stopped, and
    slid along the trade-off on three others. Depth is capped and a bracketed
    parameter is closed.
  * **Cluster-time waste.** A round was spent on ten arms of which eight were
    already dead on evidence the project had. Escalation requires a written
    question and proof that the cheaper tiers are exhausted.
  * **Stale numbers.** When the anchor moves, everything measured against the
    old one stops being current, and the planner's first job is to notice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..core.models import (
    CtcRequest,
    Experiment,
    ExperimentStatus,
    MechanismClass,
    Tier,
    Verdict,
)
from ..core.registry import Registry
from ..util import log

LOG = log.get("agent.planner")


@dataclass
class Governor:
  """Limits that keep a long autonomous run from degenerating."""

  max_open_experiments: int = 6
  max_per_subsystem_fraction: float = 0.4      # of open experiments
  max_tuning_depth: int = 3                    # variants in one family
  min_distinct_lenses: int = 3
  ctc_slots_per_day: float = 4.0
  ctc_min_arms_per_round: int = 2              # batch, do not drip-feed
  require_holdout_before_ctc: bool = True
  halt_on_base_drift: bool = True

  def concentration_cap(self, open_count: int) -> int:
    return max(2, int(open_count * self.max_per_subsystem_fraction))


@dataclass
class Action:
  kind: str                     # propose | implement | screen | escalate | collect | idle | halt
  experiment_id: str = ""
  detail: str = ""
  payload: dict = field(default_factory=dict)

  def __str__(self) -> str:
    target = f" {self.experiment_id}" if self.experiment_id else ""
    return f"{self.kind}{target}: {self.detail}"


@dataclass
class EscalationCase:
  """The written argument for spending a CTC round. Required, not optional."""

  experiment_id: str
  question: str
  cheaper_evidence: list[str] = field(default_factory=list)
  unanswerable_locally: str = ""
  expected_outcome: str = ""
  blockers: list[str] = field(default_factory=list)

  @property
  def admissible(self) -> bool:
    return bool(self.question and self.unanswerable_locally and not self.blockers)

  def render(self) -> str:
    lines = [f"CTC justification for {self.experiment_id}", f"  question: {self.question}"]
    for item in self.cheaper_evidence:
      lines.append(f"  already established: {item}")
    if self.unanswerable_locally:
      lines.append(f"  why nothing cheaper answers it: {self.unanswerable_locally}")
    if self.expected_outcome:
      lines.append(f"  expected outcome: {self.expected_outcome}")
    for blocker in self.blockers:
      lines.append(f"  BLOCKED: {blocker}")
    return "\n".join(lines)


def build_escalation_case(experiment: Experiment, *, require_holdout: bool = True) -> EscalationCase:
  """Assemble -- and audit -- the argument for a cluster round."""
  case = EscalationCase(experiment_id=experiment.id, question="")
  hypothesis = experiment.hypothesis

  t0 = experiment.tier_result(Tier.T0_BUILD)
  t1 = experiment.tier_result(Tier.T1_EXACT)
  t2 = experiment.tier_result(Tier.T2_COMPLEXITY)
  t3 = experiment.tier_result(Tier.T3_SCREEN)
  t4 = experiment.tier_result(Tier.T4_HELDOUT)

  if not (t0 and t0.passed):
    case.blockers.append("has not passed the build and conformance tier")
  if hypothesis.mechanism.must_be_bit_exact:
    if t1 and t1.passed:
      case.cheaper_evidence.append(
          "bit-exactness proven locally: quality risk is zero by proof"
      )
      case.blockers.append(
          "a bit-exact patch needs no CTC *quality* round. If the question is "
          "whether it is faster at 4K, that is a timing question -- state it "
          "explicitly, because the corpus's three bit-exact patches were all "
          "slower at 4K and each cost a round to discover"
      )
    else:
      case.blockers.append(
          "declared bit-exact but not yet proven so; prove it locally first, it "
          "costs minutes"
      )
  if not (t2 and t2.passed):
    case.blockers.append(
        "the speedup has not been resolved above the noise floor locally. CTC "
        "measures quality; it cannot rescue an inconclusive timing measurement"
    )
  else:
    interval = (t2.evidence or {}).get("speed_delta")
    if interval:
      case.cheaper_evidence.append(
          f"speedup resolved locally at {-interval[1]:+.2f}% "
          f"[{-interval[2]:+.2f}, {-interval[0]:+.2f}] on "
          f"{(t2.evidence or {}).get('metric', 'unknown metric')}"
      )
  if t1 and t1.passed and not hypothesis.mechanism.must_be_bit_exact:
    case.cheaper_evidence.append((t1.reason or "activation confirmed")[:160])
  if t3 and t3.passed:
    case.cheaper_evidence.append(
        "local screen shows no catastrophic regression and ranks it worth testing"
    )
  elif t3:
    case.blockers.append(f"local screen did not pass: {t3.reason[:160]}")
  else:
    case.blockers.append("no local screen has been run")
  if require_holdout:
    if t4 and t4.passed:
      case.cheaper_evidence.append("the effect survives on held-out clips")
    elif t4:
      case.blockers.append(f"held-out clips contradict the screen: {t4.reason[:160]}")
    else:
      case.blockers.append(
          "no held-out measurement: without it the screening number is an upper "
          "bound that has never been checked against clips the search did not see"
      )

  case.question = (
      f"Does {hypothesis.title} clear the speed-{hypothesis.target_presets[0]} bar "
      "on both A1 and A2 independently, at CTC resolutions and frame counts?"
  )
  case.unanswerable_locally = (
      "local screening runs 3 QPs on a handful of short clips; its BD-rate has "
      "had the wrong sign against CTC on 3 of 10 prior patches, so the quality "
      "question is genuinely open and only the real test conditions settle it"
  )
  case.expected_outcome = (
      f"predicted {hypothesis.expected_speedup_pct:+.2f}% speed and "
      f"{hypothesis.expected_bdrate_pct:+.2f}% BD-rate; kill criteria: "
      + "; ".join(hypothesis.kill_criteria)
  )
  return case


class Planner:

  def __init__(self, registry: Registry, governor: Governor | None = None):
    self.registry = registry
    self.governor = governor or Governor()

  # -- portfolio state -----------------------------------------------------

  def open_experiments(self) -> list[Experiment]:
    out: list[Experiment] = []
    for status in (
        ExperimentStatus.PROPOSED, ExperimentStatus.IMPLEMENTING,
        ExperimentStatus.BUILDING, ExperimentStatus.SCREENING,
        ExperimentStatus.AWAITING_CTC, ExperimentStatus.CTC_RUNNING,
    ):
      out.extend(self.registry.query(status=status.value, limit=200))
    return out

  def portfolio(self) -> dict:
    open_ones = self.open_experiments()
    subsystems: dict[str, int] = {}
    lenses: dict[str, int] = {}
    for experiment in open_ones:
      subsystems[experiment.hypothesis.subsystem] = (
          subsystems.get(experiment.hypothesis.subsystem, 0) + 1
      )
      lenses[experiment.hypothesis.lens] = lenses.get(experiment.hypothesis.lens, 0) + 1
    return {
        "open": len(open_ones),
        "by_subsystem": subsystems,
        "by_lens": lenses,
        "ctc_slots_last_day": self.registry.ctc_slots_used(since_ts=time.time() - 86400),
    }

  def blocked_subsystems(self) -> set[str]:
    state = self.portfolio()
    cap = self.governor.concentration_cap(max(state["open"], 1))
    return {name for name, count in state["by_subsystem"].items() if count >= cap}

  def blocked_lenses(self) -> set[str]:
    state = self.portfolio()
    cap = self.governor.concentration_cap(max(state["open"], 1))
    return {name for name, count in state["by_lens"].items() if count >= cap}

  def tuning_depth(self, experiment: Experiment) -> int:
    from ..core.ids import family_of

    family = family_of(experiment.id)
    return len(self.registry.query(family=family, limit=50))

  # -- the decision --------------------------------------------------------

  def next_action(self, *, base_drifted: bool = False, capacity_arms: int = 8) -> Action:
    if base_drifted and self.governor.halt_on_base_drift:
      return Action(
          kind="halt",
          detail=(
              "the anchor moved: every recorded measurement is scoped to the old "
              "base and is not automatically valid against the new one. Re-run "
              "the cheap tiers on affected experiments before continuing."
          ),
      )

    running = self.registry.query(status=ExperimentStatus.CTC_RUNNING.value, limit=50)
    if running:
      return Action(
          kind="collect", experiment_id=running[0].id,
          detail="a cluster round is in flight; poll it before starting new work",
      )

    ready = [
        e for e in self.registry.query(status=ExperimentStatus.AWAITING_CTC.value, limit=50)
    ]
    if ready:
      admissible = []
      for experiment in ready:
        case = build_escalation_case(
            experiment, require_holdout=self.governor.require_holdout_before_ctc
        )
        if case.admissible:
          admissible.append((experiment, case))
        else:
          LOG.info("%s not admissible for CTC: %s", experiment.id, case.blockers[:2])
      if admissible:
        used = self.registry.ctc_slots_used(since_ts=time.time() - 86400)
        if used >= self.governor.ctc_slots_per_day:
          return Action(
              kind="idle",
              detail=(
                  f"CTC budget for the day is spent ({used:.1f} of "
                  f"{self.governor.ctc_slots_per_day:.1f} slots). Keep screening; "
                  "arms batch better than they drip-feed."
              ),
          )
        batch = admissible[: max(1, min(capacity_arms, len(admissible)))]
        if (
            len(batch) < self.governor.ctc_min_arms_per_round
            and len(ready) > len(batch)
        ):
          return Action(
              kind="idle",
              detail=(
                  f"only {len(batch)} arm(s) are admissible; a round's latency is "
                  "set by its slowest arm, so waiting for a fuller batch costs "
                  "nothing and saves rounds"
              ),
          )
        return Action(
            kind="escalate", experiment_id=batch[0][0].id,
            detail=batch[0][1].render(),
            payload={"batch": [e.id for e, _ in batch]},
        )

    screening = self.registry.query(status=ExperimentStatus.SCREENING.value, limit=50)
    if screening:
      return Action(
          kind="screen", experiment_id=screening[0].id, detail="continue the local ladder"
      )
    implementing = self.registry.query(status=ExperimentStatus.IMPLEMENTING.value, limit=50)
    if implementing:
      return Action(
          kind="implement", experiment_id=implementing[0].id, detail="finish the patch"
      )
    proposed = self.registry.query(status=ExperimentStatus.PROPOSED.value, limit=50)
    if proposed:
      return Action(
          kind="implement", experiment_id=proposed[0].id, detail="implement the proposal"
      )

    state = self.portfolio()
    if state["open"] >= self.governor.max_open_experiments:
      return Action(
          kind="idle",
          detail=(
              f"{state['open']} experiments already open (cap "
              f"{self.governor.max_open_experiments}); finishing beats starting"
          ),
      )
    return Action(
        kind="propose",
        detail="portfolio has room for a new hypothesis",
        payload={
            "avoid_subsystems": sorted(self.blocked_subsystems()),
            "avoid_lenses": sorted(self.blocked_lenses()),
        },
    )

  # -- escalation ----------------------------------------------------------

  def build_request(
      self, experiment: Experiment, case: EscalationCase, *, testsets: list[str],
      configs: list[str], patch_ref: str,
  ) -> CtcRequest:
    return CtcRequest(
        experiment_id=experiment.id,
        base_sha=experiment.base_sha,
        patch_ref=patch_ref,
        presets=list(experiment.hypothesis.target_presets),
        testsets=list(testsets),
        configs=list(configs),
        question=case.question,
        submitted_ts=time.time(),
    )

  def should_abort_partial(self, experiment: Experiment) -> tuple[bool, str]:
    """Early-abort a running round whose partial results already answer it.

    Cluster quota released early is quota available for the next question. The
    bar for aborting is deliberately high: a partial round is a biased sample
    (the fastest clips finish first), so only an unambiguous signal counts.
    """
    ctc = experiment.ctc
    if not ctc or ctc.state != "partial" or ctc.fraction_complete < 0.35:
      return (False, "")
    for cls in ctc.classes:
      if len(cls.sequences) < 3:
        continue
      speedup = cls.speedup_pct
      bdrate = cls.average_bdrate.get("PSNR-YUV", cls.average_bdrate.get("PSNR-Y", 0.0))
      if speedup <= 0.2:
        return (
            True,
            f"{cls.testset.upper()} is {len(cls.sequences)} clips in and shows "
            f"{speedup:+.2f}% speedup. The patch is not faster on this class; "
            "finishing the round buys a more precise version of a number that "
            "already fails.",
        )
      if bdrate > 2.0 and speedup < bdrate * 10:
        return (
            True,
            f"{cls.testset.upper()} shows {bdrate:+.2f}% BD-rate for "
            f"{speedup:+.2f}% speedup: a ratio of {speedup / bdrate:.1f} with no "
            "plausible path to the bar.",
        )
    return (False, "")
