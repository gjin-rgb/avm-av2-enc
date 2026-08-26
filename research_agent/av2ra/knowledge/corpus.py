"""Ingesting prior research into something an agent can actually use.

The existing demo ingester classified a report as a "success" if the word
"gain" appeared anywhere in it and as a "failure" if the word "loss" did, then
wrote the first 400 characters of the file -- header included -- into a
markdown file. The same report landed in both lists. No result, mechanism,
verdict, anchor or ratio survived. That is not memory; it is a filename index
with adjectives.

What an agent needs from ~50 prior attempts is narrow and specific:

  * **What was tried**, identified structurally (which functions, which
    mechanism) so a new idea can be recognised as a re-derivation of an old one
    even when worded completely differently.
  * **What happened**, as numbers with their anchor, so the agent can calibrate
    its own predictions instead of guessing.
  * **Why it was killed**, so a dead end is not re-entered. Four experiments in
    the corpus died because upstream had already implemented the same idea;
    three of those were discovered only after cluster time was spent.
  * **Which conclusions were later retracted**, because one rule in the corpus
    was derived from three arms that never fired and was carried forward for
    two rounds before anyone noticed.

Everything here is parsed from the artifacts the prior effort actually wrote --
``registry.csv``, ``DECISIONS.md``, ``patches/*.patch`` and the round reports --
so the ingester improves as those artifacts do, and never invents a result.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field

from ..buildkit.worktree import describe_diff
from ..util import log

LOG = log.get("knowledge.corpus")

_STATUS_MEANING = {
    "PROMOTE": "cleared the bar and was recommended",
    "ADOPT-FINAL": "the peak of a tuning chain; further tuning was ruled out",
    "IMPROVE": "real effect, below the bar, worth another variant",
    "DISCARD": "killed",
    "NEXT-ROUND": "queued, no result yet",
    "PASS-A1-ONLY": "cleared the bar on 4K only; the 1080p class failed",
}


@dataclass
class PriorAttempt:
  """One thing somebody already tried, with what came of it."""

  id: str
  title: str
  source: str
  mechanism: str = "unknown"
  status: str = ""
  preset: int | None = None
  a1_speedup: float | None = None
  a1_bdrate: float | None = None
  a1_ratio: float | None = None
  a2_speedup: float | None = None
  a2_bdrate: float | None = None
  a2_ratio: float | None = None
  notes: str = ""
  patch_path: str = ""
  touched_files: list[str] = field(default_factory=list)
  touched_functions: list[str] = field(default_factory=list)
  normalized_hash: str = ""
  base_sha: str = ""
  bit_exact: str = ""

  @property
  def failed(self) -> bool:
    return self.status.upper() in ("DISCARD", "PASS-A1-ONLY")

  @property
  def succeeded(self) -> bool:
    return self.status.upper() in ("PROMOTE", "ADOPT-FINAL")

  @property
  def subsystem_guess(self) -> str:
    return guess_subsystem(self.touched_files, self.title)

  def one_line(self) -> str:
    parts = [f"{self.id}: {self.title}"]
    if self.mechanism and self.mechanism != "unknown":
      parts.append(f"[{self.mechanism}]")
    numbers = []
    if self.a1_ratio is not None:
      numbers.append(f"A1 {self.a1_speedup:+.2f}%/{self.a1_bdrate:+.2f}% r={self.a1_ratio:.1f}")
    if self.a2_ratio is not None:
      numbers.append(f"A2 {self.a2_speedup:+.2f}%/{self.a2_bdrate:+.2f}% r={self.a2_ratio:.1f}")
    if numbers:
      parts.append(" ".join(numbers))
    if self.status:
      parts.append(f"-> {self.status}")
    return " ".join(parts)


@dataclass
class PriorLesson:
  """A dated conclusion from the decision log, with its retraction status."""

  date: str
  heading: str
  body: str
  source: str
  retracted: bool = False

  def summary(self, limit: int = 400) -> str:
    text = " ".join(self.body.split())
    marker = "[RETRACTED] " if self.retracted else ""
    return f"{marker}{self.heading}: {text[:limit]}"


@dataclass
class Corpus:
  attempts: list[PriorAttempt] = field(default_factory=list)
  lessons: list[PriorLesson] = field(default_factory=list)
  base_shas: list[str] = field(default_factory=list)
  root: str = ""

  # -- lookups the agent actually makes ------------------------------------

  def by_hash(self, normalized_hash: str) -> PriorAttempt | None:
    for attempt in self.attempts:
      if attempt.normalized_hash and attempt.normalized_hash == normalized_hash:
        return attempt
    return None

  def touching(self, functions: list[str], files: list[str]) -> list[PriorAttempt]:
    """Prior attempts that overlap this idea structurally.

    Function overlap first, file overlap second. This is the query that answers
    "has someone already tried this?" for an idea phrased in completely
    different words -- which is the normal case when two different models
    propose the same optimisation.
    """
    wanted_functions = {f.lower() for f in functions}
    wanted_files = {os.path.basename(f).lower() for f in files}
    scored = []
    for attempt in self.attempts:
      function_overlap = len(
          wanted_functions & {f.lower() for f in attempt.touched_functions}
      )
      file_overlap = len(
          wanted_files & {os.path.basename(f).lower() for f in attempt.touched_files}
      )
      if function_overlap or file_overlap:
        scored.append((function_overlap * 10 + file_overlap, attempt))
    scored.sort(key=lambda pair: -pair[0])
    return [attempt for _score, attempt in scored]

  def in_subsystem(self, subsystem: str) -> list[PriorAttempt]:
    return [a for a in self.attempts if a.subsystem_guess == subsystem]

  def failures(self) -> list[PriorAttempt]:
    return [a for a in self.attempts if a.failed]

  def successes(self) -> list[PriorAttempt]:
    return [a for a in self.attempts if a.succeeded]

  def hit_rate(self) -> dict:
    """What fraction of past ideas worked. The agent's prior on its own ideas."""
    resolved = [a for a in self.attempts if a.status and a.status != "NEXT-ROUND"]
    wins = [a for a in resolved if a.succeeded]
    return {
        "attempts": len(self.attempts),
        "resolved": len(resolved),
        "wins": len(wins),
        "rate": len(wins) / len(resolved) if resolved else 0.0,
    }

  def stats(self) -> dict:
    subsystems: dict[str, int] = {}
    mechanisms: dict[str, int] = {}
    for attempt in self.attempts:
      subsystems[attempt.subsystem_guess] = subsystems.get(attempt.subsystem_guess, 0) + 1
      mechanisms[attempt.mechanism] = mechanisms.get(attempt.mechanism, 0) + 1
    return {
        "attempts": len(self.attempts),
        "lessons": len(self.lessons),
        "retracted_lessons": sum(1 for l in self.lessons if l.retracted),
        "by_subsystem": dict(sorted(subsystems.items(), key=lambda kv: -kv[1])),
        "by_mechanism": dict(sorted(mechanisms.items(), key=lambda kv: -kv[1])),
        "hit_rate": self.hit_rate(),
    }


