"""The experiment registry: one writer, full provenance, nothing overwritten.

The prior research on this codebase ended up with two different sets of CTC
numbers for the same three patches in two different documents, and nobody could
say which was right. That is what this module exists to prevent. Three rules:

1. **Every measurement row carries the conditions it was taken under** -- base
   SHA, patch SHA, build fingerprint, preset, class, clip set, repetition
   count, metric definition, node, and the back end's own job id. A number
   without those is not a result, it is an anecdote.

2. **Measurements are append-only.** Re-measuring does not overwrite; it adds a
   row, and the disagreement becomes visible instead of being resolved by
   whoever wrote last.

3. **A number is scoped to a base SHA.** When the anchor moves, every row taken
   against the old base is marked stale rather than silently reinterpreted.
   :mod:`av2ra.core.basewatch` decides when that happens.

SQLite is used because it gives atomic multi-row writes and concurrent readers
across the fleet without a server, and because the file itself is a
self-describing artifact that can be copied to the shared store and read
anywhere.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable

from ..util.io import ensure_dir
from .models import Experiment, ExperimentStatus, Verdict

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
  id            TEXT PRIMARY KEY,
  family        TEXT NOT NULL,
  title         TEXT NOT NULL,
  subsystem     TEXT NOT NULL,
  mechanism     TEXT NOT NULL,
  lens          TEXT NOT NULL,
  status        TEXT NOT NULL,
  verdict       TEXT NOT NULL,
  quadrant      TEXT NOT NULL,
  base_sha      TEXT NOT NULL,
  presets       TEXT NOT NULL,
  parent        TEXT,
  node_id       TEXT,
  created_ts    REAL NOT NULL,
  updated_ts    REAL NOT NULL,
  stale         INTEGER NOT NULL DEFAULT 0,
  patch_hash    TEXT,
  record        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exp_verdict   ON experiments(verdict);
CREATE INDEX IF NOT EXISTS idx_exp_subsystem ON experiments(subsystem);
CREATE INDEX IF NOT EXISTS idx_exp_family    ON experiments(family);
CREATE INDEX IF NOT EXISTS idx_exp_base      ON experiments(base_sha);

-- Append-only. A re-measurement is a new row, never an update.
CREATE TABLE IF NOT EXISTS measurements (
  row_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL,
  tier          TEXT NOT NULL,
  class_name    TEXT NOT NULL,     -- A1 / A2 / screen / holdout
  preset        INTEGER NOT NULL,
  metric        TEXT NOT NULL,     -- e.g. PSNR-YUV, instructions
  value         REAL,
  ci_lo         REAL,
  ci_hi         REAL,
  n             INTEGER,
  base_sha      TEXT NOT NULL,
  patch_hash    TEXT,
  build_fp      TEXT,
  clip_set      TEXT,
  reps          INTEGER,
  metric_def    TEXT,              -- e.g. "bdrate/pchip/yuv-14-1-1"
  backend       TEXT,              -- local | eda | sim
  job_id        TEXT,              -- back-end invocation id, for traceability
  node_id       TEXT,
  ts            REAL NOT NULL,
  stale         INTEGER NOT NULL DEFAULT 0,
  extra         TEXT
);

CREATE INDEX IF NOT EXISTS idx_meas_exp  ON measurements(experiment_id);
CREATE INDEX IF NOT EXISTS idx_meas_base ON measurements(base_sha);

CREATE TABLE IF NOT EXISTS hypotheses_seen (
  signature   TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  title       TEXT NOT NULL,
  subsystem   TEXT NOT NULL,
  verdict     TEXT NOT NULL,
  ts          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ctc_budget (
  row_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL,
  slots      REAL NOT NULL,
  question   TEXT NOT NULL,
  granted_ts REAL NOT NULL
);
"""


@dataclass
class MeasurementRow:
  experiment_id: str
  tier: str
  class_name: str
  preset: int
  metric: str
  value: float | None
  ci_lo: float | None = None
  ci_hi: float | None = None
  n: int = 0
  base_sha: str = ""
  patch_hash: str = ""
  build_fp: str = ""
  clip_set: str = ""
  reps: int = 0
  metric_def: str = ""
  backend: str = ""
  job_id: str = ""
  node_id: str = ""
  extra: dict | None = None


