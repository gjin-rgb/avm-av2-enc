"""Turning a hypothesis into a patch that compiles and does what it claims.

The model edits by exact search-and-replace, not by emitting a unified diff.
That choice is deliberate: a diff carries line numbers and hunk headers that a
language model gets subtly wrong often enough to matter, and a diff that fails
to apply after ten minutes of context assembly is a wasted experiment. Exact
anchors either match or they do not, the failure is immediate and specific, and
the repair prompt can say *which* anchor was not found.

The build-repair loop is bounded and it is not a free retry. Each failure is fed
back with the compiler's own message, but after the budget is spent the
experiment fails as ``BUILD_FAIL`` and is recorded that way, because an idea
that cannot be expressed in three attempts is usually an idea whose author does
not understand the code it touches -- and that is worth knowing.

Everything the model writes passes through :mod:`av2ra.integrity.policy` before
it is built. Nothing here decides whether a patch is *allowed*; it only decides
whether it *compiles*.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..buildkit.builder import BuildResult, BuildSpec, Builder
from ..buildkit.worktree import Worktree, WorktreeManager, describe_diff
from ..core.models import Hypothesis, PatchInfo
from ..integrity import policy as policy_mod
from ..knowledge.codemap import CodeMap
from ..util import log
from .llm import LLMClient

LOG = log.get("agent.implementer")

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "search": {"type": "string"},
                    "replace": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["file", "search", "replace"],
                "additionalProperties": False,
            },
        },
        "defines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "default": {"type": "string"},
                },
                "required": ["name", "default"],
                "additionalProperties": False,
            },
        },
        "activation_note": {"type": "string"},
    },
    "required": ["summary", "edits"],
    "additionalProperties": False,
}

SYSTEM = """You are writing C for the AV2 reference encoder (libavm). You are
editing a real codebase that other people read, so the change must look like it
belongs: same brace and indentation style as the surrounding code, same naming
conventions, no reformatting of lines you are not changing, and comments only
where the reason for the code is not obvious from the code.

Rules that are checked mechanically after you answer, so violating them wastes
the attempt:

  * You may only modify encoder sources. Not the build files, not the test
    framework, not the metric tooling, not the decoder.
  * No environment variables, no randomness, no wall-clock reads, no special
    casing on frame index, resolution, QP or sequence name. A heuristic that
    keys on properties of the test set is benchmark fitting, not encoding.
  * A tunable threshold must be a #define with a default, so it can be swept
    with -D without editing the patch again.
  * Each edit's `search` text must appear EXACTLY ONCE in the named file,
    copied character for character from the source shown to you, including
    indentation. Include two or three surrounding lines to make it unique.

