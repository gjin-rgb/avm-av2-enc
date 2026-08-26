"""The CTC job contract: what the core asks for, and what it gets back.

This is the third and last sanctioned infrastructure touchpoint. The core hands
over a :class:`~av2ra.core.models.CtcRequest` -- an experiment id, a base SHA, a
way to obtain the patch, the presets, test sets and configurations to run, and
*the question the run is meant to answer* -- and receives a
:class:`~av2ra.core.models.CtcResult`. Nothing in that exchange names a cluster,
a scheduler, or a shell script.

The ``question`` field is not documentation. Policy requires it to be non-empty
and requires the planner to record what cheaper evidence has already been
exhausted, because the failure mode this whole design exists to prevent is
spending the scarcest resource on something a five-minute local check could
have settled. The prior effort spent an entire round on ten patches of which
eight were already dead on evidence it had.

Back ends implement four operations. ``submit`` returns a handle;
``poll`` reports progress and may return partial results; ``collect``
finalises; ``cancel`` releases quota. Partial results are first class because a
run that is 40% complete and already showing no speedup should be killed rather
than finished -- cluster quota saved that way is quota available for the next
question.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from ..core.models import CtcRequest, CtcResult


@dataclass
class CtcCapacity:
  """What the back end can currently accept. Drives the planner's batching."""

  available_slots: float = 1.0
  typical_latency_s: float = 3600.0
  max_arms_per_round: int = 8
  supports_partial: bool = False
  supports_anchor_reuse: bool = False
  notes: list[str] = field(default_factory=list)


class CtcBackend(abc.ABC):
  """Runs common-test-conditions benchmarks. One implementation per site."""

  name: str = "abstract"

  @abc.abstractmethod
  def submit(self, request: CtcRequest) -> CtcResult:
    """Start a run. Must return promptly with a handle, never block to completion.

    Non-blocking submission is what allows the agent to keep working: the state
    machine launches a run and immediately moves to the next hypothesis in a
    fresh worktree. A back end that blocks turns a fleet into a queue of one.
    """

  @abc.abstractmethod
  def poll(self, result: CtcResult, *, want_partial: bool = True) -> CtcResult:
    ...

  @abc.abstractmethod
  def collect(self, result: CtcResult) -> CtcResult:
    ...

  def cancel(self, result: CtcResult, reason: str) -> bool:
    return False

  def capacity(self) -> CtcCapacity:
    return CtcCapacity()

  def validate(self, request: CtcRequest) -> list[str]:
    """Problems that would waste a round. Checked before anything is spent."""
    problems = []
    if not request.question.strip():
      problems.append(
          "no question recorded: every CTC run must state what it decides and "
          "why nothing cheaper could decide it"
      )
    if not request.presets:
      problems.append("no preset selected")
    if not request.testsets:
      problems.append("no test set selected")
    if not request.base_sha:
      problems.append("no base SHA: a result that is not scoped to an anchor is not a result")
    return problems


#: Per-class frame counts actually used for reduced-length CTC runs on this
#: codebase: 4K classes run 17 frames, 1080p classes run 33. A single scalar
#: frame count -- as the draft protocol specifies -- cannot express this, and
#: submitting 33 frames for A1 silently doubles the cost of the 4K half of every
#: round.
DEFAULT_FRAMES_BY_TESTSET = {
    "a1": 17, "a2": 33, "a3": 33, "a4": 33, "a5": 33, "b1": 33, "b2": 33,
}


def frames_for(testset: str, override: int | None = None) -> int:
  if override:
    return override
  return DEFAULT_FRAMES_BY_TESTSET.get(testset.lower(), 33)
