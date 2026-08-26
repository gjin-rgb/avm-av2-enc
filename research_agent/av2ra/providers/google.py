"""Google-internal implementations. The only file in this package that knows CNS exists.

Together with :mod:`av2ra.ctc.eda`, this is the entire Google surface of the
system, matching the boundary the design sets: **data storage, code storage and
CTC runs may be internal; nothing else may be.** Everything here is reachable
only through the interfaces in :mod:`av2ra.providers.base`, so removing this
file leaves a working system that runs locally.

Nothing here is importable-at-a-distance: the module imports cleanly on a
machine with no Google tooling, and every operation degrades to a clear error
rather than a traceback, because a laptop developer reading this code should be
able to run the test suite.

Tooling used, and why each is the sanctioned choice:
  ``fileutil``      the supported CLI for CNS; avoids linking a client library
                    into a process that must also run outside google3.
  ``gcertstatus``   the only supported way to read LOAS2 validity.
  ``g4``/CitC       code storage for internal publication.
  ``sendgmr``       message delivery from a Cloudtop without an SMTP setup.
"""

from __future__ import annotations

import json
import os
import re
import time

from ..util import log, proc
from ..util.io import ensure_dir
from .base import BlobStore, CodeStore, HealthProvider, HealthStatus, Notifier

LOG = log.get("providers.google")


class ToolMissing(RuntimeError):
  """Raised when an internal CLI is not available on this machine."""


def _require(binary: str) -> None:
  if not proc.have(binary):
    raise ToolMissing(
        f"'{binary}' is not on PATH. This site profile expects a Google "
        f"workstation; use --site=local to run without internal infrastructure."
    )


