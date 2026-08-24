# Round 0821: two near-misses, and where the 47% actually goes

Anchor `5d628d8`. Reflection on the `0025` CTC results, plus two new patches.

---

## 1. What `0025` measured

| Preset | A1 (4K, 17f) | A2 (1080p, 33f) | Bar | Verdict |
|---|---|---|---|---|
| **Speed 3** | +3.26% / +0.14% → **23.3** | +4.23% / +0.17% → **24.9** | 25 | miss by 1.7 / by 0.1 |
| Speed 4 | +1.23% / +0.26% → 4.7 | +3.86% / +0.20% → 19.3 | 20 | fail |

The Speed-3 gap is the smallest this project has faced. A1 needs its BD-rate to
fall about 7% at constant speed; A2 needs 0.4%.

### Speed 4 is not a tuning problem, it is the wrong preset for this change

At Speed 4 the patch delivered **less speed and more damage** than at Speed 3:
+1.23% for +0.26% BD on A1, against +3.26% for +0.14% at Speed 3. Both halves
have the same explanation, and it is worth recording because it generalises.

The dry pass at Speed 4 already prunes harder, so there are fewer trellis calls
left to remove — hence less speed. And the wet pass at Speed 4 trusts the dry
pass more: more shapes are taken on `forced_partition` without re-search, so a
ranking error made in the dry pass propagates further before anything corrects
it — hence more damage.

**Dry-pass fidelity reductions get more expensive at faster presets, because
there is less downstream correction.** That is the opposite of the direction
preset promotion normally runs, and it means dry-pass approximations belong at
*slower* presets. Speed 4 should not get another arm for this family.

### Per-sequence: the cost is concentrated, and not where the speed is

Correlation between per-sequence speedup and per-sequence BD-rate:

```
A1:  r = -0.07      A2:  r = -0.20
```

Essentially independent. The sequences that pay the most quality are not the
ones giving the most speed. On A1 the three worst sequences carry **66% of the
total BD-rate but only 35% of the total speedup**.

```
A1, exempting the worst sequences entirely (aggregate ratio 22.8 as measured):
  exempt Crosswalk                              2.99% / 0.101%  ->  29.6   PASS
  exempt Crosswalk, TimeLapse                   2.63% / 0.075%  ->  35.1
  exempt Crosswalk, TimeLapse, FoodMarket2      2.12% / 0.049%  ->  43.6
```

Removing one sequence out of eight clears the bar with margin. That is the
signature of a change that is nearly right and mis-targeted at the margin, not
one that is fundamentally over-priced — and it is exactly the condition under
which splitting the change can work, as opposed to turning its threshold down.

The three expensive A1 sequences — Crosswalk, TimeLapse, FoodMarket2 — are the
high-spatial-detail ones. Detailed content is coded with finer transform
partitioning and denser coefficients, which is *consistent with* the block-size
hypothesis behind `0026` below. Consistent with, not evidence for: block size is
a within-sequence variable and these are between-sequence numbers.

---

## 2. `0026` — split the dry-pass trellis skip by transform block size

`patches/0026-tx-dry-trellis-size-split.patch`. Supersedes `0025`; apply
instead of it, not on top.

The `0021` method: when a bundled change fails on ratio, split it and keep the
members that are cheap in quality. `0021` disabled only the uneven 4-way
partitions and kept 39% of the family's speedup for 6% of its BD-rate cost —
ratio 207.7 where the whole family gave 30.6.

The split available here is transform block size. Trellis cost scales with
coefficient count, so large blocks carry the time; ranking damage does not
scale the same way, because on a small block each coefficient is a larger share
of the block's rate. So the trellis is skipped in the dry pass only when the
transform block's short side is at least `AVM_TX_DRY_TRELLIS_MIN_DIM`.

Measured locally (192x128, 4 frames, cpu-used=3, paired):

