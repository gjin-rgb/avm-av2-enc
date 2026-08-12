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
