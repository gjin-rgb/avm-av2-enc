# Decision log

Append-only. Newest entry at the top. Every entry says what changed state, and
why. If a patch was killed, the reason must be recorded here so nobody spends a
CTC slot rediscovering it.

---

## 2026-08-12 — CTC round 1 results: 1 promote, 1 improve, 8 discard

Anchor `d6b40b789381601440e4ce2cc1164cd57e8c3c7d`, Class A1 (17f 4K) and A2
(33f 4K). Bar is the Complexity-to-Efficiency ratio, speedup% / BD-rate%:
>=20 at Speed 4, >=25 S3, >=30 S2, >=35 S1.

### PROMOTE — i09 (tx-partition stationarity)

+2.60% / +0.11% on A1 (ratio 23.6) and +2.27% / +0.09% on A2 (ratio 25.2). The
only patch clearing the bar on **both** classes. Small, but it is a genuine
speed feature and it is finished. Scope it to Speed 4: the A2 ratio would also
meet the Speed-3 bar of 25, but A1's 23.6 would not.

### IMPROVE — i06 (orientation partition pruning)

+14.00% / +0.75% on A1 (ratio 18.7), +18.52% / +1.29% on A2 (ratio 14.4). Fails
the bar, but it is the only patch in the set with real magnitude, and it fails
*close* on A1.

The binding constraint is A2. To reach ratio 20 at unchanged speed it needs

    A2: BD 1.29% -> 0.926%   (-28%)
    A1: BD 0.75% -> 0.700%   ( -6.7%)

The diagnostic that matters: **A2's ratio is worse than A1's despite a higher
speedup.** A2 is 33 frames against A1's 17, so the extra loss is not in how much
is pruned but in how long the consequences persist — the damage compounds
through the reference structure. That points the fix at *which frames* get
pruned, not just how hard.

Three changes follow from reading the patch, in order of expected BD-rate
returned per unit of speed given up:

1. **Absolute variance floor.** The test is currently a pure *ratio*:
   `var_cols * anisotropy < var_rows`. On a near-flat block both variances are
   tiny and their ratio is noise, so a block with `var_rows=8, var_cols=1` is
   pruned on no real evidence. Flat blocks also settle on PARTITION_NONE quickly,
   so refusing to prune them should cost very little speed. Best ratio of the
   three.
2. **Temporal gating.** Do not prune (or require stronger anisotropy) on frames
   high in the reference pyramid. Targets the propagation that the A1-vs-A2 gap
   points to.
3. **Raise the anisotropy thresholds** (`EXT_PART_ANISOTROPY 4`,
   `RECT_PART_ANISOTROPY 8`). The blunt knob; trades speed roughly in
   proportion, so it is the fallback rather than the fix.

A fourth observation, not yet actioned: the profile is measured on the **source**
only. For inter blocks it is the *residual* that determines whether a cut pays,
and a strongly structured block that is well predicted has a flat residual. That
makes the whole heuristic weakest exactly where A2 spends most of its frames.

### DISCARD — everything else (8 of 10)

- **i01** (+4.27%/+1.22%, ratio 3.5; +5.05%/+0.88%, 5.7). Real speedup, but the
  BD cost is 4-6x too high. The gap to the bar is too large to tune out.
- **i07** (+0.22%/+0.30%, 0.7; +2.84%/+0.76%, 3.7). Near-zero speedup on A1.
- **i08** (-0.45% on A1; +0.91% on A2). Slower on A1. Its A2 "infinite ratio" is
  an artifact of a -0.01% BD rounding to a gain, not a win.
- **i04** (-1.31% A1, +0.05% A2). Slower. This is the outcome the round-1 paired
  data already implied (-5.1%, -0.5% across two reps) while the round-robin
  design called it noise.
- **i10** (-0.14% A1, -0.71% A2). Slower on both classes.

### DISCARD — the bit-exact three, for the opposite reason

i02, i03 and i05 returned **exactly +0.00%** on every metric — PSNR-Y/U/V, SSIM,
MS-SSIM, VMAF — across the complete A1 runs and the >85% A2 runs. The Tier-0
prediction was correct, and it was available in 25 minutes for the cost of two
short encodes rather than a CTC slot.

But on A1 4K they are all *slower*: -2.34% (i02), -2.50% (i03), -1.39% (i05),
and -2.55% for the three combined. Zero risk and zero benefit is not a feature.
The mechanism is the same in each case — overhead that does not repay itself at
4K scale:

- i05 pays a memo lookup on every call, and at 4K the MV field is diverse enough
  that the hit rate never repays it.
- i03 replaces many small allocations, which fit in cache, with one large arena
  that does not.
- i02's sparse-restore bookkeeping loses to a bulk clear once blocks are large
  and dense.

The general lesson is that all three were designed against 416x240 intuitions
about what is expensive, and 4K inverts them.

### The strategic result: the combination is i06 plus ballast

All ten together give +20.34%/+2.50% (ratio 8.1) on A1 and +24.00%/+3.45%
(7.0) on A2. Backing i06 out of those numbers — speedups compound
multiplicatively, BD-rate adds — leaves the other nine contributing

    A1: ~7.4% speed for ~1.75% BD   ratio ~4.2
    A2: ~6.7% speed for ~2.16% BD   ratio ~3.1

So nine patches are collectively buying about a third of i06's speedup at more
than twice its quality cost. **The next round must not test all-10 again.** The
arm worth running is i06+i09, estimated at ~16.2%/~0.86% (18.9) on A1 and
~20.4%/~1.38% (14.8) on A2 — barely better than i06 alone, because i09 is small
and everything else is ballast.

One finding worth keeping from the combination runs: the speedup *rises* at
slower presets (+30.74% at Speed 2 vs +20.34% at Speed 4 on A1) for essentially
unchanged BD-rate. These heuristics prune redundancy that only exists in deeper
searches. The bars rise faster than the ratio does, so this does not rescue the
set, but it is the right place to aim future pruning work.

---

## 2026-08-12 — Tier 0 result: i02, i03, i05 are all BIT-EXACT

Run on the fixed harness (clean-tree assertion active, patches verified isolated
one at a time):

    config: 416x240, 6 frames, cpu-used=3, single-threaded, QP 110 and 185
    baseline                          48ebfecfc46f8cb6a43277e7739b33a6
    0002-ist-sparse-coeff-restore     48ebfecfc46f8cb6a43277e7739b33a6  BIT-EXACT
    0003-pmc-arena-allocation         48ebfecfc46f8cb6a43277e7739b33a6  BIT-EXACT
    0005-fullpel-search-memo          48ebfecfc46f8cb6a43277e7739b33a6  BIT-EXACT

All three change no encode decision. For i05 specifically this says the memo key
is correct across DRL index and MV precision — the exact thing that would have
been wrong if the patch were buggy.

**Decision: none of these three should consume a CTC round.** Their quality risk
is zero on the tested configuration, which is a stronger and far cheaper result
than a BD-rate table could give. What remains unknown for all three is *speed* —
round-1 measured 1.3%, 1.2% and 1.2%, all below the 3.5% noise floor, so their
benefit is still entirely unquantified. They go to Tier 1 (`perf stat`
instruction counts), not to CTC.

**Scope of the claim, stated precisely.** This is proof for the configuration
tested, not a universal proof. One clip at 416x240, 6 frames, two QPs and
cpu-used=3 does not exercise every block size, partition shape or reference
structure that A1 4K at the CTC preset will. A cache-key bug living only in a
path this config never reaches would not have been caught. Re-running Tier 0 on
a 4K clip at the CTC preset before final sign-off is cheap and worth doing;
until then the correct statement is "bit-exact on the tested configuration",
not "bit-exact".

---

## 2026-08-12 — Tier-0 harness had a silent contamination bug (fixed)

The first version of `bin/bitexact.sh` reverted the tree between patches with
`git checkout -- av2/ aom/ 2>/dev/null`. This repo has no `aom/` directory — AVM
renamed it `av2/` — so git rejected the entire command on the bad pathspec,
reverted **nothing**, and returned 1, with the error swallowed by `2>/dev/null`.

Consequence: patches accumulated instead of being tested in isolation. i03 would
have been measured with i02 still applied, i05 with both, and on the following
run the *baseline itself* was built with all three patches in the tree. The
script would have printed confident, precisely formatted, entirely wrong
signatures — and "bit-exact" would have been the expected output, since the
baseline and the patched builds were becoming the same binary.

Caught by noticing `git status` showed all three patches' files modified at once
during what was supposed to be a clean baseline build. Fixed by reverting with a
valid pathspec and then **asserting** the tree is clean, aborting loudly if not.

This is the same failure mode as the round-1 measurements below: a number that
looks precise, is produced by a process nobody verified, and is wrong. The
general rule now applied in the tooling: *assert the state you depend on; never
assume the command that was supposed to establish it worked.*

