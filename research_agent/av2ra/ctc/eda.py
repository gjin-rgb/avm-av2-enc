"""Cloud EDA back end. With :mod:`av2ra.providers.google`, the whole internal surface.

Wraps the two internal scripts the prior research used by hand:

  ``kick_off_av2ctc_eda.sh``   submits an anchor or candidate job set
  ``compare_eda_runs.py``      polls status and produces the comparison

Three corrections to the draft invocation are implemented here, each of which
costs real cluster time if left alone:

1. **Per-class frame counts.** The draft passes a single ``--frame_count=33``.
   Every report in the corpus runs A1 at 17 frames and A2 at 33. One scalar
   cannot express that, and using 33 everywhere doubles the cost of the 4K half
   of each round for numbers nobody compares against.

2. **Anchor reuse.** An anchor job set for a given (base SHA, preset, testset)
   is a durable artifact; the corpus reuses the same anchor invocation UUIDs
   across several rounds. This back end records anchor invocations and reuses
   them, which halves the cost of every round after the first on a given base.

3. **Arm identity.** Each arm is recorded as (anchor SHA, patch SHA, preset,
   testset, invocation UUID), the tuple the corpus's own traceability matrices
   use. Without it, two rounds' numbers cannot be reconciled -- which is exactly
   how the prior effort ended up with two different CTC results for the same
   three patches and no way to tell which was right.

Every path and flag is configurable, because these scripts live outside the AVM
tree and their location is a property of a particular internal checkout.
"""

from __future__ import annotations

import csv
import io
import os
import re
import time
from dataclasses import dataclass, field

from ..core.models import CtcClassResult, CtcRequest, CtcResult, CtcSequenceResult
from ..util import log, proc
from ..util.io import ensure_dir, read_json, write_json_atomic
from .contract import CtcBackend, CtcCapacity, frames_for

LOG = log.get("ctc.eda")

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

#: Column aliases seen in EDA comparison output, normalised to the names the
#: core uses. Kept as data so a change in the internal tool is a config edit.
METRIC_ALIASES = {
    "psnr-y": "PSNR-Y", "psnr_y": "PSNR-Y", "bdrate-y": "PSNR-Y",
    "psnr-u": "PSNR-U", "psnr_u": "PSNR-U",
    "psnr-v": "PSNR-V", "psnr_v": "PSNR-V",
    "psnr-yuv": "PSNR-YUV", "psnr_yuv": "PSNR-YUV", "overall": "PSNR-YUV",
    "ssim": "SSIM", "ms-ssim": "MS-SSIM", "msssim": "MS-SSIM",
    "vmaf": "VMAF", "vmaf-neg": "VMAF-NEG", "vmaf_neg": "VMAF-NEG",
}
_TIME_COLUMNS = {"enctime": "enc", "enc_time": "enc", "encodetime": "enc",
                 "dectime": "dec", "dec_time": "dec", "decodetime": "dec"}


@dataclass
class EdaConfig:
  kickoff_script: str
  compare_script: str
  project: str = "blade"
  timing_accuracy: str = "high"
  chunk_size: int = 65
  crosscheck: int = 0
  extra_cmake_args: str = "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"
  python: str = "python3"
  state_dir: str = ""
  poll_timeout_s: float = 900.0
  submit_timeout_s: float = 3600.0
  anchor_reuse: bool = True
  frame_count_override: int | None = None


@dataclass
class EdaArm:
  """One submitted job set. This is the traceability record."""

  role: str            # anchor | candidate
  testset: str
  preset: int
  invocation: str = ""
  tag: str = ""
  sha: str = ""
  folder: str = ""
  submitted_ts: float = field(default_factory=time.time)


