# Round 0825: the gate works, the split failed for a reason worth keeping

Anchor `44be072`. Results for `0026` and `0027`, two follow-up patches, and a
reflection on the temporal-filter SSE2 result — which points at a whole class of
work this project has been ignoring.

---

## 1. `0027` is the second-best result this project has produced

| Arm | Preset | A1 (4K) | A2 (1080p) |
|---|---|---|---|
| **shift 3** | **3** | **+3.79% / +0.09% → 42.1 PASS** | +3.03% / +0.15% → 20.2 |
| shift 4 | 3 | +4.71% / +0.17% → 27.7 PASS | +5.40% / +0.37% → 14.6 |
| shift 3 | 4 | +1.14% / +0.04% → 28.5 PASS | +0.92% / +0.10% → 9.2 |
| shift 4 | 4 | +1.34% / +0.03% → 44.7 PASS | +2.06% / +0.26% → 7.9 |

**A1 passed in all four arms**, between 27.7 and 44.7 against a bar of 25/20.
A2 binds everywhere. Only `0021` (207.7 / 111.7) has done better.

### Which way to move: the marginal ratio settles it

```
A1:  (4.71 - 3.79) / (0.17 - 0.09) = 11.5
A2:  (5.40 - 3.03) / (0.37 - 0.15) = 10.8
```

Both marginal ratios are far below the ratio each class already operates at
(42.1 and 20.2). A change improves a ratio only when its marginal ratio is
*above* that ratio, so tightening the margin drags the whole family down —
exactly what the shift-4 arms show. **The direction is to loosen.**

Solving for where A2 reaches the bar, at A2's own marginal ratio of 10.8:

```
give up 0.55 of A2's 3.03% speedup  ->  (+2.48%, +0.099%)  =  ratio 25.0
the same proportional give-up on A1 ->  (+3.11%, +0.031%)  =  ratio ~100
```

An **18%** reduction in speedup. The problem is that the next power-of-two step
is nothing like 18%.

---

## 2. `0028` — a margin finer than a power of two

`patches/0028-pre-trellis-gate-fine-margin.patch`. Supersedes `0027`.

Reparameterises the margin from `>> shift` to `* K >> 6`, so settings between
the two powers of two CTC has already bracketed become reachable.

Measured locally (192x128, 4 frames, QP 185, cpu-used=3, three paired reps),
speedup retained relative to the shift-3 setting:

| margin | equivalent | speedup | retained |
|---|---|---|---|
| 8/64 | = shift 3 | +17.68% | 100% |
| **10/64** | new | +14.60% | **83%** |
| **12/64** | new (default) | +13.39% | 76% |
| 16/64 | = shift 2 | +8.44% | 48% |

The arithmetic asked for an 18% give-up — retain 82%. **K=10 retains 83%.**
The next power-of-two step gives up 52% to buy a correction that needed 18%.

### The reparameterisation is exact, and that is checked

Built at K=8 and run against the `0027` shift-3 binary on the same clip:

```
0027 shift 3     md5 bd66192aa502bbec   476 bytes
0028 MARGIN64=8  md5 bd66192aa502bbec   476 bytes
```

Byte-identical. So K=10 and K=12 sit on the same curve as the CTC points already
measured, and the new arms are directly comparable to the numbers above rather
than being a fresh unknown.

**Predicted:** if the retention transfers and BD-rate falls at the observed
marginal ratio, A2 lands near 24.6 at K=10 and near 28.0 at K=12. That comes
from a two-point fit extrapolated backwards and BD-rate cannot fall linearly as
it approaches zero, so the model certainly over-predicts at the low end. The
honest statement is **A2 somewhere in 24–30 at K=12, A1 comfortably clear in
both arms.** K=12 is the default because it has the margin.

---

## 3. `0029` — remove the bias instead of padding it

`patches/0029-pre-trellis-gate-self-calibrated.patch`. Contains `0028`'s change
too — apply one or the other, never both.