class CnsBlobStore(BlobStore):
  """Artifact storage on CNS, via ``fileutil``.

  Leases are implemented with ``fileutil test -f`` plus a write, which is not
  atomic. That is stated rather than hidden: CNS has no compare-and-swap
  primitive exposed through ``fileutil``, so the lease here is advisory and the
  window is the round-trip time. The queue tolerates it because a duplicated
  experiment costs local CPU and is detected at registry insert time by id
  collision -- whereas a *missed* lease would deadlock the fleet. Sites that
  need a hard lock should point ``coordination_root`` at a filesystem that
  supports ``O_EXCL`` (an NFS home directory works) and use the local store for
  leases alone.
  """

  def __init__(self, prefix: str, *, local_cache: str | None = None):
    self.prefix = prefix.rstrip("/")
    self.local_cache = os.path.expanduser(local_cache) if local_cache else None
    if self.local_cache:
      ensure_dir(self.local_cache)

  def _remote(self, key: str) -> str:
    return f"{self.prefix}/{key.strip('/')}"

  def put(self, key: str, data: bytes) -> str:
    _require("fileutil")
    remote = self._remote(key)
    proc.run(["fileutil", "mkdir", "-p", os.path.dirname(remote)], timeout=120)
    # Write through a local temp file: fileutil reads stdin only for some
    # subcommands and the behaviour differs across versions.
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as handle:
      handle.write(data)
      tmp = handle.name
    try:
      result = proc.run(["fileutil", "cp", "-f", tmp, remote], timeout=1800)
      if not result.ok:
        raise RuntimeError(f"fileutil cp failed for {remote}: {result.tail(6)}")
    finally:
      os.unlink(tmp)
    return remote

  def get(self, key: str) -> bytes | None:
    _require("fileutil")
    import tempfile

    remote = self._remote(key)
    with tempfile.TemporaryDirectory() as tmpdir:
      local = os.path.join(tmpdir, "blob")
      result = proc.run(["fileutil", "cp", "-f", remote, local], timeout=1800)
      if not result.ok or not os.path.exists(local):
        return None
      with open(local, "rb") as handle:
        return handle.read()

  def exists(self, key: str) -> bool:
    _require("fileutil")
    return proc.run(["fileutil", "test", "-f", self._remote(key)], timeout=120).returncode == 0

  def list(self, prefix: str) -> list[str]:
    _require("fileutil")
    result = proc.run(["fileutil", "ls", "-R", self._remote(prefix)], timeout=600)
    if not result.ok:
      return []
    out = []
    for line in result.stdout.splitlines():
      line = line.strip()
      if line.startswith(self.prefix):
        out.append(line[len(self.prefix) + 1 :])
    return sorted(out)

  def delete(self, key: str) -> None:
    _require("fileutil")
    proc.run(["fileutil", "rm", "-f", self._remote(key)], timeout=300)

  def acquire_lease(self, name: str, owner: str, ttl_s: float) -> bool:
    key = f"_leases/{name.replace('/', '_')}.json"
    existing = self.get(key)
    now = time.time()
    if existing:
      try:
        record = json.loads(existing.decode())
      except (ValueError, UnicodeDecodeError):
        record = {}
      if record.get("owner") not in (owner, None) and record.get("expires", 0) > now:
        return False
    self.put(
        key,
        json.dumps({"owner": owner, "acquired": now, "expires": now + ttl_s}).encode(),
    )
    # Read back: with no atomic primitive, confirming we are the recorded owner
    # closes most of the race, and a lost race then costs duplicated local work
    # rather than two agents writing the same experiment id.
    confirm = self.get(key)
    try:
      return bool(confirm and json.loads(confirm.decode()).get("owner") == owner)
    except (ValueError, UnicodeDecodeError):
      return False

  def release_lease(self, name: str, owner: str) -> None:
    key = f"_leases/{name.replace('/', '_')}.json"
    existing = self.get(key)
    if not existing:
      return
    try:
      if json.loads(existing.decode()).get("owner") == owner:
        self.delete(key)
    except (ValueError, UnicodeDecodeError):
      self.delete(key)

  def lease_holder(self, name: str) -> tuple[str, float] | None:
    data = self.get(f"_leases/{name.replace('/', '_')}.json")
    if not data:
      return None
    try:
      record = json.loads(data.decode())
    except (ValueError, UnicodeDecodeError):
      return None
    if record.get("expires", 0) < time.time():
      return None
    return (record.get("owner", ""), float(record.get("expires", 0.0)))


class CitcCodeStore(CodeStore):
  """Publication into a google3 CitC client.

  Publishing is a copy plus ``g4 add``; committing a change list is left to a
  human on purpose. An autonomous agent that can mail a CL is an autonomous
  agent that can land one, and code review is the point at which a person
  should still be in the loop.
  """

  def __init__(self, client_root: str, package: str):
    self.client_root = os.path.expanduser(client_root)
    self.package = package.strip("/")

  def publish(self, local_dir: str, remote_path: str, message: str) -> str:
    import shutil

    target = os.path.join(self.client_root, self.package, remote_path)
    ensure_dir(os.path.dirname(target) or ".")
    if os.path.isdir(local_dir):
      if os.path.exists(target):
        shutil.rmtree(target, ignore_errors=True)
      shutil.copytree(local_dir, target)
    else:
      shutil.copy2(local_dir, target)
    if proc.have("g4"):
      proc.run(["g4", "add", target], cwd=self.client_root, timeout=300)
    LOG.info("staged %s in CitC client (%s)", remote_path, message)
    return target

  def fetch(self, remote_path: str, local_dir: str) -> bool:
    import shutil

    source = os.path.join(self.client_root, self.package, remote_path)
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
    return f"citc:{self.client_root}/{self.package}"


