"""Logging that is readable by a human and parseable by the next agent run.

Two sinks, always: a human-facing console stream, and a structured JSONL event
log per node. The JSONL log is what the digest, the dashboard and post-mortem
analysis read, so events carry an explicit ``kind`` rather than free text.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

from .io import append_jsonl, ensure_dir

_EVENT_LOG_PATH: str | None = None
_CONFIGURED = False


def configure(workspace: str, *, node_id: str = "local", verbose: bool = False) -> None:
  global _EVENT_LOG_PATH, _CONFIGURED
  log_dir = ensure_dir(os.path.join(workspace, "logs"))
  _EVENT_LOG_PATH = os.path.join(log_dir, f"events-{node_id}.jsonl")
  if _CONFIGURED:
    return
  handler = logging.StreamHandler(sys.stderr)
  handler.setFormatter(
      logging.Formatter("%(asctime)s %(levelname)-5s %(name)-18s %(message)s",
                        datefmt="%H:%M:%S")
  )
  root = logging.getLogger("av2ra")
  root.setLevel(logging.DEBUG if verbose else logging.INFO)
  root.handlers = [handler]
  root.propagate = False
  file_handler = logging.FileHandler(os.path.join(log_dir, f"av2ra-{node_id}.log"))
  file_handler.setFormatter(
      logging.Formatter("%(asctime)s %(levelname)-5s %(name)s %(message)s")
  )
  root.addHandler(file_handler)
  _CONFIGURED = True


def get(name: str) -> logging.Logger:
  return logging.getLogger(f"av2ra.{name}")


def event(kind: str, **fields: Any) -> None:
  """Record a structured event. Never raises -- logging must not kill a run."""
  record = {"ts": time.time(), "kind": kind}
  record.update(fields)
  if _EVENT_LOG_PATH:
    try:
      append_jsonl(_EVENT_LOG_PATH, record)
    except OSError:
      pass
