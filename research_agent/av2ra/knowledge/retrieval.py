"""Assembling what the model is allowed to see, and keeping the prefix stable.

Two constraints shape this module.

**Grounding.** The model may only propose changes to functions that exist. So
the context carries a real symbol list drawn from the code map, and ideation
validates every returned symbol against it. Two experiments in the prior corpus
were authored against functions that had moved or been renamed upstream.

**Cache stability.** Prompt caching is a prefix match, so the expensive, slowly
changing part of the context -- methodology rules, the code map digest, the
corpus digest, the profile -- is rendered once per base SHA into a *frozen*
system block that is byte-identical across calls. Everything that varies per
call (the lens, the specific targets, the last few results) goes in the user
message. Getting this backwards means paying full input price on every call for
context that never changed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import lessons
from .codemap import CodeMap
from .corpus import Corpus, PriorAttempt
from .profiles import Profile


@dataclass
class ContextPack:
  system: str                   # frozen, cacheable
  prompt: str                   # volatile
  keys: set[str] = field(default_factory=set)
  symbols: set[str] = field(default_factory=set)

  @property
  def fingerprint(self) -> str:
    return hashlib.sha256(self.system.encode()).hexdigest()[:12]

  @property
  def size(self) -> tuple[int, int]:
    return (len(self.system), len(self.prompt))


def code_digest(codemap: CodeMap, *, subsystems: list[str] | None = None,
                per_subsystem: int = 12) -> str:
  """A compact, verified symbol table: the only names ideation may use."""
  lines = ["ENCODER CODE MAP (only these symbols exist; do not invent others)"]
  chosen = subsystems or [
      s for s in codemap.subsystems
      if s not in ("unknown", "common", "libc", "dsp", "encoder_other")
  ]
  for subsystem in sorted(chosen):
    functions = codemap.functions_in(subsystem)[:per_subsystem]
    if not functions:
      continue
    lines.append(f"\n[{subsystem}] files: " + ", ".join(
        sorted({f.path for f in functions})[:6]
    ))
    for info in functions:
      lines.append(f"  {info.name}  ({info.path}:{info.line}, ~{info.length} lines)")
  return "\n".join(lines)


def speed_feature_digest(codemap: CodeMap, *, limit: int = 60) -> str:
  """Knobs that already vary by preset: the surface a promotion lens works on."""
  varying = [f for f in codemap.speed_features.values() if f.varies_by_preset]
  varying.sort(key=lambda f: f.qualified)
  lines = ["SPEED-FEATURE KNOBS THAT VARY BY PRESET (group.name -> preset:value)"]
  for feature in varying[:limit]:
    ladder = ", ".join(
        f"s{preset}:{value}" for preset, value in sorted(feature.assignments.items())
    )
    lines.append(f"  {feature.qualified} -> {ladder}")
  return "\n".join(lines)


def profile_digest(profile: Profile, *, limit: int = 22) -> str:
  lines = [
      f"PROFILE (preset {profile.preset}, {profile.source}, "
      f"{'deterministic' if profile.deterministic else 'sampled'})",
      "  share%  function                             subsystem",
  ]
  for entry in profile.top(limit):
    lines.append(f"  {entry.share_pct:6.2f}  {entry.function:36s} {entry.subsystem}")
  lines.append("  by subsystem: " + ", ".join(
      f"{name} {share:.1f}%" for name, share in list(profile.by_subsystem().items())[:8]
  ))
  for note in profile.notes:
    lines.append(f"  note: {note}")
  return "\n".join(lines)


def corpus_digest(corpus: Corpus, *, limit: int = 40) -> str:
  """What has already been tried, with outcomes. The negative-results memory."""
  hit = corpus.hit_rate()
  lines = [
      "PRIOR ATTEMPTS ON THIS CODEBASE "
      f"({hit['attempts']} recorded, {hit['resolved']} resolved, "
      f"{hit['wins']} cleared both class bars -- a {hit['rate'] * 100:.0f}% hit rate; "
      "calibrate your confidence accordingly)",
  ]
  resolved = [a for a in corpus.attempts if a.status]
  resolved.sort(key=lambda a: (not a.succeeded, a.id))
  for attempt in resolved[:limit]:
    lines.append(f"  {attempt.one_line()[:190]}")
  unresolved = [a for a in corpus.attempts if not a.status]
  if unresolved:
    lines.append(
        f"  ...plus {len(unresolved)} patch series with no recorded verdict "
        "(structural overlap is still checked against them)"
    )
  return "\n".join(lines)


def decisions_digest(corpus: Corpus, *, limit: int = 8) -> str:
  lines = ["PRIOR DECISIONS (newest first; [RETRACTED] entries were later overturned)"]
  for lesson in corpus.lessons[:limit]:
    lines.append(f"  {lesson.date} {lesson.summary(300)}")
  return "\n".join(lines)


def related_attempts(corpus: Corpus, functions: list[str], files: list[str],
                     *, limit: int = 8) -> str:
  overlapping = corpus.touching(functions, files)[:limit]
  if not overlapping:
    return "No prior attempt overlaps these symbols."
  lines = ["PRIOR ATTEMPTS OVERLAPPING THESE SYMBOLS:"]
  for attempt in overlapping:
    lines.append(f"  {attempt.one_line()[:200]}")
    if attempt.notes:
      lines.append(f"      note: {attempt.notes[:220]}")
  return "\n".join(lines)


ROLE = """You are the research engine of an autonomous R&D system working on the
AV2 reference encoder (libavm). Your output directs real compute: a proposal you
make will be implemented in C, built, and measured, and a bad one costs hours of
encode time that a good one could have used.

