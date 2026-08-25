# Decision log

Append-only. Newest entry at the top. Every entry says what changed state, and
why. If a patch was killed, the reason must be recorded here so nobody spends a
CTC slot rediscovering it.

---

## 2026-08-14 — Round 3: both bets lost. 09b is final; 06e rebuilt from marginal ratios

    09c   A1 +4.40% / +0.30%  14.7 FAIL    A2 +4.71% / +0.40%  11.8 FAIL
    06d   A1 +11.96% / +0.75% 15.9 FAIL    A2 +9.57% / +0.92%  10.4 FAIL

### 09 is finished: 09b is the peak, adopt it and stop

The margin curve has three points now and 09b is the maximum on both classes:

    MARGIN 4   A1 23.6   A2 25.2
    MARGIN 2   A1 26.7   A2 51.0     <- peak, PASS both
    MARGIN 1   A1 14.7   A2 11.8     <- decay

Margin 1 bought only +0.4% to +1.1% more speedup while BD-rate roughly doubled
to +0.30%/+0.40%. The decay I said had to arrive eventually arrived at exactly
the next step. **Adopt 09b. No further tuning of this parameter is warranted** —
the curve has been bracketed on both sides and the top is measured, not
inferred.

### 06: use marginal ratios, not hypotheses

Every 06 variant so far was built on a story about which blocks are unsafe, and
two of the three stories were wrong. The three measured variants allow the
question to be answered directly instead. For each change, the speed given up
divided by the BD-rate recovered gives the marginal ratio of the prunes that
change removed; a change raises the overall ratio only when its marginal ratio
is *below* the ratio already achieved.

    A1  variance floor              +2.04% speed for +0.00% BD   marginal  inf
    A1  frame gate                  +5.89% speed for +0.46% BD   marginal 12.8
    A2  var floor + rect floor 32   +8.95% speed for +0.37% BD   marginal 24.2
    A2  frame gate + ext floor 32   +3.36% speed for +0.46% BD   marginal  7.3

**The variance floor was worthless.** It cost 2.04% of the 4K speedup and
returned 0.00% BD-rate. Not a poor trade — no trade. It has been carried in
every variant since 06b on the strength of an argument about flat blocks that
the data never supported.

**The frame gate works, and 06d was wrong to remove it.** At marginal 12.8 the
prunes it removes sit well under the bar. My structural argument for deleting it
— that a source-based profile is most trustworthy on intra frames — was not
wrong about accuracy, but accuracy was not the deciding factor: a prune on a
frame the whole GOP predicts from is paid for by every frame that follows,
however well justified it looked. Restored in 06e.

**The block-size floor was backwards.** Raising it to 32 on 1080p removed prunes
worth marginal 24.2, comfortably *above* the bar. The 16-31 blocks 06c excluded
were the good prunes, which is why 06c moved A2 backwards (14.4 -> 13.5). The
damage on 1080p is in the large blocks.

In hindsight that is the reading that should have come first. The profile
measures orientation about equally well at any size; what does not scale is the
cost of being wrong. A mistaken prune on a 128x128 block commits sixty-four
times the area to a worse partition than the same mistake on a 16x16. Large
blocks are not less measurable — they are more expensive to get wrong.

### 06e

Floor stays at 16 everywhere; the variance floor is gone; the frame gate is
back; and the required anisotropy now scales with block size (base under 32,
doubled at 32-63, quadrupled at 64+). The frame and size multipliers compose, so
a large block on a heavily referenced frame must clear a very high bar while a
small block on a leaf frame is pruned on the original evidence.

**Honest odds on A2.** It has never exceeded 14.4, and its best value came from
the original patch with no corrections at all. Predicting it clears 20 would be
optimistic. 06e is the first version aimed at where the measurements locate the
damage rather than where I assumed it was, which is a better position than the
last two rounds, not a guarantee.

If 06e lands short on A2, stop tuning. The remaining lever is the one named two
rounds ago and still unbuilt: the profile is measured on the **source**, and A2
spends most of its 33 frames on inter blocks where the residual, not the source,
decides whether a cut pays.