# --------------------------------------------------------------------------
# Subsystem attribution
# --------------------------------------------------------------------------

SUBSYSTEM_BY_FILE = {
    "partition_search.c": "partition", "partition_strategy.c": "partition",
    "tx_search.c": "transform", "encodetxb.c": "transform", "txb_rdopt.c": "transform",
    "encodemb.c": "quantization", "av1_quantize.c": "quantization",
    "quantize.c": "quantization", "trellis": "quantization",
    "intra_mode_search.c": "intra", "palette.c": "intra",
    "mcomp.c": "motion", "motion_search_facade.c": "motion",
    "compound_type.c": "inter", "rdopt.c": "mode_decision",
    "nonrd_pickmode.c": "mode_decision", "rd.c": "rdcost",
    "pickrst.c": "loop_restoration", "pickcdef.c": "cdef", "pickccso.c": "ccso",
    "temporal_filter.c": "temporal_filter", "tpl_model.c": "tpl",
    "ratectrl.c": "rate_control", "pass2_strategy.c": "rate_control",
    "bitstream.c": "entropy", "speed_features.c": "speed_features",
    "encodeframe.c": "frame_loop", "encoder.c": "frame_loop",
    "ethread.c": "threading",
}

_TITLE_HINTS = [
    ("trellis", "quantization"), ("tcq", "quantization"), ("quant", "quantization"),
    ("partition", "partition"), ("tx", "transform"), ("transform", "transform"),
    ("intra", "intra"), ("motion", "motion"), ("mv", "motion"),
    ("ccso", "ccso"), ("cdef", "cdef"), ("restoration", "loop_restoration"),
    ("wiener", "loop_restoration"), ("temporal filter", "temporal_filter"),
    ("simd", "simd"), ("sse2", "simd"), ("avx", "simd"), ("drl", "inter"),
]


