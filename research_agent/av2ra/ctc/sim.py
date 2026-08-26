"""A simulated CTC back end.

Runs instantly, reports the same schema as the real one, and reproduces the two
properties of a real cluster that matter to the planner: results arrive after a
delay, and they can arrive *partially*. Partial results exist here so the
early-abort path -- kill a run that is 40% through and already showing no
speedup, and give the quota back -- is exercised in tests rather than first
tried in production.

The numbers come from the same ground truth the simulated encoder uses, but
without the screening-set bias, so a simulated run reproduces the real
phenomenon the corpus documents: the local screen and the cluster disagree, and
the cluster is right.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..core.models import CtcClassResult, CtcRequest, CtcResult, CtcSequenceResult
from ..sim import effects
from .contract import CtcBackend, CtcCapacity

#: Stand-in test sets, sized like the real ones (A1: 8 clips 4K, A2: 19 at 1080p).
SIM_TESTSETS = {
    "a1": [f"A1_clip{i:02d}_3840x2160" for i in range(8)],
    "a2": [f"A2_clip{i:02d}_1920x1080" for i in range(19)],
}


@dataclass
class SimCtcBackend(CtcBackend):

  truth_lookup: dict[str, effects.PatchTruth] = field(default_factory=dict)
  latency_s: float = 0.0
  reveal_fraction: float = 1.0     # <1 makes poll() return partial results
  clock: float = field(default_factory=time.time)
  name: str = "sim"

  def register(self, experiment_id: str, truth: effects.PatchTruth) -> None:
    self.truth_lookup[experiment_id] = truth

  def submit(self, request: CtcRequest) -> CtcResult:
    problems = self.validate(request)
    if problems:
      return CtcResult(
          experiment_id=request.experiment_id, state="failed", error="; ".join(problems)
      )
    return CtcResult(
        experiment_id=request.experiment_id,
        job_id=f"sim:{request.experiment_id}:{int(self.clock)}",
        state="running",
        submitted_ts=self.clock,
        anchor_job_id=f"sim-anchor-{request.base_sha[:8]}",
        candidate_job_id=f"sim-cand-{request.experiment_id}",
        raw_paths=[f"sim://{request.experiment_id}"],
    )

  def poll(self, result: CtcResult, *, want_partial: bool = True) -> CtcResult:
    truth = self.truth_lookup.get(result.experiment_id)
    if truth is None:
      result.state = "failed"
      result.error = "no simulated ground truth registered for this experiment"
      return result
    request = self._request_for(result)
    fraction = self.reveal_fraction if want_partial else 1.0
    classes = []
    for preset in request["presets"]:
      for testset in request["testsets"]:
        classes.append(self._class_result(truth, testset, preset, fraction))
    result.classes = classes
    result.fraction_complete = fraction
    result.state = "done" if fraction >= 1.0 else "partial"
    if result.state == "done":
      result.finished_ts = self.clock + self.latency_s
    return result

  def collect(self, result: CtcResult) -> CtcResult:
    return self.poll(result, want_partial=False)

  def cancel(self, result: CtcResult, reason: str) -> bool:
    result.state = "cancelled"
    result.error = reason
    return True

  def capacity(self) -> CtcCapacity:
    return CtcCapacity(
        available_slots=8.0, typical_latency_s=self.latency_s,
        max_arms_per_round=16, supports_partial=True, supports_anchor_reuse=True,
        notes=["simulated back end: results are instant and exactly reproducible"],
    )

  # -- internals -----------------------------------------------------------

  _requests: dict = field(default_factory=dict)

  def remember(self, result: CtcResult, request: CtcRequest) -> None:
    self._requests[result.job_id] = {
        "presets": list(request.presets), "testsets": list(request.testsets)
    }

  def _request_for(self, result: CtcResult) -> dict:
    return self._requests.get(result.job_id, {"presets": [4], "testsets": ["a1", "a2"]})

  def _class_result(
      self, truth: effects.PatchTruth, testset: str, preset: int, fraction: float
  ) -> CtcClassResult:
    clips = SIM_TESTSETS.get(testset.lower(), SIM_TESTSETS["a2"])
    shown = max(1, int(round(len(clips) * fraction)))
    modifier = truth.class_modifier.get(testset.lower(), 1.0)
    sequences = []
    for clip in clips[:shown]:
      damage = effects.sequence_damage(truth, clip) * modifier
      speed = effects.sequence_speed(truth, clip) * modifier * effects.preset_modifier(preset)
      noise = effects._rng("ctcnoise", truth.patch_id, clip, preset)
      sequences.append(
          CtcSequenceResult(
              sequence=clip,
              bdrate={
                  "PSNR-Y": round(damage + noise.gauss(0.0, 0.01), 4),
                  "PSNR-YUV": round(damage * 1.05 + noise.gauss(0.0, 0.01), 4),
              },
              enc_time_ratio_pct=round(
                  100.0 * (1.0 - speed) * (1.0 + noise.gauss(0.0, 0.004)), 3
              ),
              dec_time_ratio_pct=round(100.0 + noise.gauss(0.0, 0.5), 3),
          )
      )
    out = CtcClassResult(
        testset=testset, config="ra", preset=preset, sequences=sequences,
        complete=shown == len(clips), n_expected=len(clips),
    )
    for metric in ("PSNR-Y", "PSNR-YUV"):
      values = [s.bdrate[metric] for s in sequences]
      out.average_bdrate[metric] = round(sum(values) / len(values), 4)
    out.average_enc_time_ratio_pct = round(
        sum(s.enc_time_ratio_pct for s in sequences) / len(sequences), 3
    )
    out.average_dec_time_ratio_pct = round(
        sum(s.dec_time_ratio_pct for s in sequences) / len(sequences), 3
    )
    return out