**06c + 09b remains shippable today** at +11.87% / ratio 22.0 on 4K. Nothing
here is a prerequisite for taking that.

---

## 2026-08-13 — Round 2: 09b passes both classes; 06c improved A1 by the wrong mechanism

Speed 4, anchor `d6b40b7893`, bar 20.

    09b   A1 +4.01% / +0.15%  ratio 26.7  PASS     A2 +3.57% / +0.07%  51.0  PASS
    06c   A1 +6.07% / +0.29%  ratio 20.9  PASS     A2 +6.21% / +0.46%  13.5  FAIL
    both  A1 +11.87% / +0.54% ratio 22.0  PASS     A2 +9.80% / +0.72%  13.6  FAIL

**09b is the first patch to clear the bar on both classes**, and it beat 09 on
every axis: more speedup (+4.01% vs +2.60%) at a better ratio. Both of its
changes worked. The lazy-evaluation fix was free as predicted, and loosening
`TX_PART_STATIONARITY_MARGIN` 4 -> 2 was ratio-*positive* rather than merely
ratio-neutral: speed rose ~1.55x on both classes while BD-rate rose only 1.36x
on A1 and actually fell on A2.

### 06c improved A1's ratio, but not by the mechanism it was built on

    A1: speed -57%, BD -61%   ratio 18.7 -> 20.9
    A2: speed -66%, BD -64%   ratio 14.4 -> 13.5   (worse)

Both classes lost speed and quality cost in near-equal proportion. That is what
a blunt reduction in pruning strength does, and it is precisely the failure mode
the 06c commit message warned against before walking into it. On A2 it came out
behind where it started.

The detail that identifies the culprit: **A1's block-size floor never changed.**
4K keeps a floor of 16 in both 06 and 06c, so the resolution scaling — the whole
point of 06c — did nothing on A1. A1's entire -57% speedup came from the
variance floor and the frame gate. The frame gate is the prime suspect, because
it withheld pruning from frames low in the reference pyramid, which are the ones
given the deepest search and therefore the most expensive.

### 06d and 09c

**`0009c`** — supersedes 09b. `TX_PART_STATIONARITY_MARGIN` 2 -> 1. Two measured
points both trending favourably justify testing a third, but do not establish
it: a pruning threshold must decay as it loosens. A1 binds, with 33% headroom.
`-DTX_PART_STATIONARITY_MARGIN=2` restores the known-good 09b point without a
patch edit, so 09b and 09c as two arms would bracket the curve.

Note that A2's BD-rate of 0.07% is at the edge of measurement resolution, so its
ratio of 51 is speed divided by noise and should not be read as real headroom.

**`0006d`** — supersedes 06c. Two changes, both restoring speed that 06c gave up
cheaply:

1. *Frame gate removed.* Beyond its measured cost, the structural argument was
   backwards: this heuristic profiles the **source** block, and source structure
   drives partition choice most directly when no prediction has flattened it —
   so the test is most trustworthy on intra frames, exactly the ones the gate
   disabled it on. It was protecting the case the heuristic handles best.
2. *Resolution floor relaxed for extended partitions only.* 06c raised one floor
   for every partition type, but the patch already grades them by value —
   rectangular partitions demand anisotropy 8, extended ones 4. The floor should
   grade the same way. At 1080p rectangular partitions keep the floor of 32
   while extended ones return to 16, restoring the cheaper half of the prunes
   that 06c discarded wholesale. 4K is unaffected.

**Risk to state plainly:** 06c *passes* on A1 at 20.9, and that margin is thin.
If BD-rate returns faster than speed in 06d, it could fall below the bar on a
class that currently clears it. 06c must stay in the round as a fallback arm.

### A2 is the unsolved class, and threshold work will not solve it

