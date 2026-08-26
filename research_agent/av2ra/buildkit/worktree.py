"""Isolated git worktrees, one per experiment.

Every experiment gets its own checkout and its own build directory. The
alternative -- patch, build, measure, revert, repeat in one tree -- is what the
prior research actually did, and it failed in a way worth remembering: the
revert step used a pathspec that did not exist in this repository, git rejected
the whole command, the error went to ``/dev/null``, and three patches
accumulated on top of each other. The "baseline" was eventually built with all
three applied, and the harness reported confident, precisely formatted,
completely wrong signatures.

So: no shared mutable tree, and every state this module depends on is asserted
rather than assumed. :meth:`WorktreeManager.create` verifies the new tree is at
the expected commit and clean before returning it; :meth:`apply_patch` verifies
the diff actually landed.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass

from ..core.models import PatchInfo
from ..util import log, proc
from ..util.io import free_gib

LOG = log.get("worktree")


class WorktreeError(RuntimeError):
  pass


@dataclass
class Worktree:
  experiment_id: str
  path: str
  branch: str
  base_sha: str

  @property
  def build_dir(self) -> str:
    return os.path.join(self.path, "build_av2ra")


class WorktreeManager:

  def __init__(self, repo: str, root: str, *, min_free_gib: float = 15.0):
    self.repo = os.path.abspath(os.path.expanduser(repo))
    self.root = os.path.abspath(os.path.expanduser(root))
    self.min_free_gib = min_free_gib
    os.makedirs(self.root, exist_ok=True)
    if not os.path.isdir(os.path.join(self.repo, ".git")):
      raise WorktreeError(f"{self.repo} is not a git checkout")

  # -- repository queries --------------------------------------------------

  def _git(self, *args: str, cwd: str | None = None, check: bool = True) -> proc.RunResult:
    return proc.run(["git", *args], cwd=cwd or self.repo, check=check, timeout=900)

  def resolve(self, ref: str) -> str:
    result = self._git("rev-parse", ref)
    return result.stdout.strip()

  def fetch(self, remote: str = "origin", ref: str | None = None) -> None:
    args = ["fetch", "--quiet", remote]
    if ref:
      args.append(ref)
    self._git(*args, check=False)

  def commits_between(self, old: str, new: str) -> int:
    result = self._git("rev-list", "--count", f"{old}..{new}", check=False)
    try:
      return int(result.stdout.strip())
    except ValueError:
      return -1

  def files_changed_between(self, old: str, new: str) -> list[str]:
    result = self._git("diff", "--name-only", f"{old}..{new}", check=False)
    return sorted(set(filter(None, result.stdout.splitlines())))

  def log_between(self, old: str, new: str, limit: int = 60) -> list[str]:
    result = self._git(
        "log", "--oneline", f"-{limit}", f"{old}..{new}", check=False
    )
    return [line for line in result.stdout.splitlines() if line]

  # -- worktree lifecycle --------------------------------------------------

  def create(self, experiment_id: str, base_ref: str) -> Worktree:
    if free_gib(self.root) < self.min_free_gib:
      raise WorktreeError(
          f"only {free_gib(self.root):.1f} GiB free under {self.root}; refusing "
          f"to create a worktree (need {self.min_free_gib:.0f} GiB). Run "
          "'av2ra gc' or free space -- a build that runs out of disk halfway "
          "produces a binary that links but is not the code you think it is."
      )
    base_sha = self.resolve(base_ref)
    path = os.path.join(self.root, f"exp_{experiment_id}")
    branch = f"av2ra/exp/{experiment_id}"
    if os.path.exists(path):
      self.remove(experiment_id)

    self._git("worktree", "add", "--force", "-B", branch, path, base_sha)

    # Assert the state we are about to depend on.
    head = self._git("rev-parse", "HEAD", cwd=path).stdout.strip()
    if head != base_sha:
      raise WorktreeError(
          f"worktree for {experiment_id} is at {head[:12]}, expected {base_sha[:12]}"
      )
    dirty = self._git("status", "--porcelain", cwd=path).stdout.strip()
    if dirty:
      raise WorktreeError(f"fresh worktree for {experiment_id} is already dirty:\n{dirty}")
    log.event("worktree_created", experiment_id=experiment_id, path=path, base=base_sha)
    return Worktree(experiment_id=experiment_id, path=path, branch=branch, base_sha=base_sha)

  def remove(self, experiment_id: str) -> None:
    path = os.path.join(self.root, f"exp_{experiment_id}")
    branch = f"av2ra/exp/{experiment_id}"
    if os.path.exists(path):
      self._git("worktree", "remove", "--force", path, check=False)
    if os.path.exists(path):
      shutil.rmtree(path, ignore_errors=True)
    self._git("branch", "-D", branch, check=False)
    self._git("worktree", "prune", check=False)

  def list_worktrees(self) -> list[str]:
    result = self._git("worktree", "list", "--porcelain", check=False)
    return [
        line.split(" ", 1)[1]
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    ]

  # -- patches -------------------------------------------------------------

  def apply_patch(self, worktree: Worktree, diff_text: str) -> None:
    """Apply a unified diff, then verify it actually changed the tree."""
    if not diff_text.strip():
      raise WorktreeError("refusing to apply an empty patch")
    patch_path = os.path.join(worktree.path, ".av2ra-patch.diff")
    with open(patch_path, "w", encoding="utf-8") as handle:
      handle.write(diff_text if diff_text.endswith("\n") else diff_text + "\n")
    result = proc.run(
        ["git", "apply", "--whitespace=nowarn", patch_path],
        cwd=worktree.path, timeout=300,
    )
    if not result.ok:
      three_way = proc.run(
          ["git", "apply", "--3way", "--whitespace=nowarn", patch_path],
          cwd=worktree.path, timeout=300,
      )
      if not three_way.ok:
        raise WorktreeError(
            f"patch does not apply to {worktree.base_sha[:12]}:\n{result.tail(20)}"
        )
    os.unlink(patch_path)
    if not self._git("status", "--porcelain", cwd=worktree.path).stdout.strip():
      raise WorktreeError(
          "patch applied but the tree is unchanged: the diff is a no-op against "
          "this base. Check whether the change already landed upstream."
      )

  def diff(self, worktree: Worktree) -> str:
    return self._git("diff", worktree.base_sha, cwd=worktree.path, check=False).stdout

  def describe_patch(self, worktree: Worktree) -> PatchInfo:
    diff_text = self.diff(worktree)
    return describe_diff(diff_text, base_sha=worktree.base_sha)

  def commit(self, worktree: Worktree, message: str) -> str:
    self._git("add", "-A", cwd=worktree.path, check=False)
    self._git(
        "-c", "user.name=av2ra", "-c", "user.email=av2ra@localhost",
        "commit", "--quiet", "-m", message, cwd=worktree.path, check=False,
    )
    return self._git("rev-parse", "HEAD", cwd=worktree.path).stdout.strip()

  def check_applies(self, diff_text: str, ref: str) -> str:
    """Does this diff still apply to ``ref``? Cheap, and not sufficient.

    A clean apply after an upstream move means the patch is *textually*
    compatible. It says nothing about whether the code it hooks into still runs
    on the path it was measured on. The prior corpus has the worked example: an
    upstream commit added 187 lines to a file two patches modified, and all ten
    patches still reported APPLIES.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
      patch_path = os.path.join(tmp, "p.diff")
      with open(patch_path, "w", encoding="utf-8") as handle:
        handle.write(diff_text)
      probe = os.path.join(tmp, "probe")
      self._git("worktree", "add", "--detach", "--force", probe, ref, check=False)
      try:
        if proc.run(["git", "apply", "--check", patch_path], cwd=probe).ok:
          return "APPLIES"
        if proc.run(["git", "apply", "--3way", "--check", patch_path], cwd=probe).ok:
          return "NEEDS-3WAY"
        return "FAILS"
      finally:
        self._git("worktree", "remove", "--force", probe, check=False)


