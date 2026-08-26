"""Reading a result and deciding what it means -- including about the idea's author.

The analyst does four things the prior research had to do by hand, and got wrong
often enough to be worth automating:

1. **Chooses the next move from a fixed vocabulary.** Kill, tune, split, scope,
   escalate, promote. Naming the move explicitly is what stops "it almost
   cleared the bar" from turning into six more threshold arms; the record shows
   every threshold move on that project slid along the trade-off, while the one
   *split* moved off it and produced the best result in the corpus.

2. **Computes the marginal ratio against the parent arm**, so a tuning decision
   is made from the measured value of the decisions a variant removed rather
   than from a story about which blocks are unsafe. Two of three such stories in
   the record were wrong.

3. **Runs the exemption analysis** on per-sequence spread, because the clip that
   carries the damage is a design signal -- twice, exempting one clip in eight
   cleared a bar the mean had failed.

4. **Scores the prediction.** Every hypothesis states an expected speedup and
   BD-rate; the analyst grades both and feeds the calibration error back into
   ideation. Without it the generator never learns that its magnitudes are
   routinely 5x optimistic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import (
    Experiment,
    Interval,
    MechanismClass,
    Tier,
    TierResult,
    Verdict,
)
from ..measure.acceptance import exemption_analysis, marginal_ratio
from ..util import log
from .llm import LLMClient

LOG = log.get("agent.analyst")

#: The complete set of moves. A result that does not map to one of these is a
#: result nobody has decided about.
MOVES = (
    "kill",       # the mechanism is wrong or the cost is structurally too high
    "tune",       # bracket a parameter; only when the marginal ratio supports it
    "split",      # find the cheap members of a bundle
    "scope",      # narrow to the content, preset or class where it works
    "escalate",   # it has earned a CTC round
    "promote",    # it cleared CTC on every class; recommend upstream
    "instrument", # measure the decisions before designing the heuristic
    "hold",       # blocked on something external; do not re-queue yet
)

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "move": {"type": "string", "enum": list(MOVES)},
        "reasoning": {"type": "string"},
        "lesson": {"type": "string"},
        "next_hypothesis": {"type": "string"},
        "confidence": {"type": "number"},
        "prediction_error_explanation": {"type": "string"},
    },
    "required": ["move", "reasoning", "lesson"],
    "additionalProperties": False,
}

SYSTEM = """You are the analyst of an autonomous encoder-research system. You are
given one experiment's evidence and you decide what happens to it next.

You are not being asked whether the idea was good. You are being asked which of
a fixed set of moves the *evidence* supports. The moves and when they apply:

  kill        the mechanism is wrong, or the quality cost is structurally too
              high (a ratio at or under half the bar is not tunable), or the
              patch never fired and there is no reason to think it can.
  tune        ONLY when a marginal-ratio calculation says the decisions a
              tighter variant would remove are worth less than the ratio already
              achieved. A threshold move without that calculation slides along
              the trade-off; it does not move off it.
  split       the change bundles several behaviours and the per-sequence or
              per-member evidence suggests the cost is concentrated in a few.
  scope       it works on one class, one preset or one kind of content and the
              evidence says where.
  escalate    local evidence is exhausted and only the real test conditions can
              answer the remaining question. Say what that question is.
  promote     it cleared the bar on every class independently.
  instrument  the honest next step is to measure the decisions rather than guess
              at a better threshold.
  hold        blocked on something outside the experiment.

