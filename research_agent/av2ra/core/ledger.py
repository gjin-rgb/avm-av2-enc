"""The decision log: why something was killed or kept, in the agent's own words.

The registry says *where* every experiment stands. This says *why*, and it is
the artifact that stops the next run -- or the next engineer -- from spending a
CTC slot rediscovering a dead end. It is append-only and dated, mirroring the
``DECISIONS.md`` convention the prior research settled on after losing a round
to an undocumented kill.

Two forms are written for every entry:

* ``decisions.jsonl`` -- structured, for the planner's novelty check and for the
  ideation prompt's "what has already been ruled out" section.
* ``DECISIONS.md`` -- newest first, for a human reading the repository.

Entries are never edited. A conclusion that later turns out to be wrong gets a
``retraction`` entry pointing at it, because the corpus contains a real case of
a rule derived from three arms that never fired, carried forward for two rounds
before anyone noticed. A memory that quietly rewrites itself cannot be audited.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ..util.io import append_jsonl, ensure_dir, read_jsonl, write_text_atomic


@dataclass
class Decision:
  experiment_id: str
  action: str                 # killed | kept | tuned | promoted | retracted | noted
  summary: str
  reasoning: str = ""
  evidence: dict = field(default_factory=dict)
  lessons: list[str] = field(default_factory=list)
  base_sha: str = ""
  retracts: str = ""          # the decision id this overturns
  author: str = "av2ra"
  ts: float = field(default_factory=time.time)

  @property
  def decision_id(self) -> str:
    return f"{time.strftime('%Y%m%d', time.localtime(self.ts))}-{self.experiment_id}-{self.action}"

  def to_dict(self) -> dict:
    return {
        "decision_id": self.decision_id,
        "experiment_id": self.experiment_id,
        "action": self.action,
        "summary": self.summary,
        "reasoning": self.reasoning,
        "evidence": self.evidence,
        "lessons": self.lessons,
        "base_sha": self.base_sha,
        "retracts": self.retracts,
        "author": self.author,
        "ts": self.ts,
    }


class Ledger:

  def __init__(self, root: str):
    self.root = ensure_dir(root)
    self.jsonl_path = os.path.join(root, "decisions.jsonl")
    self.markdown_path = os.path.join(root, "DECISIONS.md")

  def append(self, decision: Decision) -> str:
    append_jsonl(self.jsonl_path, decision.to_dict())
    self._render_markdown()
    return decision.decision_id

  def entries(self) -> list[dict]:
    return list(read_jsonl(self.jsonl_path))

  def for_experiment(self, experiment_id: str) -> list[dict]:
    return [e for e in self.entries() if e.get("experiment_id") == experiment_id]

  def lessons(self, *, limit: int = 100) -> list[str]:
    """Distinct lessons, newest first. This is what ideation is shown."""
    seen, out = set(), []
    for entry in reversed(self.entries()):
      for lesson in entry.get("lessons") or []:
        if lesson not in seen:
          seen.add(lesson)
          out.append(lesson)
      if len(out) >= limit:
        break
    return out

  def retracted_ids(self) -> set[str]:
    return {e["retracts"] for e in self.entries() if e.get("retracts")}

  def _render_markdown(self) -> None:
    entries = self.entries()
    retracted = self.retracted_ids()
    lines = [
        "# Decision log",
        "",
        "Append-only, newest first. Every entry says what changed state and why.",
        "A conclusion that was later overturned keeps its entry and gains a",
        "**RETRACTED** marker -- a memory that edits itself cannot be audited.",
        "",
    ]
    for entry in reversed(entries):
      stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.get("ts", 0)))
      marker = " **[RETRACTED]**" if entry.get("decision_id") in retracted else ""
      lines.append(
          f"## {stamp} - {entry.get('experiment_id')} - "
          f"{entry.get('action', '').upper()}{marker}"
      )
      lines.append("")
      lines.append(entry.get("summary", ""))
      if entry.get("retracts"):
        lines.append("")
        lines.append(f"> Retracts `{entry['retracts']}`.")
      if entry.get("reasoning"):
        lines.append("")
        lines.append(entry["reasoning"])
      if entry.get("evidence"):
        lines.append("")
        lines.append("```")
        for key, value in sorted(entry["evidence"].items()):
          lines.append(f"{key}: {value}")
        lines.append("```")
      if entry.get("lessons"):
        lines.append("")
        lines.append("**Lessons**")
        for lesson in entry["lessons"]:
          lines.append(f"- {lesson}")
      if entry.get("base_sha"):
        lines.append("")
        lines.append(f"_measured against base `{entry['base_sha'][:12]}`_")
      lines.append("")
      lines.append("---")
      lines.append("")
    write_text_atomic(self.markdown_path, "\n".join(lines))
