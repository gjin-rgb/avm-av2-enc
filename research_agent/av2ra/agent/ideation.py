"""Generating hypotheses that are grounded, novel, and falsifiable.

Three filters sit between the model and the experiment queue, and each one
exists because of a specific failure in the record:

**Grounding.** Every symbol the model names is checked against the code map. A
name that does not exist is repaired from the nearest match if one is close
enough, and the proposal is rejected otherwise. Two prior experiments were
authored against functions that had been renamed or moved upstream, and one
consumed a cluster arm before anyone noticed it was a no-op.

**Novelty.** Every proposal is checked structurally against the prior corpus --
by touched functions and files, not by wording -- and against this system's own
registry. Re-deriving a dead idea in different words is the default behaviour of
a language model asked the same question twice.

**Falsifiability.** A hypothesis must state what it expects to buy and what
result would retire it rather than tune it. The prior work found that writing the
falsifier before the run cost one arm instead of a round of tuning something
that never fired; a proposal without one is sent back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..core.models import Hypothesis, MechanismClass
from ..knowledge import retrieval
from ..knowledge.codemap import CodeMap
from ..knowledge.corpus import Corpus
from ..knowledge.profiles import Profile
from ..util import log
from .lenses import Lens, LensStats, available, missing_evidence
from .llm import LLMClient

LOG = log.get("agent.ideation")

HYPOTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subsystem": {"type": "string"},
        "mechanism": {
            "type": "string",
            "enum": ["approx", "reuse", "kernel", "dispatch", "structural", "rdcost"],
        },
        "statement": {"type": "string"},
        "rationale": {"type": "string"},
        "target_functions": {"type": "array", "items": {"type": "string"}},
        "target_files": {"type": "array", "items": {"type": "string"}},
        "target_presets": {"type": "array", "items": {"type": "integer"}},
        "expected_speedup_pct": {"type": "number"},
        "expected_bdrate_pct": {"type": "number"},
        "amdahl_ceiling_pct": {"type": "number"},
        "confidence": {"type": "number"},
        "risk_notes": {"type": "array", "items": {"type": "string"}},
        "kill_criteria": {"type": "array", "items": {"type": "string"}},
        "parameters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "define": {"type": "string"},
                    "default": {"type": "string"},
                    "sweep": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["define", "default"],
                "additionalProperties": False,
            },
        },
        "why_not_already_done": {"type": "string"},
    },
    "required": [
        "title", "subsystem", "mechanism", "statement", "rationale",
        "target_functions", "target_presets", "expected_speedup_pct",
        "expected_bdrate_pct", "confidence", "kill_criteria",
    ],
    "additionalProperties": False,
}


@dataclass
class Proposal:
  hypothesis: Hypothesis
  lens: Lens
  raw: dict
  novelty: dict = field(default_factory=dict)
  rejected: str = ""
  repairs: list[str] = field(default_factory=list)

  @property
  def ok(self) -> bool:
    return not self.rejected


@dataclass
class IdeationInputs:
  codemap: CodeMap | None = None
  profile: Profile | None = None
  corpus: Corpus | None = None
  results_digest: str = ""
  lesson_store: object | None = None
  base_sha: str = ""

  def keys(self) -> set[str]:
    keys = set()
    if self.codemap:
      keys.add("codemap")
    if self.profile:
      keys.add("profile")
    if self.corpus:
      keys.add("corpus")
    if self.results_digest:
      keys.add("results")
    return keys


class Ideator:

  def __init__(self, client: LLMClient, *, stats: LensStats | None = None,
               min_confidence: float = 0.15):
    self.client = client
    self.stats = stats or LensStats()
    self.min_confidence = min_confidence

  # -- prompting -----------------------------------------------------------

  def _prompt(self, lens: Lens, inputs: IdeationInputs, *, focus: str = "",
              avoid: list[str] | None = None) -> str:
    parts = [
        f"LENS: {lens.name}",
        f"QUESTION: {lens.question}",
        f"WHY THIS LENS EXISTS: {lens.rationale}",
        f"HOW TO ANSWER IT: {lens.guidance}",
        "",
        "Propose exactly one hypothesis. It must:",
        "  - name only functions that appear in the code map above;",
        "  - state the mechanism class honestly (a reuse or kernel change must "
        "produce an identical bitstream; if yours might not, it is 'approx');",
        "  - predict the speedup and BD-rate cost as numbers, because you will "
        "be scored on the prediction as well as the result;",
        "  - give kill criteria: the specific result that retires the idea "
        "instead of prompting another round of tuning;",
        "  - answer 'why has nobody done this already', using the prior-attempt "
        "list. If someone has, say so and propose something else.",
    ]
    if lens.mechanism_hint:
      parts.append(f"  - the expected mechanism for this lens is '{lens.mechanism_hint}'.")
    if focus:
      parts.append(f"\nFOCUS REQUESTED BY THE PLANNER: {focus}")
    if inputs.results_digest:
      parts.append("\nRECENT RESULTS FROM THIS SYSTEM:\n" + inputs.results_digest)
    if avoid:
      parts.append(
          "\nDO NOT PROPOSE ANY OF THESE (already queued or recently tried):\n  "
          + "\n  ".join(avoid[:20])
      )
    return "\n".join(parts)

  # -- generation ----------------------------------------------------------

  def propose(
      self,
      lens: Lens,
      inputs: IdeationInputs,
      *,
      experiment_id: str,
      focus: str = "",
      avoid: list[str] | None = None,
  ) -> Proposal:
    gaps = missing_evidence(lens, inputs.keys())
    if gaps:
      return Proposal(
          hypothesis=_empty_hypothesis(experiment_id, lens),
          lens=lens, raw={},
          rejected=(
              f"lens '{lens.name}' requires {', '.join(gaps)}, which this run does "
              "not have. Generating it anyway would produce a generic idea "
              "wearing the lens's name."
          ),
      )

    pack = retrieval.build_pack(
        prompt=self._prompt(lens, inputs, focus=focus, avoid=avoid),
        codemap=inputs.codemap, profile=inputs.profile, corpus=inputs.corpus,
        store=inputs.lesson_store, stage="ideation",
    )
    data, response = self.client.json(
        system=pack.system, prompt=pack.prompt, schema=HYPOTHESIS_SCHEMA,
        max_tokens=8000,
    )
    if not response.ok or not data:
      return Proposal(
          hypothesis=_empty_hypothesis(experiment_id, lens), lens=lens, raw={},
          rejected=f"model call failed: {response.error or 'no structured output'}",
      )
    self.stats.record(lens.name, stage="proposed")
    return self._validate(data, lens, inputs, experiment_id)

  def _validate(
      self, data: dict, lens: Lens, inputs: IdeationInputs, experiment_id: str
  ) -> Proposal:
    repairs: list[str] = []
    functions = [f.strip() for f in data.get("target_functions", []) if f.strip()]

    if inputs.codemap:
      found, missing = inputs.codemap.resolve(functions)
      for name in missing:
        suggestions = inputs.codemap.suggest(name)
        if suggestions:
          repairs.append(f"{name} -> {suggestions[0]}")
          found.append(suggestions[0])
        else:
          repairs.append(f"{name} -> (no match; dropped)")
      functions = found
      if not functions:
        return Proposal(
            hypothesis=_empty_hypothesis(experiment_id, lens), lens=lens, raw=data,
            repairs=repairs,
            rejected=(
                "none of the proposed target functions exist in this tree "
                f"({', '.join(data.get('target_functions', [])) or 'none named'}). "
                "An idea grounded in symbols that are not there cannot be "
                "implemented and would waste an experiment."
            ),
        )
      files = sorted({
          inputs.codemap.function(name).path
          for name in functions
          if inputs.codemap.function(name)
      })
    else:
      files = [f.strip() for f in data.get("target_files", []) if f.strip()]

    hypothesis = Hypothesis(
        id=experiment_id,
        title=data.get("title", "").strip()[:140] or f"{lens.name} proposal",
        subsystem=data.get("subsystem", "unknown").strip().lower(),
        mechanism=_mechanism(data.get("mechanism", lens.mechanism_hint)),
        lens=lens.name,
        statement=data.get("statement", "").strip(),
        rationale=data.get("rationale", "").strip(),
        target_presets=[int(p) for p in data.get("target_presets", [2]) if isinstance(p, int)] or [2],
        target_functions=functions,
        target_files=files,
        expected_speedup_pct=float(data.get("expected_speedup_pct", 0.0)),
        expected_bdrate_pct=float(data.get("expected_bdrate_pct", 0.0)),
        confidence=max(0.0, min(1.0, float(data.get("confidence", 0.5)))),
        risk_notes=[str(n) for n in data.get("risk_notes", [])][:8],
        kill_criteria=[str(n) for n in data.get("kill_criteria", [])][:8],
        parameters={
            item["define"]: item.get("default", "")
            for item in data.get("parameters", [])
            if isinstance(item, dict) and item.get("define")
        },
    )
    proposal = Proposal(hypothesis=hypothesis, lens=lens, raw=data, repairs=repairs)

    if not hypothesis.kill_criteria:
      proposal.rejected = (
          "no kill criteria: an idea that cannot be retired by a result will be "
          "tuned forever. State what outcome ends it."
      )
      return proposal
    if hypothesis.confidence < self.min_confidence:
      proposal.rejected = (
          f"the generator's own confidence is {hypothesis.confidence:.2f}, below "
          f"the {self.min_confidence:.2f} floor for spending compute"
      )
      return proposal

    # Amdahl: reject before implementation, not after measurement.
    if inputs.profile and hypothesis.expected_speedup_pct > 0:
      share = inputs.profile.share_of(functions)
      subsystem_share = inputs.profile.share_of_subsystem(hypothesis.subsystem)
      ceiling = max(share, subsystem_share)
      if ceiling > 0 and hypothesis.expected_speedup_pct > ceiling * 1.15:
        proposal.rejected = (
            f"claims {hypothesis.expected_speedup_pct:.2f}% whole-encode speedup "
            f"from code holding {ceiling:.2f}% of the profile. Removing it "
            "entirely could not buy that."
        )
        return proposal
      proposal.novelty["profile_share_pct"] = ceiling

    if inputs.corpus:
      overlapping = inputs.corpus.touching(functions, files)
      proposal.novelty["overlapping_prior"] = [a.id for a in overlapping[:6]]
      dead = [
          a for a in overlapping
          if a.failed and set(a.touched_functions) & set(functions)
      ]
      if dead:
        proposal.novelty["prior_failures_on_same_functions"] = [
            {"id": a.id, "status": a.status, "notes": a.notes[:200]} for a in dead[:4]
        ]
        # Not an automatic rejection: the corpus itself shows one CCSO idea that
        # failed on tolerance and later succeeded on ordering. But the model has
        # to have accounted for it.
        justification = (data.get("why_not_already_done") or "").lower()
        if not any(
            token in justification
            for token in ("differ", "instead", "unlike", "previous", "prior", "failed")
        ):
          proposal.rejected = (
              "overlaps prior failed attempts on the same functions ("
              + ", ".join(a.id for a in dead[:3])
              + ") without explaining what is different this time"
          )
    return proposal


def _mechanism(value: str | None) -> MechanismClass:
  try:
    return MechanismClass(str(value or "").strip().lower())
  except ValueError:
    return MechanismClass.UNKNOWN


def _empty_hypothesis(experiment_id: str, lens: Lens) -> Hypothesis:
  return Hypothesis(
      id=experiment_id, title="(rejected proposal)", subsystem="unknown",
      mechanism=MechanismClass.UNKNOWN, lens=lens.name, statement="", rationale="",
  )


def choose_lenses(
    inputs: IdeationInputs, stats: LensStats, *, count: int, exclude: set[str] | None = None
) -> list[Lens]:
  """Pick lenses by expected value, keeping the portfolio diverse.

  Thompson-style sampling would be defensible; a deterministic score is chosen
  instead because a research run must be reproducible from its seed, and because
  the number of lenses is small enough that explicit round-robin over the top
  candidates gives most of the diversity benefit without the variance.
  """
  usable = [l for l in available(inputs.keys()) if l.name not in (exclude or set())]
  usable.sort(key=lambda l: -stats.rate(l.name))
  chosen: list[Lens] = []
  seen_mechanisms: set[str] = set()
  for lens in usable:
    if len(chosen) >= count:
      break
    # Prefer a new mechanism class before repeating one: a portfolio of six
    # pruning heuristics is one idea measured six times.
    if lens.mechanism_hint in seen_mechanisms and len(usable) > count:
      continue
    chosen.append(lens)
    seen_mechanisms.add(lens.mechanism_hint)
  for lens in usable:
    if len(chosen) >= count:
      break
    if lens not in chosen:
      chosen.append(lens)
  return chosen
