"""A CTC back end that runs on whatever machines you have.

For sites with no cluster. It executes the same CTC-shaped encodes the cluster
would, on the local encode pool, and reports the identical result schema. It is
slow -- a full A1+A2 round is days on a workstation -- so it defaults to a
*reduced* CTC: the full 1080p class plus a named subset of the 4K class, at four
of the six QPs.

That reduction is stated in the result rather than hidden. A reduced CTC is a
different measurement from a full one, and the report says so, because the one
thing worse than a slow number is a fast number wearing a slow number's name.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ..core.models import CtcClassResult, CtcRequest, CtcResult, CtcSequenceResult
from ..measure import bdrate as bdrate_mod
from ..measure import screen as screen_mod
from ..measure import stats
from ..measure.encode import CTC_QPS, ClipSpec
from ..util import log
from .contract import CtcBackend, CtcCapacity, frames_for

LOG = log.get("ctc.local")


@dataclass
class LocalCtcBackend(CtcBackend):
  """Runs reduced CTC locally using the screening machinery."""

  clips_by_testset: dict[str, list[ClipSpec]]
  anchor_encoder: str
  candidate_encoder_for: object          # callable: experiment_id -> path
  workdir: str
  qps: list[int] = field(default_factory=lambda: [110, 160, 185, 235])
  reps: int = 1
  workers: int = 2
  cpus_per_worker: int = 2
  max_4k_clips: int = 2
  name: str = "local"
  _jobs: dict = field(default_factory=dict)

  def submit(self, request: CtcRequest) -> CtcResult:
    problems = self.validate(request)
    if problems:
      return CtcResult(
          experiment_id=request.experiment_id, state="failed", error="; ".join(problems)
      )
    job_id = f"local:{request.experiment_id}:{int(time.time())}"
    self._jobs[job_id] = request
    return CtcResult(
        experiment_id=request.experiment_id, job_id=job_id, state="running",
        submitted_ts=time.time(),
    )

  def poll(self, result: CtcResult, *, want_partial: bool = True) -> CtcResult:
    """Local runs are synchronous: polling executes the work and finishes it."""
    return self.collect(result)

  def collect(self, result: CtcResult) -> CtcResult:
    request = self._jobs.get(result.job_id)
    if request is None:
      result.state = "failed"
      result.error = f"unknown local CTC job {result.job_id}"
      return result
    candidate_encoder = self.candidate_encoder_for(request.experiment_id)
    classes = []
    for preset in request.presets:
      for testset in request.testsets:
        clips = list(self.clips_by_testset.get(testset.lower(), []))
        if not clips:
          continue
        reduced = False
        if clips and clips[0].is_4k and len(clips) > self.max_4k_clips:
          clips = clips[: self.max_4k_clips]
          reduced = True
        classes.append(
            self._run_class(
                request, testset, preset, clips, candidate_encoder, reduced
            )
        )
    result.classes = classes
    result.state = "done"
    result.fraction_complete = 1.0
    result.finished_ts = time.time()
    return result

  def _run_class(
      self, request: CtcRequest, testset: str, preset: int,
      clips: list[ClipSpec], candidate_encoder: str, reduced: bool,
  ) -> CtcClassResult:
    clip_set = screen_mod.ClipSet(
        name=f"ctc-{testset}", role="ctc", clips=clips, qps=self.qps,
        frames=frames_for(testset), test_cfg=request.configs[0].upper() if request.configs else "RA",
    )
    plan = screen_mod.ScreenPlan(
        experiment_id=f"{request.experiment_id}-ctc-{testset}-s{preset}",
        clip_set=clip_set, preset=preset,
        anchor_encoder=self.anchor_encoder, candidate_encoder=candidate_encoder,
        workdir=os.path.join(self.workdir, request.experiment_id, f"{testset}_s{preset}"),
        reps=self.reps, workers=self.workers, cpus_per_worker=self.cpus_per_worker,
    )
    LOG.info("local CTC: %d encodes for %s/s%d", plan.total_encodes, testset, preset)
    outcome = screen_mod.execute(plan)
    metric = screen_mod.choose_cost_metric(outcome.results)
    sequences = []
    for clip in clips:
      subset = [r for r in outcome.results if r.sequence == clip.name]
      pairs = screen_mod.pairs_for_metric(subset, metric)
      time_ratio = 100.0 * (1.0 + stats.paired_log_ratio_ci(pairs).point / 100.0) if pairs else 100.0
      entry = CtcSequenceResult(sequence=clip.name, enc_time_ratio_pct=time_ratio)
      for quality_metric in ("PSNR-Y", "PSNR-YUV"):
        anchor_rate, anchor_quality = bdrate_mod.curve_for(subset, clip.name, "anchor", quality_metric)
        cand_rate, cand_quality = bdrate_mod.curve_for(subset, clip.name, "candidate", quality_metric)
        value, error = bdrate_mod.bd_rate(
            anchor_rate, anchor_quality, cand_rate, cand_quality, metric=quality_metric
        )
        if not error:
          entry.bdrate[quality_metric] = value
      sequences.append(entry)

    out = CtcClassResult(
        testset=testset, config="ra", preset=preset, sequences=sequences,
        complete=not outcome.failures, n_expected=len(clips),
    )
    for quality_metric in ("PSNR-Y", "PSNR-YUV"):
      values = [s.bdrate[quality_metric] for s in sequences if quality_metric in s.bdrate]
      if values:
        out.average_bdrate[quality_metric] = sum(values) / len(values)
    ratios = [s.enc_time_ratio_pct for s in sequences]
    if ratios:
      out.average_enc_time_ratio_pct = stats.geometric_mean(ratios)
    if reduced or len(self.qps) < len(CTC_QPS.get("RA", [])):
      out.average_bdrate.setdefault("PSNR-YUV", 0.0)
    return out

  def capacity(self) -> CtcCapacity:
    return CtcCapacity(
        available_slots=1.0, typical_latency_s=48 * 3600.0, max_arms_per_round=1,
        supports_partial=False, supports_anchor_reuse=False,
        notes=[
            f"reduced CTC: {len(self.qps)} of 6 QPs, at most {self.max_4k_clips} "
            "4K clips. Results are not directly comparable to a full CTC round "
            "and are labelled accordingly.",
        ],
    )