Be blunt. A result that is inside the noise is not a small win, and saying so is
more useful than finding something encouraging to say about it."""


@dataclass
class Analysis:
  move: str
  reasoning: str
  lesson: str
  next_hypothesis: str = ""
  confidence: float = 0.5
  marginal: dict = field(default_factory=dict)
  exemptions: list[dict] = field(default_factory=list)
  calibration: dict = field(default_factory=dict)
  derived_by: str = "rules"          # rules | model
  warnings: list[str] = field(default_factory=list)


def score_prediction(experiment: Experiment) -> dict:
  """Grade the hypothesis's own forecast against what happened."""
  tier = (
      experiment.tier_result(Tier.T5_CTC)
      or experiment.tier_result(Tier.T3_SCREEN)
      or experiment.tier_result(Tier.T2_COMPLEXITY)
  )
  if not tier or not tier.summary:
    return {}
  predicted_speed = experiment.hypothesis.expected_speedup_pct
  predicted_bd = experiment.hypothesis.expected_bdrate_pct
  actual_speed = -tier.summary.speed_delta.point if tier.summary.speed_delta else 0.0
  bd_interval = tier.summary.bdrate_yuv
  actual_bd = bd_interval.point if bd_interval else 0.0
  out = {
      "tier": tier.tier.value,
      "predicted_speedup_pct": predicted_speed,
      "actual_speedup_pct": actual_speed,
      "predicted_bdrate_pct": predicted_bd,
      "actual_bdrate_pct": actual_bd,
      "speed_direction_correct": (predicted_speed > 0) == (actual_speed > 0),
  }
  if predicted_speed:
    out["speed_magnitude_ratio"] = actual_speed / predicted_speed
  if predicted_bd:
    out["bdrate_magnitude_ratio"] = actual_bd / predicted_bd
  return out


def _parent_numbers(parent: Experiment | None) -> tuple[float, float] | None:
  if not parent:
    return None
  tier = parent.tier_result(Tier.T5_CTC) or parent.tier_result(Tier.T3_SCREEN)
  if not tier or not tier.summary or not tier.summary.speed_delta:
    return None
  bd = tier.summary.bdrate_yuv
  return (-tier.summary.speed_delta.point, bd.point if bd else 0.0)