def guess_subsystem(files: list[str], title: str = "") -> str:
  for path in files:
    base = os.path.basename(path)
    if base in SUBSYSTEM_BY_FILE:
      return SUBSYSTEM_BY_FILE[base]
    if "/x86/" in path or "/arm/" in path:
      return "simd"
  lowered = title.lower()
  for needle, subsystem in _TITLE_HINTS:
    if needle in lowered:
      return subsystem
  return "unknown"


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


def _percent(cell: str) -> float | None:
  cleaned = (cell or "").strip().replace("%", "").replace("+", "")
  # Strip annotations, but never the minus sign: an earlier version of this
  # used a bare "-" in the character set and silently flipped the sign of every
  # negative speedup, turning three slowdowns in the prior corpus into wins.
  cleaned = re.sub(r"\(partial\)|\bn/a\b", "", cleaned, flags=re.IGNORECASE).strip()
  if not cleaned or cleaned == "-":
    return None
  if cleaned.lower() in ("inf", "+inf", "-inf"):
    return None            # a ratio of "inf" is a division by rounded zero
  try:
    return float(cleaned)
  except ValueError:
    return None


def parse_registry_csv(path: str) -> list[PriorAttempt]:
  """Parse the prior effort's ``registry.csv``, skipping its ``>`` preamble."""
  if not os.path.exists(path):
    return []
  with open(path, "r", encoding="utf-8", errors="replace") as handle:
    lines = [line for line in handle if not line.lstrip().startswith(">")]
  reader = csv.DictReader(lines)
  out = []
  for row in reader:
    identifier = (row.get("id") or "").strip()
    if not identifier:
      continue
    out.append(
        PriorAttempt(
            id=identifier,
            title=(row.get("name") or "").strip(),
            source=os.path.basename(path),
            mechanism=(row.get("mechanism") or "unknown").strip(),
            status=(row.get("status") or "").strip(),
            preset=int(row["ctc_speed"]) if (row.get("ctc_speed") or "").strip().isdigit() else None,
            a1_speedup=_percent(row.get("a1_speedup", "")),
            a1_bdrate=_percent(row.get("a1_bdyuv", "")),
            a1_ratio=_percent(row.get("a1_ratio", "")),
            a2_speedup=_percent(row.get("a2_speedup", "")),
            a2_bdrate=_percent(row.get("a2_bdyuv", "")),
            a2_ratio=_percent(row.get("a2_ratio", "")),
            notes=(row.get("notes") or "").strip(),
            bit_exact=(row.get("t0_bitexact") or "").strip(),
        )
    )
  return out


_DECISION_HEADING = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*[-—-]\s*(.+)$", re.MULTILINE)
_RETRACTION = re.compile(r"retract|no support|was wrong|I was wrong|has no support", re.IGNORECASE)


def parse_decisions(path: str) -> list[PriorLesson]:
  if not os.path.exists(path):
    return []
  with open(path, "r", encoding="utf-8", errors="replace") as handle:
    text = handle.read()
  matches = list(_DECISION_HEADING.finditer(text))
  out = []
  for index, match in enumerate(matches):
    start = match.end()
    end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
    body = text[start:end].strip()
    out.append(
        PriorLesson(
            date=match.group(1), heading=match.group(2).strip(), body=body,
            source=os.path.basename(path), retracted=bool(_RETRACTION.search(body)),
        )
    )
  return out


