# Why patch 0006e collapsed on the two-pass base

> **CORRECTION, added after the ea89c216 result.**
>
> Rebased onto `ea89c216` -- two commits after `45dc128163`, neither of which
> touches partition search -- 0006e measures **A1 +2.96% / 0.11% (ratio 26.9)
> and A2 +2.81% / 0.09% (ratio 31.2)**, passing the Speed-4 bar of 20 on both
> classes. Against `45dc128163` it measured +0.73% / +1.64% and failed both.
>
> The numbers below, and the "~70% of the effect eliminated" figure this
> document is built around, come from the `45dc128163` run. That run no longer
> looks trustworthy:
>
> * the rebased patch is byte-identical on both bases (verified by 3-way apply);
> * neither intervening commit touches `partition_search.c` or `encodeframe.c`;
> * the base getting faster explains 1% of the A1 gap and 7% of the A2 gap;
> * **A1's BD-rate is identical on both (0.11%) while its speedup quadrupled** --
>   and a patch making the same decisions cannot save four times the work.
>
> **What survives:** the three mechanisms in sections 2-4 are read from source
> and remain correct. The wet pass really does set `forced_partition` for every
> block of 32x32 and larger, so 0006e really is a no-op there; the size grading
> really does point away from where the dry pass works; the frame gate really
> does anti-correlate with two-pass being inter-only.
>
> **What does not survive:** the *magnitude* attributed to them, and therefore
> the conclusion in section 8 that 0006e should be retired. On the ea89c216
> numbers 0006e is the first 06 variant ever to clear the bar on both classes.
> Treat section 8 as suspended pending a repeat measurement, not as a finding.
>
> The decisive experiment is one arm: re-run 0006e on `45dc128163`. If the
> number moves, it was measurement. See the end of this document.