def rule_based_move(experiment: Experiment, parent: Experiment | None = None) -> Analysis:
  """The move the evidence forces, before any model is asked.

  Most results have exactly one defensible next step and it can be derived
  mechanically. Asking a model first would let it talk itself into "tune" on a
  patch whose ratio is a third of the bar -- which is how a project spends three
  rounds on one heuristic.
  """
  verdict = experiment.verdict
  screen = experiment.tier_result(Tier.T3_SCREEN)
  ctc = experiment.tier_result(Tier.T5_CTC)
  analysis = Analysis(move="hold", reasoning="", lesson="")

  if verdict in (Verdict.BUILD_FAIL, Verdict.CONFORMANCE_FAIL, Verdict.INTEGRITY_FAIL):
    return Analysis(
        move="kill", reasoning=f"terminal failure: {verdict.value}",
        lesson=f"{experiment.hypothesis.title}: {verdict.value} -- "
               f"{(experiment.tiers[-1].reason if experiment.tiers else '')[:200]}",
    )
  if verdict == Verdict.DIVERGES:
    return Analysis(
        move="kill",
        reasoning=(
            "a reuse or kernel change altered the bitstream. That is a defect in "
            "the patch -- a wrong cache key, a stale buffer, a non-equivalent "
            "kernel -- not a quality trade-off to be measured."
        ),
        lesson=(
            f"{experiment.hypothesis.title}: claimed to be "
            f"{experiment.hypothesis.mechanism.value} but changed the bitstream; "
            "the equivalence argument was wrong"
        ),
    )
  if verdict == Verdict.NO_EFFECT and experiment.hypothesis.mechanism == MechanismClass.APPROX:
    inert = any(
        "never fired" in (t.reason or "") for t in experiment.tiers
    )
    if inert:
      return Analysis(
          move="instrument",
          reasoning=(
              "the patch produced an identical bitstream: the code path was "
              "never reached. Measuring it again will produce the same "
              "non-result. Instrument the site to find out under what "
              "conditions it is entered, if ever."
          ),
          lesson=(
              f"{experiment.hypothesis.title}: inert on the screening set -- the "
              "guarding condition is not satisfied by CTC-shaped encodes"
          ),
      )

  if ctc and ctc.passed:
    return Analysis(
        move="promote",
        reasoning=f"cleared the bar on every class independently: {ctc.reason[:300]}",
        lesson=(
            f"{experiment.hypothesis.title}: cleared the bar "
            f"(ratio {ctc.ratio:.1f} vs {ctc.bar})" if ctc.ratio else
            f"{experiment.hypothesis.title}: cleared the bar"
        ),
    )

  measured = ctc or screen
  if measured and measured.ratio is not None and measured.bar:
    ratio, bar = measured.ratio, measured.bar
    if ratio <= 0.5 * bar:
      return Analysis(
          move="kill",
          reasoning=(
              f"ratio {ratio:.1f} against a bar of {bar:.0f}. The gap is too "
              "large to close by tuning: every threshold move on record has slid "
              "along the trade-off rather than off it."
          ),
          lesson=(
              f"{experiment.hypothesis.title}: quality cost structurally too high "
              f"(ratio {ratio:.1f} vs bar {bar:.0f})"
          ),
      )
    parent_numbers = _parent_numbers(parent)
    if parent_numbers and measured.summary and measured.summary.speed_delta:
      bd = measured.summary.bdrate_yuv
      value, explanation = marginal_ratio(
          parent_numbers[0], parent_numbers[1],
          -measured.summary.speed_delta.point, bd.point if bd else 0.0,
      )
      analysis.marginal = {"value": value, "explanation": explanation}
      if value is not None and value < ratio:
        return Analysis(
            move="tune",
            reasoning=f"marginal-ratio analysis supports another arm: {explanation}",
            lesson=f"{experiment.hypothesis.title}: {explanation}",
            marginal=analysis.marginal,
        )
    if measured.summary and measured.summary.per_sequence_bdrate:
      exemptions = exemption_analysis(
          measured.summary.per_sequence_bdrate,
          measured.summary.per_sequence_speed,
          bar=bar,
      )
      analysis.exemptions = exemptions
      if exemptions and exemptions[0]["clears_bar_without"]:
        worst = exemptions[0]
        return Analysis(
            move="scope",
            reasoning=(
                f"one clip carries the damage: without {worst['sequence']} the "
                f"ratio is {worst['ratio_without']:.1f} against a bar of {bar:.0f}, "
                f"and it holds {worst['bdrate_share'] * 100:.0f}% of the BD-rate "
                f"for {worst['speedup_share'] * 100:.0f}% of the speedup. That is "
                "a content gate waiting to be found, not a clip to drop."
            ),
            lesson=(
                f"{experiment.hypothesis.title}: damage concentrated in "
                f"{worst['sequence']}; a content gate should be tried before "
                "any threshold move"
            ),
            exemptions=exemptions,
        )
    return Analysis(
        move="split",
        reasoning=(
            f"ratio {ratio:.1f} against {bar:.0f}: real effect, below the bar, and "
            "no single clip explains the gap. Look for the cheap members of the "
            "behaviour this change bundles."
        ),
        lesson=f"{experiment.hypothesis.title}: below bar at ratio {ratio:.1f}",
        exemptions=analysis.exemptions,
    )

  if screen and screen.passed and not ctc:
    return Analysis(
        move="escalate",
        reasoning=(
            "local tiers are exhausted: the speedup is resolved and the local "
            "screen cannot answer the quality question at CTC scale."
        ),
        lesson="",
    )
  if verdict == Verdict.NO_EFFECT:
    return Analysis(
        move="kill",
        reasoning="no effect was resolved on either axis at the power available",
        lesson=f"{experiment.hypothesis.title}: no measurable effect",
    )
  return analysis