```
0025, skip everywhere              +8.00%
threshold  8, keep 4x4 only        +7.26%   (91% of 0025's speedup)
threshold 16, keep 4x4 and 8x8     +4.19%   (52% of 0025's speedup)
```

### The bet, and which arm is actually the easier one

The split improves the ratio only if the spared blocks are **over-represented
in BD-rate relative to the time they carry**. With `f_s` and `f_b` the surviving
fractions of speedup and BD-rate, the ratio clears 25 iff `f_s / f_b ≥ 1.07`:

| threshold | keeps of speed | spared blocks must carry | vs. time they carry | concentration needed |
|---|---|---|---|---|
| 8 | 91% | ≥ 17% of BD | 9% | **1.9×** |
| 16 | 52% | ≥ 53% of BD | 48% | **1.1×** |

Threshold 16 asks for much less and is the default. It is also favoured by a
second effect: those 91%/52% figures come from a 192x128 clip, and 4K codes
with larger transform blocks, so at A1 the small-block share of trellis time
should be below 48% and threshold 16 should keep more speedup than it does
locally.

**Run thresholds 16 and 8 as two arms at Speed 3.** Nothing at Speed 4.

If both land below 25, the honest conclusion is that dry-pass ranking genuinely
needs trellised costs at every block size, and this family is finished.

---

## 3. Where the 47% actually goes — I had this wrong

The profile said trellis quantisation is ~47% of instructions retired and that
`av2/encoder/trellis_quant.c` contains no speed-feature references at all. I
read that as "no speed feature reaches it". Wrong, and wrong in a useful way:
**two speed features do govern per-block trellis, and both are switched off for
the plane that carries the cost.**

In `search_tx_type`, `av2/encoder/tx_search.c`:

```c
// ~line 2411
if (tcq_enable(cm->features.tcq_mode, is_lossless, plane, TX_CLASS_2D)) {
  perform_block_coeff_opt = 1;                    // unconditionally ON
} else {
  perform_block_coeff_opt =                       // sf: perform_coeff_opt
      (block_mse_q8 <= coeff_opt_dist_threshold * qstep * qstep);
}
skip_trellis |= !perform_block_coeff_opt;

// ~line 2588
if (use_tcq) {
  skip_trellis_based_on_satd[tx_type] = skip_trellis;
} else {
  skip_trellis_based_on_satd[tx_type] =           // sf: ..._based_on_satd
      skip_trellis_opt_based_on_satd(..., coeff_opt_satd_threshold, ...);
}
```

`tcq_enable()` is true for luma whenever TCQ is on and the transform class is
2D — and the first call site passes `TX_CLASS_2D` as a *literal*, so for luma it
is true for every class. TCQ is on in the CTC configuration.

So `perform_coeff_opt` (tuned `0 → 2/3 → 3/5 → 4/6` across the presets) and
`perform_coeff_opt_based_on_satd` (`0 → 1/2`) — the only two speed features
that decide whether a block gets trellised — are live only for chroma, for 1D
transform classes, and for lossless. Luma 2D, which holds essentially all of the
coefficient mass and all of the trellis time, is exempt at every preset. That is
the whole explanation for a 47% profile line with no apparent coverage.

**This is not an oversight to reverse.** Under TCQ the decoder dequantises with
the state machine; an encoder that quantised a luma block scalar-only and kept
its scalar `dqcoeff` would drift. Forcing those gates back on for luma would be
a correctness bug, not a speed feature. They are off for a reason.

But it relocates the opportunity precisely.

---

## 4. `0027` — run the trellis only on candidates that can still win

`patches/0027-pre-trellis-rd-gate.patch`. Independent of `0025`/`0026`; those
act on the dry pass, this one acts everywhere. Verified to compose with `0026`.

The waste is not that winners get trellised — they must be. It is that **losers
get trellised too.** `av2_optimize_b`, and through it `av2_trellis_quant`, is
called inside the candidate loop: once per primary transform type, per
secondary-transform index, per transform block. Up to 16 primary types before
pruning, times the IST/stx candidates. Exactly one result is kept; the rest are
computed at full 8-state cost and discarded.

