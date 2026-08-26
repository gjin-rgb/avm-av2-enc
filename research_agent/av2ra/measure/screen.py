"""The local screening pass: an A/B experiment, not a pair of benchmark runs.

Three decisions in here account for most of the difference between a screening
number you can act on and one you cannot.

**Interleaving.** The anchor and the candidate are measured in the same pass,
adjacent in time, with the arm order flipped between repetitions (A,B then
B,A). A machine drifts: another tenant starts, the CPU package heats up and
clocks down, the page cache warms. Running one arm to completion and then the
other converts that drift directly into a fake effect, and there is no
statistic that can remove it afterwards.

**Selective anchor caching.** The draft protocol caches the whole anchor result
per commit "to halve local screening compute". Half of that is right and half
is a trap. Rate and PSNR are deterministic functions of (source, binary, flags)
-- caching them is free and correct. *Timing is not*: a cached anchor time was
measured on a different machine state, so comparing today's candidate against
it silently reintroduces exactly the drift interleaving exists to remove. This
module caches quality and always re-measures time.

**Pinning and slot budgeting.** Each worker gets a disjoint, fixed set of
logical CPUs sized to the encode's own ``--threads``. Oversubscribing the
machine makes every measurement a measurement of the scheduler.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import random
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from ..core.models import EncodeResult
from ..util import log
from .encode import ClipSpec, EncodeConfig, EncodeJob, run_encode

LOG = log.get("screen")


@dataclass
class ClipSet:
  """A named set of clips with a role. Roles matter for integrity.

  ``screen`` clips are what the agent optimises against and is therefore
  allowed to overfit. ``holdout`` clips are never shown to ideation and never
  used to tune a threshold; they exist so that an effect measured on the screen
  set can be checked against clips the search never saw. An agent that improves
  on the screen set and not on the holdout set has found a property of the
  screen set.
  """

  name: str
  role: str                       # "screen" | "holdout" | "calibration"
  clips: list[ClipSpec] = field(default_factory=list)
  qps: list[int] = field(default_factory=lambda: [160, 185, 210])
  frames: int = 17
  test_cfg: str = "RA"

  def encode_points(self) -> list[tuple[ClipSpec, int]]:
    return [(clip, qp) for clip in self.clips for qp in self.qps]


@dataclass
class ScreenPlan:
  """Everything needed to execute and reproduce one screening pass."""

  experiment_id: str
  clip_set: ClipSet
  preset: int
  anchor_encoder: str
  candidate_encoder: str
  workdir: str
  reps: int = 2
  workers: int = 2
  cpus_per_worker: int = 2
  use_perf: bool = False
  keep_bitstreams: bool = False
  timeout_per_encode_s: float | None = None
  anchor_build_fingerprint: str = ""
  candidate_build_fingerprint: str = ""

  @property
  def total_encodes(self) -> int:
    return len(self.clip_set.encode_points()) * 2 * self.reps

  def seed(self) -> int:
    return int(hashlib.sha256(self.experiment_id.encode()).hexdigest()[:12], 16)


def plan_jobs(plan: ScreenPlan) -> list[EncodeJob]:
  """Build the interleaved job list.

  Ordering rule: iterate repetitions outermost, then a seeded shuffle of the
  (clip, QP) points, and inside each point emit both arms back to back with the
  order flipped on odd repetitions. The shuffle is seeded from the experiment
  id so a re-run of the same experiment executes in the same order -- a
  reproducible measurement includes reproducing the sequencing.
  """
  jobs: list[EncodeJob] = []
  points = plan.clip_set.encode_points()
  rng = random.Random(plan.seed())
  for rep in range(plan.reps):
    order = list(points)
    rng.shuffle(order)
    for clip, qp in order:
      cfg = EncodeConfig(
          preset=plan.preset,
          qp=qp,
          frames=plan.clip_set.frames,
          test_cfg=plan.clip_set.test_cfg,
      )
      arms = ["anchor", "candidate"] if rep % 2 == 0 else ["candidate", "anchor"]
      for arm in arms:
        encoder = plan.anchor_encoder if arm == "anchor" else plan.candidate_encoder
        stem = f"{arm}_{clip.name}_q{qp}_r{rep}"
        jobs.append(
            EncodeJob(
                clip=clip,
                cfg=cfg,
                arm=arm,
                encoder=encoder,
                out_path=os.path.join(plan.workdir, "bitstreams", stem + ".obu"),
                rep=rep,
                use_perf=plan.use_perf,
                timeout_s=plan.timeout_per_encode_s,
                keep_bitstream=plan.keep_bitstreams,
                log_path=os.path.join(plan.workdir, "logs", stem + ".log"),
            )
        )
  return jobs


def assign_affinities(workers: int, cpus_per_worker: int) -> list[list[int]]:
  """Disjoint CPU sets, one per worker, leaving the tail free for the agent.

  Returns an empty list when the machine cannot honour the request; callers
  then run unpinned and the tier records that its timings are less trustworthy.
  """
  try:
    available = sorted(os.sched_getaffinity(0))
  except AttributeError:
    return []
  if len(available) < workers * cpus_per_worker:
    return []
  return [
      available[i * cpus_per_worker : (i + 1) * cpus_per_worker]
      for i in range(workers)
    ]


class AnchorQualityCache:
  """Caches the *quality* of anchor encodes, keyed by everything that changes it.

  Deliberately never caches a time. See the module docstring.
  """

  def __init__(self, root: str):
    self.root = root
    os.makedirs(root, exist_ok=True)

  def _key(self, job: EncodeJob, build_fingerprint: str) -> str:
    parts = "|".join([
        build_fingerprint,
        job.clip.name,
        os.path.basename(job.clip.path),
        str(os.path.getsize(job.clip.path)) if os.path.exists(job.clip.path) else "0",
        job.cfg.fingerprint(),
    ])
    return hashlib.sha256(parts.encode()).hexdigest()[:24]

  def get(self, job: EncodeJob, build_fingerprint: str) -> dict | None:
    from ..util.io import read_json

    return read_json(os.path.join(self.root, self._key(job, build_fingerprint) + ".json"))

  def put(self, job: EncodeJob, build_fingerprint: str, result: EncodeResult) -> None:
    from ..util.io import write_json_atomic

    if not result.ok:
      return
    write_json_atomic(
        os.path.join(self.root, self._key(job, build_fingerprint) + ".json"),
        {
            "bitrate_kbps": result.bitrate_kbps,
            "psnr_y": result.psnr_y,
            "psnr_u": result.psnr_u,
            "psnr_v": result.psnr_v,
            "psnr_avg": result.psnr_avg,
            "psnr_overall": result.psnr_overall,
            "bitstream_md5": result.bitstream_md5,
            "bytes_out": result.bytes_out,
            "frames": result.frames,
        },
    )


@dataclass
class ScreenOutcome:
  results: list[EncodeResult] = field(default_factory=list)
  failures: list[EncodeResult] = field(default_factory=list)
  notes: list[str] = field(default_factory=list)
  pinned: bool = False
  wall_s: float = 0.0

  @property
  def ok(self) -> bool:
    return bool(self.results) and not self.failures


def execute(
    plan: ScreenPlan,
    *,
    quality_cache: AnchorQualityCache | None = None,
    progress: Callable[[int, int], None] | None = None,
    runner: Callable[[EncodeJob], EncodeResult] = run_encode,
) -> ScreenOutcome:
  """Run the pass. Returns every result, including the failures.

  Failures are returned rather than raised: one clip that will not encode at
  one QP is a data point about the patch (it may be exactly the bug), and the
  remaining measurements are still worth having for the report.
  """
  import time

  os.makedirs(os.path.join(plan.workdir, "bitstreams"), exist_ok=True)
  os.makedirs(os.path.join(plan.workdir, "logs"), exist_ok=True)
  jobs = plan_jobs(plan)
  affinities = assign_affinities(plan.workers, plan.cpus_per_worker)
  outcome = ScreenOutcome(pinned=bool(affinities))
  if not affinities:
    outcome.notes.append(
        "CPU pinning unavailable or machine too small: timings carry extra noise"
    )

  started = time.time()
  done = 0
  # Jobs are consumed in plan order by a bounded pool, so the interleaving
  # survives: the two arms of a point are adjacent in the queue and therefore
  # start within one slot of each other.
  with concurrent.futures.ThreadPoolExecutor(max_workers=plan.workers) as pool:
    futures = {}
    for index, job in enumerate(jobs):
      if affinities:
        job.cpu_affinity = affinities[index % plan.workers]
      cached = None
      if quality_cache and job.arm == "anchor":
        cached = quality_cache.get(job, plan.anchor_build_fingerprint)
      futures[pool.submit(_run_one, job, cached, runner)] = job

    for future in concurrent.futures.as_completed(futures):
      job = futures[future]
      try:
        result = future.result()
      except Exception as exc:  # a harness bug must not lose the whole pass
        result = EncodeResult(
            sequence=job.clip.name, qp=job.cfg.qp, preset=job.cfg.preset,
            arm=job.arm, rep=job.rep, error=f"harness error: {exc}",
        )
      if result.ok:
        outcome.results.append(result)
        if quality_cache and job.arm == "anchor":
          quality_cache.put(job, plan.anchor_build_fingerprint, result)
      else:
        outcome.failures.append(result)
      if not job.keep_bitstream and os.path.exists(job.out_path):
        try:
          os.unlink(job.out_path)
        except OSError:
          pass
      done += 1
      if progress:
        progress(done, len(jobs))

  outcome.wall_s = time.time() - started
  outcome.results.sort(key=lambda r: (r.sequence, r.qp, r.arm, r.rep))
  return outcome


def _run_one(
    job: EncodeJob,
    cached_quality: dict | None,
    runner: Callable[[EncodeJob], EncodeResult],
) -> EncodeResult:
  """Run one encode; a cache hit still runs it, but only for the timing.

  There is no shortcut here. The encode has to happen either way to produce a
  time; the cache exists so that a *quality* discrepancy between the cached and
  fresh run is detected and reported rather than silently averaged.
  """
  result = runner(job)
  if cached_quality and result.ok:
    drift = abs(result.bitrate_kbps - cached_quality.get("bitrate_kbps", 0.0))
    if drift > 1e-6 or result.bitstream_md5 != cached_quality.get("bitstream_md5", result.bitstream_md5):
      result.error = (
          "anchor drift: this build no longer reproduces the cached anchor "
          "quality for this point"
      )
      result.ok = False
  return result


def pairs_for_metric(
    results: Sequence[EncodeResult], metric: str
) -> list[tuple[float, float]]:
  """Match anchor and candidate observations on (clip, QP, repetition).

  Unmatched observations are dropped rather than compared against a mean: a
  candidate encode that failed at one QP must not silently shift the paired
  comparison at the others.
  """
  index: dict[tuple[str, int, int, str], float] = {}
  for res in results:
    if not res.ok:
      continue
    value = getattr(res, metric, None)
    if value in (None, 0) and metric != "instructions":
      continue
    if value is None:
      continue
    index[(res.sequence, res.qp, res.rep, res.arm)] = float(value)
  pairs = []
  for (seq, qp, rep, arm), value in sorted(index.items()):
    if arm != "anchor":
      continue
    other = index.get((seq, qp, rep, "candidate"))
    if other is not None:
      pairs.append((value, other))
  return pairs


def per_sequence_pairs(
    results: Sequence[EncodeResult], metric: str
) -> dict[str, list[tuple[float, float]]]:
  out: dict[str, list[tuple[float, float]]] = {}
  for res in results:
    out.setdefault(res.sequence, [])
  for sequence in out:
    subset = [r for r in results if r.sequence == sequence]
    out[sequence] = pairs_for_metric(subset, metric)
  return out


def choose_cost_metric(results: Iterable[EncodeResult]) -> str:
  """Pick the least noisy available cost metric.

  Preference order is instructions, then the encoder's own ``cx_time``, then
  process CPU time. Instruction counts are deterministic to well under 0.1% and
  immune to co-tenancy; ``cx_time`` excludes process start-up and file I/O,
  which is most of the difference between it and wall clock on short encodes.
  """
  materialised = list(results)
  if any(r.instructions for r in materialised):
    return "instructions"
  if any(r.cx_time_s for r in materialised):
    return "cx_time_s"
  return "cpu_s"