Every point on the margin curve is a different compromise between the same two
errors. `0028` picks a better point on it. This tries to move the curve.

The gate compares an *untrellised* estimate against a *trellised* `best_rd`. The
bias is however much the trellis would have lowered the estimate — and that
depends on qindex, coefficient density, block size and content. A constant
12/64 is right for none of them.

**The correction is already in hand.** Every candidate that passes the gate has
its pre-trellis estimate computed by the gate and its true post-trellis RD
computed a few statements later. Accumulate both over the candidates of the
transform block being searched:

```
alpha = sum(rd_post) / sum(pre_rd)
```

and compare `alpha * pre_rd` against `best_rd` — like with like.

De-biasing makes the estimate smaller, so calibration makes the gate **safer at
a fixed margin, not faster**. Confirmed:

| variant | margin | speedup |
|---|---|---|
| `0028` uncalibrated | 8/64 | +17.68% (the CTC point) |
| `0028` uncalibrated | 12/64 | +13.02% |
| **`0029` calibrated** | **4/64** | **+19.07%** |
| `0029` calibrated | 8/64 | +13.77% |
| `0029` calibrated | 12/64 | +9.49% |

The point is to spend that safety on a smaller margin. Default 4/64 — the
setting whose *uncalibrated* form measured A2 ratio 14.6 — on the thesis that
most of that damage was bias, not variance.

**Determinism.** The accumulators are locals in `search_tx_type`, scoped to one
transform block's candidate loop, deliberately not carried in `MACROBLOCK`. The
gate then depends only on the candidates of the block being searched — never on
tile decomposition, thread count or encode history. Same input, same output, at
any thread count. Overflow is handled by scaling both sums down until the Q12
shift is provably safe rather than by assuming a bound on RD magnitudes.

**What would falsify it:** if `0029` at margin 4 lands on the same speed/BD line
as the `0027`/`0028` points, the damage is variance and calibration is not worth
its complexity. Keep `0028` and drop this rather than tune it.

---

## 4. `0026` failed, and the reason is worth more than the patch

```
0025 (skip dry trellis everywhere)   A1 +3.26% / +0.14%     A2 +4.23% / +0.17%
0026 (skip only when short side>=16) A1 +1.17% / +0.26%     A2 +2.01% / +0.23%
```

The speed model was right — 36%/48% retention against my predicted 52%. **The
quality model was backwards.** Skipping *fewer* blocks made BD-rate *worse*:
+86% on A1, +35% on A2.

That is impossible if each skipped block contributes independent, additive
damage. So the damage is not additive, and the mechanism is visible once stated:

> The dry pass **compares** transform partitionings. At threshold 16, a 16x16
> candidate has its trellis skipped (rate over-estimated) while its four 8x8
> children keep theirs (rate accurate). Every such comparison is biased toward
> the split. Finer transform partitioning costs rate, so the bias costs BD-rate.
>
> Under `0025` both sides of every comparison are over-estimated by a similar
> factor. The bias largely cancels and the ordering survives.

**An approximation applied uniformly across a comparison set preserves ordering;
an approximation applied to only part of the set corrupts it.** Never make
cost-model fidelity depend on a property that varies *within* a single RD
comparison — and transform block size is exactly such a property.

This also explains why `0021` worked where `0026` failed, which had looked like
the same "split the family" move:

| | what it did | effect on the comparison |
|---|---|---|
| `0021` | **removed** candidates (uneven 4-way partitions) | survivors all evaluated identically — ordering intact |
| `0026` | **kept** all candidates at **different fidelities** | ordering corrupted |

And it is why `0027`/`0028`/`0029` are structurally safe: they **drop**
candidates, they never mis-cost them. Everything that survives the gate is
costed at full fidelity, so the only error channel is dropping a winner. In
`0029` alpha changes as the loop accumulates, but it still only affects
*whether* a candidate is dropped, never what a surviving candidate costs.

**`0026` is retired.** Not tuned — the threshold is not the problem.

---

