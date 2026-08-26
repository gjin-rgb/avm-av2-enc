"""The infrastructure boundary.

The requirement this file encodes: *only three things may depend on
Google-internal infrastructure* -- data storage, code storage, and CTC runs.
Everything else in this system must run unchanged on a laptop, in a container,
or inside google3.

That boundary is drawn here as four small interfaces. The core never imports
:mod:`av2ra.providers.google`; it asks the factory for a ``BlobStore`` and gets
whichever implementation the site profile names. The practical test of whether
the boundary holds: ``grep -r 'cns\\|fileutil\\|gcert\\|blade\\|google3'`` over
``av2ra/`` should only ever match ``providers/google.py`` and ``ctc/eda.py``.

Interfaces are deliberately narrow. A wide interface leaks assumptions: if
``BlobStore`` exposed a filesystem path, every caller would start joining paths
and the CNS implementation would have to fake a mount. It exposes bytes and
keys, so the local implementation is a directory and the Google implementation
is a CNS prefix, and neither leaks.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class HealthStatus:
  name: str
  ok: bool
  detail: str
  remedy: str = ""
  expires_in_s: float | None = None

  def __str__(self) -> str:
    mark = "OK" if self.ok else "ATTENTION"
    text = f"[{mark}] {self.name}: {self.detail}"
    return f"{text}\n         -> {self.remedy}" if self.remedy and not self.ok else text


class BlobStore(abc.ABC):
  """Durable storage for artifacts and shared state.

  Sanctioned Google dependency: on a Cloudtop this is CNS. Everywhere else it
  is a directory, an object store, or a git repository. Keys are POSIX-like
  relative paths; the implementation owns the prefix.
  """

  @abc.abstractmethod
  def put(self, key: str, data: bytes) -> str:
    """Store bytes; return an opaque locator for the report."""

  @abc.abstractmethod
  def get(self, key: str) -> bytes | None:
    ...

  @abc.abstractmethod
  def exists(self, key: str) -> bool:
    ...

  @abc.abstractmethod
  def list(self, prefix: str) -> list[str]:
    ...

  @abc.abstractmethod
  def delete(self, key: str) -> None:
    ...

  def put_file(self, key: str, path: str) -> str:
    with open(path, "rb") as handle:
      return self.put(key, handle.read())

  def get_file(self, key: str, path: str) -> bool:
    data = self.get(key)
    if data is None:
      return False
    import os

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as handle:
      handle.write(data)
    return True

  @abc.abstractmethod
  def acquire_lease(self, name: str, owner: str, ttl_s: float) -> bool:
    """Atomically claim a named lease. The fleet's only coordination primitive.

    A lease rather than a lock: an agent that dies mid-experiment must not hold
    the queue forever, and a TTL is the only mechanism that survives a machine
    disappearing without notice.
    """

  @abc.abstractmethod
  def release_lease(self, name: str, owner: str) -> None:
    ...

  @abc.abstractmethod
  def lease_holder(self, name: str) -> tuple[str, float] | None:
    ...


class CodeStore(abc.ABC):
  """Where patches, reports and the results ledger are published.

  Sanctioned Google dependency: a google3 CitC client, or the shared GitHub
  repository. The core only ever asks it to publish a directory of artifacts
  under a name and to hand back a citable location.
  """

  @abc.abstractmethod
  def publish(self, local_dir: str, remote_path: str, message: str) -> str:
    ...

  @abc.abstractmethod
  def fetch(self, remote_path: str, local_dir: str) -> bool:
    ...

  @abc.abstractmethod
  def describe(self) -> str:
    ...


class Notifier(abc.ABC):
  """How the system reaches a human. Never load-bearing for correctness."""

  @abc.abstractmethod
  def send(self, subject: str, body: str, *, urgency: str = "normal") -> bool:
    ...


class HealthProvider(abc.ABC):
  """Credentials, quota and machine health.

  On a Cloudtop this reads ``gcertstatus`` and cluster quota. Locally it checks
  disk, CPU and the presence of the tools the harness needs. The core only ever
  sees :class:`HealthStatus` values, so a missing LOAS certificate and a full
  disk are handled by the same code path.
  """

  @abc.abstractmethod
  def check(self) -> list[HealthStatus]:
    ...

  def blocking(self) -> list[HealthStatus]:
    return [s for s in self.check() if not s.ok]


@dataclass
class Providers:
  """The bundle handed to everything that needs to touch the outside world."""

  blobs: BlobStore
  code: CodeStore
  notifier: Notifier
  health: HealthProvider
  site: str = "local"
  notes: list[str] = field(default_factory=list)

  def describe(self) -> str:
    return (
        f"site={self.site} blobs={type(self.blobs).__name__} "
        f"code={type(self.code).__name__} notifier={type(self.notifier).__name__} "
        f"health={type(self.health).__name__}"
    )


def summarise_health(statuses: Iterable[HealthStatus]) -> str:
  return "\n".join(str(s) for s in statuses)
