"""Where the encode time actually goes, and what that permits an idea to claim.

The prior research produced twenty-four patches before anyone profiled the
encoder. When they did, in twenty-five minutes and with no cluster time,
quantisation turned out to be about 47% of instructions retired -- larger than
partition search, mode search and the loop filters combined -- and every patch
so far had attacked something else. The lesson is not "profile once"; it is
that a *feature-ablation map* (what each speed feature is worth) answers a
different question from a *hotspot map* (where time is spent), and an agent
that only has the first will optimise around the edges of the thing that
dominates.

So a profile is a first-class input here. It does three jobs:

  1. **Targeting.** Ideation is shown the ranked hot functions for the target
     preset, joined to the code map, so proposals start where the time is.
  2. **The Amdahl gate.** A claimed whole-encode speedup is checked against the
     share of the profile the touched functions occupy, *before* cluster time
     is spent. In the corpus this reasoning correctly predicted a 1.2% CTC
     result from a 38.8% kernel speedup.
  3. **Coverage accounting.** The planner can see which large shares of the
     profile no experiment has ever touched -- which is how a 47% blind spot
     becomes visible instead of persisting for twenty-four patches.

Two input formats are parsed: ``callgrind_annotate`` output (instructions
retired, deterministic, what the prior corpus used) and ``perf report --stdio``
(sampled). Deterministic profiles are preferred and the source is recorded,
because a sampled profile and an exact one disagree in ways that matter at the
1% level.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..util import log, proc
from ..util.io import read_json, write_json_atomic

LOG = log.get("knowledge.profile")

# callgrind_annotate:  19,005,312,193 ( 9.95%)  ???:av2_trellis_quant [/path]
_CALLGRIND = re.compile(
    r"^\s*([\d,]+)\s*\(\s*([\d.]+)%\)\s+(?:[^\s:]*):([A-Za-z_][A-Za-z0-9_.]*)", re.MULTILINE
)
# perf report --stdio:   9.95%  avmenc  avmenc  [.] av2_trellis_quant
_PERF_REPORT = re.compile(
    r"^\s*([\d.]+)%\s+\S+\s+\S+\s+\[[.k]\]\s+([A-Za-z_][A-Za-z0-9_.]*)", re.MULTILINE
)
_TOTAL = re.compile(r"^\s*([\d,]+)\s*\(100\.0%\)\s+PROGRAM TOTALS", re.MULTILINE)


@dataclass
class ProfileEntry:
  function: str
  share_pct: float
  count: int = 0
  subsystem: str = "unknown"


@dataclass
class Profile:
  preset: int
  source: str                    # callgrind | perf | imported
  deterministic: bool
  entries: list[ProfileEntry] = field(default_factory=list)
  total: int = 0
  clip: str = ""
  base_sha: str = ""
  notes: list[str] = field(default_factory=list)

  def share_of(self, functions: list[str]) -> float:
    """Combined profile share of a set of functions. The Amdahl ceiling."""
    wanted = {f.lower() for f in functions}
    return sum(e.share_pct for e in self.entries if e.function.lower() in wanted)

  def share_of_subsystem(self, subsystem: str) -> float:
    return sum(e.share_pct for e in self.entries if e.subsystem == subsystem)

  def by_subsystem(self) -> dict[str, float]:
    out: dict[str, float] = {}
    for entry in self.entries:
      out[entry.subsystem] = out.get(entry.subsystem, 0.0) + entry.share_pct
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))

  def top(self, limit: int = 25) -> list[ProfileEntry]:
    return sorted(self.entries, key=lambda e: -e.share_pct)[:limit]

  def uncovered(self, touched_functions: set[str], *, min_share: float = 1.0) -> list[ProfileEntry]:
    """Hot functions no experiment has ever touched. The blind-spot report."""
    lowered = {f.lower() for f in touched_functions}
    return [
        e for e in self.top(200)
        if e.share_pct >= min_share and e.function.lower() not in lowered
    ]

  def to_dict(self) -> dict:
    return {
        "preset": self.preset, "source": self.source,
        "deterministic": self.deterministic, "total": self.total,
        "clip": self.clip, "base_sha": self.base_sha, "notes": self.notes,
        "entries": [
            {"function": e.function, "share_pct": e.share_pct, "count": e.count,
             "subsystem": e.subsystem}
            for e in self.entries
        ],
    }

  def save(self, path: str) -> None:
    write_json_atomic(path, self.to_dict())

  @classmethod
  def load(cls, path: str) -> "Profile | None":
    data = read_json(path)
    if not data:
      return None
    out = cls(
        preset=data.get("preset", 4), source=data.get("source", "imported"),
        deterministic=data.get("deterministic", False), total=data.get("total", 0),
        clip=data.get("clip", ""), base_sha=data.get("base_sha", ""),
        notes=list(data.get("notes") or []),
    )
    out.entries = [
        ProfileEntry(
            function=e["function"], share_pct=e["share_pct"],
            count=e.get("count", 0), subsystem=e.get("subsystem", "unknown"),
        )
        for e in data.get("entries", [])
    ]
    return out


def parse(text: str, *, preset: int = 4, clip: str = "", base_sha: str = "") -> Profile:
  """Parse a callgrind or perf profile. Prefers callgrind when both match."""
  callgrind = _CALLGRIND.findall(text)
  if callgrind:
    total_match = _TOTAL.search(text)
    profile = Profile(
        preset=preset, source="callgrind", deterministic=True, clip=clip,
        base_sha=base_sha,
        total=int(total_match.group(1).replace(",", "")) if total_match else 0,
    )
    for count, share, function in callgrind:
      if function.upper().startswith("PROGRAM"):
        continue
      profile.entries.append(
          ProfileEntry(
              function=_strip_suffix(function), share_pct=float(share),
              count=int(count.replace(",", "")),
          )
      )
    return profile

  profile = Profile(
      preset=preset, source="perf", deterministic=False, clip=clip, base_sha=base_sha
  )
  profile.notes.append(
      "sampled profile: shares below about 0.5% are not reliable, and a sampled "
      "profile disagrees with an exact instruction count in ways that matter "
      "when an Amdahl ceiling is close"
  )
  for share, function in _PERF_REPORT.findall(text):
    profile.entries.append(
        ProfileEntry(function=_strip_suffix(function), share_pct=float(share))
    )
  return profile


def _strip_suffix(name: str) -> str:
  """``search_tx_type.constprop.0`` and ``foo.isra.5`` name the same function."""
  return re.sub(r"\.(constprop|isra|part|cold|lto_priv)\.?\d*$", "", name)


def attribute(profile: Profile, codemap) -> Profile:
  """Join profile entries to subsystems via the code map."""
  for entry in profile.entries:
    info = codemap.function(entry.function)
    if info:
      entry.subsystem = info.subsystem
    elif entry.function.startswith("__") or "libc" in entry.function:
      entry.subsystem = "libc"
    else:
      # SIMD kernels are usually named <base>_avx2/_sse2 and are not indexed as
      # C definitions; fall back to the stripped base name.
      base = re.sub(r"_(avx2|avx|sse4|sse2|ssse3|neon|c)$", "", entry.function)
      info = codemap.function(base)
      if info:
        entry.subsystem = info.subsystem
      else:
        entry.subsystem = _subsystem_from_filename(entry.function, codemap)
  return profile


def _subsystem_from_filename(function: str, codemap) -> str:
  """Last resort: find a source file whose name matches the kernel's family.

  ``av2_decide_states_q1_avx2`` is defined in a hand-written assembly-adjacent
  file the C parser does not index, but the file itself is in the map, and its
  name says which algorithm it implements.
  """
  from .codemap import _subsystem_for

  stem = re.sub(r"^(av2|avm|aom)_", "", function)
  stem = re.sub(r"_(avx2|avx|sse4|sse2|ssse3|neon|c|q1|lf|def)$", "", stem)
  tokens = [t for t in stem.split("_") if len(t) > 3]
  best, best_score = "simd", 0
  for path in codemap.files:
    base = os.path.basename(path).lower()
    score = sum(1 for token in tokens if token in base)
    if score > best_score:
      best, best_score = _subsystem_for(path), score
  return best if best_score else "simd"


def collect(
    encoder: str, argv_builder, *, preset: int = 4, clip: str = "",
    tool: str = "auto", workdir: str = "/tmp", timeout_s: float = 14400.0,
) -> Profile | None:
  """Run a profiling encode. Prefers callgrind for determinism, falls back to perf.

  Deliberately not part of the measurement path: a callgrind run is 50x slower
  than a normal encode and a perf run perturbs timing. Profiles inform
  *targeting*; they never produce a speedup number.
  """
  argv = argv_builder(encoder)
  if tool in ("auto", "callgrind") and proc.have("valgrind"):
    out_path = os.path.join(workdir, f"callgrind.out.{preset}")
    run = proc.run(
        ["valgrind", "--tool=callgrind", f"--callgrind-out-file={out_path}", *argv],
        timeout=timeout_s,
    )
    if run.ok and os.path.exists(out_path) and proc.have("callgrind_annotate"):
      annotated = proc.run(["callgrind_annotate", out_path], timeout=600)
      if annotated.ok:
        return parse(annotated.stdout, preset=preset, clip=clip)
  if tool in ("auto", "perf") and proc.have("perf"):
    data_path = os.path.join(workdir, f"perf.data.{preset}")
    record = proc.run(
        ["perf", "record", "-g", "-o", data_path, "--", *argv], timeout=timeout_s
    )
    if record.ok and os.path.exists(data_path):
      report = proc.run(
          ["perf", "report", "--stdio", "--no-children", "-i", data_path], timeout=900
      )
      if report.ok:
        return parse(report.stdout, preset=preset, clip=clip)
  LOG.warning("no profiling tool available (tried valgrind/callgrind and perf)")
  return None
