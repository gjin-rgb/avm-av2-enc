"""Local implementations: a directory, a git remote, a log file, and psutil-free checks.

This is the default site. It has no Google dependency at all, which is what
makes the rest of the system testable and portable -- and it is also a real
deployment target: a workstation with a few cores can run everything except
full CTC, and CTC can then be delegated to whatever back end is configured.
"""

from __future__ import annotations

import json
import os
import shutil
import time

from ..util import log, proc
from ..util.io import ensure_dir, free_gib, read_json, write_json_atomic, write_text_atomic
from .base import BlobStore, CodeStore, HealthProvider, HealthStatus, Notifier

LOG = log.get("providers.local")


class LocalBlobStore(BlobStore):

  def __init__(self, root: str):
    self.root = ensure_dir(os.path.abspath(os.path.expanduser(root)))

  def _path(self, key: str) -> str:
    safe = key.strip("/").replace("..", "_")
    return os.path.join(self.root, safe)

  def put(self, key: str, data: bytes) -> str:
    path = self._path(key)
    ensure_dir(os.path.dirname(path) or ".")
    tmp = path + ".part"
    with open(tmp, "wb") as handle:
      handle.write(data)
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path

  def get(self, key: str) -> bytes | None:
    try:
      with open(self._path(key), "rb") as handle:
        return handle.read()
    except OSError:
      return None

  def exists(self, key: str) -> bool:
    return os.path.exists(self._path(key))

  def list(self, prefix: str) -> list[str]:
    base = self._path(prefix)
    if os.path.isfile(base):
      return [prefix]
    out = []
    for dirpath, _dirs, files in os.walk(base):
      for name in files:
        full = os.path.join(dirpath, name)
        out.append(os.path.relpath(full, self.root))
    return sorted(out)

  def delete(self, key: str) -> None:
    path = self._path(key)
    if os.path.isdir(path):
      shutil.rmtree(path, ignore_errors=True)
    elif os.path.exists(path):
      os.unlink(path)

  # -- leases --------------------------------------------------------------

  def _lease_path(self, name: str) -> str:
    return self._path(os.path.join("_leases", name.replace("/", "_") + ".json"))

  def acquire_lease(self, name: str, owner: str, ttl_s: float) -> bool:
    """Atomic claim via O_CREAT|O_EXCL, with TTL-based takeover.

    ``os.open`` with ``O_EXCL`` is atomic on POSIX filesystems and on NFS with
    modern servers. The TTL exists because an agent can vanish: a Cloudtop
    reboot must not strand a queue entry forever.
    """
    path = self._lease_path(name)
    ensure_dir(os.path.dirname(path))
    now = time.time()
    record = {"owner": owner, "acquired": now, "expires": now + ttl_s}
    try:
      fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
      existing = read_json(path, {}) or {}
      if existing.get("owner") == owner or existing.get("expires", 0) < now:
        write_json_atomic(path, record)
        return True
      return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      json.dump(record, handle)
    return True

  def release_lease(self, name: str, owner: str) -> None:
    path = self._lease_path(name)
    existing = read_json(path, {}) or {}
    if existing.get("owner") in (owner, None) and os.path.exists(path):
      os.unlink(path)

  def lease_holder(self, name: str) -> tuple[str, float] | None:
    record = read_json(self._lease_path(name), {}) or {}
    if not record:
      return None
    if record.get("expires", 0) < time.time():
      return None
    return (record.get("owner", ""), record.get("expires", 0.0))


