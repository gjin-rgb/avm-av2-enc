"""Anchor tracking: knowing when every number you have stopped being true.

The single most expensive mistake available to a long-running research agent is
to keep quoting measurements taken against a base the upstream branch has since
moved past. It is expensive because nothing breaks: the patches still apply,
the reports still render, and the numbers are simply about a different encoder.

The prior research has the worked example. Upstream moved by one commit --
"Implement non-RD partition evaluation for real-time mode" -- which added 187
lines to ``av2/encoder/partition_search.c``. Two patches in the series modify
that file. All ten patches reported APPLIES, because the new code landed in
regions they did not touch textually. Whether those two patches still pruned
the search they were measured against was unknown and unknowable from the apply
check alone.

So this module distinguishes three things that a naive check conflates:

* **Textual rot** -- does the diff still apply? Cheap, necessary, insufficient.
* **Semantic overlap** -- did upstream touch a file, or better, a *function*,
  that this patch depends on? This is the set where a clean apply means least.
* **Substitution** -- did upstream implement the same idea? A patch can become
  a no-op because someone else fixed it, which is a good outcome for the
  encoder and a negative result for the patch. Four experiments in the prior
  corpus died this way, three of them only discovered after spending cluster
  time.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

from ..buildkit.worktree import WorktreeManager
from ..util.io import read_json, write_json_atomic


@dataclass
class DriftReport:
  old_sha: str
  new_sha: str
  moved: bool
  commits: int = 0
  changed_files: list[str] = field(default_factory=list)
  commit_subjects: list[str] = field(default_factory=list)
  checked_ts: float = field(default_factory=time.time)

  @property
  def summary(self) -> str:
    if not self.moved:
      return f"base is current at {self.new_sha[:12]}"
    return (
        f"base moved {self.old_sha[:12]} -> {self.new_sha[:12]} "
        f"({self.commits} commit(s), {len(self.changed_files)} file(s) changed)"
    )


@dataclass
class PatchRisk:
  experiment_id: str
  applies: str                      # APPLIES | NEEDS-3WAY | FAILS
  overlapping_files: list[str] = field(default_factory=list)
  overlapping_functions: list[str] = field(default_factory=list)
  possible_substitution: list[str] = field(default_factory=list)
  verdict: str = "revalidate"       # ok | revalidate | rebuild | dead

  @property
  def explanation(self) -> str:
    if self.applies == "FAILS":
      return "the diff no longer applies; the patch must be rewritten against the new base"
    if self.possible_substitution:
      return (
          "upstream commits mention the same mechanism: check whether this idea "
          "already landed before spending anything on it -- "
          + "; ".join(self.possible_substitution[:3])
      )
    if self.overlapping_functions:
      return (
          "upstream changed functions this patch hooks into ("
          + ", ".join(self.overlapping_functions[:5])
          + "): a clean apply proves nothing here. Re-run the cheap tiers."
      )
    if self.overlapping_files:
      return (
          "upstream changed files this patch touches ("
          + ", ".join(self.overlapping_files[:5])
          + "): re-run the cheap tiers before trusting any recorded number."
      )
    return "no overlap with the upstream change set; previous tiers are likely still valid"


class BaseWatch:
  """Tracks the anchor commit and reports what a move invalidates."""

  def __init__(self, worktrees: WorktreeManager, state_path: str, anchor_ref: str):
    self.worktrees = worktrees
    self.state_path = state_path
    self.anchor_ref = anchor_ref

  @property
  def recorded_base(self) -> str:
    return (read_json(self.state_path, {}) or {}).get("base_sha", "")

  def record_base(self, sha: str, *, note: str = "") -> None:
    write_json_atomic(
        self.state_path,
        {"base_sha": sha, "anchor_ref": self.anchor_ref, "ts": time.time(), "note": note},
    )

  def check(self, *, fetch: bool = True) -> DriftReport:
    if fetch:
      remote, _, ref = self.anchor_ref.partition("/")
      self.worktrees.fetch(remote or "origin", ref or None)
    new_sha = self.worktrees.resolve(self.anchor_ref)
    old_sha = self.recorded_base
    if not old_sha:
      self.record_base(new_sha, note="first observation")
      return DriftReport(old_sha=new_sha, new_sha=new_sha, moved=False)
    if old_sha == new_sha:
      return DriftReport(old_sha=old_sha, new_sha=new_sha, moved=False)
    return DriftReport(
        old_sha=old_sha,
        new_sha=new_sha,
        moved=True,
        commits=self.worktrees.commits_between(old_sha, new_sha),
        changed_files=self.worktrees.files_changed_between(old_sha, new_sha),
        commit_subjects=self.worktrees.log_between(old_sha, new_sha),
    )

  def assess_patch(
      self,
      experiment_id: str,
      diff_text: str,
      patch_files: list[str],
      patch_functions: list[str],
      report: DriftReport,
  ) -> PatchRisk:
    """How much does this drift threaten one specific experiment?"""
    applies = self.worktrees.check_applies(diff_text, report.new_sha) if diff_text else "FAILS"
    overlapping_files = sorted(set(patch_files) & set(report.changed_files))
    overlapping_functions = self._overlapping_functions(
        patch_functions, report.old_sha, report.new_sha, overlapping_files
    )
    substitution = _substitution_hints(report.commit_subjects, patch_functions, patch_files)

    if applies == "FAILS":
      verdict = "dead"
    elif substitution:
      verdict = "rebuild"
    elif overlapping_functions or overlapping_files:
      verdict = "revalidate"
    else:
      verdict = "ok"
    return PatchRisk(
        experiment_id=experiment_id,
        applies=applies,
        overlapping_files=overlapping_files,
        overlapping_functions=overlapping_functions,
        possible_substitution=substitution,
        verdict=verdict,
    )

  def _overlapping_functions(
      self, functions: list[str], old: str, new: str, files: list[str]
  ) -> list[str]:
    """Which of the patch's target functions appear in the upstream diff.

    Function-level overlap is a much sharper signal than file-level: files in
    this encoder run to thousands of lines and are touched constantly, while a
    change inside the exact function a heuristic hooks into is nearly always a
    reason to re-measure.
    """
    if not functions or not files:
      return []
    result = self.worktrees._git(  # noqa: SLF001 - deliberate reuse of the runner
        "diff", "-U0", f"{old}..{new}", "--", *files, check=False
    )
    hunks = re.findall(r"^@@[^@]*@@\s*(.*)$", result.stdout, re.MULTILINE)
    body = "\n".join(hunks) + "\n" + result.stdout
    return sorted({name for name in functions if re.search(rf"\b{re.escape(name)}\b", body)})


_MECHANISM_WORDS = re.compile(
    r"\b(prune|pruning|early|terminat|skip|fast|speed|threshold|reuse|cache|"
    r"memoi[sz]|simd|avx|sse|neon|trellis|partition|transform|intra|inter|"
    r"motion|search|filter)\w*", re.IGNORECASE
)


def _substitution_hints(
    subjects: list[str], functions: list[str], files: list[str]
) -> list[str]:
  """Upstream commits that plausibly implement the same idea.

  Deliberately keyword-based and deliberately noisy in the safe direction: a
  false positive costs one reading of a commit message, while a false negative
  costs a cluster round measuring a patch that upstream already made redundant.
  """
  hints = []
  # Tokenise the touched file names: "av2/encoder/partition_search.c" becomes
  # {partition, search}. Matching whole stems misses the common case, because a
  # commit subject says "partition evaluation", not "partition_search.c".
  file_tokens = {
      token
      for path in files
      for token in re.split(r"[^a-z0-9]+", os.path.basename(path).lower())
      if len(token) > 4 and token not in ("encoder", "decoder")
  }
  function_tokens = {
      token
      for name in functions
      for token in re.split(r"[^a-z0-9]+", name.lower())
      if len(token) > 4
  }
  for subject in subjects:
    lowered = subject.lower()
    if any(token in lowered for token in function_tokens):
      hints.append(subject)
      continue
    mechanism = {m.group(0).lower() for m in _MECHANISM_WORDS.finditer(lowered)}
    if file_tokens & set(re.split(r"[^a-z0-9]+", lowered)) and mechanism:
      hints.append(subject)
      continue
    if len(mechanism) >= 2:
      hints.append(subject)
  return hints[:12]
