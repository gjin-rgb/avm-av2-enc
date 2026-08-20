# A measured profile of the encoder, and what it says we've been missing

Anchor `5d628d8` (current `origin/av2-enc`). Callgrind, `cpu-used=4`, RA,
192x128 2-frame clip, 191 billion instructions retired.

---

## 1. The headline

**Trellis quantisation is ~47% of the encoder.**

```
av2_trellis_quant                     9.95%
av2_decide_states_q1_avx2             7.47%
av2_decide_states_avx2                5.86%
av2_optimize_txb_new                  3.72%
av2_get_rate_dist_def_luma_q1_avx2    3.31%
av2_update_states_avx2                3.28%
av2_get_rate_dist_def_luma_avx2       3.17%
av2_find_best_path_avx2               2.89%
av2_update_nbr_diagonal_avx2          2.86%
av2_optimize_fsc_block                2.51%
                                     ------
                            TCQ total ~47%

forward transform                      6.2%
memory clearing (memset)               5.3%
coefficient rate estimation            3.6%
transform-type search                  2.0%
```

For scale: quantisation is larger than partition search, mode search and the
loop filters **combined**. And **every patch this project has produced —
mine, GPT's, and every promotion — has attacked something else.**

Twenty-four patches into this effort, we have been optimising around the edges
of the thing that actually dominates.

### The sharpest detail

```
$ grep -cE "cpi->sf\.|sf->" av2/encoder/trellis_quant.c
0
```

**`av2_trellis_quant` contains no speed-feature references at all.** It runs at
full strength at every preset, and at every stage of the search — including
inside loops whose only purpose is to *order* candidates before the real
decision is made.

That is not an oversight to fix with a flag. It is a structural gap: the
encoder has a rich speed-feature vocabulary for partitions, modes, transforms
and filters, and none of it reaches the single most expensive routine.

### Why the 101-feature study missed this

That study measured `disable_tcq` at +13.09% time for +1.31% BD-rate, ratio
10.0, and filed it as a failure. Both numbers are consistent with what I now
see, and the conclusion drawn from them was wrong in an instructive way.

`tcq_mode` is a **bitstream feature**, not a search knob:

```c
int use_tcq = tcq_enable(cm->features.tcq_mode, lossless, plane, tx_class);
if (use_tcq) return av2_trellis_quant(...);
else         return av2_optimize_txb_new(...);
```

Turning it off doesn't stop the encoder quantising — it switches to
`av2_optimize_txb_new`, which is itself 3.7% and does similar work. So
`disable_tcq` gave up a coding tool (hence 1.31% BD-rate) while recovering only
part of the cost. **The opportunity was never "turn TCQ off". It is "stop
running the full trellis on candidates that are about to be discarded."**

## 2. Two patches

Both target the same structural pattern the two-pass partition search already
established for shapes: **rank cheaply, decide properly.**

### `0023` — cut the dry pass down to tools that rank *(verified to fire)*

The two-pass commit says its first pass "only has to rank shapes against each
other, not produce the final coding decision", and expresses its tool reduction
as an 18-field `DryPassCfg`. Nobody has tuned those fields. Three are doing more
than ranking needs:

**Warp modes bypass the dry pass's own reduction.** `motion_mode_mask` restricts
it to `SIMPLE_TRANSLATION`, but:

```c
if (base_mbmi.mode == WARPMV || base_mbmi.mode == WARP_NEWMV) {
  modes_to_search = allowed_motion_modes;     // full set
} else if (x->apply_dry_pass_shortcuts) {
  modes_to_search = cfg->motion_mode_mask;    // SIMPLE_TRANSLATION
}
```

The bypass is correct on its own terms — `WARPMV` asserts
`motion_mode == WARP_DELTA` — but it makes these two modes the **only** ones in
the dry pass running the full motion-mode set, including `WARP_DELTA`'s nested
`warp_ref_idx × precision` and `warp_ref_idx × mvd_flag × inter_intra` loops.
Every other mode gets one trial. The dry pass's most expensive inner loops
belong to the two modes its own reduction cannot reach.

Also: **3 MV precisions → 1** (a within-shape refinement that shifts every
candidate's MV cost the same way), and **wedge dropped** (itself a shape
decision, evaluated inside the shape-choosing pass).

In all three, the wet pass still runs the full tool on the surviving shape — so
what is given up is *ranking fidelity*, not final quality.

**Verified:** bitstream md5 changes (`615ff9e8…` vs `c2a7fb30…`). Local runtime
was flat, but my clip is 1 key + 1 inter frame and two-pass is inter-only, so
the key frame dominates. Magnitude needs CTC.

### `0024` — rank transform types without the trellis *(NOT verified — read this)*

`prune_txk_type` and `prune_txk_type_separ` evaluate transform-type candidates
purely to order them into `txk_map`. Both pass a literal `1` for
`use_optimize_b`, forcing the trellis on for every candidate — independently of
the `skip_trellis` decision every other `av2_setup_quant` call site honours.

**I could not make this fire on my test clip.** Output was bit-identical, runtime
unchanged. That is exactly the signature I criticised in P09/P10/P12, and I am
not going to present it as a speedup on that basis.

The likely reason is scale, not gating: the sites need
`prune_tx_type_est_rd` (a speed-4 feature, so enabled) **and**
`num_allowed > 2`. On 192x128 at qp=135 there are few coefficients and few
surviving types; on 4K CTC content `num_allowed > 2` should be common. **That is
a hypothesis.** If the CTC arm comes back bit-identical, the patch is a no-op
and should be dropped, not tuned.

## 3. What I would build next, given the profile

**A search-stage gate on the trellis itself.** This is the real prize and it is
larger than anything else on the board. The trellis should run at full strength
for the final coding decision and in a reduced form during RD search — exactly
the distinction `perform_coeff_opt` and `enable_winner_mode_for_coeff_opt` draw
for `av2_optimize_txb_new`, and which the TCQ path never received.

I have not built it this round because it deserves care: the state-space search
has a natural reduction (fewer states, shorter lookahead) that keeps the
rate-distortion signal while cutting the work, and choosing that reduction
needs a look at `trellis_quant.c` I have not yet done. If you want one thing
from the next round, this is it.

**The memset finding, 5.3%.** Worth understanding before acting. My earlier `i02`
attacked buffer clearing and was slower at 4K, so the naive fix is known not to
work — but 5.3% of the encoder spent zeroing memory is large enough to deserve a
proper look at *which* buffers and *how often*.

## 4. Method note

This is the first direct measurement of where encoder time goes in this project.
Everything before it — including my own reasoning — inferred hot spots from the
101-feature study's speed-feature deltas, which measure *what a feature is worth*
rather than *where time is spent*. Those are different questions, and the gap
between them is why 47% of the encoder went unexamined for twenty-four patches.

The profile cost about 25 minutes of wall clock and needs no CTC slot. It should
be re-run whenever the anchor moves substantially — `PROFILE_5d628d8_cpu4.txt`
in this directory is the raw top-40 for comparison.

One caveat on the profile itself: it is a 192x128 2-frame clip, so the absolute
percentages will shift at 4K with longer GOPs — more inter frames, more motion
search, relatively less intra. The *ordering* is unlikely to change much, since
quantisation runs per transform block regardless of resolution, but if a
decision hinges on the exact share, re-profile on a bigger clip first.