class Analyst:

  def __init__(self, client: LLMClient | None = None):
    self.client = client

  def analyse(
      self,
      experiment: Experiment,
      *,
      parent: Experiment | None = None,
      use_model: bool = True,
  ) -> Analysis:
    analysis = rule_based_move(experiment, parent)
    analysis.calibration = score_prediction(experiment)
    if analysis.calibration.get("speed_direction_correct") is False:
      analysis.warnings.append(
          "the hypothesis predicted the wrong direction on speed; the mechanism "
          "story behind it is suspect even if the numbers are fine"
      )
    ratio = analysis.calibration.get("speed_magnitude_ratio")
    if ratio is not None and (ratio > 3.0 or 0 < ratio < 0.33):
      analysis.warnings.append(
          f"realised speedup is {ratio:.1f}x the prediction; ideation's magnitude "
          "calibration for this lens needs adjusting"
      )

    if not self.client or not use_model:
      return analysis

    prompt = _evidence_block(experiment, analysis, parent)
    data, response = self.client.json(
        system=SYSTEM, prompt=prompt, schema=ANALYSIS_SCHEMA, max_tokens=6000
    )
    if not response.ok or not data:
      return analysis

    proposed = data.get("move", analysis.move)
    # The model may explain, refine and add a lesson. It may not overturn a move
    # that follows from terminal evidence -- a kill on a build failure or a
    # divergence, or a promote on a result that cleared every class bar. Both are
    # facts; a model asked to reconsider a fact will find a reason to.
    if analysis.move in ("kill", "promote") and proposed != analysis.move:
      analysis.warnings.append(
          f"the model proposed '{proposed}' over a mechanically derived "
          f"'{analysis.move}'; kept the derived move and recorded the disagreement"
      )
    elif proposed in MOVES:
      analysis.move = proposed
    analysis.reasoning = (data.get("reasoning") or analysis.reasoning).strip()
    analysis.lesson = (data.get("lesson") or analysis.lesson).strip()
    analysis.next_hypothesis = (data.get("next_hypothesis") or "").strip()
    analysis.confidence = float(data.get("confidence", analysis.confidence))
    analysis.derived_by = "model"
    return analysis


def _evidence_block(experiment: Experiment, analysis: Analysis, parent: Experiment | None) -> str:
  lines = [
      f"EXPERIMENT {experiment.id}: {experiment.hypothesis.title}",
      f"lens={experiment.hypothesis.lens} mechanism={experiment.hypothesis.mechanism.value} "
      f"subsystem={experiment.hypothesis.subsystem} presets={experiment.hypothesis.target_presets}",
      f"claim: {experiment.hypothesis.statement}",
      f"predicted: {experiment.hypothesis.expected_speedup_pct:+.2f}% speed, "
      f"{experiment.hypothesis.expected_bdrate_pct:+.2f}% BD-rate",
      f"kill criteria stated up front: {'; '.join(experiment.hypothesis.kill_criteria)}",
      "",
      "TIER RESULTS:",
  ]
  for tier in experiment.tiers:
    lines.append(
        f"  {tier.tier.value:16s} {'PASS' if tier.passed else 'FAIL'} "
        f"{tier.verdict.value:18s} {tier.reason[:400]}"
    )
    if tier.summary:
      if tier.summary.speed_delta:
        lines.append(
            f"      speed ({tier.summary.metric_for_speed}): {tier.summary.speed_delta}"
        )
      for metric, interval in sorted(tier.summary.bdrate_by_metric.items()):
        lines.append(f"      {metric}: {interval}")
      if tier.summary.per_sequence_bdrate:
        worst = sorted(
            tier.summary.per_sequence_bdrate.items(), key=lambda kv: -kv[1]
        )[:5]
        lines.append(
            "      worst clips: "
            + ", ".join(f"{name} {value:+.2f}%" for name, value in worst)
        )
      for note in tier.summary.notes[:4]:
        lines.append(f"      note: {note}")
  if parent:
    lines.append(f"\nPARENT ARM {parent.id}: {parent.hypothesis.title}")
  if analysis.marginal:
    lines.append(f"\nMARGINAL RATIO: {analysis.marginal.get('explanation')}")
  if analysis.exemptions:
    lines.append("\nEXEMPTION ANALYSIS (ratio if this clip were excluded):")
    for row in analysis.exemptions[:4]:
      ratio = row["ratio_without"]
      lines.append(
          f"  without {row['sequence']}: ratio "
          + (f"{ratio:.1f}" if ratio is not None else "n/a")
          + f" (carries {row['bdrate_share'] * 100:.0f}% of BD-rate, "
          f"{row['speedup_share'] * 100:.0f}% of speedup)"
      )
  if analysis.calibration:
    lines.append(f"\nPREDICTION SCORE: {analysis.calibration}")
  lines.append(
      f"\nThe mechanical rules already derived move='{analysis.move}' because: "
      f"{analysis.reasoning or '(no rule fired)'}"
  )
  lines.append(
      "Confirm or refine that move, give the reasoning a human should read, and "
      "write one durable lesson that would change how a future proposal in this "
      "area is made. If you disagree with a 'kill', say so -- it will be recorded "
      "but not acted on."
  )
  return "\n".join(lines)
