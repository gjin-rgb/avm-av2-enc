"""Filesystem helpers that survive a crashed or killed agent.

An autonomous run is long, unattended, and gets interrupted: a Cloudtop
reboots, a lease expires mid-write, the operator hits Ctrl-C during a 40-minute
screening pass. Every persistent write in this package therefore goes through
``write_text_atomic`` / ``write_json_atomic``, so a reader never observes a
half-written experiment record. The failure this prevents is not hypothetical:
a truncated registry entry silently becomes an experiment with no verdict,
which the planner then re-queues forever.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
from typing import Any, Iterator


def ensure_dir(path: str) -> str:
  os.makedirs(path, exist_ok=True)
  return path


def write_text_atomic(path: str, text: str, *, encoding: str = "utf-8") -> None:
  """Replace ``path`` with ``text`` in one observable step."""
  directory = os.path.dirname(os.path.abspath(path)) or "."
  ensure_dir(directory)
  fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
  try:
    with os.fdopen(fd, "w", encoding=encoding) as handle:
      handle.write(text)
      handle.flush()
      # fsync before rename: on a machine that loses power mid-run the rename
      # can otherwise land while the data behind it has not.
      os.fsync(handle.fileno())
    os.replace(tmp, path)
  except BaseException:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise


def write_json_atomic(path: str, obj: Any, *, indent: int = 2) -> None:
  write_text_atomic(
      path, json.dumps(obj, indent=indent, sort_keys=True, default=str) + "\n"
  )


def read_json(path: str, default: Any = None) -> Any:
  try:
    with open(path, "r", encoding="utf-8") as handle:
      return json.load(handle)
  except FileNotFoundError:
    return default
  except json.JSONDecodeError:
    # A corrupt record is a bug worth surfacing, but it must not take down a
    # fleet-wide scan of a hundred experiment directories.
    return default


def append_jsonl(path: str, obj: Any) -> None:
  """Append one JSON record. Line-atomic for records under the pipe buffer."""
  ensure_dir(os.path.dirname(os.path.abspath(path)) or ".")
  line = json.dumps(obj, sort_keys=True, default=str) + "\n"
  with open(path, "a", encoding="utf-8") as handle:
    handle.write(line)
    handle.flush()


def read_jsonl(path: str) -> Iterator[dict]:
  try:
    with open(path, "r", encoding="utf-8") as handle:
      for line in handle:
        line = line.strip()
        if not line:
          continue
        try:
          yield json.loads(line)
        except json.JSONDecodeError:
          continue
  except FileNotFoundError:
    return


def free_gib(path: str) -> float:
  """Free space in GiB on the filesystem holding ``path`` (0.0 if absent)."""
  try:
    st = os.statvfs(path)
  except OSError:
    return 0.0
  return st.f_bavail * st.f_frsize / (1024.0 ** 3)


def link_or_copy(src: str, dst: str) -> None:
  """Hard-link when possible, copy otherwise.

  Screening clips are hundreds of megabytes and every worktree wants one. A
  hard link costs nothing; a copy per worktree fills the NVMe disk that the
  builds also need.
  """
  ensure_dir(os.path.dirname(os.path.abspath(dst)) or ".")
  if os.path.exists(dst):
    return
  try:
    os.link(src, dst)
    return
  except OSError as exc:
    if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK):
      raise
  import shutil

  shutil.copy2(src, dst)
