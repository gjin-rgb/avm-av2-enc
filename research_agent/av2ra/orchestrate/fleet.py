"""Coordinating several machines without a server.

The only shared primitives are the blob store and a TTL lease, which is enough
because the work is naturally partitioned: an experiment belongs to whichever
node holds its lease, and a node that dies loses its lease when the TTL expires
rather than stranding the queue.

Two things are deliberately *not* here. There is no scheduler assigning work to
nodes -- each node pulls what it can lease, which means adding a machine is a
matter of running the agent on it and nothing else. And there is no central
mutable state beyond the registry: heartbeats are per-node files, so two nodes
writing at once cannot corrupt one another's status.

Fleet status is derived from heartbeats, and a heartbeat older than its interval
is reported as stale rather than silently believed. A dashboard that shows a
node as RUNNING three hours after it crashed is worse than one that shows
nothing.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import asdict, dataclass, field

from ..providers.base import BlobStore
from ..util import log

LOG = log.get("orchestrate.fleet")

HEARTBEAT_INTERVAL_S = 30.0
LEASE_TTL_S = 3600.0


@dataclass
class NodeState:
  node_id: str
  hostname: str = ""
  status: str = "idle"              # idle | proposing | building | screening | awaiting_ctc | halted
  experiment_id: str = ""
  subsystem: str = ""
  preset: int = 0
  background_ctc: list[str] = field(default_factory=list)
  cpus: int = 0
  workers: int = 0
  last_tick: float = 0.0
  updated_ts: float = field(default_factory=time.time)
  base_sha: str = ""
  notes: list[str] = field(default_factory=list)
  version: str = ""

  @property
  def age_s(self) -> float:
    return max(0.0, time.time() - self.updated_ts)

  @property
  def stale(self) -> bool:
    return self.age_s > HEARTBEAT_INTERVAL_S * 4


class Fleet:

  def __init__(self, blobs: BlobStore, *, prefix: str = "fleet"):
    self.blobs = blobs
    self.prefix = prefix.strip("/")

  # -- heartbeats ----------------------------------------------------------

  def _key(self, node_id: str) -> str:
    return f"{self.prefix}/nodes/{node_id}.json"

  def heartbeat(self, state: NodeState) -> None:
    state.updated_ts = time.time()
    state.hostname = state.hostname or socket.gethostname()
    try:
      self.blobs.put(self._key(state.node_id), json.dumps(asdict(state)).encode())
    except Exception as exc:  # a failed heartbeat must never stop research
      LOG.warning("heartbeat failed for %s: %s", state.node_id, exc)

  def nodes(self) -> list[NodeState]:
    out = []
    for key in self.blobs.list(f"{self.prefix}/nodes"):
      if not key.endswith(".json"):
        continue
      raw = self.blobs.get(key)
      if not raw:
        continue
      try:
        payload = json.loads(raw.decode())
      except (ValueError, UnicodeDecodeError):
        continue
      out.append(NodeState(**{k: v for k, v in payload.items() if k in NodeState.__annotations__}))
    return sorted(out, key=lambda n: n.node_id)

  # -- leases --------------------------------------------------------------

  def claim(self, name: str, node_id: str, *, ttl_s: float = LEASE_TTL_S) -> bool:
    return self.blobs.acquire_lease(f"{self.prefix}/{name}", node_id, ttl_s)

  def release(self, name: str, node_id: str) -> None:
    self.blobs.release_lease(f"{self.prefix}/{name}", node_id)

  def holder(self, name: str) -> tuple[str, float] | None:
    return self.blobs.lease_holder(f"{self.prefix}/{name}")

  # -- steering ------------------------------------------------------------

  def steer(self, node_id: str, action: str, reason: str) -> None:
    """Leave an instruction a node will pick up on its next tick.

    A file rather than a signal: the node may be mid-encode, and an instruction
    that survives a restart is the only kind worth giving an unattended system.
    """
    self.blobs.put(
        f"{self.prefix}/steer/{node_id}.json",
        json.dumps({"action": action, "reason": reason, "ts": time.time()}).encode(),
    )

  def pending_steer(self, node_id: str) -> dict | None:
    key = f"{self.prefix}/steer/{node_id}.json"
    raw = self.blobs.get(key)
    if not raw:
      return None
    try:
      instruction = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
      return None
    self.blobs.delete(key)
    return instruction

  # -- presentation --------------------------------------------------------

  def render_status(self, *, base_sha: str = "", extra: list[str] | None = None) -> str:
    nodes = self.nodes()
    lines = [
        "=" * 108,
        "AV2 RESEARCH FLEET",
        "=" * 108,
        f"anchor: {base_sha[:12] or 'unknown'}"
        + (f"   |   {'   |   '.join(extra)}" if extra else ""),
        "-" * 108,
        f"{'NODE':<12}{'STATUS':<16}{'EXPERIMENT':<12}{'SUBSYSTEM':<14}"
        f"{'PRESET':<8}{'BACKGROUND CTC':<22}{'HEARTBEAT'}",
        "-" * 108,
    ]
    if not nodes:
      lines.append("(no nodes have reported yet)")
    for node in nodes:
      age = f"{node.age_s:.0f}s ago" + (" [STALE]" if node.stale else "")
      lines.append(
          f"{node.node_id:<12}{node.status:<16}{node.experiment_id or '-':<12}"
          f"{node.subsystem or '-':<14}{node.preset or '-':<8}"
          f"{','.join(node.background_ctc) or '-':<22}{age}"
      )
    lines.append("-" * 108)
    return "\n".join(lines)