Three things are true about this domain and they should shape everything you say:

1. The hit rate is low. Of roughly fifty recorded prior attempts on this exact
   codebase, fewer than ten ever cleared the acceptance bar on both test classes.
   Confident proposals are not rewarded; falsifiable ones are.
2. The measurement is harder than the idea. Most failures in the record were not
   bad ideas -- they were effects too small to resolve, patches that never fired,
   or numbers taken against a base that had moved. Say what would falsify your
   proposal and what would make it unmeasurable.
3. You may only reference symbols that appear in the code map you are given.
   Inventing a plausible function name wastes an entire experiment.
"""


def build_system(
    *,
    codemap: CodeMap | None = None,
    profile: Profile | None = None,
    corpus: Corpus | None = None,
    store: lessons.LessonStore | None = None,
    stage: str = "ideation",
    subsystems: list[str] | None = None,
) -> tuple[str, set[str], set[str]]:
  """The frozen, cacheable half of the context."""
  parts = [ROLE, (store or lessons.LessonStore()).render(stage)]
  keys: set[str] = set()
  symbols: set[str] = set()
  if codemap:
    parts.append(code_digest(codemap, subsystems=subsystems))
    parts.append(speed_feature_digest(codemap))
    keys.add("codemap")
    symbols = set(codemap.functions)
  if profile:
    parts.append(profile_digest(profile))
    keys.add("profile")
  if corpus:
    parts.append(corpus_digest(corpus))
    parts.append(decisions_digest(corpus))
    keys.add("corpus")
  return ("\n\n".join(parts), keys, symbols)


def build_pack(
    *,
    prompt: str,
    codemap: CodeMap | None = None,
    profile: Profile | None = None,
    corpus: Corpus | None = None,
    store: lessons.LessonStore | None = None,
    stage: str = "ideation",
    extra_keys: set[str] | None = None,
    subsystems: list[str] | None = None,
) -> ContextPack:
  system, keys, symbols = build_system(
      codemap=codemap, profile=profile, corpus=corpus, store=store,
      stage=stage, subsystems=subsystems,
  )
  return ContextPack(
      system=system, prompt=prompt, keys=keys | (extra_keys or set()), symbols=symbols
  )