class GitCodeStore(CodeStore):
  """Publishes artifacts by committing them to a git repository.

  This is how results reach the shared ``avm-patches``-style repository. It is
  intentionally the same mechanism the prior research used by hand, because
  that repository is the durable memory: "this repo is the only thing that
  survives you".
  """

  def __init__(self, repo_dir: str, *, remote: str = "origin", branch: str = "main",
               push: bool = False, author: str = "av2ra <av2ra@localhost>"):
    self.repo_dir = os.path.abspath(os.path.expanduser(repo_dir))
    self.remote = remote
    self.branch = branch
    self.push = push
    self.author = author

  def _git(self, *args: str, check: bool = False) -> proc.RunResult:
    return proc.run(["git", *args], cwd=self.repo_dir, check=check, timeout=600)

  def publish(self, local_dir: str, remote_path: str, message: str) -> str:
    if not os.path.isdir(os.path.join(self.repo_dir, ".git")):
      raise RuntimeError(f"{self.repo_dir} is not a git checkout")
    target = os.path.join(self.repo_dir, remote_path)
    ensure_dir(os.path.dirname(target) or ".")
    if os.path.isdir(local_dir):
      if os.path.exists(target):
        shutil.rmtree(target, ignore_errors=True)
      shutil.copytree(local_dir, target)
    else:
      shutil.copy2(local_dir, target)
    self._git("add", "-A", remote_path)
    name, _, email = self.author.partition(" <")
    self._git(
        "-c", f"user.name={name}", "-c", f"user.email={email.rstrip('>')}",
        "commit", "--quiet", "-m", message,
    )
    if self.push:
      result = self._git("push", self.remote, f"HEAD:{self.branch}")
      if not result.ok:
        LOG.warning("push failed: %s", result.tail(6))
    return target

  def fetch(self, remote_path: str, local_dir: str) -> bool:
    source = os.path.join(self.repo_dir, remote_path)
    if not os.path.exists(source):
      return False
    if os.path.isdir(source):
      if os.path.exists(local_dir):
        shutil.rmtree(local_dir, ignore_errors=True)
      shutil.copytree(source, local_dir)
    else:
      ensure_dir(os.path.dirname(local_dir) or ".")
      shutil.copy2(source, local_dir)
    return True

  def describe(self) -> str:
    url = self._git("remote", "get-url", self.remote).stdout.strip()
    head = self._git("rev-parse", "--short", "HEAD").stdout.strip()
    return f"git:{url or self.repo_dir}@{head} (push={'on' if self.push else 'off'})"


class FileNotifier(Notifier):
  """Writes notifications to a file and the console.

  Deliberately not email: a local run has no mail transport, and a system that
  fails because it could not send a digest is a system with a notifier on its
  critical path.
  """

  def __init__(self, path: str):
    self.path = path
    ensure_dir(os.path.dirname(os.path.abspath(path)) or ".")

  def send(self, subject: str, body: str, *, urgency: str = "normal") -> bool:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n===== [{urgency}] {stamp} {subject} =====\n{body}\n"
    with open(self.path, "a", encoding="utf-8") as handle:
      handle.write(entry)
    if urgency == "urgent":
      LOG.warning("%s: %s", subject, body.splitlines()[0] if body else "")
    else:
      LOG.info("notification: %s", subject)
    return True


class LocalHealth(HealthProvider):

  def __init__(self, workspace: str, *, min_free_gib: float = 20.0,
               required_tools: tuple[str, ...] = ("git", "cmake")):
    self.workspace = os.path.expanduser(workspace)
    self.min_free_gib = min_free_gib
    self.required_tools = required_tools

  def check(self) -> list[HealthStatus]:
    out: list[HealthStatus] = []
    free = free_gib(self.workspace if os.path.exists(self.workspace) else "/tmp")
    out.append(
        HealthStatus(
            "disk", free >= self.min_free_gib,
            f"{free:.1f} GiB free under {self.workspace}",
            remedy="run 'av2ra gc' to drop stale worktrees and build caches",
        )
    )
    for tool in self.required_tools:
      out.append(
          HealthStatus(
              f"tool:{tool}", proc.have(tool),
              "present" if proc.have(tool) else "not on PATH",
              remedy=f"install {tool}",
          )
      )
    from ..measure.encode import perf_available

    has_perf = perf_available()
    out.append(
        HealthStatus(
            "perf", has_perf,
            "instruction counting available"
            if has_perf
            else "unavailable: complexity measurements will fall back to timing",
            remedy=(
                "install linux-perf and set kernel.perf_event_paranoid<=2. "
                "Instruction counts are reproducible to well under 0.1%; timing "
                "on a shared machine has a ~2% floor, which is larger than most "
                "effects worth finding."
            ),
        )
    )
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    out.append(
        HealthStatus(
            "cpu", (cpus or 0) >= 4, f"{cpus} logical CPUs available",
            remedy="fewer than 4 CPUs leaves no room to pin encode workers",
        )
    )
    return out
