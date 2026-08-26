"""Reconstructing domain objects from stored JSON.

Kept separate from :mod:`av2ra.core.models` so the model file stays a
description of the domain rather than half serialisation plumbing, and so that
schema evolution has one obvious place to live: every ``_get`` below tolerates
a missing key, because records written by an older version of the agent must
still load. A registry that cannot read its own history is not a memory.
"""

from __future__ import annotations

from typing import Any

from .models import (
    CtcClassResult,
    CtcResult,
    CtcSequenceResult,
    Experiment,
    ExperimentStatus,
    Hypothesis,
    Interval,
    MechanismClass,
    PatchInfo,
    Quadrant,
    ScreenSummary,
    Tier,
    TierResult,
    Verdict,
)


def _enum(cls, value, default):
  try:
    return cls(value)
  except (ValueError, TypeError):
    return default


def interval_from_dict(data: dict | None) -> Interval | None:
  if not data:
    return None
  return Interval(
      point=float(data.get("point", 0.0)),
      lo=float(data.get("lo", 0.0)),
      hi=float(data.get("hi", 0.0)),
      n=int(data.get("n", 0)),
      method=data.get("method", ""),
  )


def screen_summary_from_dict(data: dict | None) -> ScreenSummary | None:
  if not data:
    return None
  return ScreenSummary(
      preset=int(data.get("preset", 0)),
      clip_set=data.get("clip_set", ""),
      metric_for_speed=data.get("metric_for_speed", "instructions"),
      bdrate_by_metric={
          k: interval_from_dict(v)
          for k, v in (data.get("bdrate_by_metric") or {}).items()
          if interval_from_dict(v)
      },
      speed_delta=interval_from_dict(data.get("speed_delta")),
      per_sequence_bdrate=dict(data.get("per_sequence_bdrate") or {}),
      per_sequence_speed=dict(data.get("per_sequence_speed") or {}),
      worst_sequence_bdrate=float(data.get("worst_sequence_bdrate", 0.0)),
      worst_sequence_name=data.get("worst_sequence_name", ""),
      n_encodes=int(data.get("n_encodes", 0)),
      reps=int(data.get("reps", 0)),
      notes=list(data.get("notes") or []),
  )


def tier_result_from_dict(data: dict) -> TierResult:
  return TierResult(
      tier=_enum(Tier, data.get("tier"), Tier.T0_BUILD),
      passed=bool(data.get("passed", False)),
      verdict=_enum(Verdict, data.get("verdict"), Verdict.PENDING),
      quadrant=_enum(Quadrant, data.get("quadrant"), Quadrant.UNRESOLVED),
      reason=data.get("reason", ""),
      summary=screen_summary_from_dict(data.get("summary")),
      ratio=data.get("ratio"),
      bar=data.get("bar"),
      started_ts=float(data.get("started_ts", 0.0)),
      finished_ts=float(data.get("finished_ts", 0.0)),
      evidence=dict(data.get("evidence") or {}),
      artifacts=list(data.get("artifacts") or []),
  )


def hypothesis_from_dict(data: dict) -> Hypothesis:
  return Hypothesis(
      id=data.get("id", ""),
      title=data.get("title", ""),
      subsystem=data.get("subsystem", "unknown"),
      mechanism=_enum(MechanismClass, data.get("mechanism"), MechanismClass.UNKNOWN),
      lens=data.get("lens", ""),
      statement=data.get("statement", ""),
      rationale=data.get("rationale", ""),
      target_presets=list(data.get("target_presets") or [2]),
      target_functions=list(data.get("target_functions") or []),
      target_files=list(data.get("target_files") or []),
      expected_speedup_pct=float(data.get("expected_speedup_pct", 0.0)),
      expected_bdrate_pct=float(data.get("expected_bdrate_pct", 0.0)),
      confidence=float(data.get("confidence", 0.5)),
      risk_notes=list(data.get("risk_notes") or []),
      kill_criteria=list(data.get("kill_criteria") or []),
      parameters=dict(data.get("parameters") or {}),
      parent_experiment=data.get("parent_experiment"),
      novelty=dict(data.get("novelty") or {}),
      created_ts=float(data.get("created_ts", 0.0)),
  )