class SendgmrNotifier(Notifier):

  def __init__(self, recipient: str, sender: str = ""):
    self.recipient = recipient
    self.sender = sender or recipient

  def send(self, subject: str, body: str, *, urgency: str = "normal") -> bool:
    if not proc.have("sendgmr"):
      LOG.warning("sendgmr unavailable; notification not delivered: %s", subject)
      return False
    prefix = "[URGENT] " if urgency == "urgent" else ""
    result = proc.run(
        [
            "sendgmr",
            f"--to={self.recipient}",
            f"--from={self.sender}",
            f"--subject={prefix}{subject}",
            "--body_file=/dev/stdin",
        ],
        input_text=body,
        timeout=300,
    )
    if not result.ok:
      LOG.warning("sendgmr failed: %s", result.tail(4))
    return result.ok


_LOAS = re.compile(r"LOAS2? .*?expires in (?:(\d+)h)?\s*(?:(\d+)m)?", re.IGNORECASE)


class GoogleHealth(HealthProvider):
  """Credential and quota health on a Cloudtop.

  The alert threshold is deliberately generous. A CTC round takes hours; a
  certificate with ninety minutes left will expire mid-submission, and the
  failure mode is a half-submitted job set that must be diagnosed by hand.
  """

  def __init__(self, workspace: str, *, warn_hours: float = 4.0,
               clip_cache: str = "", min_free_gib: float = 20.0):
    self.workspace = os.path.expanduser(workspace)
    self.warn_hours = warn_hours
    self.clip_cache = os.path.expanduser(clip_cache) if clip_cache else ""
    self.min_free_gib = min_free_gib

  def check(self) -> list[HealthStatus]:
    from ..util.io import free_gib

    out: list[HealthStatus] = []
    out.append(self._loas())
    free = free_gib(self.workspace if os.path.exists(self.workspace) else "/tmp")
    out.append(
        HealthStatus(
            "disk", free >= self.min_free_gib, f"{free:.1f} GiB free",
            remedy="run 'av2ra gc'; a build that fills the disk links a binary that is not the code you think it is",
        )
    )
    if self.clip_cache:
      present = (
          len([f for f in os.listdir(self.clip_cache) if f.endswith((".y4m", ".yuv"))])
          if os.path.isdir(self.clip_cache) else 0
      )
      out.append(
          HealthStatus(
              "clips", present > 0, f"{present} clip(s) cached in {self.clip_cache}",
              remedy="run 'av2ra clips sync' to fetch the screening set from CNS",
          )
      )
    return out

  def _loas(self) -> HealthStatus:
    if not proc.have("gcertstatus"):
      return HealthStatus(
          "loas", False, "gcertstatus not found",
          remedy="this site profile expects a Google workstation; use --site=local otherwise",
      )
    result = proc.run(["gcertstatus", "--check_remaining=0s"], timeout=60)
    text = (result.stdout or "") + (result.stderr or "")
    match = _LOAS.search(text)
    if not match:
      return HealthStatus(
          "loas", result.returncode == 0,
          text.strip().splitlines()[0] if text.strip() else "no certificate information",
          remedy="run 'gcert'",
      )
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    remaining_h = hours + minutes / 60.0
    ok = remaining_h >= self.warn_hours
    return HealthStatus(
        "loas", ok, f"LOAS2 valid for {hours}h{minutes:02d}m",
        remedy=(
            "run 'gcert' before submitting: a certificate that expires mid-round "
            "leaves a half-submitted job set to untangle by hand"
        ),
        expires_in_s=remaining_h * 3600.0,
    )


def sync_clips_from_cns(cns_dir: str, local_dir: str, filenames: list[str]) -> dict:
  """Fetch the screening clips to local NVMe. Sanctioned: this is data storage."""
  _require("fileutil")
  ensure_dir(local_dir)
  fetched, failed = [], []
  for name in filenames:
    target = os.path.join(local_dir, name)
    if os.path.exists(target) and os.path.getsize(target) > 4096:
      continue
    result = proc.run(
        ["fileutil", "cp", "-f", f"{cns_dir.rstrip('/')}/{name}", target], timeout=7200
    )
    (fetched if result.ok else failed).append(name)
  return {"fetched": fetched, "failed": failed, "local_dir": local_dir}