class EdaBackend(CtcBackend):

  name = "eda"

  def __init__(self, config: EdaConfig):
    self.config = config
    self.state_dir = ensure_dir(
        os.path.expanduser(config.state_dir or "~/.av2ra/eda")
    )

  # -- anchor registry -----------------------------------------------------

  def _anchor_path(self) -> str:
    return os.path.join(self.state_dir, "anchors.json")

  def _anchor_key(self, base_sha: str, preset: int, testset: str, config: str) -> str:
    return f"{base_sha[:12]}:{preset}:{testset.lower()}:{config.lower()}"

  def lookup_anchor(self, base_sha: str, preset: int, testset: str, config: str) -> dict | None:
    if not self.config.anchor_reuse:
      return None
    anchors = read_json(self._anchor_path(), {}) or {}
    return anchors.get(self._anchor_key(base_sha, preset, testset, config))

  def record_anchor(self, base_sha: str, preset: int, testset: str, config: str, arm: EdaArm) -> None:
    anchors = read_json(self._anchor_path(), {}) or {}
    anchors[self._anchor_key(base_sha, preset, testset, config)] = {
        "invocation": arm.invocation, "folder": arm.folder, "tag": arm.tag,
        "sha": arm.sha, "ts": arm.submitted_ts,
    }
    write_json_atomic(self._anchor_path(), anchors)

  # -- submission ----------------------------------------------------------

  def build_kickoff_argv(
      self, *, tag: str, worktree: str, preset: int, testset: str, config: str
  ) -> list[str]:
    cfg = self.config
    return [
        cfg.kickoff_script,
        tag,
        worktree,
        cfg.extra_cmake_args,
        f"--crosscheck={cfg.crosscheck}",
        f"--project={cfg.project}",
        f"--chunk_size={cfg.chunk_size}",
        f"--timing_accuracy={cfg.timing_accuracy}",
        f"--test_configs={config}",
        f"--testsets={testset}",
        f"--frame_count={frames_for(testset, cfg.frame_count_override)}",
        f"--extra_params=--cpu-used={preset}",
    ]

  def submit(self, request: CtcRequest) -> CtcResult:
    problems = self.validate(request)
    if problems:
      return CtcResult(
          experiment_id=request.experiment_id, state="failed",
          error="; ".join(problems),
      )
    if not os.path.exists(self.config.kickoff_script):
      return CtcResult(
          experiment_id=request.experiment_id, state="failed",
          error=(
              f"EDA kickoff script not found at {self.config.kickoff_script}. "
              "Set ctc.kickoff_script in the site profile, or use --site=local."
          ),
      )

    arms: list[EdaArm] = []
    for preset in request.presets:
      for testset in request.testsets:
        for config in request.configs:
          cached = self.lookup_anchor(request.base_sha, preset, testset, config)
          if cached:
            arms.append(
                EdaArm(role="anchor", testset=testset, preset=preset,
                       invocation=cached["invocation"], tag=cached.get("tag", ""),
                       sha=request.base_sha, folder=cached.get("folder", ""))
            )
            LOG.info(
                "reusing anchor invocation %s for %s/s%d",
                cached["invocation"][:8], testset, preset,
            )
          else:
            tag = _tag(request.experiment_id, "base", preset, testset)
            arm = self._kick_off(tag, request.anchor_ref or request.patch_ref,
                                 preset, testset, config, role="anchor",
                                 sha=request.base_sha)
            arms.append(arm)
            if arm.invocation:
              self.record_anchor(request.base_sha, preset, testset, config, arm)

          tag = _tag(request.experiment_id, "cand", preset, testset)
          arms.append(
              self._kick_off(tag, request.patch_ref, preset, testset, config,
                             role="candidate", sha=request.patch_ref)
          )

    failed = [a for a in arms if not a.invocation]
    result = CtcResult(
        experiment_id=request.experiment_id,
        job_id=f"eda:{request.experiment_id}:{int(time.time())}",
        state="failed" if failed and len(failed) == len(arms) else "running",
        submitted_ts=time.time(),
        anchor_job_id=",".join(a.invocation for a in arms if a.role == "anchor"),
        candidate_job_id=",".join(a.invocation for a in arms if a.role == "candidate"),
        error="; ".join(f"{a.role}/{a.testset}/s{a.preset} did not return an invocation id"
                        for a in failed),
    )
    self._save_arms(result.job_id, arms, request)
    return result

  def _kick_off(
      self, tag: str, worktree: str, preset: int, testset: str, config: str,
      *, role: str, sha: str,
  ) -> EdaArm:
    argv = self.build_kickoff_argv(
        tag=tag, worktree=worktree, preset=preset, testset=testset, config=config
    )
    run = proc.run(argv, timeout=self.config.submit_timeout_s)
    text = (run.stdout or "") + (run.stderr or "")
    match = _UUID.search(text)
    folder = ""
    folder_match = re.search(r"(/[\w./-]*(?:eda|invocation)[\w./-]*)", text)
    if folder_match:
      folder = folder_match.group(1)
    if not match:
      LOG.error("no invocation id from %s: %s", tag, run.tail(10))
    return EdaArm(
        role=role, testset=testset, preset=preset,
        invocation=match.group(0) if match else "", tag=tag, sha=sha, folder=folder,
    )

  def _arms_path(self, job_id: str) -> str:
    return os.path.join(self.state_dir, job_id.replace(":", "_") + ".json")

  def _save_arms(self, job_id: str, arms: list[EdaArm], request: CtcRequest) -> None:
    write_json_atomic(
        self._arms_path(job_id),
        {
            "request": request.to_dict(),
            "arms": [a.__dict__ for a in arms],
        },
    )

  def _load_arms(self, job_id: str) -> tuple[list[EdaArm], dict]:
    data = read_json(self._arms_path(job_id), {}) or {}
    return ([EdaArm(**a) for a in data.get("arms", [])], data.get("request", {}))

  # -- polling and collection ---------------------------------------------

  def poll(self, result: CtcResult, *, want_partial: bool = True) -> CtcResult:
    arms, request = self._load_arms(result.job_id)
    if not arms:
      result.error = result.error or f"no arm record for {result.job_id}"
      return result
    classes: list[CtcClassResult] = []
    complete_fractions = []
    for preset in sorted({a.preset for a in arms}):
      for testset in sorted({a.testset for a in arms}):
        anchor = next(
            (a for a in arms if a.role == "anchor" and a.preset == preset and a.testset == testset),
            None,
        )
        candidate = next(
            (a for a in arms if a.role == "candidate" and a.preset == preset and a.testset == testset),
            None,
        )
        if not (anchor and candidate and anchor.invocation and candidate.invocation):
          continue
        parsed = self._compare(anchor, candidate, want_partial=want_partial)
        if parsed:
          classes.append(parsed)
          expected = parsed.n_expected or max(len(parsed.sequences), 1)
          complete_fractions.append(len(parsed.sequences) / expected)
    result.classes = classes
    result.fraction_complete = (
        sum(complete_fractions) / len(complete_fractions) if complete_fractions else 0.0
    )
    if classes and all(c.complete for c in classes):
      result.state = "done"
      result.finished_ts = time.time()
    elif classes:
      result.state = "partial"
    return result

  def collect(self, result: CtcResult) -> CtcResult:
    return self.poll(result, want_partial=False)

  def _compare(self, anchor: EdaArm, candidate: EdaArm, *, want_partial: bool) -> CtcClassResult | None:
    argv = [
        self.config.python, self.config.compare_script,
        anchor.folder or anchor.invocation,
        candidate.folder or candidate.invocation,
        "--download=1", "--status=1", "--csv=1",
    ]
    if want_partial:
      argv.append("--partial=4")
    run = proc.run(argv, timeout=self.config.poll_timeout_s)
    if not run.ok and not run.stdout:
      LOG.warning("compare failed for %s: %s", candidate.tag, run.tail(6))
      return None
    parsed = parse_comparison(run.stdout, testset=anchor.testset, preset=anchor.preset)
    if parsed:
      parsed.complete = not _looks_partial(run.stdout)
    return parsed

  def cancel(self, result: CtcResult, reason: str) -> bool:
    LOG.warning(
        "cancel requested for %s (%s); the EDA wrapper exposes no cancel verb, "
        "so the arms will run to completion. Recording the decision so the "
        "quota accounting is honest.",
        result.job_id, reason,
    )
    result.state = "cancelled"
    result.error = reason
    return False

  def capacity(self) -> CtcCapacity:
    return CtcCapacity(
        available_slots=1.0,
        typical_latency_s=6 * 3600.0,
        max_arms_per_round=9,
        supports_partial=True,
        supports_anchor_reuse=self.config.anchor_reuse,
        notes=[
            "observed throughput in the prior corpus: 6-18 job sets per round, "
            "roughly 8-9 arms x 2 test sets; a round is hours to a day",
            "arms within a round are independent, so latency is set by the "
            "slowest arm: prefer few rounds with many arms",
        ],
    )