---

## 2026-08-12 — Round-1 measurements audited and largely retracted

**Trigger.** CTC round 1 (A1 17 frames, A2 33 frames, RA) was launched against
all ten patches. Before results returned, the round-1 evidence that justified
sending them was re-examined.

**Finding.** The round-1 timing design could not resolve the effects it
reported. Re-analysis of `patches/measurements-runtime-roundrobin.csv` with
`bin/screen_timing.py`:

    baseline mean 88.446s, sd 2.107s -> noise floor 2.38% (1 sigma)
    minimum detectable effect 3.47% (95%, n=5 per arm)

Six patches reported speedups below that floor:

| patch | reported | 95% CI | verdict |
|---|---|---|---|
| i02 | 1.3% | [-2.2%, +4.9%] | NOISE |
| i03 | 1.2% | [-3.5%, +5.9%] | NOISE |
| i04 | 0.5% | [-4.3%, +5.4%] | NOISE |
| i05 | 1.2% | [-2.1%, +4.5%] | NOISE |
| i08 | 1.9% | [-1.5%, +5.3%] | NOISE |
| i10 | 2.1% | [-1.4%, +5.6%] | NOISE |

Four were resolved above noise, but only one clears the >5% bar with its whole
interval:

| patch | speedup | 95% CI | verdict |
|---|---|---|---|
| i06 | 17.2% | [+14.3%, +20.2%] | clears the bar |
| i09 | 7.8% | [+4.2%, +11.4%] | straddles the bar |
| i01 | 6.5% | [+2.9%, +10.0%] | straddles the bar |
| i07 | 3.2% | [+0.2%, +6.1%] | real, under the bar |

**Cross-check — the two round-1 datasets contradict each other.** The paired
dataset (`measurements-runtime-paired.csv`, base and test in the same rep, which
cancels machine drift) gives per-rep speedups that disagree with the round-robin
verdicts:

| patch | paired rep1 | paired rep2 | round-robin verdict | agrees? |
|---|---|---|---|---|
| i01 | -0.6% | +5.0% | "real, 6.5%" | no — sign flips |
| i02 | +2.1% | +6.7% | NOISE | no |
| i03 | -0.2% | +6.9% | NOISE | no — sign flips |
| i04 | -5.1% | -0.5% | NOISE | consistently *slower* |
| i05 | +2.4% | +0.5% | NOISE | yes |
| i06 | +17.6% | +19.2% | +17.2% | yes |
| i07 | +2.5% | +1.6% | +3.2% | yes |
| i08 | +4.4% | +0.1% | NOISE | unstable |
| i09 | +8.7% | +0.9% | +7.8% | no — 10x spread |
| i10 | +0.9% | +9.1% | NOISE | no — 10x spread |

Two independent measurements of the same quantity disagreeing this badly means
neither is trustworthy. **i06 is the only patch that is stable across both
designs and both datasets.** i04 appears to be a slowdown, not a speedup.

**Second finding — regime mismatch.** Round 1 was measured on a single 416x240
clip, 4 frames, `--cpu-used=4`. CTC RA is A1 4K + A2, 17/33 frames, at the CTC
preset with 6 QPs. Partition and transform pruning heuristics depend directly on
block-size distribution and search depth, both of which differ substantially
between those regimes. Round-1 magnitudes should not be expected to transfer.

**Decisions.**
- Retract the six NOISE results. They are not small wins; they are
  non-measurements. `registry.csv` marks them `hold`.
- i02, i03 and i05 are *reuse* mechanisms (buffer restore, arena allocation,
  memo cache). They must be bit-exact. Their status is decided by
  `bin/bitexact.sh`, not by any CTC round — a bit-exact patch has zero quality
  risk by proof, and a diverging one has a bug. Tier 0 launched.
- All future timing moves to `perf stat -e instructions`, which the CTC
  framework already supports (`use_perf_util: true` in
  `tools/convexhull_framework/src/config.yaml`).
- i06 carries the largest speedup and, being partition pruning, the largest
  BD-rate risk at 4K. Treat a good i06 CTC result with suspicion until
  per-sequence spread is examined.

**Not yet done.** Tier 2 (shadow-mode decision regret) is specified in
`README.md` but not implemented. It is the highest-value remaining piece of
infrastructure: it measures the quality cost of every pruning heuristic
deterministically, in one encode per (clip, QP), without a CTC round.