A2 has been ~30% worse than A1 in every variant: 18.7/14.4, then 20.9/13.5. That
consistency across two very different parameter settings is not tuning error.
The likeliest cause is the one flagged and not yet actioned: the profile is
measured on the **source**, while A2 at 33 frames spends most of its blocks on
inter prediction, where the *residual* decides whether a cut pays. A
well-predicted block with strong source structure has a flat residual, and the
heuristic cannot see that. If 06d does not move A2, the next step is a
prediction-aware profile, not another threshold.

---

## 2026-08-13 — Multi-speed data corrects two earlier conclusions; 06c and 09b

Speeds 1-3 for patches 06 and 09 arrived. Two things in the earlier analysis
were wrong and are corrected here.

### Correction 1: Class A2 is 1080p, not 4K

The 2026-08-12 entry attributed the A1-vs-A2 gap to GOP length, reading A2 as
"same content, 33 frames instead of 17". A2 is **1080p**; A1 is 4K. Resolution
and frame count are confounded in that comparison, so it does **not** establish
the propagation effect that the frame-aware gate in patch 06b was built on.

What the four presets *do* agree on is a resolution effect:

    A1 (4K)     BD 0.75% - 0.96%    speedup 12.85% - 14.85%
    A2 (1080p)  BD 1.29% - 1.50%    speedup 18.21% - 18.97%

Consistently worse BD-rate *and* higher speedup at 1080p, in four independent
measurements. The heuristic fires more often there and is more often wrong.
Block size in pixels is the likely reason: a 16x16 block at 1080p covers about
four times the scene area of one at 4K, so more blocks look directional under a
block-scale profile. Patch 06c scales the minimum block size with frame width,
which acts on A2 (floor 16 -> 32) and leaves A1 unchanged — exactly matching
which class needs the larger correction (28% vs 6.7%).

### Correction 2: patch 09 passes at Speed 4 only

The report's summary line says "PASS (Spd 2, 3, 4)". Computing each preset
against its own bar does not support that:

| Speed | Bar | A1 | A2 |
|---|---|---|---|
| 4 | 20 | 23.6 PASS | 25.2 PASS |
| 3 | 25 | -0.27% speedup, i.e. slower | 23.0 fail |
| 2 | 30 | 8.8 fail | 25.9 fail |
| 1 | 35 | 9.8 fail | 29.5 fail |

i09 passes at **Speed 4 only**, and is a net slowdown on 4K at Speed 3.
Adopting it at Speeds 2-3 on the strength of that summary would be a mistake.

### Patch 06 fails at every preset; only Speed 4 is worth targeting

Speed and BD-rate are both essentially flat across presets (12.8-14.9% / 0.75-
0.96% on A1) while the bar rises 20 -> 35. Its best ratio, 18.7, is at Speed 4
where the bar is lowest. Speeds 1-3 are unreachable and should not consume
slots.

### New patches

**`0009b`** — supersedes 09. Two changes:
1. The stationarity measurement was computed unconditionally before the
   candidate loop, and its check ran *before* the cheap eligibility tests. So
   the residual walk was paid for candidates that would be rejected anyway, and
   on blocks where every split is eliminated by other speed features and it can
   never fire. That is the most likely source of the -0.27% at Speed 3 on 4K.
   It is now lazy, runs after the cheap checks, and is skipped entirely when the
   only surviving candidate is TX_PARTITION_NONE. Expected to be strictly
   better: same decisions, less work.
2. `TX_PART_STATIONARITY_MARGIN` 4 -> 2, loosening the test from "strongest
   strip within 1.25x of mean" to "within 1.50x". Clearing a bar of 20 at ratio
   23-25 while costing ~0.1% BD-rate means the threshold sat far inside the safe
   region with speedup unclaimed. If the ratio merely held near 23, +0.33% BD
   would be worth roughly +8% speedup and still pass.

**`0006c`** — supersedes 06 and 06b. Keeps the variance floor, adds the
resolution-scaled block floor, and **narrows** the frame gate to key frames
only (layers 1 and 2 now get 4x and 2x stricter bars rather than being disabled
outright), recovering speedup that 06b was giving away for a reason the data
did not support.

### A note on the "tighten alpha by 15-20%" recommendation