def _tag(experiment_id: str, role: str, preset: int, testset: str) -> str:
  stamp = time.strftime("%m%d")
  return f"av2ra_{stamp}_{experiment_id}_{role}_{testset}_s{preset}".lower()


def _looks_partial(text: str) -> bool:
  return bool(re.search(r"\bpartial\b|\bincomplete\b|\brunning\b", text, re.IGNORECASE))


def parse_comparison(text: str, *, testset: str, preset: int, config: str = "ra") -> CtcClassResult | None:
  """Parse the EDA comparison output into the infrastructure-free result schema.

  Accepts the CSV form produced by ``--csv=1`` and falls back to the pipe- or
  whitespace-separated table that appears in the shared reports. Both are
  handled because the exact shape depends on the internal tool's version, and a
  parser that only understands one of them fails silently at the worst moment --
  after the cluster time has already been spent.
  """
  rows = _read_rows(text)
  if not rows:
    return None
  header = [h.strip().lower().replace(" ", "").replace("%", "") for h in rows[0]]
  metric_columns: dict[int, str] = {}
  time_columns: dict[int, str] = {}
  name_column = 0
  for index, name in enumerate(header):
    if name in METRIC_ALIASES:
      metric_columns[index] = METRIC_ALIASES[name]
    elif name in _TIME_COLUMNS:
      time_columns[index] = _TIME_COLUMNS[name]
    elif name in ("sequence", "clip", "name", "video", ""):
      name_column = index
  if not metric_columns and not time_columns:
    return None

  sequences: list[CtcSequenceResult] = []
  averages: dict[str, list[float]] = {}
  average_row: CtcSequenceResult | None = None
  for row in rows[1:]:
    if len(row) <= max([name_column, *metric_columns, *time_columns] or [0]):
      continue
    label = row[name_column].strip().strip("*").strip()
    if not label:
      continue
    entry = CtcSequenceResult(sequence=label)
    for index, metric in metric_columns.items():
      value = _percent(row[index])
      if value is not None:
        entry.bdrate[metric] = value
        averages.setdefault(metric, []).append(value)
    for index, kind in time_columns.items():
      value = _percent(row[index])
      if value is None:
        continue
      if kind == "enc":
        entry.enc_time_ratio_pct = value
      else:
        entry.dec_time_ratio_pct = value
    if re.match(r"(?i)^\**\s*(average|overall|mean|total)", label):
      average_row = entry
      continue
    sequences.append(entry)

  result = CtcClassResult(
      testset=testset, config=config, preset=preset, sequences=sequences,
      n_expected=len(sequences),
  )
  if average_row:
    result.average_bdrate = dict(average_row.bdrate)
    result.average_enc_time_ratio_pct = average_row.enc_time_ratio_pct
    result.average_dec_time_ratio_pct = average_row.dec_time_ratio_pct
  else:
    for metric, values in averages.items():
      result.average_bdrate[metric] = sum(values) / len(values)
    enc = [s.enc_time_ratio_pct for s in sequences]
    dec = [s.dec_time_ratio_pct for s in sequences]
    if enc:
      result.average_enc_time_ratio_pct = sum(enc) / len(enc)
    if dec:
      result.average_dec_time_ratio_pct = sum(dec) / len(dec)
  return result if sequences else None


def _read_rows(text: str) -> list[list[str]]:
  lines = [line for line in text.splitlines() if line.strip()]
  if not lines:
    return []
  pipe_rows = [
      [cell.strip() for cell in line.strip().strip("|").split("|")]
      for line in lines
      if line.count("|") >= 3 and not re.match(r"^[\s|:-]+$", line)
  ]
  if len(pipe_rows) >= 2:
    return pipe_rows
  comma_lines = [line for line in lines if line.count(",") >= 3]
  if len(comma_lines) >= 2:
    return [row for row in csv.reader(io.StringIO("\n".join(comma_lines))) if row]
  space_rows = [re.split(r"\s{2,}|\t", line.strip()) for line in lines]
  space_rows = [row for row in space_rows if len(row) >= 3]
  return space_rows if len(space_rows) >= 2 else []


def _percent(cell: str) -> float | None:
  cleaned = cell.strip().strip("*").replace("%", "").replace("+", "")
  if not cleaned or cleaned in ("-", "n/a", "N/A"):
    return None
  try:
    return float(cleaned)
  except ValueError:
    return None
