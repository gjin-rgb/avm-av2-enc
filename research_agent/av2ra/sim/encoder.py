"""A simulated encoder that satisfies the same contract as ``avmenc``.

``SimulatedEncoder.run`` is a drop-in replacement for
:func:`av2ra.measure.encode.run_encode`, so every tier, gate and statistic in
this package runs against it unchanged. That is the point: the code under test
is the real code, and only the encoder is fake.

The rate/quality model is a plain RD curve -- rate falls and PSNR falls as QP
rises, both smooth and monotone -- perturbed by the patch's true effect. It is
not meant to be a faithful model of AV2. It is meant to be a *coherent* one, so
that BD-rate is well defined, bit-exactness means something, and the harness
cannot pass by accident.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from ..core.models import EncodeResult
from ..measure.encode import EncodeJob
from . import effects


@dataclass
class SimulatedEncoder:
  truth: effects.PatchTruth
  class_of_clip: dict[str, str] | None = None
  base_instructions: float = 1.9e11
  fail_rate: float = 0.0

  def clip_class(self, name: str) -> str:
    return (self.class_of_clip or {}).get(name, "a2")

  # -- the RD model --------------------------------------------------------

  def _anchor_point(self, clip: str, qp: int, preset: int) -> tuple[float, float, float]:
    """(bitrate_kbps, psnr_y, instruction_count) for the unpatched encoder."""
    rng = effects._rng("clip", clip)
    complexity = rng.uniform(0.6, 1.8)
    # Rate roughly halves every ~25 QP steps; PSNR falls ~6 dB per doubling.
    rate = 12000.0 * complexity * math.exp(-(qp - 110) / 42.0)
    psnr = 46.0 - 9.0 * math.log2(1.0 + (qp - 100) / 55.0) - 2.0 * (complexity - 1.0)
    work = self.base_instructions * complexity * (1.0 + 0.35 * (4 - preset))
    # Low QP means more coefficients and deeper searches.
    work *= 1.0 + 0.9 * math.exp(-(qp - 110) / 60.0)
    return (rate, psnr, work)

  def _effect(self, clip: str, qp: int, preset: int, arm: str) -> tuple[float, float]:
    """(work_multiplier, bdrate_shift_pct) for the given arm."""
    if arm == "anchor" or arm == "anchor_null":
      return (1.0, 0.0)
    truth = self.truth
    if truth.inert:
      return (1.0, 0.0)
    class_name = self.clip_class(clip)
    modifier = truth.class_modifier.get(class_name, 1.0)
    speed_gain = effects.sequence_speed(truth, clip) * modifier * effects.preset_modifier(preset)
    damage = effects.sequence_damage(truth, clip) * modifier
    if class_name in ("screen", "holdout"):
      speed_gain *= truth.screen_bias_speed
      damage *= truth.screen_bias_quality
    return (max(0.05, 1.0 - speed_gain), damage)

  # -- the runner ----------------------------------------------------------

  def run(self, job: EncodeJob) -> EncodeResult:
    clip, qp, preset, arm = job.clip.name, job.cfg.qp, job.cfg.preset, job.arm
    result = EncodeResult(
        sequence=clip, qp=qp, preset=preset, arm=arm, rep=job.rep
    )
    truth = self.truth
    if arm == "candidate" and "high_qp" in truth.crashes_on and qp >= 210:
      result.error = "simulated encoder crash: assertion failed in tx_search.c"
      return result

    rate, psnr, work = self._anchor_point(clip, qp, preset)
    multiplier, damage_pct = self._effect(clip, qp, preset, arm)

    # A BD-rate cost of d% means the candidate needs d% more bits for the same
    # quality; applying it as a rate shift keeps the curves comparable.
    result.bitrate_kbps = rate * (1.0 + damage_pct / 100.0)
    result.psnr_y = psnr
    result.psnr_u = psnr + 3.2
    result.psnr_v = psnr + 3.1
    result.psnr_avg = psnr + 1.0
    result.psnr_overall = psnr + 0.8

    instructions = work * multiplier
    if arm == "candidate":
      instructions *= 1.0 + effects.PATCHED_ARM_BIAS
    noise = effects._rng("noise", clip, qp, preset, arm, job.rep)
    result.instructions = int(
        instructions * (1.0 + noise.gauss(0.0, effects.NOISE_CV["instructions"]))
    )
    result.cx_time_s = (
        instructions / 4.4e9 * (1.0 + noise.gauss(0.0, effects.NOISE_CV["cx_time_s"]))
    )
    result.cpu_s = result.cx_time_s * 1.03
    result.wall_s = result.cpu_s * 1.05
    result.frames = job.cfg.frames
    result.bytes_out = max(1, int(result.bitrate_kbps * 40))

    # The bitstream identity is what the activation and determinism gates read,
    # so it must depend on exactly the things that really change a bitstream:
    # the source, the operating point, and -- unless the patch is bit-exact or
    # inert -- the patch itself.
    parts = [clip, str(qp), str(preset)]
    if arm == "candidate" and not (truth.bit_exact or truth.inert):
      parts.append(truth.patch_id)
    if truth.nondeterministic and arm == "candidate":
      parts.append(str(job.rep))
    result.bitstream_md5 = hashlib.md5("|".join(parts).encode()).hexdigest()
    result.decoded_md5 = "" if truth.decode_fails and arm == "candidate" else result.bitstream_md5
    result.ok = True
    return result

  def decode(self, bitstream_md5: str) -> tuple[bool, str, str]:
    if self.truth.decode_fails:
      return (False, "", "simulated decoder rejected the bitstream: invalid OBU syntax")
    return (True, bitstream_md5, "")