Tightening reduces pruning, which lowers BD-rate **and** speedup together. The
ratio only improves if BD-rate falls *faster* than speed does, and a blunt
threshold move tends to shift both in proportion, leaving the ratio near 18.7
with less speed to show for it. Every change in 06c instead removes a class of
prune that is expensive in quality and cheap in speed — flat blocks, oversized
blocks at low resolution — which is what actually moves a ratio.

### Verified locally

Bit-signature comparison, 416x240, 16 frames, QP 110/185:

    baseline   d3b62260c8f56fb5b2eeacdf4b73730c
    0006       bc9ce9efde34073d859accbb4166b12a   differs from baseline
    0006b      aecedc46b9080213cbb8b4ff9ad20bbb   differs from both

confirming 06b's gate was live rather than inert. All patches build and encode.

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

---

## 2026-08-21 — `0025` results, and a correction about the 47%

**`0025` came back as two near-misses at Speed 3** (A1 +3.26%/+0.14% → 23.3,
A2 +4.23%/+0.17% → 24.9, bar 25) and a clear fail at Speed 4.

**Killed: Speed 4 for the whole dry-pass family.** At Speed 4 the patch gave
*less* speed and *more* damage than at Speed 3. Both halves have one
explanation: a faster preset has already pruned more of the dry pass (less left
to remove) and trusts the dry-pass ranking further through `forced_partition`
(less downstream correction of a ranking error). **Dry-pass fidelity reductions
get more expensive at faster presets.** That runs opposite to preset promotion
and means this family belongs at slower presets only.

**Per-sequence.** corr(speedup, BD) = −0.07 on A1, −0.20 on A2 — essentially
independent. On A1 the three worst sequences carry 66% of the BD-rate and 35%
of the speedup; exempting Crosswalk alone moves the aggregate from 22.8 to 29.6.
A near-miss with concentrated, uncorrelated cost is the case where splitting
works and where turning a threshold down does not.

**Correction — "no speed feature reaches the trellis" was wrong.** The profile
finding (trellis ≈ 47% of instructions; `trellis_quant.c` has zero `sf->`
references) was right; my reading of it was not. Two speed features do gate
per-block trellis — `perform_coeff_opt`/`coeff_opt_dist_threshold` and
`perform_coeff_opt_based_on_satd`/`coeff_opt_satd_threshold`, both tuned per
preset — and both are bypassed in `search_tx_type` when `tcq_enable()` is true.
That is true for luma whenever TCQ is on (the CTC configuration), and the first
call site passes `TX_CLASS_2D` as a literal so it holds for every transform
class. Luma 2D is exempt at every preset.

This is **not** a bug to reverse: under TCQ the decoder dequantises with the
state machine, so quantising a luma block scalar-only and keeping its scalar
`dqcoeff` would drift. The gates are off for a correctness reason. But it moves
the target from "turn the missing gate on" to "stop trellising candidates that
cannot win", which is `0027`.

**Added `0026`** (the `0021` split method applied along transform block size)
and **`0027`** (pre-trellis RD gate). `0027` is the larger prize: +7.7% locally,
and its exposure is confined to dropping a candidate that would have won — it
never alters the winner's coefficients, unlike `0025`.

**Recorded as a method note:** for a size split, the number that decides the bet
is not "how much BD must fall" but the *concentration* — whether the spared
members carry more BD-rate share than time share. Stated as a BD cut,
`0026`'s threshold 8 (−15%) looks easier than threshold 16 (−51%); stated as
concentration, threshold 8 needs 1.9× and threshold 16 needs 1.1×. The second
framing is the invariant one and reverses the choice of default.

**Still not done, and now blocking judgement:** the cluster's EncTime
repeatability has never been measured. Decisions this round turn on 0.1–1.7
ratio points. One anchor-vs-anchor job — the same commit submitted twice as two
arms — prices it permanently.

---

## 2026-08-25 — `0026` retired with a lesson; `0027` is the second-best result; the first bit-exact patch