## 4b. A local RD screen that complicates the story

Before spending CTC arms on `0028` and `0029` I ran a local BD-rate screen —
416x240, 4 frames, QPs 110/160/210, cpu-used=3, PSNR-YUV weighted — pairing each
variant's BD-rate against its measured local speedup:

| variant | local BD | local speedup | ratio |
|---|---|---|---|
| `0028` K=8 (= the CTC point) | 1.051% | +17.68% | 16.8 |
| `0028` K=12 (default) | **1.262%** | +13.39% | 10.6 |
| `0029` margin 4 (calibrated) | **0.858%** | +19.07% | 22.2 |
| `0029` margin 8 (calibrated) | −0.427% | +13.77% | — |

**Read this with heavy scepticism.** The absolute scale is wrong by an order of
magnitude — the K=8 arm measured 0.09%/0.15% on CTC and 1.05% here — and one arm
produced a *negative* BD-rate, which for a pruning heuristic is not credible. A
3-point quadratic fit over 4 frames of synthetic content cannot rank closely
spaced arms. I would not overturn a CTC-derived conclusion with it.

Two things are still worth extracting.

**It supports `0029`.** The calibrated arm at margin 4 shows both more speed and
less BD-rate than the uncalibrated K=8 point — dominance, not a slide along the
curve, which is exactly `0029`'s claim.

**It does not support `0028`'s premise, and there is a mechanism by which
`0028` could genuinely be non-monotone.** Loosening the margin drops fewer
candidates, which ought to be weakly better in quality. But dropping a candidate
does more than skip its trellis: the `continue` also skips the loop's
`search_level` early-break test at the bottom, which is evaluated from
`mb_plane->eobs[block]`. **So the margin changes not only which candidates are
trellised but when the candidate loop terminates**, and the effect on the search
is not monotone in the margin.

That is a hypothesis, not a finding. But it means the CTC arithmetic in §1 —
which is solid, being two real CTC points on each class — establishes the
direction, not that every intermediate point lies on the line between them.
Hence: **run `0029` first.**

---

## 5. The temporal-filter result changes what I should be looking for

The SSE2 temporal-filter patch (`0824/0002`) reported **+1.20% A1 / +0.34% A2 at
exactly 0.00% BD-rate on every metric**, having found that `TF_BLOCK_SIZE` was
`BLOCK_64X64` while the dispatch check tested `BLOCK_32X32` — so every encode had
been running the C reference.

Two things follow.

**Bit-exact speedups have an unbounded complexity-to-efficiency ratio.** Speedup
divided by zero BD-rate cannot fail the bar at any preset. This project has spent
eleven rounds on approximation patches that must clear 20–35, and almost none on
a category that clears it by construction. That allocation was wrong.

**The bug is a class, not an instance.** A kernel can exist, be declared in the
RTCD tables, be compiled, and still never run.

### The audit

I cross-referenced every direct `_c` call in `av2/encoder/` and `av2/common/`
against the `specialize` lists in both RTCD definition files, and cross-
referenced a callgrind profile against the same lists.

**Finding 1 — the trellis is not a missing-SIMD problem. This closes an open
question.**

All twelve TCQ kernels have AVX2 implementations in
`av2/encoder/x86/trellis_quant_avx2.c`, all are built, all are dispatched, and
the profile confirms it — `av2_decide_states_q1_avx2` at 7.47%,
`av2_decide_states_avx2` at 5.86%, and so on down the list. The ~47% is real
work at full vector width, not a dispatch failure.

My architecture study listed "what is the SIMD state of the TCQ decision loop?"
as the highest-value audit in the codebase. The answer is: fully covered.
**Therefore the only lever on that 47% is algorithmic — which is what `0027`
is.** That is a useful thing to have settled.

**Finding 2 — temporal filtering has a second unvectorised path, in the same
subsystem.**

`highbd_convolve_2d_facade_single()` (`av2/common/convolve.c`) routes to the C
convolve whenever the filter has more than 8 taps:

```c
// TODO(any): need SIMD for > 8 taps filters
if (filter_x_taps_gt8 || filter_y_taps_gt8 || is_intrabc) {
  av2_highbd_convolve_2d_sr_c(...);       // no SIMD
} else {
  av2_highbd_convolve_2d_sr(...);         // ssse3, avx2
}
```

There is exactly one 12-tap filter in the codec:

```c
{ (const int16_t *)av2_sub_pel_filters_12sharp, 12, MULTITAP_SHARP2 },
// "for encoder only, and now they are used in temporal filtering"
```

and exactly one user:

```c
av2/encoder/temporal_filter.c:486:  const InterpFilter interp_filters = MULTITAP_SHARP2;
```

**So `tf_build_predictor()` does all of its motion compensation in scalar C** —
for every block, against every neighbour frame in the filtering window. The
other agent's patch vectorised the *filter apply* kernel that consumes those
predictors; this is the *predictor build* that feeds it. The two stack.

This is a specified, bit-exact work item:

- Add AVX2 (and ideally NEON) implementations of `av2_highbd_convolve_x_sr`,
  `av2_highbd_convolve_y_sr` and `av2_highbd_convolve_2d_sr` for 12-tap filters,
  or a separate `_12tap` entry point.
- The `is_intrabc` leg of the same condition is screen-content only and can be
  left on C.
- Verify with the existing convolve unit tests plus a bit-exactness check —
  `experiments/bin/bitexact.sh` already does the encode-level half.

I am not shipping a half-written SIMD kernel; correctness here needs the unit
tests, and the specification above is the useful part.

**Finding 3 — one hot coefficient-context function has no SIMD at all.**

`av2_get_nz_map_contexts_skip` has no `specialize` line, while its sibling
`av2_get_nz_map_contexts` has `sse2`. It is called from three sites in
`encodetxb.c` (lines 816, 1604, 4075) on the coefficient-cost path.

**Finding 4 — everything else checked out.** The other direct `_c` calls are
correctly guarded (`bw < 8` for `av2_copy_pred_array_highbd`), are C reference
implementations calling other C reference implementations
(`av2_cdef_find_dir_dual_c`), or have no SIMD to dispatch to.

---

## 5b. `0030` — the first bit-exact patch this project has produced

`patches/0030-winner-mode-stats-skip-palette-map.patch`. Independent of
`0027`–`0029`; verified to compose with both.

The clean profile attributes **2.77% of retired instructions to libc memset
reached through `av2_rd_pick_intra_sby_mode`.** One line:

```c
av2/encoder/intra_mode_search.c:1759   av2_zero(x->winner_mode_stats);
av2/encoder/rdopt.c:9441               av2_zero(x->winner_mode_stats);
```

```
WinnerModeStats           66,240 bytes
  MB_MODE_INFO               632
  RD_STATS                    40
  color_index_map         65,536      <- MAX_SB_SQUARE, 256x256
array of 2, zeroed       132,480 bytes per call
```

**98.9% of the 132 KB cleared on every mode search is the palette colour map.**
A 4x4 luma block's intra mode search zeroes 128 KB of palette reconstruction
state before it starts. And note the profile only caught the *intra* call site —
the inter one at `rdopt.c:9441` fires far more often in a random-access encode.

The map is written before it is read: the palette search writes a winner's map
when it records that winner, and `x->winner_mode_count` — reset to zero on the
line immediately after — bounds the valid entries. The patch clears every field
except the map, field by field so that adding a member to the struct produces a
compile-time reminder rather than a silent hole.

**Bit-exact on six configurations** — two clips × three QPs spanning the CTC
ladder × two presets, every one byte-identical. That is evidence, not proof: it
shows the map is written before read on these paths, not on all paths. The CTC
run reporting 0.00% on every metric is the check to insist on, exactly as the
temporal-filter patch got.

### I am not quoting a speedup for it

