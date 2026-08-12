# AV2 encoder speed-up patch set

Ten standalone encoder speed features for the AVM reference encoder, developed
against `av2-enc` (`a341351`). Each patch is independent: it applies to a
pristine `av2-enc` checkout on its own, touches one optimisation aspect, and
builds clean.

```
git checkout av2-enc
git apply patches/0006-ext-partition-texture-pruning.patch
```

All ten have been verified to apply individually with `git apply --check`
against unmodified `av2-enc`, and each was compiled and run before and after.

---

## Results

Measured at `--cpu-used=4`, single-threaded, 4 QPs (85/110/145/175), 4 frames of
a 416×240 clip. BD-rate is PSNR-YUV weighted (6·Y + U + V)/8, Bjøntegaard cubic
fit. Runtime is user CPU time, minimum of 7 samples per binary (see
*Measurement* below for why the minimum).

| # | Patch | Runtime | BD-rate | Ratio | s4 bar (≥20) |
|---|-------|--------:|--------:|------:|:---:|
| 01 | TX-type saturation exit | −6.40% | **−0.900%** | ∞ | pass |
| 02 | IST sparse coeff restore | −0.75% | **0.000%** | ∞ | pass |
| 03 | PMC arena allocation | −0.63% | **0.000%** | ∞ | pass |
| 04 | Sub-pel curvature gate | −1.88% | **−0.597%** | ∞ | pass |
| 05 | Full-pel search memo | −0.93% | **0.000%** | ∞ | pass |
| 06 | Orientation partition pruning | **−16.53%** | +0.105% | 158 | pass |
| 07 | RD-density termination | −2.47% | **−0.338%** | ∞ | pass |
| 08 | DRL dispersion budget | −1.80% | +0.016% | 111 | pass |
| 09 | TX-partition stationarity | −6.63% | +0.001% | 5368 | pass |
| 10 | Adaptive ME search range | −1.67% | **−0.190%** | ∞ | pass |

A negative BD-rate means less rate for the same quality. Patches 02, 03 and 05
produced **byte-identical bitstreams at every QP**, so their BD-rate is zero by
construction rather than by measurement, and their ratio is unbounded whatever
the speed-up is. Patches 01, 04, 07 and 10 came out ahead on both axes on this
clip, which makes their ratios unbounded too — but see the caveats.

### What these numbers do and do not establish

**They are a single 416×240 synthetic clip, 4 frames, 4 QPs — not a CTC
evaluation.** Treat every BD-rate figure as indicative. Specifically:

- **The BD-rate gains are the least trustworthy entries.** A pruning feature
  improving compression is plausible — the RD search is greedy, so changing what
  it visits can change what it finds — but it is not a property to rely on.
  Digging into patch 01's −0.90%, most of it comes from chroma, and this clip's
  chroma is an unnaturally smooth function of its luma. That one in particular
  is likely an artefact of the content.
- **The keyframe dominates the timing.** At 4 frames roughly half the measured
  CPU time is the single intra frame, which flatters intra-affecting patches and
  dilutes the inter-only ones (04, 05, 08, 10). Their true share of a long
  sequence will be larger than shown.
- **Only patches 06 and 09 have effects comfortably above the measurement
  floor.** The sub-1% figures (02, 03, 05) are at the edge of what this
  environment can resolve; their *direction* is trustworthy because those three
  patches provably only remove work, but their magnitude is not precise.

Before adopting any of these, re-run them over the AOM CTC set.

### Measurement

Runtime measurement had to be redone twice. Wall-clock timing was unusable — the
same binary measured 71 s and 97 s on different occasions. User CPU time is
repeatable to about 1% back-to-back, but drifts far more than that over the
length of a sweep: in the RD sweep, binaries producing byte-identical bitstreams
to the baseline appeared 3% *slower* at one QP and 8% *faster* at another, purely
from when they happened to run. Two-repetition pairing did not fix it either
(spreads up to 8 percentage points between repetitions).

The figures above therefore come from a round-robin pass — every binary measured
once per round, so all of them see the same fast and slow stretches — pooled
with the paired pass, reduced by **minimum** rather than mean. Under contention
the minimum is the observation closest to the true uncontended cost and is not
dragged around by occasional slow runs. Sample counts are held equal between
each variant and its baseline, since a minimum over more samples is
systematically lower. Residual baseline spread after this treatment: 6%
(86.44 s … 91.24 s over 7 samples).

Rate and PSNR need none of this care: the encoder is deterministic, so those
columns carry no measurement error at all.

### Composing the patches

The ratios above are per-patch against the baseline, which is how the threshold
bars are defined. Several patches prune overlapping work — 01 and 09 both act on
the transform search, 06 and 07 both on partitions — so their speed-ups do not
simply add up. The stack was therefore measured rather than extrapolated:

| | Runtime | BD-rate | Ratio |
|---|--------:|--------:|------:|
| **All ten together** | **−27.78%** | **−0.630%** | ∞ |

(Naively multiplying the individual figures would have predicted ~34%, so the
overlap costs about 6 points, as expected.)

**Patches 05 and 10 conflict textually.** Both add code to
`motion_search_facade.c` in the same region, so applying 10 after 05 fails. They
are independent in substance — one memoises search results, the other picks the
starting radius — and the conflict is a few lines of context. The combined build
measured above resolves it by hand. Every other pair applies in any order.

---

## What each patch does

### 0001 — Saturation-based exit for the transform type search
*Aspect: RDO pruning / transform pruning · `av2/encoder/tx_search.c`*

`search_tx_type()` walks a prior-ranked list of up to 16 transform kernels. The
running best RD therefore forms a decreasing sequence that flattens out quickly,
but the loop keeps walking the list regardless.

The patch adds a stopping rule built from statistics the search already
produces: the number of candidates that reached an RD evaluation, the run of
consecutive candidates that failed to improve, and the size of the last accepted
improvement. The tolerated stale run shortens when the last win was marginal
(below ~1.6 % of the best cost — the sequence has converged) and again when the
winning candidate produced a very compact residual (`eob <= max_eob/8` — the
transform already concentrated the energy, so the remaining kernels have little
left to gain). The starting budget follows the preset aggressiveness that the
existing `search_level` already encodes, so conservative presets keep searching
longer before declaring saturation.

### 0002 — Sparse coefficient-buffer handling in the secondary transform search
*Aspect: memory & buffer locality · `hybrid_fwd_txfm.c`, `encodemb.c`, `block.h`*

**Bit-exact.** For every IST (secondary transform) candidate — up to 7 sets × 3
kernels per primary kernel — `av2_xform()` restored the full primary coefficient
block into `coeff` (up to 4 kB), and `av2_fwd_stxfm()` then immediately wiped the
whole block with a `memset` before writing back only ~32 coefficients.

The patch removes both. The secondary transform now reads its primary
coefficients directly from the saved copy, and the clear is scoped to the
handful of scan positions the previous candidate actually wrote. Per candidate
this replaces ~8 kB of memory traffic with ~128 bytes. The retained-position
state is tracked per plane and keyed on the scan order and block offset, so a
stale descriptor can never be used.

Verified bit-identical to the baseline bitstream.

### 0003 — Single-arena allocation for `PICK_MODE_CONTEXT`
*Aspect: memory & buffer locality · `av2/encoder/context_tree.c`*

**Bit-exact.** A pick-mode context owns a fixed set of small per-4×4-block
arrays whose sizes are known as soon as the block size is, and which are never
resized. It was built from 17 separate `calloc`/`memalign` calls and torn down
with 17 `free`s — and the partition search creates and destroys contexts
constantly, since every rectangular, split and extended trial rebuilds a
subtree.

The patch lays the context and all of its arrays out in one arena: one
allocation, one free, and metadata that is always walked together now sits
contiguously. The zero-initialisation of the previously `calloc`'d regions is
reproduced exactly, and the `memalign`-backed arrays keep their 32-byte
alignment.

Verified bit-identical to the baseline bitstream.

### 0004 — Curvature gate on the sub-pixel refinement ladder
*Aspect: motion estimation · `av2/encoder/mcomp.c`*

The sub-pel search descends a fixed ladder (½ → ¼ → ⅛ pel), each level costing
several upsampled predictions. Near its minimum the prediction error behaves
like a locally quadratic surface, so a level of step *h/2* can recover at most
about a quarter of what the level of step *h* just recovered.

The patch measures the gain each level actually produced and stops descending
once that gain is a negligible fraction of the error that remains — every finer
level is then provably negligible too. A level that fails to move the MV at all
identifies the centre as a local minimum at that scale and is terminated by the
same test. Applied to `av2_find_best_sub_pixel_tree` and both pruned variants,
covering presets 0 through 4.

### 0005 — Memoisation of full-pixel motion searches within a block
*Aspect: motion estimation + memory · `motion_search_facade.c`, `block.h`*

**Bit-exact.** One inter mode is evaluated over and over for a single block —
across DRL indices, MV precisions, BAWP on/off and refined-MV on/off — and many
of those iterations pose an *identical* full-pixel search problem, which is by
far the most expensive part of evaluating them.

The patch adds a small direct-mapped memo, bound to the current block, keyed on
everything that can change the search outcome: the source and reference buffer
pointers and strides (which stand in for block position and reference frame),
the block's search limits, the reference MV, the starting MV, the step
parameter, the optical-flow iteration count, precision, motion mode and the
adaptive-MVD flag. A hit is therefore an exact substitute for re-running the
search, not an approximation of it.