**`0027` results.** Speed 3: A1 +3.79%/+0.09% → 42.1 (PASS), A2 +3.03%/+0.15% →
20.2. A1 passed in **all four** arms run (27.7–44.7). A2 binds everywhere. Only
`0021` has done better on this project.

**Direction settled by marginal ratio.** shift 3 → shift 4 has marginal ratio
11.5 (A1) and 10.8 (A2), far below each class's operating ratio, so tightening
drags the family down — which is what the shift-4 arms showed. The family must
loosen. A2 needs to give up 18% of its speedup to reach 25; the next
power-of-two step gives up 52%. Hence `0028`, a margin in 64ths, with K=8 proven
byte-identical to shift 3 so the new points sit on the measured curve.

**Killed: Speed 4 for the gate family.** A2 ratios 9.2 and 7.9, absolute
speedups 1–2%.

**`0026` retired, and the reason is worth more than the patch.** The speed model
was right (36/48% retention against 52% predicted); the quality model was
backwards. Skipping *fewer* blocks made BD-rate *worse* (+86% A1, +35% A2),
which is impossible under additive damage. Mechanism: the dry pass **compares**
transform partitionings, and at threshold 16 a 16x16 candidate is untrellised
while its four 8x8 children are trellised, biasing every such comparison toward
the split; finer TX partitioning costs rate.

> **An approximation applied uniformly across a comparison set preserves
> ordering; applied to only part of the set, it corrupts ordering. Never make
> cost-model fidelity depend on a property that varies within a single RD
> comparison.**

This also resolves why `0021` worked where `0026` failed, having looked like the
same move: `0021` **removed** candidates (survivors evaluated identically);
`0026` **kept** them at different fidelities. And it is why `0027`–`0029` are
structurally safe — they drop candidates, they never mis-cost them.

**New category opened: bit-exact patches.** The temporal-filter SSE2 result
(+1.20%/+0.34% at exactly 0.00% BD) makes the point that a bit-exact speedup has
an unbounded complexity-to-efficiency ratio and cannot fail the bar. Eleven
rounds went to approximation patches that must clear 20–35. That allocation was
wrong.

**Audit performed.** Cross-referenced every direct `_c` call against both RTCD
`specialize` lists, and a clean callgrind profile against the same lists:

- **The trellis is fully AVX2-covered and correctly dispatched.** All twelve TCQ
  kernels have AVX2 implementations, all built, all reached — the profile shows
  them by name. This closes the open question from the architecture study: the
  ~47% is real vector work, so **the only lever on it is algorithmic**, which is
  what `0027` is.
- **Temporal filtering has a second unvectorised path.** `MULTITAP_SHARP2` (12
  taps) is used only by `tf_build_predictor`, and `highbd_convolve_2d_facade_single`
  routes >8-tap filters to the C convolve. Specified in the round report; not
  shipped, because a SIMD kernel needs its unit tests.
- `av2_get_nz_map_contexts_skip` has no SIMD while its sibling has sse2.

**`0030`** (bit-exact): `av2_zero(x->winner_mode_stats)` clears 132,480 bytes of
which 131,072 (98.9%) is `color_index_map[MAX_SB_SQUARE]`, at both mode-search
call sites. Profile attributes 2.77% to memset via `av2_rd_pick_intra_sby_mode`
alone. Bit-exact on six configurations. **Speedup deliberately not quoted** —
wall-clock noise floor was ~4%, which by this project's own rule is no
measurement; instruction counts pending.

**Correction to my own profile.** `PROFILE_5d628d8_cpu4.txt` was taken on a
`gen_clip.py` synthetic clip that appears to trip the screen-content detector.
Its `av2_is_dv_valid` (1.84%) and `av2_optimize_fsc_block` (2.51%) lines are
artefacts and were used to motivate nothing. Re-profiled with screen tools off;
both vanish, and the memset lines appear with caller attribution. The trellis
lines are unaffected.

**Third request, unanswered:** the cluster's EncTime repeatability has never
been measured. This round turns on 4.8 ratio points on A2. One anchor-vs-anchor
job prices it permanently.
