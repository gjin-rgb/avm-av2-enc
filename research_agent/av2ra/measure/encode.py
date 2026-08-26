"""Running encodes the way CTC runs them, and reading back what they produced.

The command builder here is a direct transcription of
``tools/convexhull_framework/src/VideoEncoder.py:EncodeWithAOM_AV2`` in the AVM
tree -- the script that produces the bitstreams the CTC spreadsheets are built
from. Transcribing it rather than inventing a "screening command" is the whole
point: a local screen is only worth running if it is a *scaled-down version of
the real test*, differing in clip count, frame count and QP count, and in
nothing else. Every flag that differs is a way for a local result to disagree
with CTC for reasons that have nothing to do with the patch.

Two flags in the draft screening protocol did not survive contact with the
binary. ``--cq-level`` does not exist in this encoder (CTC 2.0+ passes
``--qp``), and the binaries are named ``avmenc``/``avmdec``, not
``aomenc``/``aomdec``. Both are checked at start-up by :func:`probe_encoder`
rather than discovered on the first encode of a four-hour pass.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, field

from ..core.models import EncodeResult
from ..util import proc

# CTC RA structure. Values come from tools/convexhull_framework/src/config.yaml
# (sub_gop_size: 16, gop_size: 65).
SUB_GOP_SIZE = 16
GOP_SIZE = 65

#: The full CTC QP ladders, per test configuration.
CTC_QPS = {
    "RA": [110, 135, 160, 185, 210, 235],
    "LD": [110, 135, 160, 185, 210, 235],
    "AI": [85, 110, 135, 160, 185, 210],
}


@dataclass
class ClipSpec:
  """A test clip and everything the encoder needs to be told about it."""

  name: str
  path: str
  width: int
  height: int
  fps_num: int = 30
  fps_denom: int = 1
  bit_depth: int = 8
  file_class: str = "A2"        # CTC content class; drives bit-depth coercion
  fmt: str = "420"
  frames_available: int = 0

  @property
  def pixels(self) -> int:
    return self.width * self.height

  @property
  def is_4k(self) -> bool:
    return self.width >= 3840 and self.height >= 2160


@dataclass
class EncodeConfig:
  """One operating point: preset, QP, frame count, and the CTC test config."""

  preset: int = 2
  qp: int = 185
  frames: int = 17
  start_frame: int = 0
  test_cfg: str = "RA"
  ctc_version: str = "9.0"
  threads: int | None = None          # None: derive from the CTC tiling rules
  row_mt: int = 0
  enable_keyframe_filtering: int = 0
  enable_fwd_kf: int = 0
  intrabc_ext: int = 1
  extra_args: list[str] = field(default_factory=list)

  def fingerprint(self) -> str:
    key = "|".join(
        str(x) for x in (
            self.preset, self.qp, self.frames, self.start_frame, self.test_cfg,
            self.ctc_version, self.threads, self.row_mt,
            self.enable_keyframe_filtering, self.enable_fwd_kf,
            self.intrabc_ext, ",".join(self.extra_args),
        )
    )
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def ctc_tiling(clip: ClipSpec, test_cfg: str) -> tuple[int, int, int]:
  """(tile_rows_log2, tile_columns_log2, threads) per the CTC rules.

  Threading is part of the *definition* of the test, not a free knob: a patch
  measured single-threaded and compared against a two-thread anchor produces a
  speedup that is entirely an artifact of the harness.
  """
  if test_cfg in ("RA", "AS"):
    if clip.is_4k:
      return (0, 2, 4)
    if clip.width >= 1920 and clip.height >= 1080:
      return (0, 1, 2)
    return (0, 0, 1)
  if test_cfg == "LD":
    if clip.file_class in ("A2", "B1"):
      return (1, 2, 8)
    if clip.file_class == "A3":
      return (0, 1, 2)
    return (0, 0, 1)
  if test_cfg in ("AI", "STILL") and clip.is_4k:
    return (0, 1, 2)
  return (0, 0, 1)


def encode_bit_depth(clip: ClipSpec, ctc_version: str) -> int:
  """CTC 6.0+ codes classes A2/A4/B1 at 10 bits regardless of source depth."""
  if ctc_version in ("6.0", "7.0", "8.0", "9.0") and clip.file_class in ("A2", "A4", "B1"):
    return 10
  return clip.bit_depth


def build_encode_argv(
    encoder: str, clip: ClipSpec, cfg: EncodeConfig, out_path: str
) -> list[str]:
  """The exact argv for one CTC-shaped encode."""
  tile_rows, tile_cols, default_threads = ctc_tiling(clip, cfg.test_cfg)
  threads = cfg.threads if cfg.threads is not None else default_threads
  argv = [
      encoder,
      "--verbose",
      "--codec=av2",
      "-v",
      "--psnr",
      "--obu",
      "--frame-parallel=0",
      f"--cpu-used={cfg.preset}",
      f"--limit={cfg.frames}",
      f"--skip={cfg.start_frame}",
      "--passes=1",
      "--end-usage=q",
      f"--i{clip.fmt}",
      "--use-fixed-qp-offsets=1",
      "--deltaq-mode=0",
      "--enable-tpl-model=0",
      f"--fps={clip.fps_num}/{clip.fps_denom}",
      "-w", str(clip.width),
      "-h", str(clip.height),
      f"--input-bit-depth={clip.bit_depth}",
      f"--bit-depth={encode_bit_depth(clip, cfg.ctc_version)}",
      f"--qp={cfg.qp}",
      f"--tile-rows={tile_rows}",
      f"--tile-columns={tile_cols}",
      f"--threads={threads}",
      f"--row-mt={cfg.row_mt}",
      f"--enable-fwd-kf={cfg.enable_fwd_kf}",
      f"--enable-keyframe-filtering={cfg.enable_keyframe_filtering}",
      f"--enable-intrabc-ext={cfg.intrabc_ext}",
  ]
  if cfg.test_cfg in ("AI", "STILL"):
    argv += ["--kf-min-dist=0", "--kf-max-dist=0"]
  elif cfg.test_cfg in ("RA", "AS"):
    pyr = int(math.log2(SUB_GOP_SIZE))
    argv += [
        f"--min-gf-interval={SUB_GOP_SIZE}",
        f"--max-gf-interval={SUB_GOP_SIZE}",
        f"--gf-min-pyr-height={pyr}",
        f"--gf-max-pyr-height={pyr}",
        f"--kf-min-dist={GOP_SIZE}",
        f"--kf-max-dist={GOP_SIZE}",
        f"--lag-in-frames={SUB_GOP_SIZE + 3}",
        "--auto-alt-ref=1",
    ]
  elif cfg.test_cfg == "LD":
    pyr = int(math.log2(SUB_GOP_SIZE))
    argv += [
        "--kf-min-dist=9999", "--kf-max-dist=9999", "--lag-in-frames=0",
        f"--min-gf-interval={SUB_GOP_SIZE}", f"--max-gf-interval={SUB_GOP_SIZE}",
        f"--gf-min-pyr-height={pyr}", f"--gf-max-pyr-height={pyr}",
        "--subgop-config-str=ld",
    ]
  argv += list(cfg.extra_args)
  argv += ["-o", out_path, clip.path]
  return argv


# --------------------------------------------------------------------------
# Output parsing
# --------------------------------------------------------------------------

# apps/avmenc.c prints exactly:
#   Summary:    %10.6f  |  %2.6f  |  %2.6f  |  %2.6f  |  %2.6f    |  %2.6f        |  %6.1fs (%3.1f fps)
# for kbps, PSNR Y, U, V, Avg, Overall, encode seconds, fps.
_SUMMARY_PSNR = re.compile(
    r"^Summary:\s+([\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)"
    r"\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([\d.]+)s\s*\(([\d.]+)\s*fps\)",
    re.MULTILINE,
)
# Without --psnr the same line carries only rate and time.
_SUMMARY_PLAIN = re.compile(
    r"^Summary:\s+([\d.]+)\s*\|\s*([\d.]+)s\s*\(([\d.]+)\s*fps\)", re.MULTILINE
)
_POC = re.compile(r"^POC:\s*(\d+)\s*\[(\w+)\]", re.MULTILINE)


class SummaryParseError(ValueError):
  pass


def parse_summary(text: str) -> dict:
  """Pull the encoder's own summary line out of stdout.

  Raising on a missing summary is deliberate. An encode that produced output
  but no summary line failed partway; treating its partial bitstream as a data
  point is how a "speedup" gets recorded for an encode that stopped early.
  """
  match = _SUMMARY_PSNR.search(text)
  if match:
    return {
        "bitrate_kbps": float(match.group(1)),
        "psnr_y": float(match.group(2)),
        "psnr_u": float(match.group(3)),
        "psnr_v": float(match.group(4)),
        "psnr_avg": float(match.group(5)),
        "psnr_overall": float(match.group(6)),
        "cx_time_s": float(match.group(7)),
        "fps": float(match.group(8)),
        "frames_seen": len(_POC.findall(text)),
        "has_psnr": True,
    }
  match = _SUMMARY_PLAIN.search(text)
  if match:
    return {
        "bitrate_kbps": float(match.group(1)),
        "cx_time_s": float(match.group(2)),
        "fps": float(match.group(3)),
        "frames_seen": len(_POC.findall(text)),
        "has_psnr": False,
    }
  raise SummaryParseError("encoder produced no Summary line")


def md5_file(path: str) -> str:
  digest = hashlib.md5()
  try:
    with open(path, "rb") as handle:
      for chunk in iter(lambda: handle.read(1 << 20), b""):
        digest.update(chunk)
  except OSError:
    return ""
  return digest.hexdigest()


# --------------------------------------------------------------------------
# perf integration
# --------------------------------------------------------------------------

_PERF_LINE = re.compile(r"^([\d,\.]+),[^,]*,([\w\-:\.]+)", re.MULTILINE)


def perf_available() -> bool:
  """True if ``perf stat`` can actually count in this environment.

  Presence on PATH is not enough: containers routinely ship the binary while
  ``perf_event_paranoid`` forbids counting, and a silently zero instruction
  count would look like a 100% speedup.
  """
  if not proc.have("perf"):
    return False
  probe = proc.run(
      ["perf", "stat", "-x,", "-e", "instructions:u", "--", "true"], timeout=20
  )
  return "instructions" in (probe.stderr or "") and probe.returncode == 0


def wrap_with_perf(argv: list[str], events: list[str]) -> list[str]:
  return ["perf", "stat", "-x,", "-e", ",".join(events), "--"] + argv


def parse_perf(stderr: str) -> dict[str, int]:
  out: dict[str, int] = {}
  for value, event in _PERF_LINE.findall(stderr or ""):
    cleaned = value.replace(",", "").strip()
    if not cleaned or cleaned.startswith("<"):
      continue
    try:
      out[event.split(":")[0]] = int(float(cleaned))
    except ValueError:
      continue
  return out


# --------------------------------------------------------------------------
# The encode job
# --------------------------------------------------------------------------


@dataclass
class EncodeJob:
  clip: ClipSpec
  cfg: EncodeConfig
  arm: str
  encoder: str
  out_path: str
  rep: int = 0
  use_perf: bool = False
  cpu_affinity: list[int] | None = None
  timeout_s: float | None = None
  keep_bitstream: bool = True
  log_path: str | None = None

  @property
  def key(self) -> tuple:
    return (self.clip.name, self.cfg.qp, self.cfg.preset, self.arm, self.rep)


def run_encode(job: EncodeJob) -> EncodeResult:
  """Run one encode and return a fully populated result record."""
  result = EncodeResult(
      sequence=job.clip.name,
      qp=job.cfg.qp,
      preset=job.cfg.preset,
      arm=job.arm,
      rep=job.rep,
  )
  argv = build_encode_argv(job.encoder, job.clip, job.cfg, job.out_path)
  if job.use_perf:
    argv = wrap_with_perf(argv, ["instructions:u", "cycles:u"])

  os.makedirs(os.path.dirname(os.path.abspath(job.out_path)) or ".", exist_ok=True)
  run = proc.run_measured(
      argv,
      timeout=job.timeout_s,
      cpu_affinity=job.cpu_affinity,
      stdout_path=job.log_path,
  )
  result.wall_s = run.wall_s
  result.cpu_s = run.cpu_s
  result.max_rss_kib = run.max_rss_kib

  if run.timed_out:
    result.error = f"timeout after {job.timeout_s}s"
    return result
  if run.returncode != 0:
    result.error = f"encoder exit {run.returncode}: {run.tail(12)}"
    return result

  text = run.stdout
  if job.log_path and os.path.exists(job.log_path) and not text.strip():
    with open(job.log_path, "r", encoding="utf-8", errors="replace") as handle:
      text = handle.read()
  try:
    summary = parse_summary(text)
  except SummaryParseError as exc:
    result.error = f"{exc}: {run.tail(12)}"
    return result

  result.bitrate_kbps = summary["bitrate_kbps"]
  result.cx_time_s = summary["cx_time_s"]
  result.frames = summary["frames_seen"]
  if summary.get("has_psnr"):
    result.psnr_y = summary["psnr_y"]
    result.psnr_u = summary["psnr_u"]
    result.psnr_v = summary["psnr_v"]
    result.psnr_avg = summary["psnr_avg"]
    result.psnr_overall = summary["psnr_overall"]

  if job.use_perf:
    counters = parse_perf(run.stderr)
    result.instructions = counters.get("instructions")
    result.cycles = counters.get("cycles")

  if os.path.exists(job.out_path):
    result.bytes_out = os.path.getsize(job.out_path)
    result.bitstream_md5 = md5_file(job.out_path)
  result.ok = result.bytes_out > 0
  if not result.ok:
    result.error = result.error or "encoder produced an empty bitstream"
  return result


def decode_verify(
    decoder: str, bitstream: str, *, timeout_s: float | None = 1800.0
) -> tuple[bool, str, str]:
  """Decode a bitstream and return ``(ok, decoded_md5, message)``.

  This is the conformance gate. An encoder change that produces a bitstream a
  conformant decoder cannot parse is not a fast encoder, it is a broken one,
  and no amount of BD-rate makes it publishable. It is also the cheapest
  possible catch for a whole class of pruning bugs: prune a mode out of the
  search but leave its syntax written, and the decode fails immediately.
  """
  run = proc.run([decoder, "--md5", bitstream], timeout=timeout_s)
  if run.timed_out:
    return (False, "", "decoder timed out")
  if run.returncode != 0:
    return (False, "", f"decoder exit {run.returncode}: {run.tail(8)}")
  match = re.search(r"([0-9a-f]{32})", run.stdout or "")
  if not match:
    return (False, "", "decoder produced no MD5")
  return (True, match.group(1), "")


def probe_encoder(encoder: str) -> dict:
  """Check a binary is the encoder we think it is, before a long pass starts."""
  run = proc.run([encoder, "--help"], timeout=60)
  text = (run.stdout or "") + (run.stderr or "")
  return {
      "exists": os.path.exists(encoder),
      "runs": run.returncode in (0, 1) and bool(text),
      "has_qp": "--qp=" in text,
      "has_cq_level": "--cq-level=" in text,
      "has_psnr": "--psnr" in text,
      "has_obu": "--obu" in text,
      "max_cpu_used": 9 if "0..9" in text else 6,
  }
