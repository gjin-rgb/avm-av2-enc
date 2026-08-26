"""The experiment report: the artifact a human actually reads.

Written to be readable by someone who was not there, months later, and to make
the two failure modes of an autonomous system visible rather than buried:

* A number without its conditions. Every table here carries the anchor SHA, the
  clip set, the repetition count and the metric definition.
* A verdict without its reasoning. The report states what the evidence forced,
  what the model added, and where the two disagreed.

The structure deliberately mirrors the reports the prior human-plus-model effort
converged on -- hypothesis, patch, tier ladder, per-sequence spread, decision --
because that structure was itself a research result: it is the shape that made
stale numbers and hidden per-clip regressions visible.
"""

from __future__ import annotations

import os
import time

from ..core.models import Experiment, Tier
from ..util.io import ensure_dir, write_text_atomic


def render(experiment: Experiment, *, include_patch: bool = True) -> str:
  hypothesis = experiment.hypothesis
  lines: list[str] = [
      f"# {experiment.id} - {hypothesis.title}",
      "",
      f"**Verdict:** `{experiment.verdict.value}` &nbsp;&nbsp; "
      f"**Quadrant:** `{experiment.quadrant.value}` &nbsp;&nbsp; "
      f"**Status:** `{experiment.status.value}`",
      "",
      f"- **Anchor:** `{experiment.base_sha[:12] or 'unknown'}` "
      "(every number below is scoped to this commit and is not automatically "
      "valid against any other)",
      f"- **Lens:** `{hypothesis.lens}` &nbsp; **Mechanism:** `{hypothesis.mechanism.value}` "
      f"&nbsp; **Subsystem:** `{hypothesis.subsystem}`",
      f"- **Target presets:** {hypothesis.target_presets}",
      f"- **Build fingerprint:** `{experiment.build_fingerprint or 'n/a'}`",
      f"- **Node:** `{experiment.node_id or 'n/a'}` &nbsp; "
      f"**Created:** {_stamp(experiment.created_ts)} &nbsp; "
      f"**Updated:** {_stamp(experiment.updated_ts)}",
      "",
      "## Hypothesis",
      "",
      hypothesis.statement or "_(none recorded)_",
      "",
      "### Why it should work",
      "",
      hypothesis.rationale or "_(none recorded)_",
      "",
      "### Prediction made before measuring",
      "",
      f"- speedup: **{hypothesis.expected_speedup_pct:+.2f}%**",
      f"- BD-rate: **{hypothesis.expected_bdrate_pct:+.2f}%**",
      f"- generator confidence: {hypothesis.confidence:.2f}",
      "",
  ]
  if hypothesis.kill_criteria:
    lines += ["### Kill criteria stated up front", ""]
    lines += [f"- {item}" for item in hypothesis.kill_criteria]
    lines.append("")
  if hypothesis.risk_notes:
    lines += ["### Risks noted up front", ""]
    lines += [f"- {item}" for item in hypothesis.risk_notes]
    lines.append("")

  lines += ["## Patch", "",
            f"- files: {', '.join(f'`{f}`' for f in experiment.patch.files) or 'none'}",
            f"- size: +{experiment.patch.added_lines} / -{experiment.patch.removed_lines} lines",
            f"- functions touched: {', '.join(f'`{f}`' for f in experiment.patch.touched_functions) or 'n/a'}",
            f"- normalised hash: `{experiment.patch.normalized_hash or 'n/a'}`"]
  if experiment.patch.build_defines:
    lines.append(
        "- tunable defines: "
        + ", ".join(f"`-D{k}={v}`" for k, v in sorted(experiment.patch.build_defines.items()))
    )
  lines.append("")

  lines += ["## Evidence ladder", "",
            "| tier | result | verdict | ratio | summary |",
            "| --- | --- | --- | --- | --- |"]
  for tier in experiment.tiers:
    ratio = f"{tier.ratio:.1f}" if tier.ratio is not None else "-"
    lines.append(
        f"| `{tier.tier.value}` | {'PASS' if tier.passed else '**FAIL**'} | "
        f"`{tier.verdict.value}` | {ratio} | {_one_line(tier.reason)} |"
    )
  lines.append("")

  for tier in experiment.tiers:
    if not tier.summary:
      continue
    summary = tier.summary
    lines += [f"### {tier.tier.value} detail ({summary.clip_set}, preset {summary.preset})", ""]
    lines.append(f"- cost metric: `{summary.metric_for_speed}`")
    lines.append(f"- encodes: {summary.n_encodes} over {summary.reps} repetition(s)")
    if summary.speed_delta:
      lines.append(
          f"- **speed:** {summary.speed_delta} "
          f"(negative is faster; interval is 95%, paired by clip/QP/slot)"
      )
    for metric, interval in sorted(summary.bdrate_by_metric.items()):
      lines.append(f"- **{metric}:** {interval}")
    if summary.per_sequence_bdrate:
      lines += ["", "| sequence | BD-rate | speedup |", "| --- | ---: | ---: |"]
      for name in sorted(summary.per_sequence_bdrate, key=lambda n: -summary.per_sequence_bdrate[n]):
        speed = summary.per_sequence_speed.get(name)
        lines.append(
            f"| {name} | {summary.per_sequence_bdrate[name]:+.3f}% | "
            + (f"{speed:+.2f}%" if speed is not None else "-")
            + " |"
        )
    if summary.notes:
      lines += ["", "**Notes**", ""]
      lines += [f"- {note}" for note in summary.notes]
    lines.append("")

  if experiment.ctc and experiment.ctc.classes:
    lines += ["## Common test conditions", "",
              f"- state: `{experiment.ctc.state}`  "
              f"({experiment.ctc.fraction_complete * 100:.0f}% complete)",
              f"- anchor job(s): `{experiment.ctc.anchor_job_id or 'n/a'}`",
              f"- candidate job(s): `{experiment.ctc.candidate_job_id or 'n/a'}`",
              ""]
    for cls in experiment.ctc.classes:
      lines += [
          f"### {cls.testset.upper()} / {cls.config.upper()} / speed {cls.preset}"
          + ("" if cls.complete else "  **(PARTIAL)**"),
          "",
          "| sequence | " + " | ".join(sorted(cls.average_bdrate)) + " | EncTime | DecTime |",
          "| --- | " + " | ".join("---:" for _ in cls.average_bdrate) + " | ---: | ---: |",
      ]
      for entry in cls.sequences:
        row = [entry.sequence]
        row += [f"{entry.bdrate.get(m, 0.0):+.2f}%" for m in sorted(cls.average_bdrate)]
        row += [f"{entry.enc_time_ratio_pct:.2f}%", f"{entry.dec_time_ratio_pct:.2f}%"]
        lines.append("| " + " | ".join(row) + " |")
      average = ["**Average**"]
      average += [f"**{cls.average_bdrate[m]:+.2f}%**" for m in sorted(cls.average_bdrate)]
      average += [
          f"**{cls.average_enc_time_ratio_pct:.2f}%**",
          f"**{cls.average_dec_time_ratio_pct:.2f}%**",
      ]
      lines.append("| " + " | ".join(average) + " |")
      lines.append("")
      lines.append(
          f"_Speedup for this class: **{cls.speedup_pct:+.2f}%** "
          "(100% - EncTime; both classes must clear the bar independently)._"
      )
      lines.append("")

  if experiment.integrity:
    lines += ["## Integrity", ""]
    null_arm = experiment.integrity.get("null_arm")
    if null_arm:
      lines.append(
          f"- **Null arm (anchor vs anchor):** {'clean' if null_arm['passed'] else 'BIASED'} "
          f"- {null_arm['detail']}"
      )
    violations = experiment.integrity.get("policy_violations") or []
    if violations:
      lines.append("- Policy findings:")
      for violation in violations[:8]:
        lines.append(
            f"  - `{violation['severity']}` {violation['kind']}: {violation['detail'][:160]}"
        )
    novelty = experiment.integrity.get("novelty") or {}
    if novelty.get("overlapping_prior"):
      lines.append(
          "- Overlaps prior attempts: " + ", ".join(novelty["overlapping_prior"])
      )
    if novelty.get("profile_share_pct"):
      lines.append(
          f"- Amdahl ceiling from the profile: {novelty['profile_share_pct']:.2f}%"
      )
    lines.append("")

  if experiment.lessons:
    lines += ["## Lessons", ""]
    lines += [f"- {lesson}" for lesson in experiment.lessons]
    lines.append("")
  if experiment.human_notes:
    lines += ["## Notes", ""]
    lines += [f"- {note}" for note in experiment.human_notes]
    lines.append("")

  if include_patch and experiment.patch.diff_text:
    lines += ["## Diff", "", "```diff", experiment.patch.diff_text.rstrip(), "```", ""]

  lines += [
      "---",
      "",
      "_Generated by av2ra. Local screening BD-rate is indicative only: on this "
      "codebase a local screen has disagreed with CTC in sign on 3 of 10 prior "
      "patches. Only the CTC section carries a quotable BD-rate._",
  ]
  return "\n".join(lines)


def write(experiment: Experiment, root: str, *, include_patch: bool = True) -> str:
  directory = ensure_dir(os.path.join(root, experiment.id))
  path = os.path.join(directory, "report.md")
  write_text_atomic(path, render(experiment, include_patch=include_patch))
  if experiment.patch.diff_text:
    write_text_atomic(
        os.path.join(directory, f"{experiment.id}.patch"), experiment.patch.diff_text
    )
  return path


def _stamp(ts: float) -> str:
  return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "n/a"


def _one_line(text: str, limit: int = 160) -> str:
  flat = " ".join((text or "").split())
  return (flat[: limit - 1] + "…") if len(flat) > limit else flat