The code already believes this test is worth making. Immediately *after* the
trellis it does:

```c
// If rd cost based on coeff rate alone is already more than best_rd,
// terminate early.
if (RDCOST(x->rdmult, rate_cost, 0) > best_rd) continue;
```

which saves only the distortion call, because the trellis has already been paid
for. `0027` runs the same idea before it: cost the FP-quantised coefficients
that `av2_quant` has already produced, take the transform-domain distortion, and
drop the candidate before the trellis if it cannot come near the incumbent.

The margin matters. The estimate is *pessimistic* about the candidate — the
trellis exists to lower rate, so the candidate's true post-trellis RD is below
this figure while `best_rd` is already post-trellis. Comparing them directly
would prune candidates that would have won. `AVM_TX_PRE_TRELLIS_GATE_SHIFT` sets
how much of that bias is forgiven.

### Measured locally

192x128, 4 frames, QP 185, cpu-used=3, three paired reps against the same
baseline binary:

| shift | drops candidate if | speedup | reps |
|---|---|---|---|
| 2 | 33% worse | **−2.68%** | −3.44, −2.63, −1.98 |
| 3 | 14% worse | **+7.73%** | +7.43, +7.86, +7.91 |
| 4 | 7% worse | **+7.02%** | +6.93, +6.18, +7.97 |

The shift-2 row is the informative one, and it is why this patch has a real
floor rather than an asymptote at zero. The gate is not free: `cost_coeffs`
plus `dist_block_tx_domain` on every candidate costs about **2.7% of total
encode time**. At a 33% margin the gate fires too rarely to repay that, and the
patch is a net slowdown. Above shift 2 it repays several times over. Shifts 3
and 4 are inside each other's spread on this clip, which does not separate them.

All variants produce distinct bitstreams and all decode cleanly under `avmdec`.

### Why this one cannot cause a mismatch

The gate only ever executes `continue` on a losing candidate. It never changes
how the winner is quantised, trellised, costed or reconstructed, and it modifies
no state that outlives the iteration — it runs *before* `av2_optimize_b` touches
`dqcoeff`, `eobs` and `txb_entropy_ctx`, so it perturbs strictly less than the
existing `continue` two statements later.

The quality cost is confined to one channel: a candidate that would have won may
be dropped. That is a narrower exposure than `0025`, which changed the
coefficients that were actually kept.

### What would falsify it

The premise is that a candidate whose untrellised RD is far above the incumbent
would not have won after trellising. **If BD-rate comes back above roughly 0.3%
at shift 3, that premise is wrong** — the trellis is reordering candidates, not
merely improving them — and the right response is to retire the patch, not tune
it, because the only safer setting (shift 2) cannot pay for its own overhead.

---

## 5. What to run next

| Arm | Patch | Preset | Build |
|---|---|---|---|
| 1 | `0026` | 3 | default (`MIN_DIM=16`) |
| 2 | `0026` | 3 | `-DAVM_TX_DRY_TRELLIS_MIN_DIM=8` |
| 3 | `0027` | 3 | default (`SHIFT=3`) |
| 4 | `0027` | 3 | `-DAVM_TX_PRE_TRELLIS_GATE_SHIFT=4` |

`0027` carries the larger prize and the cleaner safety argument; if only two
arms are available, run arms 3 and 4.

Nothing at Speed 4 this round.

### Still outstanding, and it is starting to cost us

The cluster's EncTime repeatability has never been measured. Two of this
round's four decisions turn on differences of 0.1–1.7 ratio points, and I have
no idea whether that is inside the measurement's own spread. **One
anchor-vs-anchor job** — the same commit submitted twice as if it were two arms
— would price it once and for all. It costs one slot and would retroactively
tell us whether `0025`'s A2 result (24.9 against a bar of 25) was a miss or a
coin flip.