def patch_from_dict(data: dict | None) -> PatchInfo:
  data = data or {}
  return PatchInfo(
      diff_text=data.get("diff_text", ""),
      base_sha=data.get("base_sha", ""),
      files=list(data.get("files") or []),
      added_lines=int(data.get("added_lines", 0)),
      removed_lines=int(data.get("removed_lines", 0)),
      touched_functions=list(data.get("touched_functions") or []),
      build_defines=dict(data.get("build_defines") or {}),
      normalized_hash=data.get("normalized_hash", ""),
  )


def ctc_result_from_dict(data: dict | None) -> CtcResult | None:
  if not data:
    return None
  classes = []
  for cls in data.get("classes") or []:
    classes.append(
        CtcClassResult(
            testset=cls.get("testset", ""),
            config=cls.get("config", "ra"),
            preset=int(cls.get("preset", 0)),
            sequences=[
                CtcSequenceResult(
                    sequence=s.get("sequence", ""),
                    bdrate=dict(s.get("bdrate") or {}),
                    enc_time_ratio_pct=float(s.get("enc_time_ratio_pct", 100.0)),
                    dec_time_ratio_pct=float(s.get("dec_time_ratio_pct", 100.0)),
                )
                for s in cls.get("sequences") or []
            ],
            average_bdrate=dict(cls.get("average_bdrate") or {}),
            average_enc_time_ratio_pct=float(cls.get("average_enc_time_ratio_pct", 100.0)),
            average_dec_time_ratio_pct=float(cls.get("average_dec_time_ratio_pct", 100.0)),
            complete=bool(cls.get("complete", True)),
            n_expected=int(cls.get("n_expected", 0)),
        )
    )
  return CtcResult(
      experiment_id=data.get("experiment_id", ""),
      job_id=data.get("job_id", ""),
      state=data.get("state", "unknown"),
      classes=classes,
      anchor_job_id=data.get("anchor_job_id", ""),
      candidate_job_id=data.get("candidate_job_id", ""),
      fraction_complete=float(data.get("fraction_complete", 0.0)),
      error=data.get("error", ""),
      raw_paths=list(data.get("raw_paths") or []),
      submitted_ts=float(data.get("submitted_ts", 0.0)),
      finished_ts=float(data.get("finished_ts", 0.0)),
  )


def experiment_from_dict(data: dict[str, Any]) -> Experiment:
  experiment = Experiment(
      id=data.get("id", ""),
      hypothesis=hypothesis_from_dict(data.get("hypothesis") or {}),
      status=_enum(ExperimentStatus, data.get("status"), ExperimentStatus.PROPOSED),
      verdict=_enum(Verdict, data.get("verdict"), Verdict.PENDING),
      quadrant=_enum(Quadrant, data.get("quadrant"), Quadrant.UNRESOLVED),
      base_sha=data.get("base_sha", ""),
      build_fingerprint=data.get("build_fingerprint", ""),
      patch=patch_from_dict(data.get("patch")),
      tiers=[tier_result_from_dict(t) for t in data.get("tiers") or []],
      ctc=ctc_result_from_dict(data.get("ctc")),
      node_id=data.get("node_id", ""),
      worktree=data.get("worktree", ""),
      branch=data.get("branch", ""),
      created_ts=float(data.get("created_ts", 0.0)),
      updated_ts=float(data.get("updated_ts", 0.0)),
      lessons=list(data.get("lessons") or []),
      integrity=dict(data.get("integrity") or {}),
      cost=dict(data.get("cost") or {}),
      human_notes=list(data.get("human_notes") or []),
      supersedes=list(data.get("supersedes") or []),
      superseded_by=data.get("superseded_by"),
  )
  experiment.tiers.sort(key=lambda t: t.tier.order)
  return experiment