If the change cannot be made without breaking one of these rules, return an
empty `edits` list and say why in `summary`. That is a useful result."""


@dataclass
class ImplementationResult:
  ok: bool
  patch: PatchInfo = field(default_factory=PatchInfo)
  build: BuildResult | None = None
  attempts: int = 0
  violations: list[policy_mod.PolicyViolation] = field(default_factory=list)
  error: str = ""
  transcript: list[str] = field(default_factory=list)
  activation_note: str = ""

  @property
  def blocked_by_policy(self) -> bool:
    return any(v.severity == "blocker" for v in self.violations)


def source_excerpt(root: str, path: str, line: int, *, before: int = 40, after: int = 160) -> str:
  """The real code around a target, with line numbers, for the model to anchor on."""
  full = os.path.join(root, path)
  try:
    with open(full, "r", encoding="utf-8", errors="replace") as handle:
      lines = handle.readlines()
  except OSError:
    return ""
  start = max(0, line - before - 1)
  end = min(len(lines), line + after)
  numbered = [f"{index + 1:6d}| {lines[index].rstrip()}" for index in range(start, end)]
  return f"--- {path} (lines {start + 1}-{end}) ---\n" + "\n".join(numbered)


class Implementer:

  def __init__(
      self,
      client: LLMClient,
      worktrees: WorktreeManager,
      builder: Builder,
      *,
      policy: policy_mod.PatchPolicy | None = None,
      max_attempts: int = 3,
      build_jobs: int = 0,
  ):
    self.client = client
    self.worktrees = worktrees
    self.builder = builder
    self.policy = policy or policy_mod.PatchPolicy()
    self.max_attempts = max_attempts
    self.build_jobs = build_jobs

  # -- context -------------------------------------------------------------

  def _context(self, hypothesis: Hypothesis, codemap: CodeMap, worktree: Worktree) -> str:
    parts = [
        f"HYPOTHESIS: {hypothesis.title}",
        f"MECHANISM: {hypothesis.mechanism.value}",
        f"CLAIM: {hypothesis.statement}",
        f"REASONING: {hypothesis.rationale}",
        f"TARGET PRESETS: {hypothesis.target_presets}",
    ]
    if hypothesis.mechanism.must_be_bit_exact:
      parts.append(
          "NOTE: this mechanism must produce a BYTE-IDENTICAL bitstream. If your "
          "change can alter any encode decision, it is the wrong mechanism and "
          "the experiment will be rejected as a bug rather than measured."
      )
    if hypothesis.parameters:
      parts.append(
          "REQUIRED TUNABLE DEFINES: "
          + ", ".join(f"{k} (default {v})" for k, v in hypothesis.parameters.items())
      )
    parts.append("\nSOURCE YOU ARE EDITING (copy `search` anchors from here verbatim):")
    seen_files = set()
    for name in hypothesis.target_functions[:4]:
      info = codemap.function(name)
      if not info:
        continue
      excerpt = source_excerpt(worktree.path, info.path, info.line)
      if excerpt:
        parts.append(f"\n### function {name}\n{excerpt}")
        seen_files.add(info.path)
    for path in hypothesis.target_files:
      if path in seen_files:
        continue
      full = os.path.join(worktree.path, path)
      if os.path.exists(full) and os.path.getsize(full) < 60000:
        with open(full, "r", encoding="utf-8", errors="replace") as handle:
          body = handle.read()
        numbered = "\n".join(
            f"{i + 1:6d}| {line}" for i, line in enumerate(body.splitlines()[:400])
        )
        parts.append(f"\n### file {path} (first 400 lines)\n{numbered}")
    return "\n".join(parts)

  # -- the loop ------------------------------------------------------------

  def implement(
      self,
      hypothesis: Hypothesis,
      worktree: Worktree,
      codemap: CodeMap,
      *,
      build_spec_factory=None,
  ) -> ImplementationResult:
    result = ImplementationResult(ok=False)
    context = self._context(hypothesis, codemap, worktree)
    feedback = ""

    for attempt in range(1, self.max_attempts + 1):
      result.attempts = attempt
      prompt = context if not feedback else f"{context}\n\n{feedback}"
      data, response = self.client.json(
          system=SYSTEM, prompt=prompt, schema=EDIT_SCHEMA, max_tokens=16000
      )
      if not response.ok or not data:
        result.error = f"model call failed: {response.error or 'no structured output'}"
        return result
      edits = data.get("edits") or []
      result.activation_note = data.get("activation_note", "")
      if not edits:
        result.error = f"the model declined to produce edits: {data.get('summary', '')}"
        return result

      applied, problems = self._apply_edits(worktree, edits)
      result.transcript.append(f"attempt {attempt}: {len(applied)} edit(s) applied")
      if problems:
        feedback = (
            "YOUR EDITS DID NOT APPLY. Fix these and return the full edit list "
            "again:\n  - " + "\n  - ".join(problems)
        )
        self._revert(worktree)
        continue

      diff_text = self.worktrees.diff(worktree)
      patch = describe_diff(diff_text, base_sha=worktree.base_sha)
      patch.build_defines = {
          item["name"]: item["default"]
          for item in (data.get("defines") or [])
          if isinstance(item, dict) and item.get("name")
      }
      result.patch = patch

      violations = (
          policy_mod.check_paths(patch.files, self.policy)
          + policy_mod.scan_diff(diff_text)
          + policy_mod.check_defines(patch.build_defines, diff_text)
          + policy_mod.check_mechanism(hypothesis.mechanism.value, patch.files)
      )
      result.violations = violations
      blockers = [v for v in violations if v.severity == "blocker"]
      if blockers:
        feedback = (
            "YOUR PATCH VIOLATED THE POLICY. These are checked mechanically and "
            "are not negotiable:\n  - "
            + "\n  - ".join(f"{v.kind}: {v.detail} ({v.why_it_matters})" for v in blockers[:6])
        )
        self._revert(worktree)
        result.error = "; ".join(str(v) for v in blockers[:4])
        continue

      spec = (
          build_spec_factory(worktree, patch)
          if build_spec_factory
          else BuildSpec(
              source_dir=worktree.path, build_dir=worktree.build_dir,
              patch_defines=patch.build_defines,
              source_identity=f"{worktree.base_sha}:{patch.normalized_hash}",
              jobs=self.build_jobs,
          )
      )
      build = self.builder.build(spec, allow_cache=False)
      result.build = build
      if build.ok:
        result.ok = True
        if build.warnings:
          result.transcript.append(
              f"build produced {len(build.warnings)} warning(s); "
              "new warnings on model-authored C are a review signal"
          )
        return result

      feedback = (
          "YOUR PATCH DID NOT COMPILE. The compiler said:\n"
          + _trim_build_error(build.error)
          + "\nReturn the corrected full edit list."
      )
      result.error = build.error
      self._revert(worktree)

    result.error = result.error or "exhausted implementation attempts"
    return result

  # -- edit application ----------------------------------------------------

  def _apply_edits(self, worktree: Worktree, edits: list[dict]) -> tuple[list[dict], list[str]]:
    applied, problems = [], []
    for index, edit in enumerate(edits):
      path = (edit.get("file") or "").strip().lstrip("./")
      search = edit.get("search") or ""
      replace = edit.get("replace") or ""
      full = os.path.join(worktree.path, path)
      if not path or not os.path.exists(full):
        problems.append(f"edit {index + 1}: file '{path}' does not exist in the tree")
        continue
      allowed, reason = self.policy.path_allowed(path)
      if not allowed:
        problems.append(f"edit {index + 1}: {path} may not be modified -- {reason}")
        continue
      with open(full, "r", encoding="utf-8", errors="replace") as handle:
        body = handle.read()
      occurrences = body.count(search)
      if occurrences == 0:
        problems.append(
            f"edit {index + 1}: the `search` text was not found in {path}. "
            "Copy it verbatim from the source shown, including indentation."
        )
        continue
      if occurrences > 1:
        problems.append(
            f"edit {index + 1}: the `search` text appears {occurrences} times in "
            f"{path}. Add surrounding lines until it is unique."
        )
        continue
      with open(full, "w", encoding="utf-8") as handle:
        handle.write(body.replace(search, replace, 1))
      applied.append(edit)
    return (applied, problems)

  def _revert(self, worktree: Worktree) -> None:
    """Return the tree to the anchor, and prove it went back.

    An unverified cleanup step is not a cleanup step: the prior research lost a
    round to a revert that silently did nothing and let three patches
    accumulate on top of each other.
    """
    from ..util import proc

    proc.run(["git", "checkout", "--", "."], cwd=worktree.path, timeout=300)
    proc.run(["git", "clean", "-fd"], cwd=worktree.path, timeout=300)
    dirty = proc.run(["git", "status", "--porcelain"], cwd=worktree.path).stdout.strip()
    if dirty:
      raise RuntimeError(
          f"worktree {worktree.path} is still dirty after revert:\n{dirty}"
      )


_ERROR_LINE = re.compile(r"^.*\b(?:error|Error|ERROR)\b.*$", re.MULTILINE)


def _trim_build_error(text: str, *, limit: int = 30) -> str:
  """Give the model the errors, not five thousand lines of ninja output."""
  errors = _ERROR_LINE.findall(text or "")
  if errors:
    return "\n".join(errors[:limit])
  return "\n".join((text or "").splitlines()[-limit:])