Verified bit-identical to the baseline bitstream.

### 0006 — Orientation-gated partition pruning
*Aspect: partition search acceleration · `partition_search.c`, `encodeframe_utils.h`*

A vertical cut can only pay off when the block content changes from left to
right, and a horizontal cut only when it changes from top to bottom.

Per-pixel gradients turn out to be a poor guide here: fine texture excites the
horizontal and vertical gradient about equally, so on detailed content their
ratio stays near one no matter how the block is structured. (An initial gradient
version of this patch fired on 44 % of the blocks it examined yet changed
nothing, because everything it pruned was already pruned.) What a partition can
exploit is variation at the scale of its own sub-blocks, and that is what the
*mean profiles* capture: averaging each row collapses horizontal detail and
leaves the top-to-bottom structure a horizontal cut could separate; averaging
each column leaves the left-to-right structure a vertical cut could separate.

The patch measures the variance of both profiles on a ≤32×32 sampling lattice
and drops the partitions cutting across the dominant axis. The rectangular
partitions demand markedly stronger evidence (8×) than the extended ones (4×),
since they carry far more of the coding gain. The test is one-sided, so
comparable variances — including a flat block's degenerate all-zero case — prune
nothing.

### 0007 — Parent-normalised RD-density termination
*Aspect: partition search acceleration · `partition_search.c`, `context_tree.{c,h}`*

When `PARTITION_NONE` wins for a block, its RD cost per pixel is known.
Normalising by area makes that figure directly comparable with the parent's,
which covers a larger region: an even split of the parent's cost would leave
every child at the parent's density, so a child well below it sits in a part of
the frame that is intrinsically easier than its surroundings. The absolute RD
still available to a finer split of such a block is bounded by its already small
total cost, while each extra partition level keeps costing signalling.

The patch records the density on each `PC_TREE` node and, when a child comes in
at less than half its parent's, drops the rectangular and extended trials —
deliberately leaving `PARTITION_SPLIT`, by far the most valuable of the
remaining options, alone. Being a ratio of two RD costs, the test carries no
absolute scale and adapts to content and quantiser on its own.

### 0008 — Dispersion budget for the DRL dimension
*Aspect: RDO pruning · `av2/encoder/rdopt.c`*

Successive DRL indices differ only in which reference MV the motion vector is
coded against. When the indices evaluated so far all land on practically the
same RD cost, the search is converging to the same motion regardless of the
predictor and the residual rate differences between predictors are immaterial —
so the indices still to come cannot change the outcome either.

The patch tracks the best RD each DRL index reaches (recorded before the global
best filters it out, so the measurement covers every index and not just the
improving ones) and stops the loop once two or more indices have been evaluated
and their spread is under ~1.5 % of the best cost seen. Measuring the *spread*
rather than the trend makes this a statement about the candidate distribution
itself, and expressing it as a ratio keeps it free of any absolute RD scale.

### 0009 — Residual-stationarity pruning of transform partitions
*Aspect: transform pruning · `av2/encoder/tx_search.c`*

Splitting a transform block trades away the coding gain of one large transform
in exchange for describing the pieces separately, and pays the rate of
signalling the split on top. That trade can only pay off when the residual is
non-stationary along the split axis: if its energy is spread evenly, every piece
sees the same statistics the whole block did, so the split gives up transform
gain and buys nothing back.

The patch divides the residual into four strips along each axis and compares the
strip energies against their mean. Horizontal partitions are governed by the
variation across rows and vertical ones by the variation across columns, so an
axis whose strips agree to within 25 % has its partitions dropped;
`TX_PARTITION_SPLIT` needs both axes to be stationary. `TX_PARTITION_NONE` is
never dropped, so a candidate always remains. Scoped to the inter path, where
the residual is available before the search begins.

### 0010 — Motion-field-adaptive full-pixel search range
*Aspect: motion estimation · `av2/encoder/motion_search_facade.c`*

The step search starts from a radius wide enough to cover the worst-case motion
anywhere in the frame and works inwards, spending several rounds of SAD
evaluations before it even reaches the neighbourhood of the predictor. That
width only earns its keep when the true motion may genuinely be far from the
predictor.

The reference MV stack is precisely the local motion field — the motion the
spatially adjacent blocks settled on — so its spread directly measures how far
the search plausibly has to travel. The patch maps that spread to the number of
coarse step levels the search can give up, gently: a spread of ≤8 full pixels
gives up one level, ≤3 gives up two, and only a field whose predictors are
practically identical gives up three. Temporal and rescaled stack entries are
excluded, since they say nothing about the local field, and the fine levels that
do the actual locating are never touched.
