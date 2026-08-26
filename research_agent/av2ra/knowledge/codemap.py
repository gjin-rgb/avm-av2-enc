"""An index of the actual encoder, built from the actual source.

Ideation is only useful if it names things that exist. An LLM asked to optimise
a video encoder will happily propose changes to ``av2_prune_intra_modes()`` in
``intra_search.c`` -- a plausible name, in a file that does not exist in this
tree (it is ``intra_mode_search.c``). Two experiments in the prior corpus were
authored against functions that had already moved or been renamed upstream, and
one of them consumed a cluster arm before anyone noticed it was a no-op.

So every hypothesis this system generates is grounded in a map built by reading
the repository: the files that exist, the functions they define, how large
those functions are, and which speed-feature knobs already govern them. The map
is rebuilt whenever the base moves, and ideation is *only* shown symbols that
are in it.

The parser is regex-based rather than clang-based on purpose: it must run on any
machine with a Python interpreter and no build artifacts, it must be fast enough
to rerun on every base change, and it only needs to be right about declarations
-- which the encoder writes in a consistent style enforced by its own linter.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..util import log
from ..util.io import read_json, write_json_atomic
from .corpus import SUBSYSTEM_BY_FILE, guess_subsystem

LOG = log.get("knowledge.codemap")

_FUNCTION = re.compile(
    r"^(?:static\s+|inline\s+|AVM_INLINE\s+|const\s+)*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\s+[*\s]*)+"
    r"([a-z_][A-Za-z0-9_]*)\s*\([^;]*?\)\s*\{",
    re.MULTILINE,
)
_STRUCT_FIELD = re.compile(r"^\s+(?:int|unsigned|bool|BLOCK_SIZE|[A-Z_]+)\s+([a-z_][a-z0-9_]*)\s*;", re.MULTILINE)
_SF_ASSIGN = re.compile(
    r"(?:sf|cpi->sf|features)->([a-z_]+)\.([a-z_0-9]+)\s*=\s*([^;]+);"
)
_SPEED_GUARD = re.compile(r"if\s*\(\s*speed\s*(>=|>|==|<=|<)\s*(\d)\s*\)")


@dataclass
class FunctionInfo:
  name: str
  path: str
  line: int
  length: int = 0
  static: bool = False
  subsystem: str = "unknown"

  @property
  def location(self) -> str:
    return f"{self.path}:{self.line}"


@dataclass
class SpeedFeature:
  """A knob in ``SPEED_FEATURES`` and the presets that set it."""

  group: str
  name: str
  assignments: dict[int, str] = field(default_factory=dict)  # preset -> value

  @property
  def qualified(self) -> str:
    return f"{self.group}.{self.name}"

  @property
  def varies_by_preset(self) -> bool:
    return len(set(self.assignments.values())) > 1


@dataclass
class CodeMap:
  root: str
  base_sha: str = ""
  files: dict[str, int] = field(default_factory=dict)          # path -> line count
  functions: dict[str, FunctionInfo] = field(default_factory=dict)
  speed_features: dict[str, SpeedFeature] = field(default_factory=dict)
  subsystems: dict[str, list[str]] = field(default_factory=dict)  # subsystem -> files

  def function(self, name: str) -> FunctionInfo | None:
    return self.functions.get(name)

  def exists(self, name: str) -> bool:
    return name in self.functions

  def resolve(self, names: list[str]) -> tuple[list[str], list[str]]:
    """Split proposed symbols into (found, not found). Used to reject fabrications."""
    found = [n for n in names if n in self.functions]
    missing = [n for n in names if n not in self.functions]
    return (found, missing)

  def suggest(self, name: str, limit: int = 5) -> list[str]:
    """Nearest existing symbols, so a near-miss can be repaired automatically."""
    import difflib

    return difflib.get_close_matches(name, self.functions.keys(), n=limit, cutoff=0.6)

  def files_in(self, subsystem: str) -> list[str]:
    return self.subsystems.get(subsystem, [])

  def functions_in(self, subsystem: str, *, min_length: int = 0) -> list[FunctionInfo]:
    return sorted(
        (f for f in self.functions.values()
         if f.subsystem == subsystem and f.length >= min_length),
        key=lambda f: -f.length,
    )

  def stats(self) -> dict:
    return {
        "files": len(self.files),
        "functions": len(self.functions),
        "speed_features": len(self.speed_features),
        "preset_varying_features": sum(
            1 for f in self.speed_features.values() if f.varies_by_preset
        ),
        "subsystems": {k: len(v) for k, v in sorted(self.subsystems.items())},
    }

  # -- persistence ---------------------------------------------------------

  def save(self, path: str) -> None:
    write_json_atomic(path, {
        "root": self.root,
        "base_sha": self.base_sha,
        "files": self.files,
        "functions": {
            n: {"path": f.path, "line": f.line, "length": f.length,
                "static": f.static, "subsystem": f.subsystem}
            for n, f in self.functions.items()
        },
        "speed_features": {
            n: {"group": f.group, "name": f.name, "assignments": f.assignments}
            for n, f in self.speed_features.items()
        },
        "subsystems": self.subsystems,
    })

  @classmethod
  def load(cls, path: str) -> "CodeMap | None":
    data = read_json(path)
    if not data:
      return None
    out = cls(root=data.get("root", ""), base_sha=data.get("base_sha", ""))
    out.files = data.get("files", {})
    out.functions = {
        n: FunctionInfo(name=n, path=v["path"], line=v["line"], length=v.get("length", 0),
                        static=v.get("static", False), subsystem=v.get("subsystem", "unknown"))
        for n, v in (data.get("functions") or {}).items()
    }
    out.speed_features = {
        n: SpeedFeature(group=v["group"], name=v["name"],
                        assignments={int(k): val for k, val in (v.get("assignments") or {}).items()})
        for n, v in (data.get("speed_features") or {}).items()
    }
    out.subsystems = data.get("subsystems", {})
    return out


DEFAULT_SCAN_DIRS = ("av2/encoder", "av2/common", "avm_dsp")

#: Directory- and prefix-level fallbacks, so a file the explicit table does not
#: name still lands somewhere useful instead of in "unknown". Attribution feeds
#: the portfolio planner's coverage accounting, so a large "unknown" bucket
#: reads as unexplored territory that is really just unclassified.
_DIR_SUBSYSTEM = (
    ("/x86/", "simd"), ("/arm/", "simd"), ("avm_dsp/", "dsp"),
    ("av2/common/", "common"), ("av2/encoder/", "encoder_other"),
)
_PREFIX_SUBSYSTEM = (
    ("cdef", "cdef"), ("ccso", "ccso"), ("restoration", "loop_restoration"),
    ("wiener", "loop_restoration"), ("quant", "quantization"),
    ("trellis", "quantization"), ("txb", "transform"), ("tx_", "transform"),
    ("txfm", "transform"), ("intra", "intra"), ("inter", "inter"),
    ("mcomp", "motion"), ("motion", "motion"), ("partition", "partition"),
    ("palette", "intra"), ("tpl", "tpl"), ("temporal_filter", "temporal_filter"),
    ("ratectrl", "rate_control"), ("bitstream", "entropy"), ("entropy", "entropy"),
    ("rd", "rdcost"), ("thread", "threading"),
)


def _subsystem_for(relative: str) -> str:
  base = os.path.basename(relative).lower()
  # SIMD kernels belong to the algorithm they implement, not to a "simd"
  # bucket. Profile shares are aggregated by subsystem to decide where research
  # is worth doing, and filing 46% of the profile under "simd" hides that most
  # of it is quantisation.
  is_simd = "/x86/" in relative or "/arm/" in relative
  if not is_simd:
    explicit = guess_subsystem([relative])
    if explicit != "unknown":
      return explicit
  for prefix, subsystem in _PREFIX_SUBSYSTEM:
    if base.startswith(prefix) or f"_{prefix}" in base:
      return subsystem
  if is_simd:
    return "simd"
  for fragment, subsystem in _DIR_SUBSYSTEM:
    if fragment in relative:
      return subsystem
  return "unknown"


def build(root: str, *, base_sha: str = "", scan_dirs: tuple[str, ...] = DEFAULT_SCAN_DIRS) -> CodeMap:
  """Walk the tree and index it."""
  root = os.path.abspath(os.path.expanduser(root))
  out = CodeMap(root=root, base_sha=base_sha)
  for scan_dir in scan_dirs:
    base = os.path.join(root, scan_dir)
    if not os.path.isdir(base):
      continue
    for dirpath, _dirs, files in os.walk(base):
      for name in sorted(files):
        if not name.endswith((".c", ".h", ".cc")):
          continue
        path = os.path.join(dirpath, name)
        relative = os.path.relpath(path, root)
        try:
          with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        except OSError:
          continue
        out.files[relative] = text.count("\n") + 1
        subsystem = _subsystem_for(relative)
        out.subsystems.setdefault(subsystem, []).append(relative)
        _index_functions(out, relative, text, subsystem)

  sf_header = os.path.join(root, "av2/encoder/speed_features.h")
  sf_source = os.path.join(root, "av2/encoder/speed_features.c")
  if os.path.exists(sf_header):
    _index_speed_features(out, sf_header, sf_source)
  LOG.info(
      "code map: %d files, %d functions, %d speed features",
      len(out.files), len(out.functions), len(out.speed_features),
  )
  return out


def _index_functions(out: CodeMap, relative: str, text: str, subsystem: str) -> None:
  lines_before = _line_starts(text)
  matches = list(_FUNCTION.finditer(text))
  for index, match in enumerate(matches):
    name = match.group(1)
    if name in ("if", "for", "while", "switch", "return", "sizeof", "defined"):
      continue
    line = _line_of(lines_before, match.start())
    # Brace matching alone over-reads when a body contains an unbalanced brace
    # inside a string or a macro; capping at the next definition keeps a stray
    # match from reporting a 4000-line function.
    next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
    length = min(
        _brace_span(text, match.end() - 1),
        max(0, _line_of(lines_before, next_start) - line),
    )
    existing = out.functions.get(name)
    # Prefer the definition in a .c file over a header inline, and the longer
    # body when a name appears twice behind different #ifdef branches.
    if existing and (existing.path.endswith(".c") or existing.length >= length):
      continue
    out.functions[name] = FunctionInfo(
        name=name, path=relative, line=line, length=length,
        static=text[max(0, match.start() - 12): match.start()].strip().endswith("static"),
        subsystem=subsystem,
    )


def _line_starts(text: str) -> list[int]:
  starts, position = [0], text.find("\n")
  while position != -1:
    starts.append(position + 1)
    position = text.find("\n", position + 1)
  return starts


def _line_of(starts: list[int], offset: int) -> int:
  low, high = 0, len(starts) - 1
  while low < high:
    mid = (low + high + 1) // 2
    if starts[mid] <= offset:
      low = mid
    else:
      high = mid - 1
  return low + 1


def _brace_span(text: str, open_index: int, limit: int = 200000) -> int:
  """Line count of a function body, by brace matching. Cheap and good enough."""
  depth, index, lines = 0, open_index, 0
  end = min(len(text), open_index + limit)
  while index < end:
    char = text[index]
    if char == "{":
      depth += 1
    elif char == "}":
      depth -= 1
      if depth == 0:
        return lines
    elif char == "\n":
      lines += 1
    index += 1
  return lines


def _index_speed_features(out: CodeMap, header_path: str, source_path: str) -> None:
  with open(header_path, "r", encoding="utf-8", errors="replace") as handle:
    header = handle.read()
  # Struct groups look like: typedef struct <NAME>_SPEED_FEATURES { ... } NAME;
  for block in re.finditer(
      r"typedef struct\s+([A-Z_0-9]*SPEED_FEATURES)\s*\{(.*?)\}\s*([A-Z_0-9]+);",
      header, re.DOTALL,
  ):
    group = block.group(3).lower()
    for field_match in _STRUCT_FIELD.finditer(block.group(2)):
      name = field_match.group(1)
      feature = SpeedFeature(group=group, name=name)
      out.speed_features[f"{group}.{name}"] = feature

  if not os.path.exists(source_path):
    return
  with open(source_path, "r", encoding="utf-8", errors="replace") as handle:
    source = handle.read()
  # Assignments are written inside `if (speed >= N)` blocks; attributing each
  # assignment to the innermost enclosing guard gives the preset ladder.
  guards: list[tuple[int, int]] = []      # (offset, preset)
  for guard in _SPEED_GUARD.finditer(source):
    guards.append((guard.start(), int(guard.group(2))))
  for assign in _SF_ASSIGN.finditer(source):
    group, name, value = assign.group(1), assign.group(2), assign.group(3).strip()
    preset = 0
    for offset, level in guards:
      if offset < assign.start():
        preset = level
      else:
        break
    key = f"{group}.{name}"
    feature = out.speed_features.get(key)
    if feature is None:
      feature = SpeedFeature(group=group, name=name)
      out.speed_features[key] = feature
    feature.assignments.setdefault(preset, value)