_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_HUNK_FUNC = re.compile(r"^@@ [^@]*@@\s*(.*)$", re.MULTILINE)
_C_FUNC = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def describe_diff(diff_text: str, *, base_sha: str = "") -> PatchInfo:
  """Extract the facts about a diff that policy and dedup need."""
  files = sorted(set(_DIFF_FILE.findall(diff_text)))
  added = sum(
      1 for line in diff_text.splitlines()
      if line.startswith("+") and not line.startswith("+++")
  )
  removed = sum(
      1 for line in diff_text.splitlines()
      if line.startswith("-") and not line.startswith("---")
  )
  functions = []
  for context in _HUNK_FUNC.findall(diff_text):
    match = _C_FUNC.search(context)
    if match and match.group(1) not in ("if", "for", "while", "switch", "return"):
      functions.append(match.group(1))
  defines = dict(re.findall(r"#\s*ifndef\s+(AVM_[A-Z0-9_]+)", diff_text) and [] or [])
  for name, value in re.findall(
      r"#\s*define\s+([A-Z][A-Z0-9_]{3,})\s+(\S+)", diff_text
  ):
    defines[name] = value
  return PatchInfo(
      diff_text=diff_text,
      base_sha=base_sha,
      files=files,
      added_lines=added,
      removed_lines=removed,
      touched_functions=sorted(set(functions)),
      build_defines=defines,
      normalized_hash=normalized_hash(diff_text),
  )


def normalized_hash(diff_text: str) -> str:
  """Identity of a change, insensitive to formatting and line numbers.

  Used for dedup against the prior corpus: the same idea re-derived by a
  different generator, or the same patch re-proposed after a rebase, must
  collide. Hunk headers, blank lines, comments and indentation are dropped.
  """
  import hashlib

  kept = []
  for line in diff_text.splitlines():
    if line.startswith(("@@", "index ", "--- ", "+++ ", "diff --git")):
      continue
    if not line[:1] in ("+", "-"):
      continue
    body = line[1:].strip()
    if not body or body.startswith(("//", "/*", "*")):
      continue
    kept.append(line[0] + re.sub(r"\s+", " ", body))
  return hashlib.sha256("\n".join(kept).encode()).hexdigest()[:20]