Analysis of `0006e-orientation-pruning-size-graded` against
`45dc128163` ("Two-pass superblock partition search", #5253), which replaced
`d6b40b7893` as the anchor.

---

## Direct answer

**Largely no longer effective, and the reason is structural rather than
tunable.** Three separate mechanisms in the new base each remove part of 0006e's
scope, and they compound. Threshold tuning cannot recover it, because the
problem is not that 0006e prunes too much or too little — it is that the work
0006e used to remove is no longer being done in the place 0006e removes it from.

There is a nearly-free repair worth taking (§5a), a risky one (§5b), and one
genuinely new opportunity that the two-pass structure itself creates (§5c). But
the honest ceiling after repair is well below the original numbers.

## 1. What actually changed

    on d6b40b7893   A1  +2.82% / 0.10%  ratio 28.2      A2  +5.20% / 0.10%  ratio 52.0
    on 45dc128163   A1  +0.73% / 0.14%  ratio  5.2      A2  +1.64% / 0.14%  ratio 11.7

    speedup retained: A1 26%, A2 32%.  Both classes passed the Speed-4 bar of
    20 before; both fail now.

Two-pass itself is a large partition-search speedup: at Speed 4 it reports
EncTime 86% on A1 (+14.0%) and 73% on A2 (+27.0%).

## 2. Mechanism 1 — the `forced_partition` guard makes 0006e a no-op on large blocks

The first executable line of 0006e's pruning function is

```c
if (part_search_state->forced_partition != PARTITION_INVALID) return;
```

and the new base defines wet-pass shape reuse as exactly that condition
(`av2_set_two_pass_flags`, `encoder_utils.c`):

```c
const bool is_wet_pass_reuse =
    (fast_two_pass && multi_pass_mode == SB_WET_PASS && part_search_state &&
     part_search_state->forced_partition != PARTITION_INVALID);
```

The wet pass "keeps the first pass's shape for blocks of 32x32 and larger", so
every such block carries `forced_partition` set. **0006e therefore returns
immediately, without evaluating anything, for every block of 32x32 and larger in
the expensive pass.** Its remaining wet-pass scope is blocks below 32x32 — and
its own floor is 16, so that means 16x16 only.

This was not a latent bug: under a single-pass search the guard is correct, since
a forced partition has nothing to prune. The new base gave the same condition a
second, much broader meaning.

## 3. Mechanism 2 — the size grading now points the wrong way

0006e requires more evidence on larger blocks: base anisotropy below 32, ×2 at
32–63, ×4 at 64 and above. That grading was derived from marginal ratios on the
old base and it was right there — a wrong prune on a 128x128 block commits 64×
the area of the same mistake on a 16x16.

Under two-pass, sizes ≥32 are decided in the **dry** pass. So 0006e is now at its
most conservative precisely where it still has scope, and its surviving prunes
are concentrated at 16x16 — the cheapest blocks in the tree. The grading and the
new structure are pulling in opposite directions.

## 4. Mechanism 3 — the frame gate anti-correlates with two-pass

`set_two_pass_partition_level()` carries the comment *"Both regimes only ever run
on inter frames"*. So intra/key frames still use the full single-pass search,
where 0006e's original economics apply unchanged.

0006e's frame gate returns 0 — pruning fully disabled — at `layer_depth <= 0`,
which is the key frame. **The frames where two-pass is off are exactly the frames
where 0006e turns itself off.** The two gates are almost perfectly
anti-correlated, and neither knows about the other.

## 5. The diagnostic that matters: BD-rate went UP

This is the part worth dwelling on, because it rules out the comfortable
explanation.

    A1: lost 2.09% of speedup while gaining 0.04% of BD-rate cost
    A2: lost 3.56% of speedup while gaining 0.04% of BD-rate cost

A patch that prunes *less* should also cost *less* quality. This one prunes far
less and costs more. That is not dilution — it is the signature of the surviving
prunes having **more leverage over the final decision**, not less.

The mechanism is in the commit description. The wet pass trusts the dry pass's
shape for ≥32 blocks; only blocks the dry pass left **unsplit** are re-searched
(*"a reduced tool set tends to under-split them"*). So:

- A dry-pass prune that changes *which split wins* is **irreversible** — the wet
  pass adopts that shape and never revisits it.
- Only a prune that leads to *no split at all* gets a second look.

Under the old single-pass search, an 0006e prune removed one candidate from a
search that continued with full tools and could still recover. Under two-pass, an
0006e prune steers a shape decision that the expensive pass then trusts. **The
cost/benefit inverted: the savings became cheap (reduced-tool dry-pass time) and
the errors became expensive (trusted, unrecoverable shape).**

## 6. The strategic reason: two-pass and 0006e are substitutes

Both attack the same waste — evaluating partition shapes that will not win.

- 0006e predicts which shapes are worth evaluating from **source structure**, a
  heuristic proxy computed before any RD is known.
- The dry pass finds out by **measuring**, cheaply, with a reduced tool set.

A measured proxy dominates a heuristic one. The dry pass is, in effect, a better
implementation of what 0006e was trying to do, and it captured the same
redundancy more accurately. That is why ~70% of 0006e's effect disappeared rather
than some of it: the two are not complementary optimisations that stack, they are
competing solutions to one problem, and the better one landed.

This is worth stating plainly because it bounds every repair below. 0006e is not
recovering its old numbers on this base under any threshold setting.

## 7. Can it be repaired, and how far

### (a) Un-gate the frames two-pass does not touch — cheap, low risk, do this first

Two-pass is inter-only. 0006e's frame gate disables it on key frames. Make the
frame gate conditional on the two-pass level: when
`two_pass_partition_search == TWO_PASS_PART_OFF` for the current frame, drop the
gate and let 0006e run with its original, validated single-pass behaviour.

This is purely additive — it restores 0006e exactly where its old measurements
still apply, and changes nothing where two-pass is active. Key frames are a small
fraction of a GOP but disproportionately expensive, so the gain is real though
modest. Expected: a few tenths of a percent, at essentially no BD-rate risk.

### (b) Make it pass-aware and drop the size grading in the dry pass — risky

`x->apply_dry_pass_shortcuts` (new in `block.h`) identifies the dry pass, so
0006e can behave differently in each. The obvious move is to remove the size
penalty in the dry pass, since ≥32 decisions now happen only there.

**I would not run this before (a) and (c).** §5 says dry-pass prunes are
higher-leverage, not lower — the wet pass trusts them. Pruning *more*
aggressively in the dry pass increases exactly the class of error that already
pushed BD-rate from 0.10% to 0.14%. It might buy speed, but it is pushing on the
side of the trade that is already losing.

### (c) Gate the wet-pass re-search of unsplit large blocks — the real new opportunity

The commit is explicit that this step is the speed/quality knob of the whole
feature:

> That re-search is limited to blocks up to 128 and stops at 8x8, which keeps
> most of the time saved.

and in the code:

> Without a cap it recovers a bit of quality but gives back most of the
> speedup.

So the wet-pass re-search of blocks the dry pass left unsplit is a **known-large,
full-tool, speculative cost** that the authors capped by block size because they
had no better signal for which re-searches are worth doing.

The orientation profile is a better signal than a size cap. If the source is
strongly directional along one axis, the re-search only needs to consider splits
along that axis rather than both. This applies the signal where the economics are
now favourable — an expensive full-tool search rather than a cheap ranking pass —
and it has a fallback, since the dry pass's unsplit shape remains valid if
nothing beats it.

This is the one place on the new base where the orientation signal has a clear
comparative advantage, and it is a different patch from 0006e rather than a
retuning of it.

## 8. Recommendation

1. **Do not spend a CTC round retuning 0006e's thresholds.** The collapse is
   structural; no anisotropy or block-size setting addresses any of the three
   mechanisms above.
2. **Take (a).** It is small, safe, and restores validated behaviour on the
   frames two-pass ignores.
3. **Build (c) as a new patch**, not as 0006e-next. It targets the new base's own
   most expensive speculative step with a signal better suited to it than the
   block-size cap currently used.
4. **Retire 0006e as a general partition speedup.** On this base its job has
   largely been done by a better mechanism. That is a good outcome for the
   encoder even though it is a negative result for the patch.

I can build (a) and (c) against `45dc128163` on request. Note that 0006e itself
no longer applies cleanly to that commit (`partition_search.c:5533` conflicts),
so both would be authored fresh against the new base rather than rebased.


---

## 9. Addendum: the ea89c216 result and what it means

    45dc128163   A1 +0.73% / 0.11%  ratio  6.6    A2 +1.64% / 0.15%  ratio 10.9
    ea89c216     A1 +2.96% / 0.11%  ratio 26.9    A2 +2.81% / 0.09%  ratio 31.2

Two commits apart: `d8b1854` (refactor ccso search) and `ea89c21` (fast warp
delta search). Neither touches partition search.

### Ruling out the mechanical explanations

**The patch is the same.** Applying 0006e with `--3way` to each base produces a
byte-identical 302-line diff. 0006e conflicts on both (`partition_search.c`),
but the resolution is forced and identical, because nothing between the two
commits touches the file.

**Base rescaling does not explain it.** `ea89c21` reports its own speedup as A1
+2.4%, A2 +4.7%. If 0006e's absolute saving were unchanged, its percentage would
scale by 1/(1-f):

    A1: 0.73% -> 0.75% predicted, 2.96% observed   (explains 1% of the gap)
    A2: 1.64% -> 1.72% predicted, 2.81% observed   (explains 7% of the gap)

To explain the A1 result by rescaling alone the base would have to have become
75% faster, and A2's would need 42% -- different amounts, from two commits that
report a few percent.

**The BD-rate is the tell.** BD-rate is a function of the encode decisions. On
A1 it is identical across the two bases (0.11% and 0.11%) while the measured
time saving quadrupled. A patch making the same decisions cannot save four
times the work. On A2 the BD-rate did move (0.15% -> 0.09%), so some decisions
genuinely changed there -- plausibly because the warp change alters mode costs
and therefore which blocks the orientation test fires on -- and A2's speedup
ratio moved less (1.7x against A1's 4.1x). The two classes are consistent with
A2 containing a partly real effect and A1 being dominated by measurement error.

### The insight worth keeping

The uncertainty band on this cluster's EncTime is wider than the effect being
measured. That is the same failure that has now bitten this project three
times: round-1 wall-clock screening with a 3.5% minimum detectable effect, the
101-feature study's +-1% floor with a sign-flipping per-preset bias, and now a
2.2-percentage-point swing across a change that touches none of the relevant
code.

Every conclusion in this project that rests on a single timing run should be
treated as provisional, including the ones I have written confidently.

### Recommended next steps

1. **Re-run 0006e on `45dc128163`.** One arm. If it reproduces 0.73%, something
   real happened between the commits and it is worth finding. If it comes back
   near 2.96%, the first run was wrong and 0006e is alive.
2. **Characterise the cluster.** Run the *same* configuration twice and report
   the spread. Every ratio in this project is a quotient whose denominator's
   uncertainty has never been measured. This is one job and it makes every
   future result interpretable.
3. **Do not retire 0006e yet.** On the ea89c216 numbers it clears the bar on
   both classes -- something no 06 variant has done, including 06c which passed
   only A1. If that number holds, 0006e is finished work rather than a dead end.