class Registry:
  """Durable store of experiments and their measurements."""

  def __init__(self, path: str):
    self.path = path
    ensure_dir(os.path.dirname(os.path.abspath(path)) or ".")
    self._conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    self._conn.row_factory = sqlite3.Row
    # WAL keeps fleet readers from blocking the single writer, and lets a
    # dashboard read a consistent snapshot while a screening pass commits.
    self._conn.execute("PRAGMA journal_mode=WAL")
    self._conn.execute("PRAGMA synchronous=NORMAL")
    self._conn.executescript(_SCHEMA)
    self._conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )

  # -- experiments ---------------------------------------------------------

  def upsert(self, experiment: Experiment) -> None:
    from .ids import family_of

    experiment.updated_ts = time.time()
    self._conn.execute(
        """INSERT INTO experiments
             (id, family, title, subsystem, mechanism, lens, status, verdict,
              quadrant, base_sha, presets, parent, node_id, created_ts,
              updated_ts, stale, patch_hash, record)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title, subsystem=excluded.subsystem,
             mechanism=excluded.mechanism, lens=excluded.lens,
             status=excluded.status, verdict=excluded.verdict,
             quadrant=excluded.quadrant, base_sha=excluded.base_sha,
             presets=excluded.presets, parent=excluded.parent,
             node_id=excluded.node_id, updated_ts=excluded.updated_ts,
             patch_hash=excluded.patch_hash, record=excluded.record""",
        (
            experiment.id,
            family_of(experiment.id),
            experiment.hypothesis.title,
            experiment.hypothesis.subsystem,
            experiment.hypothesis.mechanism.value,
            experiment.hypothesis.lens,
            experiment.status.value,
            experiment.verdict.value,
            experiment.quadrant.value,
            experiment.base_sha,
            ",".join(str(p) for p in experiment.hypothesis.target_presets),
            experiment.hypothesis.parent_experiment,
            experiment.node_id,
            experiment.created_ts,
            experiment.updated_ts,
            0,
            experiment.patch.normalized_hash,
            json.dumps(experiment.to_dict(), sort_keys=True, default=str),
        ),
    )
    self._conn.execute(
        """INSERT INTO hypotheses_seen(signature, experiment_id, title,
                                       subsystem, verdict, ts)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(signature) DO UPDATE SET verdict=excluded.verdict,
                                                ts=excluded.ts""",
        (
            experiment.hypothesis.signature(),
            experiment.id,
            experiment.hypothesis.title,
            experiment.hypothesis.subsystem,
            experiment.verdict.value,
            time.time(),
        ),
    )

  def get(self, experiment_id: str) -> Experiment | None:
    row = self._conn.execute(
        "SELECT record FROM experiments WHERE id=?", (experiment_id,)
    ).fetchone()
    return _experiment_from_json(row["record"]) if row else None

  def all_ids(self) -> list[str]:
    return [r["id"] for r in self._conn.execute("SELECT id FROM experiments ORDER BY id")]

  def query(
      self,
      *,
      verdict: str | None = None,
      subsystem: str | None = None,
      status: str | None = None,
      base_sha: str | None = None,
      family: str | None = None,
      limit: int = 500,
  ) -> list[Experiment]:
    clauses, params = [], []
    for column, value in (
        ("verdict", verdict), ("subsystem", subsystem), ("status", status),
        ("base_sha", base_sha), ("family", family),
    ):
      if value:
        clauses.append(f"{column}=?")
        params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = self._conn.execute(
        f"SELECT record FROM experiments {where} ORDER BY updated_ts DESC LIMIT ?",
        (*params, limit),
    )
    return [_experiment_from_json(r["record"]) for r in rows]

  def seen_signature(self, signature: str) -> dict | None:
    row = self._conn.execute(
        "SELECT * FROM hypotheses_seen WHERE signature=?", (signature,)
    ).fetchone()
    return dict(row) if row else None

  # -- measurements --------------------------------------------------------

  def record_measurement(self, row: MeasurementRow) -> int:
    cursor = self._conn.execute(
        """INSERT INTO measurements
             (experiment_id, tier, class_name, preset, metric, value, ci_lo,
              ci_hi, n, base_sha, patch_hash, build_fp, clip_set, reps,
              metric_def, backend, job_id, node_id, ts, stale, extra)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
        (
            row.experiment_id, row.tier, row.class_name, row.preset, row.metric,
            row.value, row.ci_lo, row.ci_hi, row.n, row.base_sha, row.patch_hash,
            row.build_fp, row.clip_set, row.reps, row.metric_def, row.backend,
            row.job_id, row.node_id, time.time(),
            json.dumps(row.extra or {}, sort_keys=True, default=str),
        ),
    )
    return int(cursor.lastrowid)

  def measurements(self, experiment_id: str, *, include_stale: bool = False) -> list[dict]:
    clause = "" if include_stale else " AND stale=0"
    rows = self._conn.execute(
        f"SELECT * FROM measurements WHERE experiment_id=?{clause} ORDER BY ts",
        (experiment_id,),
    )
    return [dict(r) for r in rows]

  def conflicting_measurements(self, tolerance: float = 0.05) -> list[dict]:
    """Rows that measure the same thing under the same conditions and disagree.

    Surfacing these is the whole point of append-only storage. A disagreement
    is either a genuine repeatability problem in the back end -- which is a
    finding about the measurement system, and the corpus never once measured it
    -- or a provenance bug in the harness. Both need a human.
    """
    rows = list(
        self._conn.execute(
            "SELECT * FROM measurements WHERE stale=0 AND value IS NOT NULL"
        )
    )
    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
      key = (
          row["experiment_id"], row["tier"], row["class_name"], row["preset"],
          row["metric"], row["base_sha"], row["patch_hash"], row["metric_def"],
      )
      grouped.setdefault(key, []).append(dict(row))
    conflicts = []
    for key, group in grouped.items():
      if len(group) < 2:
        continue
      values = [g["value"] for g in group]
      spread = max(values) - min(values)
      scale = max(abs(v) for v in values) or 1.0
      if spread / scale > tolerance:
        conflicts.append({"key": key, "rows": group, "spread": spread})
    return conflicts

  def mark_stale(self, *, base_sha: str, reason: str) -> int:
    """Invalidate everything measured against a base that is no longer current."""
    cursor = self._conn.execute(
        "UPDATE measurements SET stale=1 WHERE base_sha=? AND stale=0", (base_sha,)
    )
    self._conn.execute(
        "UPDATE experiments SET stale=1 WHERE base_sha=?", (base_sha,)
    )
    self._conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        (f"stale:{base_sha}", f"{time.time()}:{reason}"),
    )
    return cursor.rowcount

  # -- CTC budget ----------------------------------------------------------

  def grant_ctc_slots(self, experiment_id: str, slots: float, question: str) -> None:
    self._conn.execute(
        "INSERT INTO ctc_budget(experiment_id, slots, question, granted_ts)"
        " VALUES (?,?,?,?)",
        (experiment_id, slots, question, time.time()),
    )

  def ctc_slots_used(self, *, since_ts: float = 0.0) -> float:
    row = self._conn.execute(
        "SELECT COALESCE(SUM(slots), 0) AS total FROM ctc_budget WHERE granted_ts >= ?",
        (since_ts,),
    ).fetchone()
    return float(row["total"])

  # -- export --------------------------------------------------------------

  def export_csv(self, path: str) -> str:
    """A human-readable ledger in the shape the prior corpus used."""
    import csv

    ensure_dir(os.path.dirname(os.path.abspath(path)) or ".")
    rows = self._conn.execute(
        "SELECT * FROM experiments ORDER BY id"
    ).fetchall()
    with open(path, "w", newline="", encoding="utf-8") as handle:
      handle.write(
          "# Every row is scoped to its base_sha. A number measured against a\n"
          "# different base is not comparable and is marked stale.\n"
      )
      writer = csv.writer(handle)
      writer.writerow([
          "id", "title", "subsystem", "mechanism", "lens", "presets", "status",
          "verdict", "quadrant", "base_sha", "patch_hash", "stale",
          "best_ratio", "speedup_pct", "bdrate_pct", "classes", "updated",
      ])
      for row in rows:
        record = json.loads(row["record"])
        tiers = record.get("tiers", [])
        best = None
        for tier in tiers:
          if tier.get("ratio") is not None:
            best = tier
        summary = (best or {}).get("summary") or {}
        speed = (summary.get("speed_delta") or {}).get("point")
        bd = None
        for metric in ("PSNR-YUV", "PSNR-Y"):
          entry = (summary.get("bdrate_by_metric") or {}).get(metric)
          if entry:
            bd = entry.get("point")
            break
        writer.writerow([
            row["id"], row["title"], row["subsystem"], row["mechanism"],
            row["lens"], row["presets"], row["status"], row["verdict"],
            row["quadrant"], row["base_sha"][:10], (row["patch_hash"] or "")[:10],
            row["stale"], (best or {}).get("ratio"),
            None if speed is None else -speed, bd,
            ",".join(sorted({m["class_name"] for m in self.measurements(row["id"])})),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(row["updated_ts"])),
        ])
    return path

  def stats(self) -> dict:
    def _scalar(sql: str, *params: Any) -> Any:
      return self._conn.execute(sql, params).fetchone()[0]

    by_verdict = {
        r["verdict"]: r["n"]
        for r in self._conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM experiments GROUP BY verdict"
        )
    }
    return {
        "experiments": _scalar("SELECT COUNT(*) FROM experiments"),
        "measurements": _scalar("SELECT COUNT(*) FROM measurements"),
        "stale_measurements": _scalar("SELECT COUNT(*) FROM measurements WHERE stale=1"),
        "by_verdict": by_verdict,
        "ctc_slots": self.ctc_slots_used(),
        "conflicts": len(self.conflicting_measurements()),
    }

  def close(self) -> None:
    self._conn.close()

  def __enter__(self) -> "Registry":
    return self

  def __exit__(self, *exc: object) -> None:
    self.close()


def _experiment_from_json(blob: str) -> Experiment:
  from .serde import experiment_from_dict

  return experiment_from_dict(json.loads(blob))


def iter_alive(registry: Registry) -> Iterable[Experiment]:
  for status in (
      ExperimentStatus.PROPOSED, ExperimentStatus.IMPLEMENTING,
      ExperimentStatus.BUILDING, ExperimentStatus.SCREENING,
      ExperimentStatus.AWAITING_CTC, ExperimentStatus.CTC_RUNNING,
  ):
    yield from registry.query(status=status.value)


def promotable(registry: Registry) -> list[Experiment]:
  out = []
  for verdict in (Verdict.CTC_PASS, Verdict.PROMOTE):
    out.extend(registry.query(verdict=verdict.value))
  return out
