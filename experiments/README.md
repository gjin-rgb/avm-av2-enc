# Encoder speed-up experiment protocol

A CTC round on A1+A2 RA costs hours to a day. That cost is only worth paying to
answer questions that *nothing cheaper can answer*. The purpose of this protocol
is to make sure a CTC slot is never spent on a patch that a deterministic,
minutes-long check could already have killed.

Round 1 of this work spent a full CTC slot on ten patches, of which six had
never been shown to be faster than the measurement noise on the machine that
timed them. That is the failure mode this protocol exists to prevent.

## The measurement problem

Round-1 timings were wall-clock, on a shared machine, 5 reps, on a 416x240 clip
at `--cpu-used=4`. Re-analysed (`bin/screen_timing.py`):

    noise floor (1 sigma): 2.38%
    min detectable effect: 3.47%   (95%, n=5 per arm)

Six of the ten reported "speedups" (i02 1.3%, i03 1.2%, i04 0.5%, i05 1.2%,
i08 1.9%, i10 2.1%) are smaller than the smallest effect the design could
resolve. They are not small wins; they are non-measurements.

Three further mismatches against CTC RA make the round-1 numbers weak evidence
even where they were statistically real:

| Round-1 measurement | CTC RA |
|---|---|
| 416x240, 1 clip | A1 (4K) + A2, full CTC set |
| 4 frames | 17 / 33 frames |
| `--cpu-used=4` | CTC preset (much deeper search) |
| 4 QPs, ad-hoc | 6 QPs: 110/135/160/185/210/235 |
| wall clock, shared host | `perf stat` (already wired: `use_perf_util`) |

Block-size distribution, partition depth and search occupancy at 4K under a deep
preset are so different from 416x240 at `cpu-used=4` that partition- and
transform-pruning heuristics should be assumed not to transfer until measured.
Expect regressions to the mean on the CTC set.

## The funnel

Each tier is strictly cheaper than the one below it and kills patches the next
tier would otherwise have to pay for.

### Tier 0 — proof (minutes, deterministic)

For any patch whose mechanism is *reuse*, not *approximation* — caching,
buffer-restore, arena allocation — the bitstream must be byte-identical. Run
`bin/bitexact.sh`.

- **BIT-EXACT** → quality risk is zero *by proof*. Never spend a CTC round on
  it; judge on speed alone.
- **DIVERGES** → the refactor changed an encode decision. That is a bug in the
  patch, not a quality tradeoff to be measured.

This tier decides i02, i03 and i05 without any CTC time at all.

### Tier 1 — deterministic cost (minutes, no quality data)

Wall clock cannot resolve <3.5% on a shared host. Instruction counts can:
`perf stat -e instructions` is reproducible to well under 0.1% and is immune to
co-tenancy, frequency scaling and scheduler drift.

    perf stat -e instructions,cycles -x, -o out.csv -- avmenc <ctc args>

Gate: instruction-count reduction must clear the target effect (>5%) on a mix
that includes at least one A1 4K clip at CTC QPs. A patch that cannot clear it
here will not clear it in CTC.

Use `bin/screen_timing.py` on the collected counts. It reports a 95% CI and the
minimum detectable effect, and refuses to call a sub-noise difference a win.

### Tier 2 — decision regret (hours, deterministic, no CTC)

Every heuristic in this patch set has the same shape: the baseline evaluates a
candidate set S and takes `argmin RD`; the patch evaluates a subset S' and takes
`argmin` over S'. The quality cost is therefore *exactly* the regret

    regret = RD(argmin over S') - RD(argmin over S)   >= 0

and this can be measured **without changing the bitstream**. Run the encoder in
shadow mode: perform the full baseline search (so the output, and every
subsequent frame's context, is identical to the anchor), but also evaluate the
heuristic's stopping rule and record what it *would* have chosen.

Per decision site this yields two deterministic numbers:

- `sum(regret) / sum(RD)` — a normalized upper bound on local quality damage
- `skipped_evals / total_evals` — a hardware-independent speedup proxy

Both have **zero run-to-run variance**, need **one encode per (clip, QP)**, and
attribute cleanly per patch even when several patches are enabled at once,
because each instruments a different decision site.

Limits, stated honestly: this is a *local* measure. It does not capture error
propagation once decisions actually change, the gap between RD cost and BD-rate,
or interactions between patches. It is a cheap **necessary** condition, not a
sufficient one — high regret kills a patch outright, low regret still has to be
confirmed by CTC.

### Tier 3 — reduced CTC (hours)

Survivors only. A2 plus two A1 clips, 4 of the 6 QPs. Catches gross regressions
before committing a full slot.

### Tier 4 — full CTC RA, A1+A2 (day)

Report **per-sequence BD-rate spread, not only the mean**. A patch averaging
-0.05% while costing +0.4% on one sequence is a risk, not a win.

## Combination, and the order to test in

Several of these patches touch overlapping code — i04/i05/i10 all sit in motion
search, i01/i09 both in transform search — so individual gains will not add.

**Run the union first, not last.** The question that decides whether this work
is worth continuing is "can this patch family reach >5% at an acceptable
BD-rate?", and the union answers it in *one arm*. Ten one-at-a-time arms cost
ten times as much and cannot answer it at all, because they never measure the
combined effect. Only once the union clears the bar is per-patch attribution
worth a round; if the union cannot clear it, no subset can, and the whole family
can be dropped after a single CTC slot instead of ten.

So: union first (go/no-go), then attribution on the survivors, then tuning.

## Round economics

- The anchor is fixed. Encode it **once** and reuse those results across every
  round; re-encoding the anchor each round doubles the cost of every experiment
  for no information.
- Arms within a round are independent, so round *latency* is set by the slowest
  arm, not the number of arms. Prefer few rounds with many arms over many rounds
  with few. Anything that turns a serial sequence of rounds into one parallel
  round is worth more than any single patch in this set.
- Every patch killed at Tier 0/1/2 is an arm that does not have to be scheduled,
  which is why the cheap tiers are worth building before the next round rather
  than after it.

## Tools

| Path | Purpose |
|---|---|
| `bin/bitexact.sh` | Tier 0. Proves a refactor changes no encode decision. |
| `bin/screen_timing.py` | Tier 1. CI + minimum-detectable-effect on timing data. |
| `registry.csv` | Current state of every patch across all tiers. |
| `DECISIONS.md` | Append-only log: what was killed or kept each round, and why. |

BD-rate itself is already implemented in-tree at
`tools/convexhull_framework/src/CalcBDRate.py`; this protocol does not duplicate
it.