Paired wall-clock gave +1.09% (sd 3.89, n=3) and −0.79% (sd 4.30, n=2) — a noise
floor near 4%, far above anything this change produces. By this project's own
rule that is **not a small speedup, it is no measurement at all**, and the rule
exists because round 1 reported six of them. Deterministic instruction counts
are running under callgrind; the number will follow. `perf` is not available on
this machine, which is why callgrind rather than `perf stat -e instructions`.

### A second memset, specified but not shipped

The same profile shows **3.18% in memset reached through
`av2_build_inter_predictors`**:

```c
if (plane == AVM_PLANE_Y)
  memset(xd->mv_refined, 0, 2 * N_OF_OFFSETS * sizeof(int_mv));
```

With `OF_BSIZE_LOG2 == 3` and `MAX_SB_SIZE_LOG2 == 8`, that is 8,192 bytes on
every luma inter-predictor build, for a block that may need two entries.
`av2_get_optflow_based_mv()` indexes `mv_refined` densely from zero over
`n_blocks = (bw/n)*(bh/n)` sub-blocks, so a bounded clear looks correct — **but
`n` can be 4 rather than `OF_BSIZE` when `use_4x4` is set, and I have not
established the worst-case bound.** Without that bound a reduced memset could
leave a read uninitialised, so it is written down here rather than shipped
half-verified.

---

## 6. A correction to my own profile

The profile in `experiments/PROFILE_5d628d8_cpu4.txt` was taken on a clip
generated by `gen_clip.py` — synthetic content with sharp multi-orientation
edges. That plausibly trips
`av2_set_screen_content_options()`'s "few luma colors" detector.

Two lines in that profile are therefore suspect and **should not be used to
motivate work**:

```
1.84%  av2_is_dv_valid          IntraBC display-vector validation
2.51%  av2_optimize_fsc_block   forward skip coding, screen-content oriented
```

On CTC natural content, IntraBC is gated off by the detector and these should be
near zero. The trellis lines are unaffected — the trellis runs on every luma 2D
block regardless of content — so the ~47% figure stands.

I have a clean re-profile running with `--enable-intrabc=0 --enable-palette=0
--enable-fsc=0`; results will follow. The general lesson is one this project has
hit before in a different form: **a synthetic clip chosen to exercise many code
paths is not a clip that exercises them in CTC proportions.**

---

## 7. What to run next

| Priority | Patch | Preset | Build | What it answers |
|---|---|---|---|---|
| **1** | `0030` | 4 | default | Is it still 0.00% BD at CTC scale? Bit-exact, so it cannot fail the ratio bar — the only question is whether the map really is written before read on every path. Cheapest arm on the board. |
| **2** | `0029` | 3 | default (`MARGIN64=4`) | Does removing the bias move the curve, or only slide along it? |
| 3 | `0028` | 3 | default (`MARGIN64=12`) | The CTC arithmetic's ship candidate. |
| 4 | `0029` | 3 | `MARGIN64=8` | Second point on the calibrated curve. |
| 5 | `0028` | 3 | `MARGIN64=10` | Second point on the fixed-margin curve. |

Nothing at Speed 4 for the gate family — A2 failed there in every `0027` arm and
the absolute speedups were 1–2%. `0030` is preset-independent, so Speed 4 is
simply the cheapest place to ask its question.

**`0029` is ranked above `0028`** despite `0028` having the cleaner arithmetic,
because the local RD screen (§4b) supported `0029`'s dominance claim and did not
support `0028`'s monotonicity premise — and because there is a plausible
mechanism (§4b) by which `0028` could be non-monotone.

If only two arms are available: `0030` and `0029`.

### Still outstanding

The cluster's EncTime repeatability has still never been measured. This round
turns on differences of 4.8 ratio points on A2, and I have no idea whether that
is inside the measurement's own spread. **One anchor-vs-anchor job** — the same
commit submitted twice as two arms — prices it permanently and costs one slot.
I have now asked for this three rounds running; it is the cheapest unresolved
question on the board.
