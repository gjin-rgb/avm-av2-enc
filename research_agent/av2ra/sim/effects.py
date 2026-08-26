"""A synthetic encoder world, so the whole research loop can be tested.

Nothing about an autonomous research system is trustworthy until you have
watched it run end to end -- ideation, patching, screening, gating, analysis,
CTC, promotion, learning -- and inspected what it concluded. Doing that with
the real encoder costs hours per experiment (a 176x144 clip at ``--cpu-used=4``
takes about 14 seconds *per frame* on a 2.8 GHz core, so a 1080p CTC-shaped
screening pass is a many-hour job, not the 1.5 minutes the draft protocol
assumes). Doing it with a simulated encoder costs milliseconds.

So this module defines a *ground truth*: a deterministic function from a patch
to the effect it "really" has, and from an encode request to the numbers an
encoder would report. The simulated world deliberately reproduces the awkward
phenomena the real corpus documents, because a harness that only works on
well-behaved data is a harness that has not been tested:

  * **Inert patches.** Some changes never fire, and report exactly 0.00%.
  * **Class asymmetry.** 4K and 1080p disagree, sometimes in opposite
    directions; in the corpus this happened on ten arms.
  * **Per-sequence spread.** Speed is nearly uniform across clips (CV ~12%)
    while quality cost varies sevenfold (CV ~89%), so one clip carries the
    damage.
  * **Local/CTC disagreement.** Short low-resolution screens have the wrong
    *sign* on a fraction of patches. The simulator reproduces this on purpose,
    so the acceptance logic is exercised against the failure it is designed for.
  * **Measurement noise with a floor**, and a small systematic bias on the
    patched arm, as the corpus's six inert arms implied.

Every draw is seeded from stable identifiers, so a simulated run is exactly
reproducible and a test can assert on specific outcomes.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field


def _seed(*parts: object) -> int:
  key = "|".join(str(p) for p in parts)
  return int(hashlib.sha256(key.encode()).hexdigest()[:12], 16)


def _rng(*parts: object) -> random.Random:
  return random.Random(_seed(*parts))


@dataclass
class PatchTruth:
  """What a patch really does, in the simulated world."""

  patch_id: str
  inert: bool = False
  bit_exact: bool = False
  # Fraction of encoder work removed at the reference preset, before class and
  # preset modifiers. Negative means the patch adds work.
  work_removed: float = 0.0
  # Quality cost in BD-rate percent at the reference preset.
  quality_cost: float = 0.0
  class_modifier: dict[str, float] = field(default_factory=dict)
  sequence_damage: dict[str, float] = field(default_factory=dict)
  crashes_on: set[str] = field(default_factory=set)
  decode_fails: bool = False
  nondeterministic: bool = False
  # Multiplier applied to the effect when measured on the small/low-resolution
  # screening set: this is where local/CTC disagreement comes from.
  screen_bias_speed: float = 1.0
  screen_bias_quality: float = 1.0


def make_truth(
    patch_id: str,
    *,
    profile_share: float = 0.2,
    aggressiveness: float = 0.5,
    mechanism: str = "approx",
    difficulty: float = 1.0,
) -> PatchTruth:
  """Draw a patch's true behaviour from its identity.

  ``difficulty`` scales how unforgiving the world is: 1.0 reproduces the prior
  corpus's hit rate of roughly one clear win in ten. Tests that want a
  guaranteed winner pass a lower value.
  """
  rng = _rng("truth", patch_id, mechanism, round(aggressiveness, 3))
  truth = PatchTruth(patch_id=patch_id)

  if mechanism in ("reuse", "kernel"):
    truth.bit_exact = True
    truth.quality_cost = 0.0
    # The corpus's three bit-exact reuse patches were all *slower* at 4K: a
    # memo lookup unpaid by its hit rate, an arena worse for locality than the
    # allocations it replaced. Reproduced here as a real possibility.
    truth.work_removed = rng.gauss(0.01, 0.02) * profile_share * 4
    truth.class_modifier = {"a1": rng.uniform(-1.2, 0.9), "a2": rng.uniform(-0.4, 1.1)}
    if rng.random() < 0.15 * difficulty:
      truth.inert = True
    return truth

  if rng.random() < 0.18 * difficulty:
    truth.inert = True
    return truth
  if rng.random() < 0.05 * difficulty:
    truth.crashes_on = {"high_qp"}
  if rng.random() < 0.02 * difficulty:
    truth.decode_fails = True

  # Speed scales with how much of the profile the patch touches and how
  # aggressively it prunes; quality cost scales super-linearly with
  # aggressiveness, which is why threshold tuning slides along the trade-off.
  truth.work_removed = profile_share * aggressiveness * rng.uniform(0.25, 0.95)
  truth.quality_cost = (
      truth.work_removed * rng.uniform(2.0, 14.0) * (aggressiveness ** 1.4) * difficulty
  )
  truth.class_modifier = {
      "a1": rng.uniform(0.7, 1.35),
      "a2": rng.uniform(0.6, 1.5),
      "screen": rng.uniform(0.7, 1.3),
      "holdout": rng.uniform(0.6, 1.25),
  }
  # Local screens can invert. Rare but real: 3 of 10 in the corpus.
  if rng.random() < 0.3:
    truth.screen_bias_quality = rng.uniform(-1.2, 0.4)
  else:
    truth.screen_bias_quality = rng.uniform(0.3, 1.4)
  truth.screen_bias_speed = rng.uniform(0.6, 1.6)
  return truth


def sequence_damage(truth: PatchTruth, sequence: str) -> float:
  """Per-clip quality cost, with the heavy-tailed spread the corpus reports."""
  if truth.inert or truth.bit_exact:
    return 0.0
  if sequence in truth.sequence_damage:
    return truth.sequence_damage[sequence]
  rng = _rng("seqdamage", truth.patch_id, sequence)
  # Log-normal: most clips near the mean, one far above it.
  factor = math.exp(rng.gauss(-0.35, 0.85))
  return truth.quality_cost * factor


def sequence_speed(truth: PatchTruth, sequence: str) -> float:
  """Per-clip fraction of work removed. Much tighter spread than quality."""
  if truth.inert:
    return 0.0
  rng = _rng("seqspeed", truth.patch_id, sequence)
  return truth.work_removed * rng.gauss(1.0, 0.12)


def preset_modifier(preset: int) -> float:
  """Pruning heuristics remove redundancy that only exists in deeper searches.

  The corpus measured +30.7% at Speed 2 against +20.3% at Speed 4 for the same
  patch family at essentially unchanged BD-rate, so the effect grows as the
  preset gets slower.
  """
  return 1.0 + 0.12 * (4 - preset)


#: Measurement noise of the simulated harness, as a coefficient of variation.
#: ``instructions`` is near-deterministic; wall-derived metrics carry the ~2%
#: floor the prior research measured on a shared machine.
NOISE_CV = {"instructions": 0.0004, "cx_time_s": 0.021, "cpu_s": 0.024}

#: Small systematic slowdown on the patched arm, matching the +1.6% bias six
#: inert arms implied in the corpus. Present so the null-arm gate has something
#: to find.
PATCHED_ARM_BIAS = 0.006