def attach_patches(attempts: list[PriorAttempt], patch_dir: str) -> None:
  """Match ``patches/*.patch`` files to registry rows and extract their structure."""
  if not os.path.isdir(patch_dir):
    return
  by_number: dict[str, str] = {}
  for name in sorted(os.listdir(patch_dir)):
    if not name.endswith(".patch"):
      continue
    number = re.match(r"(\d+)", name)
    if number:
      by_number.setdefault(number.group(1).lstrip("0") or "0", os.path.join(patch_dir, name))
  for attempt in attempts:
    number = re.search(r"(\d+)", attempt.id)
    path = by_number.get(number.group(1).lstrip("0") or "0") if number else None
    if not path:
      continue
    try:
      with open(path, "r", encoding="utf-8", errors="replace") as handle:
        diff = handle.read()
    except OSError:
      continue
    info = describe_diff(diff)
    attempt.patch_path = path
    attempt.touched_files = info.files
    attempt.touched_functions = info.touched_functions
    attempt.normalized_hash = info.normalized_hash


def scan_standalone_patches(root: str, seen_hashes: set[str]) -> list[PriorAttempt]:
  """Pick up patch series that never made it into a registry (the GPT rounds)."""
  out = []
  for dirpath, _dirs, files in os.walk(root):
    if os.path.basename(dirpath) != "patches":
      continue
    for name in sorted(files):
      if not name.endswith(".patch"):
        continue
      path = os.path.join(dirpath, name)
      try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
          diff = handle.read()
      except OSError:
        continue
      info = describe_diff(diff)
      if info.normalized_hash in seen_hashes:
        continue
      seen_hashes.add(info.normalized_hash)
      out.append(
          PriorAttempt(
              id=os.path.splitext(name)[0],
              title=_title_from_filename(name),
              source=os.path.relpath(path, root),
              patch_path=path,
              touched_files=info.files,
              touched_functions=info.touched_functions,
              normalized_hash=info.normalized_hash,
          )
      )
  return out


def _title_from_filename(name: str) -> str:
  stem = os.path.splitext(name)[0]
  stem = re.sub(r"^[A-Za-z]?\d+[a-z]?[-_]?", "", stem)
  stem = re.sub(r"[-_][0-9a-f]{7,}$", "", stem)
  return stem.replace("-", " ").replace("_", " ").strip() or name


_REPORT_ROW = re.compile(
    r"^\|\s*([^|]+?)\s*\|(.+)\|\s*$", re.MULTILINE
)


def ingest(root: str) -> Corpus:
  """Read a prior-research repository into a structured corpus."""
  root = os.path.abspath(os.path.expanduser(root))
  corpus = Corpus(root=root)
  if not os.path.isdir(root):
    LOG.warning("prior research root %s does not exist", root)
    return corpus

  for relative in ("Claude/experiments/registry.csv", "registry.csv", "experiments/registry.csv"):
    path = os.path.join(root, relative)
    if os.path.exists(path):
      attempts = parse_registry_csv(path)
      attach_patches(attempts, os.path.join(os.path.dirname(os.path.dirname(path)), "patches"))
      corpus.attempts.extend(attempts)

  for relative in (
      "Claude/experiments/DECISIONS.md", "DECISIONS.md", "experiments/DECISIONS.md",
  ):
    path = os.path.join(root, relative)
    if os.path.exists(path):
      corpus.lessons.extend(parse_decisions(path))

  seen = {a.normalized_hash for a in corpus.attempts if a.normalized_hash}
  corpus.attempts.extend(scan_standalone_patches(root, seen))

  for relative in ("Claude/BASE", "BASE"):
    path = os.path.join(root, relative)
    if os.path.exists(path):
      with open(path, "r", encoding="utf-8", errors="replace") as handle:
        sha = handle.read().strip()
      if sha:
        corpus.base_shas.append(sha)

  LOG.info(
      "ingested %d prior attempts and %d decision entries from %s",
      len(corpus.attempts), len(corpus.lessons), root,
  )
  return corpus
