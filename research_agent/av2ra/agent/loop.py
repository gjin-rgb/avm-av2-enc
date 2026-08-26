"""The research loop: one tick at a time, resumable, and never blocking on a cluster.

The loop is a state machine over the registry rather than a long-running
function, and that is the important design decision. Every tick reads the
current state, does one unit of work, writes the result durably, and returns.
A Cloudtop reboot, a killed process or an expired credential costs one tick, not
a day; and the same loop drives a two-hour local run and a fleet of nodes
sharing a registry.

The other decision is that submitting a cluster round never blocks. A round is
hours to a day; an agent that waits for it does nothing for a day. Submission
records a handle and the next tick starts a fresh hypothesis in a fresh
worktree, exactly as a human researcher would.

The tick order is: collect finished cluster work, then advance work already
started, then start something new. Finishing beats starting -- an experiment
that is measured but not analysed is compute already spent that has produced no
knowledge yet.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ..buildkit.builder import BuildSpec, Builder
from ..buildkit.worktree import WorktreeManager
from ..core.ids import make_id, next_counter, variant_id
from ..core.ledger import Decision, Ledger
from ..core.models import (
    Experiment,
    ExperimentStatus,
    MechanismClass,
    Tier,
    TierResult,
    Verdict,
)
from ..core.registry import MeasurementRow, Registry
from ..ctc.contract import CtcBackend
from ..integrity import gates
from ..knowledge.lessons import LessonStore
from ..measure import screen as screen_mod
from ..measure import tiers as tiers_mod
from ..measure.encode import EncodeConfig, EncodeJob
from ..util import log
from .analyst import Analyst
from .ideation import IdeationInputs, Ideator, choose_lenses
from .implementer import Implementer
from .lenses import LENSES_BY_NAME, LensStats
from .planner import Planner, build_escalation_case

LOG = log.get("agent.loop")


@dataclass
class LoopConfig:
  workspace: str
  anchor_ref: str = "origin/av2-enc"
  preset: int = 2
  reps: int = 2
  workers: int = 2
  cpus_per_worker: int = 2
  testsets: list[str] = field(default_factory=lambda: ["a1", "a2"])
  configs: list[str] = field(default_factory=lambda: ["ra"])
  node_id: str = "local"
  run_holdout: bool = True
  run_null_arm_every: int = 10        # ticks between harness self-measurements
  conformance_sample: int = 4
  acceptance_mode: str = "conservative"
  keep_bitstreams: bool = True
  build_jobs: int = 0


@dataclass
class TickResult:
  action: str
  experiment_id: str = ""
  detail: str = ""
  advanced: bool = False
  errors: list[str] = field(default_factory=list)

  def __str__(self) -> str:
    target = f" [{self.experiment_id}]" if self.experiment_id else ""
    return f"{self.action}{target}: {self.detail}"


class ResearchLoop:
  """Ties ideation, implementation, measurement and analysis to durable state."""

  def __init__(
      self,
      *,
      config: LoopConfig,
      registry: Registry,
      ledger: Ledger,
      planner: Planner,
      ideator: Ideator,
      implementer: Implementer,
      analyst: Analyst,
      worktrees: WorktreeManager,
      builder: Builder,
      ctc: CtcBackend,
      inputs: IdeationInputs,
      screen_set: screen_mod.ClipSet,
      holdout_set: screen_mod.ClipSet | None = None,
      lesson_store: LessonStore | None = None,
      lens_stats: LensStats | None = None,
      encode_runner=None,
      decoder: str = "",
  ):
    self.config = config
    self.registry = registry
    self.ledger = ledger
    self.planner = planner
    self.ideator = ideator
    self.implementer = implementer
    self.analyst = analyst
    self.worktrees = worktrees
    self.builder = builder
    self.ctc = ctc
    self.inputs = inputs
    self.screen_set = screen_set
    self.holdout_set = holdout_set
    self.lesson_store = lesson_store or LessonStore()
    self.lens_stats = lens_stats or LensStats()
    self.encode_runner = encode_runner
    self.decoder = decoder
    self.ticks = 0
    self.anchor_build = None

  # -- helpers -------------------------------------------------------------

  def _workdir(self, experiment_id: str) -> str:
    return os.path.join(self.config.workspace, "experiments", experiment_id)

  def _new_id(self, *, parent: str | None = None) -> str:
    existing = self.registry.all_ids()
    if parent:
      siblings = [i for i in existing if i.startswith(parent)]
      return variant_id(parent, len(siblings) - 1 if siblings else 0)
    return make_id(next_counter(existing))

  def _record(self, experiment: Experiment) -> None:
    experiment.node_id = self.config.node_id
    self.registry.upsert(experiment)

  def _decide(self, experiment: Experiment, action: str, summary: str, *,
              reasoning: str = "", lessons: list[str] | None = None) -> None:
    self.ledger.append(
        Decision(
            experiment_id=experiment.id, action=action, summary=summary,
            reasoning=reasoning, lessons=lessons or [], base_sha=experiment.base_sha,
            evidence={
                "verdict": experiment.verdict.value,
                "quadrant": experiment.quadrant.value,
                "lens": experiment.hypothesis.lens,
                "mechanism": experiment.hypothesis.mechanism.value,
            },
        )
    )
    for lesson in lessons or []:
      self.lesson_store.add(lesson)

  # -- the tick ------------------------------------------------------------

  def tick(self, *, base_drifted: bool = False) -> TickResult:
    self.ticks += 1
    capacity = self.ctc.capacity()
    action = self.planner.next_action(
        base_drifted=base_drifted, capacity_arms=capacity.max_arms_per_round
    )
    log.event("tick", n=self.ticks, action=action.kind, experiment=action.experiment_id)

    if action.kind == "halt":
      return TickResult(action="halt", detail=action.detail)
    if action.kind == "idle":
      return TickResult(action="idle", detail=action.detail)
    if action.kind == "propose":
      return self._do_propose(action.payload)
    if action.kind in ("implement",):
      return self._do_implement(action.experiment_id)
    if action.kind == "screen":
      return self._do_screen(action.experiment_id)
    if action.kind == "escalate":
      return self._do_escalate(action.payload.get("batch") or [action.experiment_id])
    if action.kind == "collect":
      return self._do_collect(action.experiment_id)
    return TickResult(action="idle", detail=f"nothing to do for action {action.kind}")

  # -- propose -------------------------------------------------------------

  def _do_propose(self, payload: dict) -> TickResult:
    avoid_lenses = set(payload.get("avoid_lenses") or [])
    lenses = choose_lenses(self.inputs, self.lens_stats, count=3, exclude=avoid_lenses)
    if not lenses:
      return TickResult(
          action="propose",
          detail=(
              "no lens has the evidence it needs. Run 'av2ra profile' and "
              "'av2ra ingest' -- a lens without its evidence produces a generic "
              "idea wearing the lens's name."
          ),
      )
    avoid_subsystems = set(payload.get("avoid_subsystems") or [])
    recent = [e.hypothesis.title for e in self.registry.query(limit=20)]

    for lens in lenses:
      experiment_id = self._new_id()
      proposal = self.ideator.propose(
          lens, self.inputs, experiment_id=experiment_id,
          focus=(
              "avoid the subsystems " + ", ".join(sorted(avoid_subsystems))
              + " -- the portfolio is already concentrated there"
              if avoid_subsystems else ""
          ),
          avoid=recent,
      )
      if not proposal.ok:
        LOG.info("%s rejected: %s", lens.name, proposal.rejected[:160])
        continue
      if proposal.hypothesis.subsystem in avoid_subsystems:
        LOG.info("%s: subsystem %s is at its concentration cap", lens.name,
                 proposal.hypothesis.subsystem)
        continue
      seen = self.registry.seen_signature(proposal.hypothesis.signature())
      if seen:
        LOG.info("%s duplicates %s", lens.name, seen["experiment_id"])
        continue

      experiment = Experiment(
          id=experiment_id, hypothesis=proposal.hypothesis,
          status=ExperimentStatus.PROPOSED,
          base_sha=self.inputs.base_sha, node_id=self.config.node_id,
      )
      experiment.integrity["novelty"] = proposal.novelty
      if proposal.repairs:
        experiment.human_notes.append(
            "symbol repairs applied to the proposal: " + "; ".join(proposal.repairs)
        )
      self._record(experiment)
      self._decide(
          experiment, "noted",
          f"proposed via lens '{lens.name}': {proposal.hypothesis.title}",
          reasoning=proposal.hypothesis.rationale[:1200],
      )
      return TickResult(
          action="propose", experiment_id=experiment_id, advanced=True,
          detail=f"[{lens.name}] {proposal.hypothesis.title}",
      )
    return TickResult(
        action="propose",
        detail="every lens tried was rejected by grounding, novelty or the Amdahl gate",
    )

  # -- implement -----------------------------------------------------------

  def _do_implement(self, experiment_id: str) -> TickResult:
    experiment = self.registry.get(experiment_id)
    if not experiment:
      return TickResult(action="implement", detail=f"{experiment_id} not found")
    experiment.status = ExperimentStatus.IMPLEMENTING
    self._record(experiment)

    worktree = self.worktrees.create(experiment.id, self.config.anchor_ref)
    experiment.worktree = worktree.path
    experiment.branch = worktree.branch
    experiment.base_sha = worktree.base_sha

    result = self.implementer.implement(
        experiment.hypothesis, worktree,
        self.inputs.codemap,
        build_spec_factory=lambda wt, patch: BuildSpec(
            source_dir=wt.path, build_dir=wt.build_dir,
            patch_defines=patch.build_defines,
            source_identity=f"{wt.base_sha}:{patch.normalized_hash}",
            jobs=self.config.build_jobs,
        ),
    )
    experiment.patch = result.patch
    experiment.build_fingerprint = result.build.fingerprint if result.build else ""
    experiment.integrity["policy_violations"] = [
        {"kind": v.kind, "detail": v.detail, "severity": v.severity, "path": v.path}
        for v in result.violations
    ]
    if result.build and result.build.warnings:
      experiment.human_notes.append(
          f"{len(result.build.warnings)} compiler warning(s): "
          + "; ".join(result.build.warnings[:4])
      )

    if not result.ok:
      verdict = (
          Verdict.INTEGRITY_FAIL if result.blocked_by_policy else Verdict.BUILD_FAIL
      )
      experiment.apply_tier_result(
          TierResult(
              tier=Tier.T0_BUILD, passed=False, verdict=verdict,
              reason=result.error[:2000],
              evidence={"attempts": result.attempts, "transcript": result.transcript},
          )
      )
      lesson = (
          f"{experiment.hypothesis.title}: "
          + (
              "the only implementation the model found violated patch policy ("
              + "; ".join(v.kind for v in result.violations if v.severity == "blocker")[:160]
              + ")"
              if result.blocked_by_policy
              else f"did not compile in {result.attempts} attempts: "
              + result.error.splitlines()[0][:160]
          )
      )
      experiment.add_lesson(lesson)
      experiment.status = ExperimentStatus.COMPLETE
      self._record(experiment)
      self._decide(
          experiment, "killed",
          f"could not be implemented within {result.attempts} attempt(s)",
          reasoning=result.error[:1500], lessons=[lesson],
      )
      self.worktrees.remove(experiment.id)
      return TickResult(
          action="implement", experiment_id=experiment.id,
          detail=f"failed: {result.error[:200]}", errors=[result.error[:400]],
      )

    experiment.status = ExperimentStatus.SCREENING
    self._record(experiment)
    return TickResult(
        action="implement", experiment_id=experiment.id, advanced=True,
        detail=(
            f"patch built ({experiment.patch.size} lines across "
            f"{len(experiment.patch.files)} file(s)) in {result.attempts} attempt(s)"
        ),
    )

  # -- screen --------------------------------------------------------------

  def _ensure_anchor_build(self, base_sha: str) -> str:
    """Build the unpatched anchor once per base and reuse the binary.

    Caching a *binary* is safe -- it is a deterministic function of source and
    flags. Caching a *timing* is not, which is why the anchor is re-measured in
    every screening pass even though the binary is reused.
    """
    if self.anchor_build and self.anchor_build.ok:
      return self.anchor_build.encoder
    worktree = self.worktrees.create("anchor", self.config.anchor_ref)
    spec = BuildSpec(
        source_dir=worktree.path, build_dir=worktree.build_dir,
        source_identity=f"{base_sha}:anchor", jobs=self.config.build_jobs,
    )
    self.anchor_build = self.builder.build(spec)
    return self.anchor_build.encoder if self.anchor_build.ok else ""

  def _screen_plan(self, experiment: Experiment, clip_set: screen_mod.ClipSet,
                   anchor: str, candidate: str) -> screen_mod.ScreenPlan:
    return screen_mod.ScreenPlan(
        experiment_id=f"{experiment.id}:{clip_set.name}",
        clip_set=clip_set, preset=self.config.preset,
        anchor_encoder=anchor, candidate_encoder=candidate,
        workdir=os.path.join(self._workdir(experiment.id), clip_set.role),
        reps=self.config.reps, workers=self.config.workers,
        cpus_per_worker=self.config.cpus_per_worker,
        use_perf=_perf_enabled(),
        keep_bitstreams=self.config.keep_bitstreams,
        anchor_build_fingerprint=experiment.base_sha,
        candidate_build_fingerprint=experiment.build_fingerprint,
    )

  def _do_screen(self, experiment_id: str) -> TickResult:
    experiment = self.registry.get(experiment_id)
    if not experiment:
      return TickResult(action="screen", detail=f"{experiment_id} not found")

    anchor_encoder = self._ensure_anchor_build(experiment.base_sha)
    candidate_encoder = os.path.join(
        self.worktrees.root, f"exp_{experiment.id}", "build_av2ra", "avmenc"
    )
    if not self.encode_runner and not (
        os.path.exists(anchor_encoder) and os.path.exists(candidate_encoder)
    ):
      return TickResult(
          action="screen", experiment_id=experiment.id,
          detail="anchor or candidate binary is missing; cannot screen",
          errors=["missing binaries"],
      )

    context = tiers_mod.TierContext(
        workdir=os.path.join(self._workdir(experiment.id), "screen"),
        preset=self.config.preset, decoder=self.decoder,
        conformance_sample=self.config.conformance_sample,
        seed=experiment.id, acceptance_mode=self.config.acceptance_mode,
        profile_share_pct=(
            self.inputs.profile.share_of(experiment.hypothesis.target_functions)
            if self.inputs.profile else 0.0
        ),
    )

    plan = self._screen_plan(experiment, self.screen_set, anchor_encoder, candidate_encoder)
    outcome = screen_mod.execute(
        plan, runner=self.encode_runner or screen_mod.run_encode
    )
    results = outcome.results + outcome.failures

    for evaluate in (tiers_mod.evaluate_t0, tiers_mod.evaluate_t1):
      tier_result = evaluate(experiment, results, context)
      experiment.apply_tier_result(tier_result)
      if not tier_result.passed:
        return self._finish_failed(experiment, tier_result)

    null_arm = None
    if self.config.run_null_arm_every and self.ticks % self.config.run_null_arm_every == 1:
      null_arm = gates.run_null_arm(
          anchor_encoder,
          screen_mod.plan_jobs(plan)[: max(2, len(self.screen_set.clips))],
          runner=self.encode_runner or screen_mod.run_encode,
          metric=screen_mod.choose_cost_metric(outcome.results),
      )
      experiment.integrity["null_arm"] = {
          "passed": null_arm.passed, "detail": null_arm.detail, "data": null_arm.data,
      }

    t2 = tiers_mod.evaluate_t2(experiment, results, context, null_arm=null_arm)
    experiment.apply_tier_result(t2)
    if not t2.passed:
      return self._finish_failed(experiment, t2)

    t3 = tiers_mod.evaluate_t3(experiment, results, context)
    experiment.apply_tier_result(t3)
    self._store_measurements(experiment, t3, "screen")
    if not t3.passed:
      return self._finish_failed(experiment, t3)

    if self.config.run_holdout and self.holdout_set and self.holdout_set.clips:
      holdout_plan = self._screen_plan(
          experiment, self.holdout_set, anchor_encoder, candidate_encoder
      )
      holdout_outcome = screen_mod.execute(
          holdout_plan, runner=self.encode_runner or screen_mod.run_encode
      )
      t4 = tiers_mod.evaluate_t4(
          experiment, holdout_outcome.results + holdout_outcome.failures, context
      )
      experiment.apply_tier_result(t4)
      self._store_measurements(experiment, t4, "holdout")
      if not t4.passed:
        return self._finish_failed(experiment, t4)

    experiment.status = ExperimentStatus.AWAITING_CTC
    self._record(experiment)
    self.lens_stats.record(experiment.hypothesis.lens, stage="screened")
    return TickResult(
        action="screen", experiment_id=experiment.id, advanced=True,
        detail=t3.reason[:300],
    )

  def _store_measurements(self, experiment: Experiment, tier: TierResult, class_name: str) -> None:
    if not tier.summary:
      return
    if tier.summary.speed_delta:
      self.registry.record_measurement(
          MeasurementRow(
              experiment_id=experiment.id, tier=tier.tier.value, class_name=class_name,
              preset=self.config.preset, metric=tier.summary.metric_for_speed,
              value=tier.summary.speed_delta.point,
              ci_lo=tier.summary.speed_delta.lo, ci_hi=tier.summary.speed_delta.hi,
              n=tier.summary.speed_delta.n, base_sha=experiment.base_sha,
              patch_hash=experiment.patch.normalized_hash,
              build_fp=experiment.build_fingerprint, clip_set=tier.summary.clip_set,
              reps=tier.summary.reps, metric_def="paired-log-ratio",
              backend="local", node_id=self.config.node_id,
          )
      )
    for metric, interval in tier.summary.bdrate_by_metric.items():
      self.registry.record_measurement(
          MeasurementRow(
              experiment_id=experiment.id, tier=tier.tier.value, class_name=class_name,
              preset=self.config.preset, metric=metric, value=interval.point,
              ci_lo=interval.lo, ci_hi=interval.hi, n=interval.n,
              base_sha=experiment.base_sha, patch_hash=experiment.patch.normalized_hash,
              build_fp=experiment.build_fingerprint, clip_set=tier.summary.clip_set,
              reps=tier.summary.reps, metric_def="bdrate/pchip/local-screen",
              backend="local", node_id=self.config.node_id,
              extra={"indicative_only": True},
          )
      )

  def _finish_failed(self, experiment: Experiment, tier: TierResult) -> TickResult:
    experiment.status = ExperimentStatus.COMPLETE
    self._record(experiment)
    analysis = self.analyst.analyse(experiment, parent=self._parent_of(experiment))
    experiment.add_lesson(analysis.lesson)
    self._record(experiment)
    self._decide(
        experiment, analysis.move if analysis.move in ("kill", "instrument") else "killed",
        f"stopped at {tier.tier.value}: {tier.reason[:300]}",
        reasoning=analysis.reasoning, lessons=[analysis.lesson] if analysis.lesson else [],
    )
    self.worktrees.remove(experiment.id)
    return TickResult(
        action="screen", experiment_id=experiment.id, advanced=True,
        detail=f"{tier.tier.value} {tier.verdict.value}: {tier.reason[:220]}",
    )

  def _parent_of(self, experiment: Experiment) -> Experiment | None:
    parent_id = experiment.hypothesis.parent_experiment
    return self.registry.get(parent_id) if parent_id else None

  # -- escalate ------------------------------------------------------------

  def _do_escalate(self, batch: list[str]) -> TickResult:
    submitted = []
    for experiment_id in batch:
      experiment = self.registry.get(experiment_id)
      if not experiment:
        continue
      case = build_escalation_case(
          experiment, require_holdout=self.planner.governor.require_holdout_before_ctc
      )
      if not case.admissible:
        continue
      request = self.planner.build_request(
          experiment, case, testsets=self.config.testsets,
          configs=self.config.configs,
          patch_ref=experiment.worktree or experiment.branch,
      )
      result = self.ctc.submit(request)
      if hasattr(self.ctc, "remember"):
        self.ctc.remember(result, request)
      if result.state == "failed":
        experiment.human_notes.append(f"CTC submission failed: {result.error}")
        self._record(experiment)
        continue
      experiment.ctc = result
      experiment.status = ExperimentStatus.CTC_RUNNING
      self._record(experiment)
      self.registry.grant_ctc_slots(experiment.id, 1.0, case.question)
      self._decide(
          experiment, "noted", f"submitted to CTC: {case.question}",
          reasoning=case.render(),
      )
      submitted.append(experiment.id)
    if not submitted:
      return TickResult(action="escalate", detail="no arm was admissible after re-check")
    return TickResult(
        action="escalate", experiment_id=submitted[0], advanced=True,
        detail=(
            f"submitted {len(submitted)} arm(s): {', '.join(submitted)}. "
            "Moving on to the next hypothesis rather than waiting."
        ),
    )

  # -- collect -------------------------------------------------------------

  def _do_collect(self, experiment_id: str) -> TickResult:
    experiment = self.registry.get(experiment_id)
    if not experiment or not experiment.ctc:
      return TickResult(action="collect", detail=f"{experiment_id} has no cluster run")

    experiment.ctc = self.ctc.poll(experiment.ctc, want_partial=True)
    self._record(experiment)

    if experiment.ctc.state in ("running", "queued"):
      abort, reason = self.planner.should_abort_partial(experiment)
      if abort:
        self.ctc.cancel(experiment.ctc, reason)
        experiment.human_notes.append(f"aborted early: {reason}")
        self._record(experiment)
        return TickResult(
            action="collect", experiment_id=experiment.id, advanced=True,
            detail=f"early abort: {reason}",
        )
      return TickResult(
          action="collect", experiment_id=experiment.id,
          detail=f"still running ({experiment.ctc.fraction_complete * 100:.0f}% complete)",
      )

    if experiment.ctc.state == "partial":
      abort, reason = self.planner.should_abort_partial(experiment)
      if abort:
        self.ctc.cancel(experiment.ctc, reason)
        experiment.human_notes.append(f"aborted early: {reason}")
        experiment.status = ExperimentStatus.COMPLETE
        experiment.verdict = Verdict.BELOW_BAR
        self._record(experiment)
        self._decide(
            experiment, "killed", "aborted a partial cluster round",
            reasoning=reason,
            lessons=[f"{experiment.hypothesis.title}: {reason[:200]}"],
        )
        return TickResult(
            action="collect", experiment_id=experiment.id, advanced=True,
            detail=f"early abort: {reason}",
        )
      return TickResult(
          action="collect", experiment_id=experiment.id,
          detail=f"partial ({experiment.ctc.fraction_complete * 100:.0f}%), continuing",
      )

    experiment.ctc = self.ctc.collect(experiment.ctc)
    context = tiers_mod.TierContext(
        workdir=self._workdir(experiment.id), preset=self.config.preset,
        acceptance_mode=self.config.acceptance_mode, seed=experiment.id,
    )
    t5 = tiers_mod.evaluate_t5(experiment, context)
    experiment.apply_tier_result(t5)
    for cls in experiment.ctc.classes:
      for metric in ("PSNR-Y", "PSNR-YUV"):
        if metric in cls.average_bdrate:
          self.registry.record_measurement(
              MeasurementRow(
                  experiment_id=experiment.id, tier=Tier.T5_CTC.value,
                  class_name=cls.testset.upper(), preset=cls.preset, metric=metric,
                  value=cls.average_bdrate[metric], n=len(cls.sequences),
                  base_sha=experiment.base_sha,
                  patch_hash=experiment.patch.normalized_hash,
                  build_fp=experiment.build_fingerprint,
                  clip_set=cls.testset, metric_def="bdrate/pchip/ctc",
                  backend=self.ctc.name, job_id=experiment.ctc.candidate_job_id,
                  node_id=self.config.node_id,
              )
          )
      self.registry.record_measurement(
          MeasurementRow(
              experiment_id=experiment.id, tier=Tier.T5_CTC.value,
              class_name=cls.testset.upper(), preset=cls.preset, metric="EncTime",
              value=cls.average_enc_time_ratio_pct, n=len(cls.sequences),
              base_sha=experiment.base_sha, patch_hash=experiment.patch.normalized_hash,
              build_fp=experiment.build_fingerprint, clip_set=cls.testset,
              metric_def="enc-time-ratio-pct", backend=self.ctc.name,
              job_id=experiment.ctc.candidate_job_id, node_id=self.config.node_id,
          )
      )

    experiment.status = ExperimentStatus.COMPLETE
    analysis = self.analyst.analyse(experiment, parent=self._parent_of(experiment))
    experiment.add_lesson(analysis.lesson)
    if analysis.move == "promote":
      experiment.verdict = Verdict.PROMOTE
      self.lens_stats.record(experiment.hypothesis.lens, stage="passed")
    self._record(experiment)
    self._decide(
        experiment,
        {"promote": "promoted", "kill": "killed"}.get(analysis.move, analysis.move),
        f"cluster result: {t5.reason[:400]}",
        reasoning=analysis.reasoning,
        lessons=[analysis.lesson] if analysis.lesson else [],
    )
    self.worktrees.remove(experiment.id)
    return TickResult(
        action="collect", experiment_id=experiment.id, advanced=True,
        detail=f"{t5.verdict.value}: {t5.reason[:250]} -> move '{analysis.move}'",
    )

  # -- driver --------------------------------------------------------------

  def run(self, *, max_ticks: int = 20, stop_on_idle: bool = False,
          base_drift_check=None) -> list[TickResult]:
    history = []
    for _ in range(max_ticks):
      drifted = bool(base_drift_check()) if base_drift_check else False
      result = self.tick(base_drifted=drifted)
      history.append(result)
      LOG.info("%s", result)
      if result.action == "halt":
        break
      if stop_on_idle and result.action == "idle":
        break
    return history


def _perf_enabled() -> bool:
  from ..measure.encode import perf_available

  return perf_available()
